"""Event log: append semantics, common fields, aggregation, caveats, durability."""

from __future__ import annotations

import inspect
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from myharness.events import LocalEventLog, summarize
from myharness.events.log import EventLog
from myharness.events.query import (
    context_peak,
    cost_by_lane,
    derive_caveats,
    duplicate_dispatches,
    failures,
    tokens_by_lane,
)
from myharness.events.types import (
    ASK_ANSWER,
    ASK_USER,
    CTX,
    DISPATCH_END,
    DISPATCH_START,
    INGRESS,
    JOB_FINISH,
    JOB_START,
    PROXY_ROUTE,
    STATUS_BUDGET_EXCEEDED,
    STATUS_DUPLICATE,
    STATUS_OK,
    Event,
    MalformedEvent,
)
from myharness.local_layout import JobLayout

JOB = "j7"


@pytest.fixture
def log(tmp_path: Path) -> LocalEventLog:
    return LocalEventLog(tmp_path)


def ev(seq: int, t: str, **data) -> Event:
    return Event(t=t, seq=seq, ts=datetime.now(UTC), job_id=JOB, data=data)


# --- Requirement: Append-only 的結構化事件流 -----------------------------


async def test_events_are_read_back_in_append_order(log: LocalEventLog):
    """Scenario: 事件依序附加"""
    for i in range(3):
        await log.append(JOB, JOB_START, i=i)
    assert [e.get("i") for e in await log.read(JOB)] == [0, 1, 2]


def test_interface_offers_no_update_or_delete():
    """Scenario: 不允許就地修改"""
    public = {n for n, _ in inspect.getmembers(EventLog, inspect.isfunction)
              if not n.startswith("_")}
    assert public == {"append", "read"}


# --- Requirement: 事件的共通欄位 ------------------------------------------


async def test_common_fields_present(log: LocalEventLog):
    """Scenario: 共通欄位齊備"""
    await log.append(JOB, PROXY_ROUTE, lane="txn")
    (event,) = await log.read(JOB)
    assert event.t == PROXY_ROUTE
    assert event.seq == 0
    assert isinstance(event.ts, datetime)
    assert event.job_id == JOB


async def test_sequence_numbers_are_contiguous_and_unique(log: LocalEventLog):
    """Scenario: 序號連續"""
    n = 25
    for _ in range(n):
        await log.append(JOB, CTX, used=1)
    seqs = [e.seq for e in await log.read(JOB)]
    assert seqs == list(range(n))


async def test_sequence_resumes_after_restart(tmp_path: Path):
    first = LocalEventLog(tmp_path)
    await first.append(JOB, JOB_START)
    await first.append(JOB, CTX, used=1)
    resumed = await LocalEventLog(tmp_path).append(JOB, JOB_FINISH)
    assert resumed.seq == 2


async def test_jobs_have_independent_streams(log: LocalEventLog):
    await log.append(JOB, JOB_START)
    other = await log.append("j8", JOB_START)
    assert other.seq == 0
    assert len(await log.read(JOB)) == 1


# --- Requirement: 涵蓋 job 生命週期的事件型別 -----------------------------


async def test_dispatch_end_carries_cost_and_transcript(log: LocalEventLog):
    """Scenario: dispatch 結束事件含成本與 token"""
    await log.append(
        JOB, DISPATCH_END, id="d3", lane="txn-2024", status=STATUS_OK,
        artifact="j7/note/lanes/txn-2024/findings/003",
        tokens={"in": 41200, "out": 3800}, turns=9, usd=0.31,
        transcript="traces/d3.jsonl",
    )
    (e,) = await log.read(JOB)
    assert e.get("status") == STATUS_OK
    assert e.get("artifact")
    assert e.get("tokens") == {"in": 41200, "out": 3800}
    assert e.get("turns") == 9 and e.get("usd") == 0.31
    assert e.get("transcript")


async def test_proxy_route_carries_reason_and_usage(log: LocalEventLog):
    """Scenario: proxy 路由事件含決策理由"""
    await log.append(
        JOB, PROXY_ROUTE, payload="j7/blob/raw/txns-2024", lane="txn-2024",
        reason="欄位含 ts/amt，屬 2024 交易明細", model="google/gemini-2.5-flash",
        tokens={"in": 2140, "out": 48},
    )
    (e,) = await log.read(JOB)
    for key in ("payload", "lane", "reason", "model", "tokens"):
        assert e.get(key), f"missing {key}"


async def test_unknown_event_types_are_tolerated(log: LocalEventLog):
    """Readers must not break when a new event type is introduced."""
    await log.append(JOB, "some.future.type", whatever=1)
    (e,) = await log.read(JOB)
    assert e.t == "some.future.type"
    assert not e.is_known_type
    assert e.get("whatever") == 1


# --- Requirement: Context 用量事件 ----------------------------------------


async def test_context_usage_is_recorded(log: LocalEventLog):
    """Scenario: 記錄 orchestrator 用量"""
    await log.append(JOB, CTX, who="orchestrator", used=74210, pct=0.38)
    (e,) = await log.read(JOB)
    assert e.get("who") == "orchestrator" and e.get("used") == 74210 and e.get("pct") == 0.38


async def test_context_peak_is_the_maximum(log: LocalEventLog):
    """Scenario: 可查得整個 job 的 context 峰值"""
    for used in (12_000, 74_210, 51_000):
        await log.append(JOB, CTX, who="orchestrator", used=used)
    await log.append(JOB, CTX, who="lane:txn", used=180_000)
    assert context_peak(await log.read(JOB)) == 74_210
    assert context_peak(await log.read(JOB), who="lane:txn") == 180_000


# --- Requirement: 聚合查詢介面 --------------------------------------------


def _stream() -> list[Event]:
    return [
        ev(0, JOB_START, task="分析交易"),
        ev(1, INGRESS, payload="j7/blob/raw/a", bytes=100),
        ev(2, INGRESS, payload="j7/blob/raw/orphan", bytes=50),
        ev(3, PROXY_ROUTE, payload="j7/blob/raw/a", lane="txn", usd=0.001,
           tokens={"in": 2000, "out": 40}),
        ev(4, DISPATCH_START, id="d1", lane="txn", inputs=["j7/blob/raw/a"]),
        ev(5, DISPATCH_END, id="d1", lane="txn", status=STATUS_OK, usd=0.31,
           tokens={"in": 41200, "out": 3800}),
        ev(6, DISPATCH_START, id="d2", lane="kyc"),
        ev(7, DISPATCH_END, id="d2", lane="kyc", status=STATUS_BUDGET_EXCEEDED,
           headline="完成 Q1-Q3，Q4 未處理", partial="j7/note/p", usd=0.12),
        ev(8, DISPATCH_END, id="d3", lane="txn", status=STATUS_DUPLICATE),
        ev(9, ASK_USER, qid="q1", text="需要 2023 對照資料嗎？", default="否"),
        ev(10, CTX, who="orchestrator", used=74210, pct=0.38),
        ev(11, JOB_FINISH, report="j7/note/report"),
    ]


def test_cost_aggregates_by_lane_with_proxy_separate():
    """Scenario: 依 lane 聚合成本"""
    costs = cost_by_lane(_stream())
    assert costs["txn"] == pytest.approx(0.31)
    assert costs["kyc"] == pytest.approx(0.12)
    assert costs["(proxy)"] == pytest.approx(0.001)


def test_tokens_aggregate_by_lane():
    assert tokens_by_lane(_stream())["txn"] == {"in": 41200, "out": 3800}


def test_failures_and_duplicates_are_listed():
    """Scenario: 列出降級與失敗"""
    bad = failures(_stream())
    assert {e.get("id") for e in bad} == {"d2", "d3"}
    assert [e.get("id") for e in duplicate_dispatches(_stream())] == ["d3"]


# --- Requirement: 交付物的 caveats 由事件流推導 ---------------------------


def test_budget_exceeded_lane_becomes_a_caveat():
    """Scenario: 超預算的 lane 進入 caveats"""
    kinds = {c.kind: c for c in derive_caveats(_stream())}
    assert STATUS_BUDGET_EXCEEDED in kinds
    caveat = kinds[STATUS_BUDGET_EXCEEDED]
    assert caveat.context["lane"] == "kyc"
    assert "Q4" in caveat.detail


def test_unanswered_question_becomes_a_caveat():
    """Scenario: 逾時未答的提問進入 caveats"""
    kinds = {c.kind for c in derive_caveats(_stream())}
    assert "unanswered_question" in kinds

    answered = _stream() + [ev(12, ASK_ANSWER, qid="q1", text="不用")]
    assert "unanswered_question" not in {c.kind for c in derive_caveats(answered)}


def test_unrouted_payload_becomes_a_caveat():
    caveat = next(c for c in derive_caveats(_stream()) if c.kind == "unprocessed_payload")
    assert caveat.context["payload"] == "j7/blob/raw/orphan"


def test_duplicate_dispatch_is_not_a_caveat():
    """A blocked duplicate cost nothing and lost nothing -- it is not a shortfall."""
    assert STATUS_DUPLICATE not in {c.kind for c in derive_caveats(_stream())}


# --- Requirement: 事件流可作為回歸斷言的來源 ------------------------------


def test_summary_supports_golden_job_assertions():
    """Scenario: 對 golden job 下斷言"""
    s = summarize(_stream())
    assert s.job_id == JOB
    assert s.finished is True
    assert s.dispatches == 2
    assert s.duplicates == 1
    assert s.failures == 2
    assert s.context_peak == 74_210
    assert s.total_usd == pytest.approx(0.431)
    assert len(s.caveats) == 3


def test_summary_of_empty_stream_is_not_finished():
    s = summarize([])
    assert s.finished is False and s.dispatches == 0


# --- Requirement: 寫入的耐久性 --------------------------------------------


async def test_truncated_final_line_is_tolerated(log: LocalEventLog, tmp_path: Path):
    await log.append(JOB, JOB_START)
    await log.append(JOB, CTX, used=1)
    path = JobLayout(tmp_path, JOB).events_path
    path.write_text(path.read_text(encoding="utf-8") + '{"t":"ctx","seq":2', encoding="utf-8")
    assert [e.seq for e in await log.read(JOB)] == [0, 1]


async def test_corruption_before_the_end_is_not_hidden(log: LocalEventLog, tmp_path: Path):
    await log.append(JOB, JOB_START)
    await log.append(JOB, CTX, used=1)
    path = JobLayout(tmp_path, JOB).events_path
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(["{ broken", *lines]) + "\n", encoding="utf-8")
    with pytest.raises(MalformedEvent):
        await log.read(JOB)


def test_events_survive_a_killed_process(tmp_path: Path):
    """Scenario: 中途中止仍可讀回"""
    script = f"""
import asyncio, os, signal, sys
sys.path.insert(0, {str(Path.cwd())!r})
from myharness.events import LocalEventLog

async def main():
    log = LocalEventLog({str(tmp_path)!r})
    for i in range(20):
        await log.append({JOB!r}, "ctx", used=i)
    os.kill(os.getpid(), signal.SIGKILL)

asyncio.run(main())
"""
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True)
    assert proc.returncode == -9, f"expected SIGKILL, got {proc.returncode}: {proc.stderr!r}"

    import asyncio

    events = asyncio.run(LocalEventLog(tmp_path).read(JOB))
    assert len(events) == 20
    assert [e.get("used") for e in events] == list(range(20))
