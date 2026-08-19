"""Jobs that outlive the call that started them.

``analysis_start`` has to return before the analysis finishes -- otherwise a
client sits inside one tool call for half an hour, unable to see progress,
answer a question, or hand over more data (DESIGN #2). So a job runs as a
background ``asyncio.Task`` addressed by its id.

Three consequences, all handled here rather than discovered later:

* An un-awaited task's exception is swallowed until garbage collection prints a
  warning nobody reads. The final state is written by a done-callback, so it
  exists whether or not anyone asks.
* Every job starts several lanes and every lane is an SDK subprocess. Without a
  concurrency cap there is no cap.
* Tasks are in memory. What survives a restart is the event log, which is why
  results are a projection of it and not of anything in here (design.md D4).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from myharness.events.log import EventLog
from myharness.events.types import (
    ASK_ANSWER,
    ASK_USER,
    CTX,
    DISPATCH_END,
    DISPATCH_START,
    HANDOFF_RESTART,
    INGRESS,
    JOB_FINISH,
    JOB_START,
    LIMIT_REACHED,
    PLAN_UPDATE,
)
from myharness.jobs.channel import QueueChannel
from myharness.jobs.runner import JobRunner
from myharness.orchestrator.loop import LoopOutcome

#: Each job runs several lanes, each an SDK subprocess. Four jobs is already a
#: lot of concurrent model spend for one machine.
DEFAULT_MAX_CONCURRENT_JOBS = 4

#: Event kinds that mean "something a client would want to know changed".
#: Named by constant rather than by literal -- my first pass wrote "limit" for
#: what is actually "limit.reached", and a wrong string here fails as silence.
MEANINGFUL: frozenset[str] = frozenset({
    JOB_START, JOB_FINISH, PLAN_UPDATE, INGRESS,
    DISPATCH_START, DISPATCH_END, ASK_USER, ASK_ANSWER,
    LIMIT_REACHED, HANDOFF_RESTART,
})

#: Deliberately excluded. ``ctx`` fires every orchestrator turn, so treating it
#: as news would turn a 30-second wait into a periodic empty poll (design.md
#: D2). Named here so the omission reads as a decision, not an oversight.
NOT_NEWS: frozenset[str] = frozenset({CTX})


class RunState(StrEnum):
    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ManagerError(RuntimeError):
    """Raised only for programming errors; client-facing refusals are values."""


@dataclass
class JobHandle:
    """One job, its runner, and the way to wait for it to do something."""

    job_id: str
    runner: JobRunner
    channel: QueueChannel
    task: asyncio.Task[LoopOutcome] | None = None
    state: RunState = RunState.RUNNING
    outcome: LoopOutcome | None = None
    error: str = ""

    #: Bumped on every meaningful event. A client that reports the revision it
    #: last saw can be told immediately that it has already fallen behind --
    #: without it, a change landing between two polls is simply lost, because
    #: the next wait clears the flag before blocking.
    revision: int = 0
    _changed: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def running(self) -> bool:
        return self.state is RunState.RUNNING

    def notify(self) -> None:
        """Record a change and wake everyone waiting on it."""
        self.revision += 1
        self._changed.set()
        # Replace rather than clear: everyone blocked on the old event has
        # already been woken, and a new waiter must block on a fresh one.
        self._changed = asyncio.Event()

    async def wait_for_change(self, timeout: float, *, since: int | None = None) -> bool:
        """``True`` if something happened, ``False`` on timeout.

        A timeout is not an error -- it means "still running, nothing new"
        (design.md D2), and the caller decides whether to ask again.

        ``since`` is the revision the caller last saw. If the job has moved on
        already, this returns immediately rather than waiting for the *next*
        change, which would hide the one that just happened.
        """
        if not self.running:
            return True
        if since is not None and since != self.revision:
            return True
        changed = self._changed
        try:
            await asyncio.wait_for(changed.wait(), timeout)
        except TimeoutError:
            return False
        return True


class JobManager:
    """Owns every running job in this process."""

    def __init__(self, *, max_concurrent: int = DEFAULT_MAX_CONCURRENT_JOBS) -> None:
        self._jobs: dict[str, JobHandle] = {}
        self._max_concurrent = max_concurrent

    # ---- inspection ------------------------------------------------------

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    def get(self, job_id: str) -> JobHandle | None:
        return self._jobs.get(job_id)

    def ids(self) -> list[str]:
        """Every job this manager knows, running or not."""
        return list(self._jobs)

    def running_ids(self) -> list[str]:
        return [j for j, h in self._jobs.items() if h.running]

    def at_capacity(self) -> bool:
        return len(self.running_ids()) >= self._max_concurrent

    # ---- lifecycle -------------------------------------------------------

    def register(
        self, job_id: str, *, runner: JobRunner, channel: QueueChannel
    ) -> JobHandle:
        """Create the handle without starting anything.

        Two phases because the caller has to wire the notifying event log to
        *this* handle before the job produces its first event. A single
        ``start`` that built the handle internally meant the caller was
        attaching the notifier to a different object than the one being run.
        """
        if job_id in self._jobs:
            raise ManagerError(f"job {job_id} already exists")
        handle = JobHandle(job_id=job_id, runner=runner, channel=channel)
        self._jobs[job_id] = handle
        return handle

    def launch(
        self, handle: JobHandle, run: Callable[[], Awaitable[LoopOutcome]]
    ) -> JobHandle:
        """Set a registered job running in the background."""
        if self._jobs.get(handle.job_id) is not handle:
            raise ManagerError(f"{handle.job_id} was not registered here")
        if handle.task is not None:
            raise ManagerError(f"{handle.job_id} is already running")
        handle.task = asyncio.create_task(run(), name=f"job:{handle.job_id}")
        handle.task.add_done_callback(lambda t: self._settle(handle, t))
        return handle

    def start(
        self,
        job_id: str,
        *,
        runner: JobRunner,
        channel: QueueChannel,
        run: Callable[[], Awaitable[LoopOutcome]],
    ) -> JobHandle:
        """Register and launch in one step, for callers with nothing to wire."""
        return self.launch(self.register(job_id, runner=runner, channel=channel), run)

    def _settle(self, handle: JobHandle, task: asyncio.Task[LoopOutcome]) -> None:
        """Record how the job ended, including ways nobody asked about.

        Without this, a crash in the loop is an exception on a task no one
        awaits: Python prints it at collection time and the client sees a job
        that is simply never done.

        Idempotent, because it runs both from the task's done-callback (which
        the event loop schedules, so it may not have run yet) and from
        ``aclose``, which must not return with a job still marked running.
        """
        if not handle.running:
            return
        if task.cancelled():
            handle.state = RunState.CANCELLED
            handle.error = "cancelled"
        else:
            exc = task.exception()
            if exc is not None:
                handle.state = RunState.FAILED
                handle.error = f"{type(exc).__name__}: {exc}"
            else:
                handle.state = RunState.FINISHED
                handle.outcome = task.result()
        handle.notify()

    async def cancel(self, job_id: str) -> bool:
        handle = self._jobs.get(job_id)
        if handle is None or handle.task is None or not handle.running:
            return False
        handle.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await handle.task
        return True

    async def aclose(self) -> None:
        """Cancel every running job and wait for it to unwind."""
        pending = [(h, h.task) for h in self._jobs.values() if h.task and h.running]
        for _, task in pending:
            task.cancel()
        for handle, task in pending:
            # Shutting down: how each job failed is already recorded by
            # _settle, so nothing here needs to propagate.
            with contextlib.suppress(BaseException):
                await task
            # The done-callback is scheduled, not immediate, so settle here
            # too rather than return with a job still marked running.
            self._settle(handle, task)

    async def __aenter__(self) -> JobManager:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


class NotifyingEventLog(EventLog):
    """Wraps an event log so meaningful events wake the job's waiters.

    A decorator rather than a hook inside ``EventLog`` because the event log is
    the system's foundation and should not know that anything is watching it.
    """

    def __init__(self, inner: EventLog, handle: JobHandle) -> None:
        self._inner = inner
        self._handle = handle

    async def append(self, job_id: str, t: str, **data: Any) -> Any:
        event = await self._inner.append(job_id, t, **data)
        if t in MEANINGFUL:
            self._handle.notify()
        return event

    async def read(self, job_id: str) -> Any:
        return await self._inner.read(job_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


__all__ = [
    "DEFAULT_MAX_CONCURRENT_JOBS",
    "MEANINGFUL",
    "NOT_NEWS",
    "JobHandle",
    "JobManager",
    "ManagerError",
    "NotifyingEventLog",
    "RunState",
]
