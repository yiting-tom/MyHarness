"""`myharness run`: start, provide, answer, report -- through the real service.

The orchestrator is ScriptedLoop from the MCP full-flow test, so this is free and
deterministic; everything below it (runner, store, event log) is real.
"""

from __future__ import annotations

from pathlib import Path

from myharness.lanes.types import LaneRegistry, LaneType
from myharness.mcp.service import AnalysisService
from myharness.run import drive
from tests.mcp.test_full_flow import ScriptedLoop


def service_at(tmp_path: Path) -> AnalysisService:
    charter = tmp_path / "c.md"
    charter.write_text("charter", encoding="utf-8")
    return AnalysisService(
        tmp_path / "root",
        lanes=LaneRegistry(LaneType(name="analyst", charter_path=charter, state_max_tokens=100)),
        loop_factory=ScriptedLoop,
        readable=[tmp_path],
    )


async def test_a_run_provides_answers_and_writes_the_report(tmp_path: Path, capsys):
    data = tmp_path / "txn.csv"
    data.write_text("txn_id,amount\n1,10\n", encoding="utf-8")
    out = tmp_path / "report.md"
    asked: list[str] = []

    async def ask(question):
        asked.append(question["text"])
        return "否"

    service = service_at(tmp_path)
    try:
        code = await drive(service, "分析交易", [data], ask=ask, job_id="j1",
                           out=out, wait=0.5)
    finally:
        await service.aclose()

    assert code == 0
    assert asked == ["要含 2023 年嗎？"], "the question reached the person exactly once"
    printed = capsys.readouterr().out
    assert "已提供" in printed and "txn.csv" in printed
    assert "765" in out.read_text(encoding="utf-8"), "-o holds the drilled sections"
    assert list((tmp_path / "root").rglob("blobs/raw/txn.csv")), \
        "the file went in under its full name -- the lanes pick a reader by suffix"


async def test_an_empty_task_is_refused_before_anything_runs(tmp_path: Path, capsys):
    service = service_at(tmp_path)
    try:
        assert await drive(service, "  ", [], ask=None) == 1  # type: ignore[arg-type]
    finally:
        await service.aclose()
    assert "無法開始" in capsys.readouterr().err
