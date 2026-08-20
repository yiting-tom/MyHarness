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


async def test_the_job_runs_until_finish_is_called(bench):
    """It stops when finish is called, not when it runs out of things to say."""
    session = ScriptedSession(turns=[tool_turn() for _ in range(5)], usage_series=[1_000])
    factory = ScriptedSessionFactory([session])
    outcome = await make_loop(bench, factory).run()
    assert outcome.turns > 1, "acting turns must not end the loop"
    assert any("請繼續" in text for text in session.sent)


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
    # A session that called nothing is idle, not finished. Reporting "finished"
    # was the bug the golden job found twice.
    assert event.get("reason") == "idle"
    assert event.get("salvaged") is True
    assert event.get("report")


# --- the loop must see what a turn actually did ---------------------------


def tool_turn(name: str = "plan_update"):
    from claude_agent_sdk import AssistantMessage, ToolUseBlock

    return [
        AssistantMessage(
            content=[ToolUseBlock(id="t1", name=f"mcp__harness__{name}", input={})],
            model="m",
        ),
        result_msg(),
    ]


def error_turn(*, transient: bool = False):
    from claude_agent_sdk import ResultMessage, SystemMessage

    messages = []
    if transient:
        messages.append(SystemMessage(subtype="api_retry",
                                      data={"error_status": 429, "error": "rate_limit"}))
    messages.append(ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                                  is_error=True, num_turns=1, session_id="s"))
    return messages


async def test_a_silent_turn_is_nudged_before_giving_up(bench):
    """Text alone changes nothing — it acts only through tools."""
    from myharness.orchestrator.loop import MAX_IDLE_TURNS

    session = ScriptedSession(turns=[[result_msg()] for _ in range(6)],
                              usage_series=[1_000])
    outcome = await make_loop(bench, ScriptedSessionFactory([session])).run()

    assert outcome.reason == "idle"
    assert any("沒有呼叫任何工具" in text for text in session.sent), "nudge it first"
    assert len(session.sent) == MAX_IDLE_TURNS + 1


async def test_acting_resets_the_idle_counter(bench):
    """One tool call buys the orchestrator more turns before the idle cap bites."""
    silent = [result_msg()]
    session = ScriptedSession(
        turns=[silent, silent, tool_turn(), silent, silent, silent, silent],
        usage_series=[1_000],
    )
    outcome = await make_loop(bench, ScriptedSessionFactory([session])).run()
    assert outcome.reason == "idle"
    assert outcome.turns == 6, "2 idle + 1 acting (resets) + 3 idle"


async def test_an_errored_turn_stops_rather_than_looping(bench):
    """A failed turn produced nothing to react to; retrying blindly repeats it."""
    session = ScriptedSession(turns=[error_turn()] * 4, usage_series=[1_000])
    outcome = await make_loop(bench, ScriptedSessionFactory([session])).run()
    assert outcome.reason == "session_error"
    assert len(session.sent) == 1


async def test_a_persistent_rate_limit_is_named_as_such(bench, monkeypatch):
    """"The backend refused us" and "the model went quiet" call for opposite fixes.

    One 429 is retried; a backend that keeps refusing past the gate's time
    budget is reported as unavailable rather than as an idle orchestrator.
    """
    import random

    from myharness.backends import gate as gate_module
    from tests.lanes.conftest import FakeClock

    clock = FakeClock()
    monkeypatch.setattr(
        gate_module, "gates",
        gate_module.GateRegistry(retry_budget_s=30.0, clock=clock,
                                 sleep=clock.sleep, rng=random.Random(0)),
    )
    monkeypatch.setattr("myharness.orchestrator.loop.gates", gate_module.gates)

    session = ScriptedSession(turns=[error_turn(transient=True)] * 30,
                              usage_series=[1_000])
    outcome = await make_loop(bench, ScriptedSessionFactory([session])).run()
    assert outcome.reason == "backend_unavailable"
    assert outcome.salvaged


async def test_a_rate_limit_after_real_work_does_not_abandon_the_job(bench, monkeypatch):
    """A 429 can land at the end of a turn that already planned and dispatched.

    The third golden run lost exactly that: one turn did real work, hit a rate
    limit on the way out, and the whole job stopped.
    """
    import random

    from myharness.backends import gate as gate_module
    from .conftest import FakeLane  # noqa: F401

    from tests.lanes.conftest import FakeClock  # virtual time, so no real sleeping

    clock = FakeClock()
    monkeypatch.setattr(
        gate_module, "gates",
        gate_module.GateRegistry(retry_budget_s=60.0, clock=clock,
                                 sleep=clock.sleep, rng=random.Random(0)),
    )
    monkeypatch.setattr("myharness.orchestrator.loop.gates", gate_module.gates)

    worked_then_failed = tool_turn() + error_turn(transient=True)
    session = ScriptedSession(
        turns=[worked_then_failed, tool_turn(), *[[result_msg()]] * 4],
        usage_series=[1_000],
    )
    outcome = await make_loop(bench, ScriptedSessionFactory([session])).run()

    assert outcome.turns > 1, "the job must survive a transient error"
    assert outcome.reason != "backend_unavailable"
    assert any("請繼續" in text for text in session.sent)


# --- refused tool calls are not progress -----------------------------------


class TestRefusalsAreNotProgress:
    """Calling a tool and getting somewhere are different things.

    The orchestrator's tools answer refusals as values rather than errors, so
    a rejected call is indistinguishable from a productive one in the message
    stream: the tool ran, it returned, nothing errored. Counting it as action
    let a live job burn turns re-sending the same rejected plan_update with
    only `ctx` events to show for it.
    """

    def test_a_refused_turn_did_not_act(self):
        from myharness.orchestrator.loop import TurnResult

        assert not TurnResult(tool_calls=1, refused=1).acted

    def test_a_partly_refused_turn_still_acted(self):
        from myharness.orchestrator.loop import TurnResult

        assert TurnResult(tool_calls=3, refused=1).acted

    def test_a_clean_turn_acted(self):
        from myharness.orchestrator.loop import TurnResult

        assert TurnResult(tool_calls=1).acted

    def test_a_silent_turn_did_not_act(self):
        from myharness.orchestrator.loop import TurnResult

        assert not TurnResult(tool_calls=0).acted


class TestReadingRefusals:
    def _block(self, content, is_error=False):
        from claude_agent_sdk import ToolResultBlock

        return ToolResultBlock(tool_use_id="t1", content=content, is_error=is_error)

    def test_an_error_payload_is_a_refusal(self):
        from myharness.orchestrator.loop import _refusal_of

        block = self._block([{"type": "text", "text":
                              '{"error": "bad_routing_table", "message": "no accepts"}'}])
        reason = _refusal_of(block)
        assert reason and "bad_routing_table" in reason and "no accepts" in reason

    def test_a_successful_payload_is_not(self):
        from myharness.orchestrator.loop import _refusal_of

        block = self._block([{"type": "text", "text": '{"plan_revision": 2}'}])
        assert _refusal_of(block) is None

    def test_a_plain_string_result_is_not_a_refusal(self):
        from myharness.orchestrator.loop import _refusal_of

        assert _refusal_of(self._block("wrote j/note/x")) is None

    def test_is_error_is_still_honoured(self):
        from myharness.orchestrator.loop import _refusal_of

        assert _refusal_of(self._block("boom", is_error=True))

    def test_unparseable_content_is_not_a_refusal(self):
        """Absent evidence of failure, assume it worked -- the alternative is
        treating every prose result as a rejection."""
        from myharness.orchestrator.loop import _refusal_of

        assert _refusal_of(self._block([{"type": "text", "text": "{not json"}])) is None

    def test_empty_content(self):
        from myharness.orchestrator.loop import _refusal_of

        assert _refusal_of(self._block(None)) is None


class TestTheRefusalNudge:
    def _loop(self, tmp_path):
        import asyncio

        from myharness.artifacts.local import LocalArtifactStore
        from myharness.events.log import LocalEventLog
        from myharness.jobs.runner import JobRunner
        from myharness.jobs.spec import JobSpec
        from myharness.lanes.types import LaneRegistry
        from myharness.orchestrator.loop import OrchestratorLoop

        store = LocalArtifactStore(tmp_path)
        asyncio.get_event_loop()
        runner = JobRunner(JobSpec(job_id="j", goal="g"), store=store,
                           event_log=LocalEventLog(tmp_path))
        return OrchestratorLoop(runner=runner, lanes=LaneRegistry(),
                                backend="anthropic")

    async def test_the_refusal_is_repeated_as_an_instruction(self, tmp_path):
        from myharness.orchestrator.loop import TurnResult

        loop = self._loop(tmp_path)
        turn = TurnResult(tool_calls=1, refused=1,
                          refusals=("bad_routing_table: lane 'a' has no accepts",))
        prompt = loop._next_prompt(idle=True, turn=turn)
        assert "被拒絕" in prompt and "no accepts" in prompt
        assert "不要用同樣的參數重送" in prompt

    async def test_a_productive_turn_gets_the_ordinary_nudge(self, tmp_path):
        from myharness.orchestrator.loop import TurnResult

        loop = self._loop(tmp_path)
        prompt = loop._next_prompt(idle=False, turn=TurnResult(tool_calls=2))
        assert "被拒絕" not in (prompt or "")
