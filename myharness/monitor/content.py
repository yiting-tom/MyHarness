"""What an artifact says, and what each write to it added or removed.

The store keeps one copy of a note -- the latest. Every version before it is
gone from the store, but not from the record: ``write_finding`` and
``update_state`` keep their full input in the dispatch's transcript (tool
inputs are not excerpted; tool *results* are). So the history is rebuilt from
what the lanes actually sent, never guessed -- and where the record cannot
cover it (a dispatch still running, a store that disagrees with the last
recorded version) the view says so instead of filling the gap.

Golden #26 is why this exists: ``txn-2024-analysis`` went from 2,531 characters
to 839 across two dispatches, the monitor flagged ``overwritten_output``, and
there was no way to see what the 1,700 removed characters had said.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from myharness.artifacts.errors import ArtifactError
from myharness.artifacts.ids import ArtifactId, InvalidArtifactId
from myharness.artifacts.local import LocalArtifactStore
from myharness.artifacts.types import GrantSet

#: The tools whose input *is* the artifact's new content.
_WRITERS: Final = ("write_finding", "update_state")
#: What a successful write says back; the id is what ties a call to an artifact.
_WROTE: Final = re.compile(r"^wrote (\S+)")
_STATE: Final = re.compile(r"^state updated \(revision (\d+)")

#: How much of an artifact the page is sent. A note is a few thousand
#: characters; anything past this is not something to read in a side panel.
MAX_SHOWN_CHARS: Final = 200_000
#: Unchanged lines kept around each change, the way a unified diff does.
CONTEXT_LINES: Final = 2
#: Lines of a text blob shown as a preview.
BLOB_HEAD_LINES: Final = 20
_BLOB_HEAD_BYTES: Final = 16_384


@dataclass(frozen=True, slots=True)
class Write:
    """One successful write, as the transcript recorded it."""

    artifact: str
    text: str
    #: Which write this was within its dispatch, counted from one.
    n: int


def _short(name: str) -> str:
    return name.rsplit("__", 1)[-1]


def writes_in(
    rows: Sequence[Mapping[str, Any]], *, job_id: str, lane_namespace: str,
) -> list[Write]:
    """Successful content writes in one transcript, in the order they happened.

    ``tool_use`` rows keep no id, so calls and results are paired by order --
    the same pairing ``trace.parse_trace`` uses. A refused write (``ERROR ...``)
    is not a version: nothing reached the store.
    """
    pending: list[tuple[str, str]] = []  # (tool, text) for every call, in order
    out: list[Write] = []
    for row in rows:
        for block in row.get("content") or ():
            if not isinstance(block, Mapping):
                continue
            if block.get("type") == "tool_use":
                tool = _short(str(block.get("name") or ""))
                inp = block.get("input") or {}
                pending.append((tool, str(inp.get("text") or "") if tool in _WRITERS else ""))
            elif block.get("type") == "tool_result" and pending:
                tool, text = pending.pop(0)
                if tool not in _WRITERS or block.get("is_error"):
                    continue
                body = str(block.get("content") or "")
                if body.startswith("ERROR"):
                    continue
                if tool == "write_finding" and (m := _WROTE.match(body)):
                    target = m.group(1)
                elif tool == "update_state" and _STATE.match(body):
                    target = f"{job_id}/note/{lane_namespace}/state"
                else:
                    continue
                out.append(Write(target, text, 0))
    return [Write(w.artifact, w.text, i) for i, w in enumerate(out, start=1)]


def line_diff(old: str, new: str) -> dict[str, Any]:
    """Added and removed lines, with long unchanged runs folded away.

    Lines rather than words: findings are markdown, and a line is a bullet, a
    table row or a paragraph -- the unit a reader recognises as "this was cut".
    """
    a, b = old.splitlines(), new.splitlines()
    ops: list[list[Any]] = []
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            ops.extend(["same", line] for line in a[i1:i2])
            continue
        if tag in ("replace", "delete"):
            ops.extend(["del", line] for line in a[i1:i2])
            removed += i2 - i1
        if tag in ("replace", "insert"):
            ops.extend(["add", line] for line in b[j1:j2])
            added += j2 - j1
    return {"added": added, "removed": removed, "lines": _fold(ops)}


def _fold(ops: list[list[Any]]) -> list[list[Any]]:
    """Keep CONTEXT_LINES of unchanged text around each change; count the rest."""
    changed = [i for i, (op, _) in enumerate(ops) if op != "same"]
    keep: set[int] = set()
    for i in changed:
        keep.update(range(i - CONTEXT_LINES, i + CONTEXT_LINES + 1))
    out: list[list[Any]] = []
    skipped = 0
    for i, op in enumerate(ops):
        if op[0] != "same" or i in keep:
            if skipped:
                out.append(["skip", skipped])
                skipped = 0
            out.append(op)
        else:
            skipped += 1
    if skipped:
        out.append(["skip", skipped])
    return out


@lru_cache(maxsize=256)
def _transcript_writes(path: str, size: int, mtime_ns: int,
                       job_id: str, namespace: str) -> tuple[Write, ...]:
    """Parsed once per file version. A transcript never changes after its
    dispatch ends, and /state asks for this on every one-second poll."""
    del size, mtime_ns  # part of the cache key only
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ()
    rows = []
    for line in text.splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return tuple(writes_in(rows, job_id=job_id, lane_namespace=namespace))


def dispatch_writes(layout: Any, flow: Any) -> dict[str, list[Write]]:
    """Every finished dispatch's writes, by dispatch id, in dispatch order."""
    out: dict[str, list[Write]] = {}
    for d in flow.dispatches.values():
        if d.running or not d.transcript:
            continue
        try:
            tid = ArtifactId.parse(d.transcript)
        except InvalidArtifactId:
            continue
        if tid.job_id != layout.job_id or not tid.is_blob:
            continue
        path = layout.blob_path(tid.name)
        try:
            st = path.stat()
        except OSError:
            continue
        out[d.id] = list(_transcript_writes(str(path), st.st_size, st.st_mtime_ns,
                                            layout.job_id, f"lanes/{d.lane}"))
    return out


# --- the view ---------------------------------------------------------------


def artifact_view(layout: Any, flow: Any, raw_id: str) -> dict[str, Any]:
    """One artifact: what it holds now, and every recorded version of it."""
    try:
        aid = ArtifactId.parse(raw_id)
    except InvalidArtifactId as exc:
        return {"error": "bad_id", "why": f"不是合法的 artifact id：{exc}"}
    if aid.job_id != layout.job_id:
        return {"error": "other_job", "why": "這份 artifact 不屬於被監看的 job，所以不讀。"}

    store = LocalArtifactStore(layout.root)
    grants = GrantSet.unrestricted(layout.job_id)
    try:
        meta = asyncio.run(store.stat(aid, grants=grants))
    except ArtifactError:
        return {"error": "not_found", "id": str(aid),
                "why": "存放區的索引裡沒有這份 artifact —— 事件流提到了它，但它沒有被寫進存放區。"}

    view: dict[str, Any] = {
        "id": str(aid), "kind": meta.kind, "bytes": meta.bytes,
        "est_tokens": meta.est_tokens, "produced_by": meta.produced_by,
        "revision": meta.revision, "schema": meta.schema,
    }
    if aid.is_blob:
        view.update(_blob_preview(store, aid, grants))
        return view

    current = asyncio.run(store.read_note(aid, grants=grants, max_tokens=10**9))
    view["content"], view["content_cut"] = _cap(current)
    view.update(_history(layout, flow, str(aid), current))
    return view


def _cap(text: str) -> tuple[str, bool]:
    return (text[:MAX_SHOWN_CHARS], True) if len(text) > MAX_SHOWN_CHARS else (text, False)


def _history(layout: Any, flow: Any, artifact: str, current: str) -> dict[str, Any]:
    per_dispatch = dispatch_writes(layout, flow)
    versions: list[dict[str, Any]] = []
    previous = ""
    for d in flow.dispatches.values():
        for w in per_dispatch.get(d.id, ()):
            if w.artifact != artifact:
                continue
            text, cut = _cap(w.text)
            versions.append({
                "dispatch": d.id, "lane": d.lane, "n": w.n, "chars": len(w.text),
                "text": text, "cut": cut,
                "diff": line_diff(previous, w.text),
                "first": not versions,
            })
            previous = w.text

    gaps: list[str] = []
    running = [d for d in flow.dispatches.values() if d.running]
    if running:
        lanes = "、".join(sorted({d.lane for d in running}))
        gaps.append(f"{lanes} 還在跑。它們寫的版本要等派工結束、逐輪紀錄寫下之後才看得到。")
    if versions and previous != current:
        gaps.append("存放區目前的內容和紀錄中最後一版不同 —— 有一次寫入沒有出現在任何逐輪紀錄裡"
                    + ("（可能來自還在跑的派工）。" if running else "，來源沒有被記錄。"))
    if not versions:
        gaps.append("逐輪紀錄裡沒有任何一次寫入它 —— 它可能是 harness 或使用者放進來的，"
                    "或者寫它的派工沒有留下逐輪紀錄。只看得到目前的內容。")
    return {"versions": versions, "matches_last": bool(versions) and previous == current,
            "history_why": " ".join(gaps)}


def _blob_preview(store: LocalArtifactStore, aid: ArtifactId, grants: GrantSet) -> dict[str, Any]:
    async def head() -> bytes:
        async with store.localize(aid, grants=grants) as path:
            with path.open("rb") as fh:
                return fh.read(_BLOB_HEAD_BYTES)

    try:
        data = asyncio.run(head())
    except (ArtifactError, OSError, ValueError):
        return {"preview": None, "preview_why": "讀不到這份資料的內容。"}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {"preview": None,
                "preview_why": "這是二進位格式（例如 parquet），不能當文字預覽。欄位與大小見上方。"}
    lines = text.splitlines()
    return {"preview": lines[:BLOB_HEAD_LINES],
            "preview_why": (f"只顯示前 {BLOB_HEAD_LINES} 行。" if len(lines) > BLOB_HEAD_LINES
                            or len(data) == _BLOB_HEAD_BYTES else "")}


def writes_summary(layout: Any, flow: Any) -> list[dict[str, Any]]:
    """Every recorded write, for the graph: who wrote what, and how many times."""
    out = []
    for did, ws in dispatch_writes(layout, flow).items():
        for w in ws:
            out.append({"dispatch": did, "artifact": w.artifact, "n": w.n, "chars": len(w.text)})
    return out


__all__ = ["MAX_SHOWN_CHARS", "Write", "artifact_view", "dispatch_writes",
           "line_diff", "writes_in", "writes_summary"]
