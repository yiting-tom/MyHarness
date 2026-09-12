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
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

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


#: Share of the token budget after which every tool result carries a warning.
#: Golden run #9 spent a whole 60k budget on 24 queries without once calling
#: write_finding, and the analysis died with it. The lane was not being
#: careless -- it had no way to know how much was left.
BUDGET_WARN_AT = 0.75

#: Share of the budget after which the tools that pull content into a lane's
#: context stop answering. The warning above is a request, and golden runs #17
#: and #18 are the same request answered two different ways: #17's d1 read
#: "預算已用 82%", then 92%, made fifteen more queries and never called
#: write_finding; #18's d1 read it at 77% and called it. Same string, same
#: model, same threshold. Delivery can be guaranteed and compliance cannot, and
#: every other ceiling in this harness holds by construction rather than by
#: asking (README: 由構造保證，不是由 prompt 祈禱).
BUDGET_GATE_AT = 0.90

#: What the gate closes: the tools that put content the lane does not already
#: have into its context. write_finding and update_state stay open, because
#: closing the others is only worth doing if there is somewhere left to put the
#: work. localize_blob stays open too -- it returns a path, not content, so
#: refusing it would cost a lane its working file and save nothing.
GATED_ABOVE_BUDGET: frozenset[str] = frozenset({
    "read_note", "inspect_blob", "duckdb_query",
})

#: Longest a finding's name may be. Names appear in artifact ids, grant lists
#: and the flow graph; a sentence there is unreadable everywhere at once.
MAX_FINDING_NAME_CHARS = 60


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


#: What ArtifactId already permits in one path segment. Mirrored rather than
#: imported so the refusal can name the rule; ArtifactId's own check raises,
#: and an exception is not something a worker can act on (design.md D5).
_FINDING_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _bad_finding_name(name: str) -> str | None:
    """Why this name cannot be used, or None if it can.

    Every branch here would otherwise be an InvalidArtifactId raised from
    inside put_note -- a stack trace where the caller is a model that needs a
    sentence.
    """
    if "/" in name:
        return "name 不可以含有 '/' —— 它是一個標籤，不是路徑或 artifact id"
    if len(name) > MAX_FINDING_NAME_CHARS:
        return f"name 最多 {MAX_FINDING_NAME_CHARS} 個字元，收到 {len(name)}"
    if not _FINDING_NAME.match(name):
        # The charters are written in Chinese, so a Chinese finding name is the
        # likeliest thing a worker will reach for, and it used to raise.
        return ("name 只能用 ASCII 英數字、'.'、'-'、'_'，且需以英數字開頭 —— "
                "中文請放在 finding 的內容裡，不要放在名稱")
    return None


def _err(payload: dict[str, Any]) -> dict[str, Any]:
    """Refusals are data the worker can act on, not crashes."""
    body = "ERROR " + json.dumps(payload, ensure_ascii=False)
    return {"content": [{"type": "text", "text": body}]}


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
    #: How many calls the budget gate refused. Recorded on the dispatch event so
    #: a run says whether the gate fired rather than leaving it to be inferred.
    gated: int = 0
    #: Share of the lane's token budget consumed so far, updated by the worker
    #: loop after every streamed message. The worker knows this and the lane
    #: does not, which is the whole reason it gets attached to tool results.
    budget_used: float = 0.0

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

    def _gate(self, tool_name: str) -> dict[str, Any] | None:
        """Refuse a content tool once the gate is crossed, or None to proceed.

        A refusal rather than a truncation: the lane is not being kept from
        finishing, it is being kept from starting something it cannot afford to
        finish. What is left open is exactly what it needs to land the work it
        already has.
        """
        if tool_name not in GATED_ABOVE_BUDGET or self.budget_used < BUDGET_GATE_AT:
            return None
        self.gated += 1
        pct = int(self.budget_used * 100)
        gate = int(BUDGET_GATE_AT * 100)
        if self.findings:
            message = (
                f"token 預算已用 {pct}%。超過 {gate}% 之後不再受理取用類工具。"
                "你已經寫過 finding —— 用 write_finding 把新結論補進去，"
                "然後回傳 handle 結束。"
            )
        else:
            message = (
                f"token 預算已用 {pct}%。超過 {gate}% 之後不再受理取用類工具。"
                "現在就用 write_finding 寫下目前為止的結論 —— "
                "預算用盡時未落檔的分析會全部消失。"
            )
        return _err({
            "code": "budget_gate", "message": message, "budget_used": pct,
            "still_available": ["write_finding", "update_state"],
        })

    def _result(self, text: str) -> dict[str, Any]:
        """A tool result, plus a budget warning once one is warranted.

        Repeated on every call rather than said once: a single notice thirty
        messages back is not what the model is attending to when it decides
        whether to run one more query.
        """
        if self.budget_used < BUDGET_WARN_AT:
            return _ok(text)
        pct = int(self.budget_used * 100)
        if self.findings:
            warning = (
                f"\n\n[harness] token 預算已用 {pct}%。你已經寫過 finding —— "
                "把新結論補進去，然後回傳 handle 結束。不要再開新的查詢。"
            )
        else:
            warning = (
                f"\n\n[harness] token 預算已用 {pct}%，而你還沒有寫任何 finding。"
                "現在就用 write_finding 寫下目前為止的結論 —— "
                "預算用盡時未落檔的分析會全部消失。"
            )
        return _ok(text + warning)

    def build_server(self) -> McpSdkServerConfig:
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
        async def read_note(args: dict[str, Any]) -> dict[str, Any]:
            if refusal := self._gate("read_note"):
                return refusal
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
            return self._result(text)

        @tool(
            "write_finding",
            "Write your full analysis. Do NOT put the analysis in your final reply.",
            {"name": str, "text": str},
            annotations=mutating,
        )
        async def write_finding(args: dict[str, Any]) -> dict[str, Any]:
            name = str(args.get("name", "")).strip() or str(len(self.findings) + 1)
            text = str(args.get("text", ""))
            if not text.strip():
                return _err({"code": "empty_finding", "message": "text must not be empty"})
            # A worker reads artifact ids all run and reaches for one here.
            # Golden #10's critic passed a whole id and the harness nested it
            # under its own namespace, producing
            # lanes/critic/findings/<job>/note/lanes/analyst/findings/critique
            # -- a path nothing looks for and the flow graph reports as an
            # orphan. Refuse with something the model can act on rather than
            # silently building it (design.md D5).
            if problem := _bad_finding_name(name):
                return _err({
                    "code": "bad_name", "message": problem, "given": name[:120],
                    "hint": "name 是一個短標籤，例如 'critique' 或 'txn-stats'，"
                            "不是 artifact id。namespace 由 harness 加上。",
                })
            meta = await self.store.put_note(
                self.job_id, self.lane.finding_name(name), text,
                produced_by=f"lane:{self.lane.id}",
            )
            self.findings.append(str(meta.id))
            return self._result(f"wrote {meta.id} ({meta.est_tokens} est tokens)")

        @tool(
            "update_state",
            "Replace this lane's carried-over knowledge. Conclusions and open "
            "questions only -- details belong in findings.",
            {"text": str},
            annotations=mutating,
        )
        async def update_state(args: dict[str, Any]) -> dict[str, Any]:
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
            return self._result(f"state updated (revision {meta.revision}, ~{est} tokens)")

        @tool(
            "localize_blob",
            "Get a local file path for a raw data blob so tools can read it. "
            "Never try to read a blob's contents into your reply.",
            {"artifact": str},
            annotations=read_only,
        )
        async def localize_blob(args: dict[str, Any]) -> dict[str, Any]:
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
                return self._result(json.dumps(
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
        async def inspect_blob(args: dict[str, Any]) -> dict[str, Any]:
            if refusal := self._gate("inspect_blob"):
                return refusal
            result = await self._query_runner().inspect(
                str(args.get("artifact", "")).strip()
            )
            if isinstance(result, QueryFailure):
                return self._result(result.text())
            self.reads += 1
            return self._result(result.text())

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
        async def duckdb_query(args: dict[str, Any]) -> dict[str, Any]:
            if refusal := self._gate("duckdb_query"):
                return refusal
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
                return self._result(result.text())
            self.queries += 1
            if isinstance(result, IntoResult):
                self.derived.append(str(result.artifact.id))
            return self._result(result.text())

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
        declared = list(self.lane.type.tools) or list(DEFAULT_TOOLS)
        return [f"mcp__{SERVER_NAME}__{name}" for name in declared]
