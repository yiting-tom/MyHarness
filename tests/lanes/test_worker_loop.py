"""The worker loop: ephemeral context, durable state, failures as values."""

from __future__ import annotations

import json

import pytest

from myharness.artifacts.ids import ArtifactId
from myharness.artifacts.types import GrantSet
from myharness.events.types import CTX, DISPATCH_END, DISPATCH_START
from myharness.lanes.contract import ContractPath
from myharness.lanes.handle import HandleStatus
from myharness.lanes.transport import ScriptedTransport
from myharness.lanes.worker import WorkerRequest, run_lane_worker

from .conftest import JOB, GOOD_HANDLE, api_retry, assistant, handle_text, result, with_backend


def req(bench, task="分析 2024 交易", inputs=(), dispatch_id="d1"):
    return WorkerRequest(job_id=JOB, lane=bench.lane, task=task,
                         dispatch_id=dispatch_id, inputs=tuple(inputs))


async def run(bench, transport, **kw):
    return await run_lane_worker(
        req(bench, **kw), store=bench.store, event_log=bench.events, transport=transport
    )


# --- happy path -----------------------------------------------------------


async def test_enforced_path_returns_the_structured_handle(bench):
    transport = ScriptedTransport([assistant("done"), result(structured=GOOD_HANDLE)])
    handle = await run(bench, transport)

    assert handle.ok
    assert handle.artifact == GOOD_HANDLE["artifact"]
    assert handle.lane == "txn-2024"
    assert handle.dispatch_id == "d1"
    assert handle.transcript


async def test_degraded_path_parses_json_from_the_reply(bench):
    """Scenario: 不支援時退回應用層驗證"""
    lane = with_backend(bench.lane, "test-degraded")
    transport = ScriptedTransport([assistant(handle_text()), result()])
    handle = await run_lane_worker(
        WorkerRequest(job_id=JOB, lane=lane, task="t", dispatch_id="d1"),
        store=bench.store, event_log=bench.events, transport=transport,
    )
    assert handle.ok and handle.artifact == GOOD_HANDLE["artifact"]

    (start,) = await bench.events_for(DISPATCH_START)
    assert start.get("contract_path") == ContractPath.DEGRADED


async def test_enforced_path_is_recorded_in_the_event(bench):
    """Scenario: 支援結構化輸出時走強制路徑"""
    await run(bench, ScriptedTransport([result(structured=GOOD_HANDLE)]))
    (start,) = await bench.events_for(DISPATCH_START)
    assert start.get("contract_path") == ContractPath.ENFORCED
    assert start.get("charter"), "charter hash ties the run to a charter version"


async def test_schema_is_only_sent_when_the_backend_can_enforce_it(bench):
    enforcing = ScriptedTransport([result(structured=GOOD_HANDLE)])
    await run(bench, enforcing)
    assert enforcing.calls[0][1].output_format is not None

    lane = with_backend(bench.lane, "test-degraded")
    degraded = ScriptedTransport([assistant(handle_text()), result()])
    await run_lane_worker(
        WorkerRequest(job_id=JOB, lane=lane, task="t", dispatch_id="d2"),
        store=bench.store, event_log=bench.events, transport=degraded,
    )
    assert degraded.calls[0][1].output_format is None


# --- ephemeral context, durable state ------------------------------------


async def test_first_run_has_no_prior_state(bench):
    """Scenario: 首次執行時沒有既有 state"""
    transport = ScriptedTransport([result(structured=GOOD_HANDLE)])
    handle = await run(bench, transport)
    assert handle.ok
    assert "尚無累積認知" in transport.last_prompt()


async def test_state_is_carried_into_the_next_run(bench):
    """Scenario: Lane state 提供跨任務的連續性"""
    await bench.store.put_note(
        JOB, bench.lane.state_name, "## 已確認結論\n夜間高頻", produced_by="lane:txn-2024"
    )
    transport = ScriptedTransport([result(structured=GOOD_HANDLE)])
    await run(bench, transport)
    assert "夜間高頻" in transport.last_prompt()


async def test_previous_conversation_does_not_leak_into_the_next_run(bench):
    """Scenario: 前一次任務的對話不影響下一次

    The first run says something memorable that never reaches lane state; the
    second run's prompt must not contain it.
    """
    secret = "此細節只出現在第一次對話中"
    first = ScriptedTransport([assistant(secret), result(structured=GOOD_HANDLE)])
    await run(bench, first, dispatch_id="d1")

    second = ScriptedTransport([result(structured=GOOD_HANDLE)])
    await run(bench, second, dispatch_id="d2")
    assert secret not in second.last_prompt()


async def test_inputs_are_listed_and_granted(bench):
    granted = await bench.store.put_note(JOB, "lanes/kyc/findings/001", "kyc", produced_by="kyc")
    transport = ScriptedTransport([result(structured=GOOD_HANDLE)])
    await run(bench, transport, inputs=[str(granted.id)])
    assert str(granted.id) in transport.last_prompt()


# --- failures are values --------------------------------------------------


async def test_budget_exhaustion_returns_a_handle_with_partials(bench):
    """Scenario: 超出預算回傳部分結果

    task_budget kills the stream before ResultMessage, so the loop must build
    the handle from what it already accumulated (design.md D1).
    """
    boom = RuntimeError("Claude Code returned an error result: success")
    transport = ScriptedTransport([assistant("working…"), boom])
    handle = await run(bench, transport)

    assert handle.status is HandleStatus.BUDGET_EXCEEDED
    assert not handle.ok
    assert handle.suggest
    assert handle.transcript, "a dead run still leaves a transcript"


async def test_max_turns_returns_a_handle_not_an_exception(bench):
    """Scenario: 回合數用盡不拋例外"""
    transport = ScriptedTransport([assistant("…"), result(subtype="error_max_turns", is_error=True)])
    handle = await run(bench, transport)
    assert handle.status is HandleStatus.MAX_TURNS
    assert "回合" in handle.headline


async def test_tool_failure_is_attributed(bench):
    """Scenario: 工具持續失敗被歸因"""
    lane = with_backend(bench.lane, "test-degraded")  # no API-side budget
    transport = ScriptedTransport([assistant("x"), ValueError("duckdb: bad encoding at row 40912")])
    handle = await run_lane_worker(
        WorkerRequest(job_id=JOB, lane=lane, task="t", dispatch_id="d1"),
        store=bench.store, event_log=bench.events, transport=transport,
    )
    assert handle.status is HandleStatus.TOOL_FAILURE
    assert "duckdb" in (handle.detail or "")
    assert handle.suggest


async def test_run_lane_worker_never_raises_for_semantic_failure(bench):
    for script in (
        [RuntimeError("boom")],
        [assistant("not json at all"), result()],
        [result(subtype="error_max_turns", is_error=True)],
    ):
        handle = await run(bench, ScriptedTransport(script))
        assert handle is not None and not handle.ok


async def test_partial_finding_is_referenced_on_failure(bench):
    """A worker that wrote something before dying must not lose it."""
    from myharness.lanes import worker as W

    transport = ScriptedTransport([assistant("…"), RuntimeError("dead")])
    original = W.WorkerToolbox.build_server

    def build_and_write(self):
        server = original(self)
        self.findings.append(f"{JOB}/note/lanes/txn-2024/findings/partial")
        return server

    W.WorkerToolbox.build_server = build_and_write
    try:
        handle = await run(bench, transport)
    finally:
        W.WorkerToolbox.build_server = original
    assert handle.partial and handle.partial.endswith("findings/partial")


# --- transient handling ---------------------------------------------------


async def test_rate_limit_is_retried_then_succeeds(bench, monkeypatch):
    """Scenario: 速率限制被靜默重試"""
    monkeypatch.setattr("myharness.lanes.worker.TRANSIENT_BACKOFF_S", (0.0, 0.0))
    transport = ScriptedTransport(
        [api_retry(429), RuntimeError("gave up")],
        [result(structured=GOOD_HANDLE)],
    )
    handle = await run(bench, transport)
    assert handle.ok, "caller should not see the retry"
    assert transport.call_count == 2


async def test_persistent_rate_limit_becomes_a_value(bench, monkeypatch):
    """Scenario: 重試耗盡後成為失敗值"""
    monkeypatch.setattr("myharness.lanes.worker.TRANSIENT_BACKOFF_S", (0.0, 0.0))
    transport = ScriptedTransport(*[[api_retry(429), RuntimeError("429")] for _ in range(6)])
    handle = await run(bench, transport)
    assert handle.status is HandleStatus.BACKEND_UNAVAILABLE
    assert "429" in (handle.detail or "")


async def test_semantic_failure_is_not_retried(bench):
    """Scenario: 語意失敗不被重試"""
    transport = ScriptedTransport([assistant("x"), RuntimeError("budget")])
    await run(bench, transport)
    assert transport.call_count == 1, "budget exhaustion must not be re-run"


# --- schema retries -------------------------------------------------------


async def test_degraded_path_reprompts_then_succeeds(bench):
    lane = with_backend(bench.lane, "test-degraded")
    transport = ScriptedTransport(
        [assistant("這是我的報告，很長很長"), result()],
        [assistant(handle_text()), result()],
    )
    handle = await run_lane_worker(
        WorkerRequest(job_id=JOB, lane=lane, task="t", dispatch_id="d1"),
        store=bench.store, event_log=bench.events, transport=transport,
    )
    assert handle.ok
    assert transport.call_count == 2
    assert "not a valid handle" in transport.calls[1][0]


async def test_repeated_schema_violation_becomes_a_failure_handle(bench):
    """Scenario: 不合 schema 的輸出觸發重試後失敗"""
    lane = with_backend(bench.lane, "test-degraded")
    transport = ScriptedTransport(*[[assistant("prose only"), result()] for _ in range(5)])
    handle = await run_lane_worker(
        WorkerRequest(job_id=JOB, lane=lane, task="t", dispatch_id="d1"),
        store=bench.store, event_log=bench.events, transport=transport,
    )
    assert handle.status is HandleStatus.SCHEMA_VIOLATION
    assert transport.call_count == 3, "1 attempt + MAX_SCHEMA_RETRIES"


# --- transcript & events --------------------------------------------------


async def test_transcript_is_a_blob_and_cannot_be_read_into_context(bench):
    """Scenario: 成功執行留下 transcript"""
    handle = await run(bench, ScriptedTransport([assistant("hi"), result(structured=GOOD_HANDLE)]))
    aid = ArtifactId.parse(handle.transcript)
    assert aid.is_blob

    from myharness.artifacts.errors import BlobNotReadable

    with pytest.raises(BlobNotReadable):
        await bench.store.read_note(aid, grants=GrantSet.unrestricted(JOB), max_tokens=99_999)

    async with bench.store.localize(aid, grants=GrantSet.unrestricted(JOB)) as path:
        rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert any(r["role"] == "assistant" for r in rows)


async def test_failed_run_still_leaves_a_transcript(bench):
    """Scenario: 失敗執行同樣留下 transcript"""
    handle = await run(bench, ScriptedTransport([assistant("partial work"), RuntimeError("dead")]))
    async with bench.store.localize(ArtifactId.parse(handle.transcript),
                                    grants=GrantSet.unrestricted(JOB)) as path:
        assert "partial work" in path.read_text()


async def test_end_event_carries_cost_and_usage(bench):
    """Scenario: 成功執行的事件含成本與用量"""
    await run(bench, ScriptedTransport([result(structured=GOOD_HANDLE, usd=0.31,
                                               usage={"input_tokens": 4000, "output_tokens": 300})]))
    (end,) = await bench.events_for(DISPATCH_END)
    assert end.get("status") == "ok"
    assert end.get("tokens") == {"in": 4000, "out": 300}
    assert end.get("usd") == 0.31
    assert end.get("transcript") and end.get("artifact")


async def test_degraded_run_writes_an_end_event_too(bench):
    """Scenario: 失敗執行同樣寫入結束事件"""
    await run(bench, ScriptedTransport([RuntimeError("dead")]))
    (end,) = await bench.events_for(DISPATCH_END)
    assert end.get("status") == HandleStatus.BUDGET_EXCEEDED
    (ctx,) = await bench.events_for(CTX)
    assert ctx.get("who") == "lane:txn-2024"


async def test_null_usage_fields_do_not_crash_the_run(bench):
    """A provider may report usage keys with null values rather than omitting them.

    `.get(key, 0)` returns None in that case, and int(None) took down a real
    live run before this test existed.
    """
    nulls = {"input_tokens": None, "output_tokens": None,
             "cache_read_input_tokens": None, "cache_creation_input_tokens": None}
    transport = ScriptedTransport([
        assistant("x", usage=nulls),
        result(structured=GOOD_HANDLE, usage=nulls),
    ])
    handle = await run(bench, transport)
    assert handle.ok

    (end,) = await bench.events_for(DISPATCH_END)
    assert end.get("tokens") == {"in": 0, "out": 0}


async def test_rate_limited_run_is_not_mistaken_for_a_bad_handle(bench, monkeypatch):
    """A 429 that ends without raising must not burn the schema retries.

    The CLI exhausts its own retries, then returns is_error=True with the error
    text as the assistant's reply. Treating that as malformed output and
    re-prompting triples the load on a backend already refusing us.
    """
    monkeypatch.setattr("myharness.lanes.worker.TRANSIENT_BACKOFF_S", (0.0, 0.0))
    rate_limited = [
        api_retry(429),
        assistant("API Error: Request rejected (429) · Provider returned error"),
        result(is_error=True),
    ]
    transport = ScriptedTransport(*[list(rate_limited) for _ in range(6)])
    handle = await run(bench, transport)

    assert handle.status is HandleStatus.BACKEND_UNAVAILABLE
    assert transport.call_count <= 3, "transient retries only, no schema re-prompts"


async def test_errored_result_without_transient_signal_is_not_a_schema_violation(bench):
    transport = ScriptedTransport([assistant("something went wrong"), result(is_error=True)])
    handle = await run(bench, transport)
    assert handle.status is not HandleStatus.SCHEMA_VIOLATION
    assert not handle.ok
