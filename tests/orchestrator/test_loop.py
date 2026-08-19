"""The orchestrator loop: continuity, the handoff, and always delivering."""

from __future__ import annotations

import pytest

from claude_agent_sdk import ResultMessage

from myharness.backends.profile import BackendCapability, BackendProfile, registry
from myharness.events.types import CTX, HANDOFF_RESTART, JOB_FINISH, LIMIT_REACHED
from myharness.jobs.spec import JobPhase
from myharness.orchestrator.loop import OrchestratorLoop
from myharness.orchestrator.plan import read_plan, write_plan
from myharness.orchestrator.session import ScriptedSession, ScriptedSessionFactory

from .conftest import JOB, payload

TEST_BACKEND = BackendProfile(
    name="loop-test", models={"strong": "test-model"},
    capabilities=frozenset({BackendCapability.STRUCTURED_OUTPUT}),
)


@pytest.fixture(autouse=True)
def _register_backend():
    registry.register(TEST_BACKEND)


def result_msg() -> ResultMessage:
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                         is_error=False, num_turns=1, session_id="s")


def make_loop(bench, sessions, **kw) -> OrchestratorLoop:
    return OrchestratorLoop(
        runner=bench.runner, lanes=bench.tools.lanes, backend="loop-test",
        sessions=sessions, tools=bench.tools, **kw
    )


async def _finish_via_tools(bench, report_name: str = "report") -> None:
    await bench.store.put_note(JOB, report_name, "# 報告\n完成。", produced_by="lane:syn")
    await bench.h["finish"]({"report_artifact": f"{JOB}/note/{report_name}"})


# --- normal completion ----------------------------------------------------


async def test_a_finished_job_stops_after_one_session(bench):
    async def turn(_):
        await _finish_via_tools(bench)

    session = ScriptedSession(turns=[[result_msg()]], usage_series=[10_000])
    factory = ScriptedSessionFactory([session])

    # The orchestrator "finishes" by calling the tool; emulate that side effect.
    original_send = session.send

    def send(text: str):
        stream = original_send(text)

        async def wrapped():
            async for m in stream:
                yield m
            await _finish_via_tools(bench)

        return wrapped()

    session.send = send  # type: ignore[method-assign]

    outcome = await make_loop(bench, factory).run()
    assert outcome.phase is JobPhase.COMPLETE
    assert outcome.report_artifact and outcome.handoffs == 0
    assert factory.open_count == 1


async def test_kickoff_describes_the_lane_catalogue(bench):
    session = ScriptedSession(turns=[[result_msg()]], usage_series=[1000])
    await make_loop(bench, ScriptedSessionFactory([session])).run()
    assert "ta" in session.sent[0]
    assert bench.runner.spec.goal in session.sent[0]


async def test_context_usage_is_recorded_every_turn(bench):
    session = ScriptedSession(turns=[[result_msg()]], usage_series=[42_000])
    await make_loop(bench, ScriptedSessionFactory([session])).run()
    events = await bench.kinds(CTX)
    assert events and events[0].get("who") == "orchestrator"
    assert events[0].get("used") == 42_000


# --- Requirement: Context 逼近上限時交接重啟 ------------------------------


async def test_threshold_triggers_a_handoff_request(bench):
    """Scenario: 達到門檻時觸發交接"""
    threshold = bench.runner.spec.handoff_threshold_tokens
    first = ScriptedSession(turns=[[result_msg()], [result_msg()]],
                            usage_series=[threshold + 1])
    second = ScriptedSession(turns=[[result_msg()]], usage_series=[1_000])
    factory = ScriptedSessionFactory([first, second])

    await make_loop(bench, factory).run()

    assert any("交接" in text for text in first.sent), "must ask for a handoff"
    assert factory.open_count == 2, "a fresh conversation must take over"


async def test_handoff_is_recorded_with_the_usage_that_caused_it(bench):
    """Scenario: 交接重啟被記錄"""
    threshold = bench.runner.spec.handoff_threshold_tokens
    factory = ScriptedSessionFactory([
        ScriptedSession(turns=[[result_msg()], [result_msg()]], usage_series=[threshold + 5]),
        ScriptedSession(turns=[[result_msg()]], usage_series=[500]),
    ])
    outcome = await make_loop(bench, factory).run()

    (event,) = await bench.kinds(HANDOFF_RESTART)
    assert event.get("used") == threshold + 5
    assert event.get("pct") >= bench.runner.spec.handoff_ratio
    assert outcome.handoffs == 1


async def test_successor_receives_the_plan_not_the_conversation(bench):
    """Scenario: 重啟後承接未完成的工作

    The successor cannot see the previous conversation, so whatever was not
    written into the plan is gone. That is exactly why the handoff asks for it.
    """
    await write_plan(bench.store, JOB, "# 目標\n測試\n\n## 已確認結論\n夜間高頻是主因。\n")
    threshold = bench.runner.spec.handoff_threshold_tokens
    first = ScriptedSession(turns=[[result_msg()], [result_msg()]], usage_series=[threshold + 1])
    second = ScriptedSession(turns=[[result_msg()]], usage_series=[100])
    await make_loop(bench, ScriptedSessionFactory([first, second])).run()

    resume = second.sent[0]
    assert "夜間高頻是主因" in resume
    assert "看不到先前的對話" in resume


async def test_a_small_job_never_hands_off(bench):
    """Scenario: 正常規模的 job 不觸發"""
    session = ScriptedSession(turns=[[result_msg()]], usage_series=[5_000])
    factory = ScriptedSessionFactory([session])
    outcome = await make_loop(bench, factory).run()
    assert outcome.handoffs == 0
    assert await bench.kinds(HANDOFF_RESTART) == []


async def test_repeated_handoffs_are_bounded(bench):
    """A conversation that fills up instantly must not restart forever."""
    threshold = bench.runner.spec.handoff_threshold_tokens
    factory = ScriptedSessionFactory([
        ScriptedSession(turns=[[result_msg()]] * 4, usage_series=[threshold + 1])
        for _ in range(8)
    ])
    outcome = await make_loop(bench, factory).run()
    assert outcome.handoffs <= 3
    assert outcome.reason == "handoff_limit"
    assert outcome.salvaged, "it never called finish, so the harness delivers"


# --- always deliver -------------------------------------------------------


async def test_an_unfinished_job_still_delivers(bench):
    """Scenario: 善終後仍有交付

    Past the bounded grace the fallback is written by code, not by the model.
    """
    dispatched = payload(await bench.h["dispatch"]({"lane": "txn", "task": "x"}))
    await bench.declare("txn")
    session = ScriptedSession(turns=[[result_msg()]], usage_series=[1_000])
    outcome = await make_loop(bench, ScriptedSessionFactory([session])).run()

    assert outcome.report_artifact, "a job must never end empty-handed"
    assert outcome.salvaged

    text = await bench.store.read_note(
        __import__("myharness.artifacts.ids", fromlist=["ArtifactId"]).ArtifactId.parse(
            outcome.report_artifact
        ),
        grants=__import__("myharness.artifacts.types", fromlist=["GrantSet"]).GrantSet.unrestricted(JOB),
        max_tokens=10_000,
    )
    assert "自動產出" in text


@pytest.mark.parametrize("bench", [{"max_dispatches": 1, "wrap_up_grace": 0}], indirect=True)
async def test_a_limited_job_is_marked_aborted_but_still_delivers(bench):
    await bench.declare("txn")
    d = payload(await bench.h["dispatch"]({"lane": "txn", "task": "分析"}))
    await bench.h["await_tasks"]({"task_ids": [d["task_id"]]})

    session = ScriptedSession(turns=[[result_msg()]], usage_series=[1_000])
    outcome = await make_loop(bench, ScriptedSessionFactory([session])).run()

    assert outcome.phase is JobPhase.ABORTED
    assert outcome.report_artifact
    assert await bench.kinds(LIMIT_REACHED)


async def test_finish_event_carries_the_reason(bench):
    session = ScriptedSession(turns=[[result_msg()]], usage_series=[1_000])
    await make_loop(bench, ScriptedSessionFactory([session])).run()
    (event,) = await bench.kinds(JOB_FINISH)
    assert event.get("reason") == "finished"
    assert event.get("salvaged") is True, "the loop ended normally but nobody called finish"
    assert event.get("report")
