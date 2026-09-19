"""lane.step: what a lane is doing, written while it is doing it.

Golden #25's d1 ran six minutes and 22 queries and wrote nothing, and all that
anyone watching could see for those six minutes was a timer: the transcript is
written when a dispatch ends.
"""

from __future__ import annotations

from datetime import UTC, datetime

from claude_agent_sdk import (
    AssistantMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from myharness.dataflow import build_dataflow
from myharness.events.types import LANE_STEP, THROTTLE_WAIT, Event
from myharness.lanes.transport import ScriptedTransport
from myharness.lanes.worker import (
    STEP_ARG_CHARS,
    Accumulated,
    WorkerRequest,
    _consume,
    run_lane_worker,
    step_event,
)
from myharness.mcp.manager import MEANINGFUL, NOT_NEWS
from myharness.monitor.live import current_activity

from .conftest import GOOD_HANDLE, JOB, result

SECRET = "這一行是查詢結果的內容，不該出現在事件流裡"


def _call(sql: str = "SELECT channel, count(*) FROM t GROUP BY 1") -> AssistantMessage:
    return AssistantMessage(
        content=[ThinkingBlock(thinking="想一下", signature="s"),
                 TextBlock(text="先看通路分布"),
                 ToolUseBlock(id="t1", name="mcp__lane__run_query", input={"sql": sql})],
        model="m", usage={"input_tokens": 100, "output_tokens": 20},
    )


def _answer(error: bool = False) -> UserMessage:
    return UserMessage(content=[ToolResultBlock(tool_use_id="t1", content=SECRET,
                                                is_error=error)])


def _fed(*messages) -> tuple[Accumulated, list[dict]]:
    acc, out = Accumulated(), []
    for m in messages:
        _consume(m, acc)
        step = step_event(m, acc, 10_000)
        if step is not None:
            out.append(step)
    return acc, out


def test_a_turn_names_the_tool_and_a_short_argument():
    _, (step,) = _fed(_call())
    assert step["phase"] == "turn"
    assert step["calls"] == [{"tool": "run_query",
                              "arg": "sql=SELECT channel, count(*) FROM t GROUP BY 1"}]
    assert step["thinking_chars"] == 3 and step["text_chars"] == 6
    assert step["thinking_blocks"] == 1
    assert step["turn"] == 1 and step["attempt"] == 1


def test_the_argument_is_bounded():
    _, (step,) = _fed(_call("SELECT " + "x, " * 400 + "1"))
    arg = step["calls"][0]["arg"]
    assert len(arg) == STEP_ARG_CHARS and arg.endswith("…")


def test_a_result_carries_its_size_and_never_its_content():
    """Scenario: 結果只記大小"""
    _, (_, step) = _fed(_call(), _answer(error=True))
    assert step["phase"] == "results"
    assert step["results"] == [{"chars": len(SECRET), "error": True}]
    assert SECRET not in repr(step)


def test_reasoning_text_never_reaches_a_step():
    _, (step,) = _fed(_call())
    assert "想一下" not in repr(step)


def test_the_budget_share_moves_with_the_run():
    _, steps = _fed(_call(), _answer(), _call(), _answer())
    shares = [s["pct"] for s in steps]
    assert shares == sorted(shares) and shares[-1] > shares[0]


def test_a_message_that_did_nothing_writes_no_step():
    assert step_event(UserMessage(content="plain"), Accumulated(), 1) is None


async def test_steps_are_in_the_stream_before_the_dispatch_ends(bench):
    """Scenario: 執行中就看得到工具呼叫"""
    seen_while_running: list[str] = []

    class Watching(ScriptedTransport):
        def stream(self, prompt, options):
            inner = super().stream(prompt, options)

            async def gen():
                async for m in inner:
                    yield m
                    seen_while_running.extend(e.t for e in await bench.events.read(JOB))
            return gen()

    await run_lane_worker(
        WorkerRequest(job_id=JOB, lane=bench.lane, task="t", dispatch_id="d1"),
        store=bench.store, event_log=bench.events,
        transport=Watching([_call(), _answer(), result(structured=GOOD_HANDLE)]),
    )

    assert LANE_STEP in seen_while_running
    assert "dispatch.end" not in seen_while_running
    steps = [e for e in await bench.events.read(JOB) if e.t == LANE_STEP]
    assert [s.get("phase") for s in steps] == ["turn", "results"]
    assert all(s.get("dispatch") == "d1" and s.get("lane") == bench.lane.id for s in steps)


def test_a_step_never_wakes_a_long_poll():
    """Scenario: 不喚醒 long-poll"""
    assert LANE_STEP not in MEANINGFUL and LANE_STEP in NOT_NEWS


def test_steps_do_not_push_a_throttle_wait_out_of_view():
    """Scenario: 不擠掉限流狀態"""
    t = datetime(2026, 9, 19, tzinfo=UTC)
    events = [Event("dispatch.start", 1, t, JOB, {"id": "d1", "lane": "a"}),
              Event("dispatch.start", 2, t, JOB, {"id": "d2", "lane": "b"}),
              Event(THROTTLE_WAIT, 3, t, JOB, {"backend": "proxy", "lane": "a"})]
    events += [Event(LANE_STEP, 4 + i, t, JOB, {"dispatch": "d2", "phase": "turn"})
               for i in range(12)]
    activity = current_activity(events, build_dataflow(events, job_id=JOB))
    assert activity.state == "等待限流"
