"""Job lifecycle: the things that go wrong when a task has no owner.

A background task nobody awaits is the standard way to lose an exception in
asyncio, and "the job is simply never done" is what a client would see. Most of
these tests are about that, and about the long-poll waking on the right events.
"""

from __future__ import annotations

import asyncio

import pytest

from myharness.events.types import CTX, DISPATCH_START, JOB_FINISH
from myharness.mcp.manager import (
    MEANINGFUL,
    NOT_NEWS,
    JobManager,
    ManagerError,
    NotifyingEventLog,
    RunState,
)


class FakeLog:
    """Just enough EventLog to drive the notifier."""

    def __init__(self) -> None:
        self.appended: list[tuple[str, str]] = []

    async def append(self, job_id: str, t: str, **data: object):
        self.appended.append((job_id, t))
        return {"t": t, **data}

    async def read(self, job_id: str):
        return []


def make(manager: JobManager, job_id: str, run):
    return manager.start(job_id, runner=object(), channel=object(), run=run)


class TestLifecycle:
    async def test_start_returns_before_the_job_finishes(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow():
            started.set()
            await release.wait()
            return "done"

        async with JobManager() as m:
            handle = make(m, "j1", slow)
            await started.wait()
            assert handle.running
            release.set()
            await handle.task
            assert handle.state is RunState.FINISHED
            assert handle.outcome == "done"

    async def test_a_crash_becomes_the_final_state(self):
        """Otherwise it is an exception on a task nobody awaits: Python prints
        it at collection time and the client sees a job that never ends."""

        async def boom():
            raise RuntimeError("the loop died")

        async with JobManager() as m:
            handle = make(m, "j1", boom)
            with pytest.raises(RuntimeError):
                await handle.task
            assert handle.state is RunState.FAILED
            assert "the loop died" in handle.error
            assert not handle.running

    async def test_a_crashed_job_is_still_addressable(self):
        async def boom():
            raise ValueError("x")

        async with JobManager() as m:
            make(m, "j1", boom)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert m.get("j1") is not None
            assert m.get("j1").state is RunState.FAILED

    async def test_cancel_marks_the_job_cancelled(self):
        async def forever():
            await asyncio.Event().wait()

        async with JobManager() as m:
            make(m, "j1", forever)
            await asyncio.sleep(0)
            assert await m.cancel("j1")
            assert m.get("j1").state is RunState.CANCELLED

    async def test_cancelling_an_unknown_job_is_false_not_an_error(self):
        async with JobManager() as m:
            assert await m.cancel("nope") is False

    async def test_duplicate_job_id_is_a_programming_error(self):
        async def noop():
            return None

        async with JobManager() as m:
            make(m, "j1", noop)
            with pytest.raises(ManagerError):
                make(m, "j1", noop)

    async def test_closing_cancels_everything_still_running(self):
        ran = []

        async def forever():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                ran.append("unwound")
                raise

        m = JobManager()
        make(m, "j1", forever)
        make(m, "j2", forever)
        await asyncio.sleep(0)
        await m.aclose()
        assert ran == ["unwound", "unwound"]
        assert not m.running_ids()


class TestCapacity:
    async def test_running_ids_counts_only_running_jobs(self):
        async def quick():
            return None

        async def forever():
            await asyncio.Event().wait()

        async with JobManager(max_concurrent=2) as m:
            make(m, "done", quick)
            make(m, "live", forever)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert m.running_ids() == ["live"]

    async def test_at_capacity_reflects_the_cap(self):
        async def forever():
            await asyncio.Event().wait()

        async with JobManager(max_concurrent=1) as m:
            assert not m.at_capacity()
            make(m, "j1", forever)
            await asyncio.sleep(0)
            assert m.at_capacity()

    async def test_a_finished_job_frees_a_slot(self):
        async def quick():
            return None

        async with JobManager(max_concurrent=1) as m:
            handle = make(m, "j1", quick)
            await handle.task
            assert not m.at_capacity()


class TestWaitForChange:
    async def test_returns_as_soon_as_something_happens(self):
        async def forever():
            await asyncio.Event().wait()

        async with JobManager() as m:
            handle = make(m, "j1", forever)
            waiter = asyncio.create_task(handle.wait_for_change(30.0))
            await asyncio.sleep(0)
            handle.notify()
            assert await asyncio.wait_for(waiter, 1.0) is True

    async def test_timeout_is_false_not_an_error(self):
        async def forever():
            await asyncio.Event().wait()

        async with JobManager() as m:
            handle = make(m, "j1", forever)
            assert await handle.wait_for_change(0.05) is False

    async def test_a_finished_job_does_not_wait(self):
        async def quick():
            return None

        async with JobManager() as m:
            handle = make(m, "j1", quick)
            await handle.task
            assert await asyncio.wait_for(handle.wait_for_change(30.0), 1.0) is True

    async def test_consecutive_waits_each_need_their_own_event(self):
        """A fresh waiter blocks; it is not satisfied by an earlier change."""
        async def forever():
            await asyncio.Event().wait()

        async with JobManager() as m:
            handle = make(m, "j1", forever)
            handle.notify()
            assert await handle.wait_for_change(0.05) is False

    async def test_a_change_between_polls_is_not_lost(self):
        """The reason `since` exists.

        A client polls, gets revision 3, goes away to think, and a dispatch
        ends. Without the revision the next wait blocks for the *next* change
        and the client never hears about the one it missed.
        """
        async def forever():
            await asyncio.Event().wait()

        async with JobManager() as m:
            handle = make(m, "j1", forever)
            seen = handle.revision
            handle.notify()          # happens while nobody is waiting
            assert await handle.wait_for_change(0.05, since=seen) is True

    async def test_an_up_to_date_client_still_waits(self):
        async def forever():
            await asyncio.Event().wait()

        async with JobManager() as m:
            handle = make(m, "j1", forever)
            assert await handle.wait_for_change(0.05, since=handle.revision) is False

    async def test_revision_advances_on_every_change(self):
        async def forever():
            await asyncio.Event().wait()

        async with JobManager() as m:
            handle = make(m, "j1", forever)
            before = handle.revision
            handle.notify()
            handle.notify()
            assert handle.revision == before + 2


class TestNotifyingEventLog:
    async def test_a_meaningful_event_wakes_a_waiter(self):
        async def forever():
            await asyncio.Event().wait()

        async with JobManager() as m:
            handle = make(m, "j1", forever)
            log = NotifyingEventLog(FakeLog(), handle)
            waiter = asyncio.create_task(handle.wait_for_change(30.0))
            await asyncio.sleep(0)
            await log.append("j1", DISPATCH_START, id="d1")
            assert await asyncio.wait_for(waiter, 1.0) is True

    async def test_ctx_does_not_wake_a_waiter(self):
        """It fires every orchestrator turn; treating it as news would make a
        long-poll into a periodic empty poll (design.md D2)."""
        async def forever():
            await asyncio.Event().wait()

        async with JobManager() as m:
            handle = make(m, "j1", forever)
            log = NotifyingEventLog(FakeLog(), handle)
            waiter = asyncio.create_task(handle.wait_for_change(0.2))
            await asyncio.sleep(0)
            await log.append("j1", CTX, who="orchestrator", used=1)
            assert await asyncio.wait_for(waiter, 1.0) is False

    async def test_the_event_still_reaches_the_underlying_log(self):
        async def forever():
            await asyncio.Event().wait()

        async with JobManager() as m:
            handle = make(m, "j1", forever)
            inner = FakeLog()
            log = NotifyingEventLog(inner, handle)
            await log.append("j1", CTX, used=1)
            await log.append("j1", JOB_FINISH)
            assert inner.appended == [("j1", CTX), ("j1", JOB_FINISH)]

    async def test_reads_pass_through(self):
        async def forever():
            await asyncio.Event().wait()

        async with JobManager() as m:
            handle = make(m, "j1", forever)
            assert await NotifyingEventLog(FakeLog(), handle).read("j1") == []


def test_the_two_event_sets_do_not_overlap():
    assert not (MEANINGFUL & NOT_NEWS)


def test_every_meaningful_kind_is_a_real_event_kind():
    """A typo here fails as silence -- the wait just never wakes."""
    from myharness.events import types

    known = {v for k, v in vars(types).items()
             if k.isupper() and isinstance(v, str) and not k.startswith("STATUS_")}
    assert MEANINGFUL <= known, MEANINGFUL - known
