"""The only way a worker touches storage.

The grant model from the artifact-store change holds exactly as long as the
worker has no way around it, so a lane worker gets these in-process MCP tools
and none of the CLI's file tools (design.md D4). Every one of them checks the
grant set on this side of the boundary.

Errors come back to the worker as text rather than as exceptions: a worker that
is told *why* it was refused can pick a different approach, whereas a crashed
tool just wastes a turn.
"""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool

from myharness.artifacts.errors import ArtifactError
from myharness.artifacts.ids import ArtifactId, coerce_artifact_ids
from myharness.artifacts.store import ArtifactStore
from myharness.artifacts.tokens import estimate_tokens
from myharness.artifacts.types import GrantSet
from myharness.lanes.tabular.query import IntoResult, QueryFailure, QueryRunner
from myharness.lanes.types import LaneInstance

SERVER_NAME = "lane"

# The SDK's shorthand schema ({"a": str}) marks every property required and
# gives a bare `list` no item type. Both matter here: a required `into` would
# make the model supply a name on every read-only query, and untyped items are
# how {"blob_path": ...} got through in the fourth golden run. A full JSON
# Schema is passed through untouched, so these say exactly what they mean.
_QUERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "artifacts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Artifact ids of the blobs to query, e.g. "
                           "['job/blob/raw/txns']. Not file paths.",
        },
        "sql": {
            "type": "string",
            "description": "One SELECT statement. Use CTEs for multiple steps.",
        },
        "into": {
            "type": "string",
            "description": "Optional. A short name; the full result is written "
                           "to a new blob under this lane instead of being "
                           "returned. Use it for anything large.",
        },
    },
    "required": ["artifacts", "sql"],
}

_INSPECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "artifact": {"type": "string", "description": "Artifact id of a data blob."},
    },
    "required": ["artifact"],
}

_READ_NOTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "artifact": {"type": "string", "description": "Artifact id of the note."},
        "section": {
            "type": "string",
            "description": "Optional. A single '##' section to read instead of "
                           "the whole note.",
        },
    },
    "required": ["artifact"],
}

#: Used when a lane type declares no tools of its own.
DEFAULT_TOOLS: tuple[str, ...] = (
    "read_note", "write_finding", "update_state",
    "localize_blob", "inspect_blob", "duckdb_query",
)


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(payload: dict[str, Any]) -> dict[str, Any]:
    """Refusals are data the worker can act on, not crashes."""
    return {"content": [{"type": "text", "text": "ERROR " + json.dumps(payload, ensure_ascii=False)}]}


@dataclass
class WorkerToolbox:
    """Storage tools bound to one worker execution.

    Tracks what the worker produced so the harness can still assemble a handle
    when the run dies mid-flight -- which is exactly what an exhausted
    ``task_budget`` looks like (design.md D1).
    """

    store: ArtifactStore
    job_id: str
    lane: LaneInstance
    grants: GrantSet
    read_budget: int

    handlers: dict[str, Any] = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)
    derived: list[str] = field(default_factory=list)
    state_revision: int = 0
    state_rejected: bool = False
    reads: int = 0
    queries: int = 0

    #: Holds every localisation open for as long as the worker runs. A blob
    #: materialised by an object-store backend is deleted when its context
    #: manager exits, so a path handed out from inside a ``with`` block is dead
    #: on arrival (design.md D8). Closed by ``aclose``.
    _open: AsyncExitStack = field(default_factory=AsyncExitStack)

    async def aclose(self) -> None:
        """Release every localised blob. Safe to call more than once."""
        await self._open.aclose()

    async def __aenter__(self) -> WorkerToolbox:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _query_runner(self) -> QueryRunner:
        return QueryRunner(
            self.store,
            job_id=self.job_id,
            grants=self.grants,
            produced_by=f"lane:{self.lane.id}",
            derived_namespace=self.lane.namespace,
        )

    @property
    def last_finding(self) -> str | None:
        return self.findings[-1] if self.findings else None

    def build_server(self):
        """An SDK in-process MCP server exposing this toolbox."""

        read_only = ToolAnnotations(readOnlyHint=True)
        mutating = ToolAnnotations(readOnlyHint=False)
        # readOnlyHint is the SDK's only switch for same-turn tool-call
        # concurrency (spike #1). The data tools opt out of it -- not because
        # they write, but because each call materialises a whole blob in
        # memory, and several at once multiplies that by the cap.
        serial = ToolAnnotations(readOnlyHint=False)

        @tool(
            "read_note",
            "Read an analysis note you are allowed to see. Blobs are not readable this way.",
            _READ_NOTE_SCHEMA,
            annotations=read_only,
        )
        async def read_note(args):
            raw = str(args.get("artifact", "")).strip()
            section = (args.get("section") or "").strip() or None
            try:
                aid = ArtifactId.parse(raw)
            except ValueError as exc:
                return _err({"code": "bad_artifact_id", "message": str(exc)})
            try:
                text = await self.store.read_note(
                    aid, grants=self.grants, max_tokens=self.read_budget, section=section
                )
            except ArtifactError as exc:
                return _err(exc.to_dict())
            self.reads += 1
            return _ok(text)

        @tool(
            "write_finding",
            "Write your full analysis. Do NOT put the analysis in your final reply.",
            {"name": str, "text": str},
            annotations=mutating,
        )
        async def write_finding(args):
            name = str(args.get("name", "")).strip() or str(len(self.findings) + 1)
            text = str(args.get("text", ""))
            if not text.strip():
                return _err({"code": "empty_finding", "message": "text must not be empty"})
            meta = await self.store.put_note(
                self.job_id, self.lane.finding_name(name), text,
                produced_by=f"lane:{self.lane.id}",
            )
            self.findings.append(str(meta.id))
            return _ok(f"wrote {meta.id} ({meta.est_tokens} est tokens)")

        @tool(
            "update_state",
            "Replace this lane's carried-over knowledge. Conclusions and open "
            "questions only -- details belong in findings.",
            {"text": str},
            annotations=mutating,
        )
        async def update_state(args):
            text = str(args.get("text", ""))
            limit = self.lane.type.state_max_tokens
            est = estimate_tokens(text)
            if est > limit:
                # Refuse rather than truncate: cutting state mid-sentence loses
                # meaning silently, and auto-compaction is another LLM call whose
                # loss we cannot see (design.md D3).
                self.state_rejected = True
                return _err({
                    "code": "state_too_large", "est_tokens": est, "limit": limit,
                    "message": (
                        f"state would be ~{est} tokens, limit is {limit}. "
                        "Previous state kept. Summarise harder: conclusions and "
                        "open questions only."
                    ),
                })
            try:
                meta = await self.store.compare_and_set_note(
                    self.job_id, self.lane.state_name, text,
                    produced_by=f"lane:{self.lane.id}",
                    expected_revision=self.state_revision,
                )
            except ArtifactError as exc:
                self.state_rejected = True
                return _err(exc.to_dict())
            self.state_revision = meta.revision
            return _ok(f"state updated (revision {meta.revision}, ~{est} tokens)")

        @tool(
            "localize_blob",
            "Get a local file path for a raw data blob so tools can read it. "
            "Never try to read a blob's contents into your reply.",
            {"artifact": str},
            annotations=read_only,
        )
        async def localize_blob(args):
            try:
                aid = ArtifactId.parse(str(args.get("artifact", "")).strip())
            except ValueError as exc:
                return _err({"code": "bad_artifact_id", "message": str(exc)})
            try:
                meta = await self.store.stat(aid, grants=self.grants)
                # Enter the localisation on the toolbox's stack, not on a block
                # that ends at this return: an object-store backend deletes its
                # scratch copy on exit and the worker would get a dead path.
                path = await self._open.enter_async_context(
                    self.store.localize(aid, grants=self.grants)
                )
                return _ok(json.dumps(
                    {"path": str(path), "bytes": meta.bytes, "schema": meta.schema},
                    ensure_ascii=False,
                ))
            except ArtifactError as exc:
                return _err(exc.to_dict())
            except ValueError as exc:
                return _err({"code": "not_a_blob", "message": str(exc)})

        @tool(
            "inspect_blob",
            "See a data blob's columns, types, row count and first few rows. "
            "Do this before writing SQL -- guessing column names wastes a turn.",
            _INSPECT_SCHEMA,
            annotations=serial,
        )
        async def inspect_blob(args):
            result = await self._query_runner().inspect(
                str(args.get("artifact", "")).strip()
            )
            if isinstance(result, QueryFailure):
                return _ok(result.text())
            self.reads += 1
            return _ok(result.text())

        @tool(
            "duckdb_query",
            "Run one SQL SELECT over data blobs you are allowed to read. "
            "List the blobs in `artifacts`; each becomes a table whose name is "
            "reported back to you. SQL must not contain file paths -- naming an "
            "artifact is the only way to reach data. Results are truncated to "
            "fit; for a full result set pass `into` and it becomes a new blob "
            "you can query later instead of flooding your context.",
            _QUERY_SCHEMA,
            annotations=serial,
        )
        async def duckdb_query(args):
            raw = args.get("artifacts")
            if isinstance(raw, str):
                raw = [raw]
            ids, rejected = coerce_artifact_ids(raw)
            if rejected:
                # Refusing now beats a baffling not_granted later: the fourth
                # golden run lost two lanes to a mangled input list.
                return _err({
                    "code": "bad_artifacts",
                    "message": "artifacts must be artifact id strings, e.g. "
                               "['job/blob/raw/data']. These could not be read:",
                    "rejected": [str(r)[:120] for r in rejected],
                    "accepted": ids,
                })
            result = await self._query_runner().query(
                ids,
                str(args.get("sql", "")),
                into=str(args.get("into") or "").strip(),
            )
            if isinstance(result, QueryFailure):
                return _ok(result.text())
            self.queries += 1
            if isinstance(result, IntoResult):
                self.derived.append(str(result.artifact.id))
            return _ok(result.text())

        available = {
            "read_note": read_note,
            "write_finding": write_finding,
            "update_state": update_state,
            "localize_blob": localize_blob,
            "inspect_blob": inspect_blob,
            "duckdb_query": duckdb_query,
        }
        declared = [name for name in self.lane.type.tools if name in available]
        if not declared:
            declared = list(available)
        # Exposed so tests can drive a tool without reaching into SDK internals.
        self.handlers = {name: available[name].handler for name in declared}
        return create_sdk_mcp_server(
            name=SERVER_NAME, version="1.0.0",
            tools=[available[name] for name in declared],
        )

    def tool_names(self) -> list[str]:
        declared = [t for t in self.lane.type.tools] or list(DEFAULT_TOOLS)
        return [f"mcp__{SERVER_NAME}__{name}" for name in declared]
