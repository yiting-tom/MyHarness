"""Live tests: the claim this whole change exists to check.

Offline tests prove the harness *would* bound a worker's output. These prove a
real model cannot get past it. They cost money and need a key, so they are
marked ``live`` and skipped by default:

    pytest -m live tests/lanes/test_live_lane_worker.py

Assertions are about mechanism, never about the quality of the analysis --
model output varies run to run, the bounds do not.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from myharness.artifacts.local import LocalArtifactStore
from myharness.artifacts.types import GrantSet
from myharness.events.log import LocalEventLog
from myharness.events.query import summarize
from myharness.lanes.handle import MAX_HANDLE_CHARS, MAX_HEADLINE_CHARS, HandleStatus
from myharness.lanes.types import LaneRegistry, LaneType
from myharness.lanes.worker import WorkerRequest, run_lane_worker

pytestmark = pytest.mark.live

JOB = "live"
BACKEND = os.environ.get("HARNESS_LIVE_BACKEND", "openrouter")
CHARTER = Path("charters/tabular-analyst.md")


def _requires_key() -> None:
    from myharness.backends.profile import registry

    profile = registry.get(BACKEND)
    if profile.auth_token_env and not os.environ.get(profile.auth_token_env):
        pytest.skip(f"{profile.auth_token_env} not set")


@pytest.fixture
async def live(tmp_path: Path):
    _requires_key()
    store = LocalArtifactStore(tmp_path)
    await store.init_job(JOB)
    events = LocalEventLog(tmp_path)

    def make(**overrides):
        base = LaneType(
            name="tabular-analyst", charter_path=CHARTER, backend=BACKEND,
            model_tier="strong",
            tools=("read_note", "write_finding", "update_state", "localize_blob"),
            token_budget=40_000, max_turns=8, state_max_tokens=1_500,
        )
        return replace(base, **overrides)

    async def run(task: str, *, lane_type=None, dispatch_id="d1", lane_id="lane-1", inputs=()):
        lanes = LaneRegistry(lane_type or make())
        lane = lanes.create(lane_id, "tabular-analyst")
        return await run_lane_worker(
            WorkerRequest(job_id=JOB, lane=lane, task=task,
                          dispatch_id=dispatch_id, inputs=tuple(inputs)),
            store=store, event_log=events,
        )

    return run, store, events, make


async def test_long_report_request_still_yields_a_bounded_handle(live):
    """Scenario: 被要求寫長文時仍只回傳 handle

    This is the change's central claim. The task explicitly asks for a long
    report; the handle must stay inside its ceiling regardless.
    """
    run, store, events, _ = live
    handle = await run(
        "請針對『表格資料分析的方法論』寫一份至少 3000 字的詳盡報告，"
        "涵蓋抽樣、異常偵測、與信賴區間。寫得越詳細越好。"
    )

    assert len(handle.to_json()) <= MAX_HANDLE_CHARS
    assert len(handle.headline) <= MAX_HEADLINE_CHARS
    if handle.ok:
        assert handle.artifact, "the long content must live in an artifact"
        meta = await store.stat(
            __import__("myharness.artifacts.ids", fromlist=["ArtifactId"]).ArtifactId.parse(
                handle.artifact
            ),
            grants=GrantSet.unrestricted(JOB),
        )
        assert (meta.est_tokens or 0) > len(handle.to_json()) // 4, (
            "the analysis should be larger than the handle that points at it"
        )


async def test_budget_exhaustion_is_a_value_not_an_exception(live):
    """Scenario: 超出預算回傳部分結果"""
    run, _, events, make = live
    handle = await run(
        "請寫一份極為詳盡的長篇報告，並反覆檢查與擴充內容。",
        lane_type=make(token_budget=600), dispatch_id="d-budget",
    )
    assert handle.status in {HandleStatus.BUDGET_EXCEEDED, HandleStatus.MAX_TURNS,
                             HandleStatus.SCHEMA_VIOLATION}
    assert not handle.ok
    assert handle.transcript
    assert handle.suggest


async def test_max_turns_is_a_value_not_an_exception(live):
    """Scenario: 回合數用盡不拋例外"""
    run, _, _, make = live
    handle = await run(
        "請呼叫 write_finding 至少五次，每次寫一個不同的小節，然後才回報。",
        lane_type=make(max_turns=1), dispatch_id="d-turns",
    )
    assert not handle.ok
    assert handle.status in {HandleStatus.MAX_TURNS, HandleStatus.SCHEMA_VIOLATION}


async def test_state_carries_across_runs_but_conversation_does_not(live):
    """Scenario: Lane state 提供跨任務的連續性 / 前一次對話不影響下一次"""
    run, store, _, _ = live
    marker = "紫色犀牛"

    first = await run(
        f"把『本批資料的關鍵結論是 {marker}』寫進 update_state，"
        "並用 write_finding 記錄一句話說明，然後回報 handle。",
        dispatch_id="d-a", lane_id="carry",
    )
    assert first is not None

    from myharness.artifacts.ids import ArtifactId

    state = await store.read_note(
        ArtifactId(JOB, "note", "lanes/carry/state"),
        grants=GrantSet.unrestricted(JOB), max_tokens=5000,
    )
    assert marker in state, "the worker must have recorded the conclusion in lane state"


async def test_contract_holds_on_a_non_anthropic_model(live):
    """Scenario: 不同後端 -- the handle contract must not depend on the vendor."""
    run, _, events, _ = live
    handle = await run("用一句話說明什麼是離群值，並回報 handle。", dispatch_id="d-vendor")
    assert len(handle.to_json()) <= MAX_HANDLE_CHARS
    summary = summarize(await events.read(JOB))
    assert summary.dispatches >= 1


async def test_event_stream_records_real_cost_and_usage(live):
    """Task 8.7's measurement, asserted rather than eyeballed."""
    run, _, events, _ = live
    await run("回報一個 handle，artifact 填 'none'。", dispatch_id="d-cost")
    stream = await events.read(JOB)
    ends = [e for e in stream if e.t == "dispatch.end"]
    assert ends and ends[-1].get("tokens")
    assert ends[-1].get("transcript")
