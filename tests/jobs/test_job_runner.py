"""Job runner: non-blocking dispatch, blocking collection, three guards."""

from __future__ import annotations

import asyncio
import time

import pytest

from myharness.events.types import LIMIT_REACHED, NO_PROGRESS
from myharness.jobs.channel import Question, QueueChannel, ScriptedChannel
from myharness.jobs.spec import JobPhase, LimitKind
from myharness.jobs.state import TaskStatus
from myharness.lanes.handle import HandleStatus

from .conftest import JOB, failing


# --- Requirement: 派工非阻塞，收割單次阻塞 -------------------------------


async def test_dispatch_returns_before_the_work_finishes(bench):
    """Scenario: 派工立即返回"""
    started = time.monotonic()
    results = [await bench.runner.dispatch(lane, f"任務 {lane}") for lane in "abc"]
    elapsed = time.monotonic() - started

    assert [r.task_id for r in results] == ["d1", "d2", "d3"]
    assert all(r.status == "running" for r in results)
    assert elapsed < bench.fake.delay, "dispatch must not wait for the worker"


async def test_background_tasks_actually_overlap(bench):
    """Scenario: 背景任務真正並行"""
    ids = [(await bench.runner.dispatch(lane, f"任務 {lane}")).task_id for lane in "abc"]
    await bench.runner.await_tasks(ids)
    assert bench.fake.overlapped(), "execution spans must overlap"


async def test_collect_all_waits_for_everything(bench):
    """Scenario: 收割等到全部完成"""
    ids = [(await bench.runner.dispatch(lane, f"任務 {lane}")).task_id for lane in "abc"]
    result = await bench.runner.await_tasks(ids, mode="all")
    assert len(result.handles) == 3
    assert not result.still_running and not result.timed_out


@pytest.mark.parametrize("bench", [{"delay": 0.05}], indirect=True)
async def test_collect_any_returns_on_the_first(bench):
    """Scenario: 收割可只等任一完成"""
    ids = [(await bench.runner.dispatch(lane, f"任務 {lane}")).task_id for lane in "ab"]
    result = await bench.runner.await_tasks(ids, mode="any", timeout=5)
    assert len(result.handles) >= 1
    # Whatever did not finish keeps going rather than being abandoned.
    later = await bench.runner.await_tasks(ids, mode="all", timeout=5)
    assert len(later.handles) == 2


@pytest.mark.parametrize("bench", [{"delay": 0.5}], indirect=True)
async def test_collect_timeout_reports_rather_than_raises(bench):
    """Scenario: 收割逾時回報未完成者"""
    ids = [(await bench.runner.dispatch(lane, f"任務 {lane}")).task_id for lane in "ab"]
    result = await bench.runner.await_tasks(ids, timeout=0.05)
    assert result.timed_out
    assert set(result.still_running) == set(ids)
    assert result.handles == ()
    await bench.runner.settle()


# --- Requirement: 重複派工不執行 ------------------------------------------


async def test_duplicate_dispatch_returns_the_previous_result(bench):
    """Scenario: 相同派工回傳前次結果"""
    first = await bench.runner.dispatch("a", "分析 2024 交易")
    await bench.runner.await_tasks([first.task_id])

    again = await bench.runner.dispatch("a", "分析 2024 交易")
    assert again.status == "duplicate"
    assert again.duplicate_of == first.task_id
    assert again.handle is not None, "the caller wanted this result; give it to them"
    assert bench.fake.calls == [first.task_id], "must not run a second time"


async def test_whitespace_does_not_defeat_duplicate_detection(bench):
    first = await bench.runner.dispatch("a", "分析")
    again = await bench.runner.dispatch("a", "  分析  ")
    assert again.duplicate_of == first.task_id


async def test_different_task_is_not_a_duplicate(bench):
    """Scenario: 內容不同即非重複"""
    first = await bench.runner.dispatch("a", "分析 2024")
    second = await bench.runner.dispatch("a", "分析 2023")
    assert second.task_id != first.task_id
    assert second.status == "running"


async def test_same_task_on_a_different_lane_is_not_a_duplicate(bench):
    await bench.runner.dispatch("a", "分析")
    other = await bench.runner.dispatch("b", "分析")
    assert other.status == "running"


async def test_unknown_lane_is_refused_with_the_available_ones(bench):
    result = await bench.runner.dispatch("nope", "x")
    assert result.status == "unknown_lane"
    assert "a" in (result.note or "")


# --- Requirement: Job 級硬上限與善終 --------------------------------------


@pytest.mark.parametrize("bench", [{"max_dispatches": 2}], indirect=True)
async def test_hitting_a_limit_asks_for_wrap_up(bench):
    """Scenario: 觸頂時要求收工"""
    ids = [(await bench.runner.dispatch(lane, f"t{lane}")).task_id for lane in "ab"]
    await bench.runner.await_tasks(ids)

    assert bench.runner.state.limit_hit is LimitKind.DISPATCHES
    assert bench.runner.state.phase is JobPhase.WRAPPING_UP
    notice = bench.runner.wrap_up_notice()
    assert notice and "收工" in notice

    (event,) = await bench.kinds(LIMIT_REACHED)
    assert event.get("limit") == LimitKind.DISPATCHES


@pytest.mark.parametrize("bench", [{"max_dispatches": 1, "wrap_up_grace": 2}], indirect=True)
async def test_wrap_up_still_allows_the_report_dispatch(bench):
    """Scenario: 觸頂時要求收工 -- 仍能派出產生報告所需的最後一次工作"""
    first = await bench.runner.dispatch("a", "分析")
    await bench.runner.await_tasks([first.task_id])
    assert bench.runner.state.wrapping_up

    report = await bench.runner.dispatch("b", "撰寫報告")
    assert report.status == "running"
    assert bench.runner.state.wrap_up_remaining == 1


@pytest.mark.parametrize("bench", [{"max_dispatches": 1, "wrap_up_grace": 1}], indirect=True)
async def test_grace_is_bounded(bench):
    """Scenario: 拒絕收工後才中止

    An unbounded "please wrap up" is the same as no limit at all.
    """
    first = await bench.runner.dispatch("a", "分析")
    await bench.runner.await_tasks([first.task_id])

    allowed = await bench.runner.dispatch("b", "報告")
    assert allowed.status == "running"
    assert bench.runner.must_abort

    refused = await bench.runner.dispatch("c", "還想再做一件事")
    assert refused.status == "aborted"
    assert refused.task_id == ""


@pytest.mark.parametrize("bench", [{"max_wall_clock_s": 0.0}], indirect=True)
async def test_wall_clock_limit_is_enforced(bench):
    first = await bench.runner.dispatch("a", "分析")
    await bench.runner.await_tasks([first.task_id])
    assert bench.runner.state.limit_hit is LimitKind.WALL_CLOCK


# --- Requirement: 無進展偵測 ----------------------------------------------


@pytest.mark.parametrize("bench", [{"no_progress_limit": 2}], indirect=True)
async def test_repeated_barren_dispatches_are_detected(bench):
    """Scenario: 連續無產出觸發升級"""
    bench.fake.default_artifact = None
    for i, lane in enumerate("abc"):
        result = await bench.runner.dispatch(lane, f"嘗試 {i}")
        await bench.runner.await_tasks([result.task_id])

    assert bench.runner.no_progress
    events = await bench.kinds(NO_PROGRESS)
    assert len(events) == 1, "report the crossing once, not on every run after"


@pytest.mark.parametrize("bench", [{"no_progress_limit": 2}], indirect=True)
async def test_a_new_finding_resets_the_streak(bench):
    """Scenario: 有產出即重置"""
    bench.fake.default_artifact = None
    barren = await bench.runner.dispatch("a", "空轉")
    await bench.runner.await_tasks([barren.task_id])
    assert bench.runner.state.no_progress_streak == 1

    bench.fake.default_artifact = "j7/note/lanes/b/findings/1"
    productive = await bench.runner.dispatch("b", "有產出")
    await bench.runner.await_tasks([productive.task_id])
    assert bench.runner.state.no_progress_streak == 0


async def test_failed_dispatches_count_as_no_progress(bench):
    bench.fake.handles = {"d1": failing(HandleStatus.TOOL_FAILURE)}
    result = await bench.runner.dispatch("a", "會失敗的任務")
    await bench.runner.await_tasks([result.task_id])
    assert bench.runner.state.no_progress_streak == 1


# --- Requirement: 向使用者提問是抽象通道 ----------------------------------


async def test_answered_question_comes_back(bench):
    """Scenario: 提問取得回覆後繼續"""
    bench.runner.channel = ScriptedChannel("資料在 /data/2023/")
    answer = await bench.runner.ask_user(Question("q1", "2023 的資料在哪？"))
    assert answer.text == "資料在 /data/2023/"
    assert not answer.defaulted


async def test_timeout_applies_the_default(bench):
    """Scenario: 逾時套用預設並記錄"""
    bench.runner.channel = QueueChannel()
    answer = await bench.runner.ask_user(
        Question("q1", "要不要納入 2023？", default="否", timeout_s=0.02)
    )
    assert answer.defaulted and answer.text == "否"
    assert "no answer" in answer.reason


@pytest.mark.parametrize("bench", [{"question_quota": 1}], indirect=True)
async def test_quota_exhaustion_stops_asking(bench):
    """Scenario: 配額耗盡後不再提問"""
    channel = ScriptedChannel("第一個回答", "第二個回答")
    bench.runner.channel = channel

    first = await bench.runner.ask_user(Question("q1", "一？", default="d1"))
    second = await bench.runner.ask_user(Question("q2", "二？", default="d2"))

    assert first.text == "第一個回答"
    assert second.defaulted and second.text == "d2"
    assert len(channel.asked) == 1, "the channel must not even see the second question"


# --- Requirement: Job 狀態可被外部查詢 ------------------------------------


async def test_status_is_bounded_regardless_of_work_done(bench):
    """Scenario: 查詢回傳有界的狀態"""
    small = len(str(bench.runner.status()))
    for i in range(12):
        result = await bench.runner.dispatch("a", f"任務 {i}")
        await bench.runner.await_tasks([result.task_id])
    large = len(str(bench.runner.status()))
    assert large < small * 2, "status must not grow with the work completed"


async def test_pending_questions_surface_in_status(bench):
    """Scenario: 待回覆的提問出現在狀態中"""
    channel = QueueChannel()
    bench.runner.channel = channel
    asking = asyncio.create_task(
        bench.runner.ask_user(Question("q1", "需要 2023 對照組嗎？", timeout_s=5))
    )
    await asyncio.sleep(0.01)

    pending = bench.runner.status()["pending_questions"]
    assert [q["id"] for q in pending] == ["q1"]

    channel.answer("q1", "需要")
    assert (await asking).text == "需要"


# --- shutdown -------------------------------------------------------------


@pytest.mark.parametrize("bench", [{"delay": 0.05}], indirect=True)
async def test_settle_collects_work_already_paid_for(bench):
    ids = [(await bench.runner.dispatch(lane, f"t{lane}")).task_id for lane in "ab"]
    handles = await bench.runner.settle(timeout=5)
    assert len(handles) == 2
    assert all(bench.runner.state.tasks[i].collected for i in ids)


@pytest.mark.parametrize("bench", [{"delay": 30.0}], indirect=True)
async def test_settle_cancels_what_it_cannot_wait_for(bench):
    """A task still running after the job ends leaks a process and keeps billing."""
    result = await bench.runner.dispatch("a", "很久的任務")
    await bench.runner.settle(timeout=0.05)
    assert bench.runner.state.tasks[result.task_id].status is TaskStatus.CANCELLED


async def test_defaulted_answer_becomes_an_unconfirmed_assumption(bench):
    """An assumption nobody confirmed must reach the final delivery as a caveat."""
    from myharness.events.query import derive_caveats

    bench.runner.channel = QueueChannel()
    await bench.runner.ask_user(
        Question("q1", "要不要納入 2023 對照組？", default="否", timeout_s=0.02)
    )
    kinds = {c.kind for c in derive_caveats(await bench.stream())}
    assert "unanswered_question" in kinds


async def test_real_answer_is_not_a_caveat(bench):
    from myharness.events.query import derive_caveats

    bench.runner.channel = ScriptedChannel("要")
    await bench.runner.ask_user(Question("q1", "要不要納入？", default="否"))
    kinds = {c.kind for c in derive_caveats(await bench.stream())}
    assert "unanswered_question" not in kinds


async def test_quota_refusal_does_not_pretend_a_question_was_asked(bench):
    from myharness.events.types import ASK_USER

    bench.runner.state.questions_asked = bench.runner.spec.question_quota
    await bench.runner.ask_user(Question("q1", "?", default="d"))
    assert await bench.kinds(ASK_USER) == []


@pytest.mark.parametrize("bench", [{"no_progress_limit": 2, "max_dispatches": 20}], indirect=True)
async def test_sustained_failure_stops_the_job_rather_than_warning_about_it(bench):
    """Advice is not a guard.

    The golden job's first run spent all fourteen dispatches on the same
    authentication error: the no-progress warning fired and the orchestrator
    ignored it. Past twice the limit the job now moves to wrap-up on its own.
    """
    bench.fake.default_artifact = None
    for i in range(4):
        result = await bench.runner.dispatch("a", f"嘗試 {i}")
        await bench.runner.await_tasks([result.task_id])

    assert bench.runner.systematically_failing
    assert bench.runner.state.limit_hit is LimitKind.NO_PROGRESS
    assert bench.runner.state.phase is JobPhase.WRAPPING_UP
    assert bench.runner.wrap_up_notice()

    (event,) = await bench.kinds(LIMIT_REACHED)
    assert event.get("limit") == LimitKind.NO_PROGRESS


@pytest.mark.parametrize("bench", [{"no_progress_limit": 2}], indirect=True)
async def test_a_productive_job_never_trips_the_futility_guard(bench):
    for i in range(6):
        bench.fake.default_artifact = f"j7/note/lanes/a/findings/{i}"
        result = await bench.runner.dispatch("a", f"任務 {i}")
        await bench.runner.await_tasks([result.task_id])
    assert not bench.runner.systematically_failing
    assert bench.runner.state.limit_hit is None
