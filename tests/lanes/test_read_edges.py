"""Every tool that opens an artifact's content must say so as it happens.

The monitor's read edges come from ``artifact.read``, which is written only when
a tool calls ``WorkerToolbox._record_read``. A tool that reads without calling it
does not merely leave an edge out: the graph draws that input as "granted but
never opened" -- a recorded-looking fact that is false.

So this is checked by behaviour, not by reading the source: the store is wrapped
to see what content was actually opened, every tool is driven once, and what the
store saw has to match what the toolbox reported. A tool added to the toolbox
without a case here fails the first test, by name.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from myharness.artifacts.local import LocalArtifactStore
from myharness.artifacts.types import GrantSet
from myharness.lanes.tools import WorkerToolbox
from myharness.lanes.types import LaneRegistry, LaneType

JOB = "j11"
ROWS = "ts,account,amount\n" + "\n".join(f"2024-01-{d:02d},A{d % 3},{d * 100}"
                                          for d in range(1, 11))


class WatchedStore:
    """The real store, plus a list of every artifact whose content was opened.

    Only content counts: ``read_note`` returning text, ``localize`` handing out a
    path. ``stat`` reads the index, not the artifact, and is not a read.
    """

    def __init__(self, inner: LocalArtifactStore) -> None:
        self._inner = inner
        self.opened: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def read_note(self, artifact_id, **kw):
        text = await self._inner.read_note(artifact_id, **kw)
        self.opened.append(str(artifact_id))
        return text

    def localize(self, artifact_id, **kw):
        @asynccontextmanager
        async def watched():
            async with self._inner.localize(artifact_id, **kw) as path:
                self.opened.append(str(artifact_id))
                yield path
        return watched()


#: One realistic call per tool. A tool missing from here is a tool whose read
#: reporting nobody has checked.
CASES: dict[str, Any] = {
    "read_note": lambda ids: {"artifact": ids["note"]},
    "write_finding": lambda ids: {"name": "f", "text": "結論"},
    "update_state": lambda ids: {"text": "開放問題：無"},
    "localize_blob": lambda ids: {"artifact": ids["blob"]},
    "inspect_blob": lambda ids: {"artifact": ids["blob"]},
    "duckdb_query": lambda ids: {"artifacts": [ids["blob"]],
                                 "sql": "SELECT count(*) AS n FROM txns_csv"},
}


@pytest.fixture
async def bench(tmp_path: Path):
    inner = LocalArtifactStore(tmp_path)
    await inner.init_job(JOB)
    charter = tmp_path / "c.md"
    charter.write_text("charter", encoding="utf-8")
    # No tools declared means every tool the toolbox has -- which is the point:
    # a new tool is covered by this file the moment it exists.
    lane = LaneRegistry(LaneType(name="ta", charter_path=charter, tools=())).create("x", "ta")
    blob = await inner.put_blob(JOB, "raw/txns.csv", data=ROWS.encode(), produced_by="user")
    note = await inner.put_note(JOB, "lanes/kyc/findings/1", "上游的結論", produced_by="kyc")
    store = WatchedStore(inner)
    toolbox = WorkerToolbox(
        store=store, job_id=JOB, lane=lane,  # type: ignore[arg-type]
        grants=GrantSet.for_lane(JOB, lane.namespace, [blob.id, note.id]),
        read_budget=3000,
    )
    reported: list[str] = []

    async def on_read(artifact: str) -> None:
        reported.append(artifact)

    toolbox.on_read = on_read
    toolbox.build_server()
    try:
        yield toolbox, store, reported, {"blob": str(blob.id), "note": str(note.id)}
    finally:
        await toolbox.aclose()


async def test_every_tool_has_a_case(bench):
    toolbox, *_ = bench
    missing = sorted(set(toolbox.handlers) - set(CASES))
    assert not missing, (
        f"新工具 {missing} 沒有被這個測試涵蓋。在 CASES 加一次實際的呼叫；"
        "如果它會打開 artifact 的內容，它必須呼叫 self._record_read(artifact_id)，"
        "否則 monitor 會把那份資料畫成「被允許但沒打開」。"
    )


@pytest.mark.parametrize("name", sorted(CASES))
async def test_what_a_tool_opens_is_what_it_reports(bench, name):
    toolbox, store, reported, ids = bench
    result = await toolbox.handlers[name](CASES[name](ids))
    body = result["content"][0]["text"]
    assert not body.startswith("ERROR"), f"the case for {name} must succeed: {body[:200]}"

    assert sorted(set(reported)) == sorted(set(store.opened)), (
        f"{name} 打開了 {sorted(set(store.opened))}，"
        f"卻回報讀了 {sorted(set(reported))}。"
        "打開內容的工具必須呼叫 self._record_read(artifact_id)。"
    )


async def test_the_check_fails_for_a_tool_that_forgets(bench, monkeypatch):
    """The check above is only worth something if it can fail: silence the
    report, and the store still sees the read."""
    toolbox, store, reported, ids = bench

    async def forgot(artifact: str) -> None:
        return None

    monkeypatch.setattr(toolbox, "_record_read", forgot)
    await toolbox.handlers["read_note"]({"artifact": ids["note"]})
    assert store.opened == [ids["note"]] and reported == []
