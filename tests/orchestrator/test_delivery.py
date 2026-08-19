"""Delivery: a summary, a priced menu, and what the job could not do."""

from __future__ import annotations

import pytest

from myharness.artifacts.ids import ArtifactId
from myharness.artifacts.types import GrantSet
from myharness.events.types import (
    ASK_USER,
    DISPATCH_END,
    JOB_FINISH,
    JOB_START,
    LIMIT_REACHED,
    STATUS_BUDGET_EXCEEDED,
    STATUS_OK,
)
from myharness.orchestrator.delivery import (
    MAX_KEY_FINDINGS,
    MAX_SUMMARY_CHARS,
    build_delivery,
    drill,
)

from .conftest import JOB

REPORT = """\
## 摘要
2024 年交易中有 3 類異常，最大宗為深夜小額高頻。

- 異常率 2.3%，樣本數 30,412
- 深夜時段（00:00–04:00）佔異常的 61%
- 金額集中在 300–800 元區間

## 方法
以 duckdb 全表掃描，並用 IQR 判定離群。

## 發現
（很長的細節）

## 限制
2023 對照組資料未取得。
"""


async def _write_report(bench, text: str = REPORT) -> str:
    meta = await bench.store.put_note(JOB, "report", text, produced_by="lane:syn")
    return str(meta.id)


async def _build(bench, report: str | None, status: str = "complete", **kw):
    return await build_delivery(
        store=bench.store, events=await bench.stream(), job_id=JOB,
        status=status, report_artifact=report, **kw
    )


# --- the priced menu ------------------------------------------------------


async def test_sections_come_with_prices(bench):
    delivery = await _build(bench, await _write_report(bench))
    ids = [s.id for s in delivery.sections]
    assert ids == ["摘要", "方法", "發現", "限制"]
    assert all(s.est_tokens > 0 for s in delivery.sections)


async def test_summary_is_the_first_section(bench):
    delivery = await _build(bench, await _write_report(bench))
    assert "3 類異常" in delivery.executive_summary
    assert "duckdb" not in delivery.executive_summary, "later sections must not leak in"


async def test_summary_is_capped(bench):
    long_report = "## 摘要\n" + "很長的摘要。" * 2000 + "\n\n## 方法\nx\n"
    delivery = await _build(bench, await _write_report(bench, long_report))
    assert len(delivery.executive_summary) <= MAX_SUMMARY_CHARS


async def test_key_findings_are_extracted_and_capped(bench):
    delivery = await _build(bench, await _write_report(bench))
    assert delivery.key_findings
    assert "異常率 2.3%" in delivery.key_findings[0]
    assert len(delivery.key_findings) <= MAX_KEY_FINDINGS


async def test_delivery_stays_small(bench):
    """The caller is often an agent mid-task; the reply must not swamp it."""
    import json

    delivery = await _build(bench, await _write_report(bench))
    encoded = json.dumps(delivery.to_dict(), ensure_ascii=False)
    assert len(encoded) < 4000, f"delivery was {len(encoded)} chars"


async def test_drill_fetches_one_section(bench):
    report = await _write_report(bench)
    text = await drill(bench.store, JOB, report, "方法")
    assert "duckdb" in text
    assert "異常率" not in text


# --- caveats come from the stream, not the report ------------------------


async def test_degraded_lane_becomes_a_caveat(bench):
    await bench.events.append(JOB, DISPATCH_END, id="d1", lane="kyc",
                              status=STATUS_BUDGET_EXCEEDED,
                              headline="完成 Q1–Q3，Q4 未處理")
    delivery = await _build(bench, await _write_report(bench))
    kinds = {c.kind for c in delivery.caveats}
    assert STATUS_BUDGET_EXCEEDED in kinds


async def test_unanswered_question_becomes_a_caveat(bench):
    await bench.events.append(JOB, ASK_USER, qid="q1", text="要納入 2023 嗎？",
                              default="否")
    delivery = await _build(bench, await _write_report(bench))
    assert "unanswered_question" in {c.kind for c in delivery.caveats}


async def test_limit_reached_becomes_a_caveat(bench):
    await bench.events.append(JOB, LIMIT_REACHED, limit="max_dispatches", value=60)
    delivery = await _build(bench, await _write_report(bench))
    assert "limit_reached" in {c.kind for c in delivery.caveats}


async def test_a_shortfall_caps_confidence(bench):
    """A degraded lane anywhere means the whole delivery cannot claim high."""
    await bench.events.append(JOB, DISPATCH_END, id="d1", lane="kyc",
                              status=STATUS_BUDGET_EXCEEDED, headline="未完成")
    delivery = await _build(bench, await _write_report(bench), confidence="high")
    assert delivery.confidence != "high"


async def test_a_clean_job_keeps_its_confidence(bench):
    await bench.events.append(JOB, DISPATCH_END, id="d1", lane="txn",
                              status=STATUS_OK, artifact="j7/note/f/1")
    delivery = await _build(bench, await _write_report(bench), confidence="high")
    assert delivery.confidence == "high"


async def test_cost_and_dispatch_count_are_reported(bench):
    await bench.events.append(JOB, JOB_START, goal="g")
    await bench.events.append(JOB, "dispatch.start", id="d1", lane="txn")
    await bench.events.append(JOB, DISPATCH_END, id="d1", lane="txn",
                              status=STATUS_OK, usd=0.31, artifact="a")
    delivery = await _build(bench, await _write_report(bench))
    assert delivery.cost_usd == pytest.approx(0.31)
    assert delivery.dispatches == 1


# --- degenerate cases -----------------------------------------------------


async def test_no_report_still_delivers_something(bench):
    delivery = await _build(bench, None, status="aborted")
    assert delivery.status == "aborted"
    assert delivery.executive_summary
    assert delivery.confidence == "low"


async def test_unreadable_report_does_not_crash(bench):
    delivery = await _build(bench, f"{JOB}/note/does-not-exist")
    assert delivery.confidence == "low"
    assert "無法讀取" in delivery.executive_summary


async def test_report_without_sections_still_summarises(bench):
    report = await _write_report(bench, "沒有分節的報告，就是一段文字。\n")
    delivery = await _build(bench, report)
    assert delivery.sections == ()
    assert "沒有分節" in delivery.executive_summary
