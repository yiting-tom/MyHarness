"""The job runner: non-blocking dispatch, blocking collection, and the guards.

``dispatch`` starts a background task and returns in milliseconds; ``await_tasks``
blocks once for the results. The split exists because an LLM has no ``await``:
if dispatch blocked, parallelism would depend on whether the backend runs
same-turn tool calls concurrently -- it only does so for read-only tools -- and
if collection polled, every empty check would cost a full orchestrator turn
(design.md D1).

Three guards, all pure code and all free: duplicate detection, hard limits, and
a no-progress counter. Between them they catch the ways an LLM burns a budget
without noticing (design.md D3).
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from myharness.artifacts.store import ArtifactStore
from myharness.events.log import EventLog
from myharness.events.query import total_cost_usd
from myharness.events.types import (
    ASK_ANSWER,
    ASK_USER,
    LIMIT_REACHED,
    NO_PROGRESS,
    STATUS_DUPLICATE,
)
from myharness.jobs.channel import Answer, DefaultingChannel, Question, UserChannel
from myharness.jobs.spec import JobPhase, JobSpec, LimitKind
from myharness.jobs.state import JobState, TaskRecord, TaskStatus
from myharness.lanes.handle import LaneHandle
from myharness.lanes.types import LaneInstance
from myharness.lanes.worker import WorkerRequest, run_lane_worker

#: What a worker call looks like, so tests can substitute one.
LaneRunner = Callable[..., Awaitable[LaneHandle]]


def fingerprint(lane: str, task: str, inputs: Sequence[str]) -> str:
    """Identity of a dispatch, for duplicate detection.

    Literal rather than semantic: two differently-worded requests for the same
    thing get through. That is the right way round -- blocking a legitimate
    re-dispatch is far worse than letting a near-duplicate run.
    """
    payload = "\x00".join([lane, task.strip(), *sorted(inputs)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """What ``dispatch`` hands back immediately."""

    task_id: str
    lane: str
    status: str = "running"
    duplicate_of: str | None = None
    handle: LaneHandle | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"task_id": self.task_id, "lane": self.lane,
                               "status": self.status}
        if self.duplicate_of:
            out["duplicate_of"] = self.duplicate_of
        if self.handle is not None:
            out["handle"] = self.handle.to_dict()
        if self.note:
            out["note"] = self.note
        return out


@dataclass(frozen=True, slots=True)
class CollectResult:
    """What ``await_tasks`` hands back: what finished, and what did not."""

    handles: tuple[LaneHandle, ...]
    still_running: tuple[str, ...]
    timed_out: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "handles": [h.to_dict() for h in self.handles],
            "still_running": list(self.still_running),
            "timed_out": self.timed_out,
        }


class LimitReached(Exception):
    """Raised inside the runner only; never surfaces to the orchestrator."""


class JobRunner:
    """Owns one job: its tasks, its budgets, and its guards."""

    def __init__(
        self,
        spec: JobSpec,
        *,
        store: ArtifactStore,
        event_log: EventLog,
        lanes: dict[str, LaneInstance] | None = None,
        channel: UserChannel | None = None,
        lane_runner: LaneRunner | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.spec = spec
        self.state = JobState(spec)
        self.store = store
        self.events = event_log
        self.lanes: dict[str, LaneInstance] = dict(lanes or {})
        self.channel = channel or DefaultingChannel()
        self._run_lane = lane_runner or run_lane_worker
        self._clock = clock
        self._started = clock()
        self._tasks: dict[str, asyncio.Task[LaneHandle]] = {}
        self._seq = 0
        self._semaphore = asyncio.Semaphore(spec.max_lane_concurrency)
        #: Things that happened outside the orchestrator's turn and that it has
        #: to be told about -- data arriving mid-run, for one. Without a way in,
        #: such an event is recorded in the log and read by nobody.
        self._notices: list[str] = []

    # ---- lanes -----------------------------------------------------------

    def register_lane(self, lane: LaneInstance) -> None:
        self.lanes[lane.id] = lane

    # ---- limits ----------------------------------------------------------

    def elapsed_s(self) -> float:
        return self._clock() - self._started

    def _limit_breached(self) -> LimitKind | None:
        if self.state.dispatches >= self.spec.max_dispatches:
            return LimitKind.DISPATCHES
        if (self.spec.max_budget_usd is not None
                and self.state.spent_usd >= self.spec.max_budget_usd):
            return LimitKind.BUDGET_USD
        if self.elapsed_s() >= self.spec.max_wall_clock_s:
            return LimitKind.WALL_CLOCK
        return None

    async def check_limits(self) -> LimitKind | None:
        """Move the job into wrap-up the first time any ceiling is reached."""
        if self.systematically_failing and self.state.limit_hit is None:
            self.state.limit_hit = LimitKind.NO_PROGRESS
            self.state.phase = JobPhase.WRAPPING_UP
            await self.events.append(
                self.spec.job_id, LIMIT_REACHED, limit=str(LimitKind.NO_PROGRESS),
                value=float(self.state.no_progress_streak),
                dispatches=self.state.dispatches,
            )
            return LimitKind.NO_PROGRESS
        breached = self._limit_breached()
        if breached is None or self.state.limit_hit is not None:
            return self.state.limit_hit
        self.state.limit_hit = breached
        self.state.phase = JobPhase.WRAPPING_UP
        await self.events.append(
            self.spec.job_id, LIMIT_REACHED, limit=str(breached),
            value=self._limit_value(breached), dispatches=self.state.dispatches,
        )
        return breached

    def _limit_value(self, kind: LimitKind) -> float:
        return {
            LimitKind.DISPATCHES: float(self.state.dispatches),
            LimitKind.BUDGET_USD: round(self.state.spent_usd, 4),
            LimitKind.WALL_CLOCK: round(self.elapsed_s(), 1),
            LimitKind.NO_PROGRESS: float(self.state.no_progress_streak),
        }[kind]

    def notify(self, text: str) -> None:
        """Queue something for the orchestrator's next turn."""
        self._notices.append(text)

    def take_notices(self) -> list[str]:
        """Drain the queue. Delivered once; the event log is the record."""
        notices, self._notices = self._notices, []
        return notices

    def wrap_up_notice(self) -> str | None:
        """The message injected into the orchestrator when a ceiling is hit."""
        if not self.state.wrapping_up:
            return None
        return (
            f"【系統】此 job 已觸及 {self.state.limit_hit} 上限。"
            f"請立即以現有產出收工：必要時再派最多 {self.state.wrap_up_remaining} 次工作"
            f"產生報告，然後呼叫 finish。"
        )

    @property
    def must_abort(self) -> bool:
        """Grace is bounded; an orchestrator that ignores wrap-up gets cut off."""
        return self.state.wrapping_up and self.state.wrap_up_remaining <= 0

    # ---- dispatch --------------------------------------------------------

    async def dispatch(
        self, lane_id: str, task: str, inputs: Sequence[str] = ()
    ) -> DispatchResult:
        if lane_id not in self.lanes:
            return DispatchResult(
                task_id="", lane=lane_id, status="unknown_lane",
                note=f"未建立的 lane；已建立：{sorted(self.lanes) or '(無)'}",
            )

        fp = fingerprint(lane_id, task, inputs)
        if (previous_id := self.state.by_fingerprint.get(fp)) is not None:
            previous = self.state.tasks[previous_id]
            return DispatchResult(
                task_id=previous_id, lane=lane_id, status=STATUS_DUPLICATE,
                duplicate_of=previous_id, handle=previous.handle,
                note="你已經派過這項工作，結果在 handle 裡",
            )

        if self.must_abort:
            return DispatchResult(task_id="", lane=lane_id, status="aborted",
                                  note="寬限已用盡，job 即將中止")

        if self.state.wrapping_up:
            self.state.wrap_up_used += 1

        self._seq += 1
        task_id = f"d{self._seq}"
        record = TaskRecord(id=task_id, lane=lane_id, task=task,
                            inputs=tuple(inputs), fingerprint=fp)
        self.state.tasks[task_id] = record
        self.state.by_fingerprint[fp] = task_id
        self.state.dispatches += 1
        if self.state.phase is JobPhase.PLANNING:
            self.state.phase = JobPhase.RUNNING

        self._tasks[task_id] = asyncio.create_task(self._execute(record))
        return DispatchResult(task_id=task_id, lane=lane_id)

    async def _execute(self, record: TaskRecord) -> LaneHandle:
        async with self._semaphore:
            request = WorkerRequest(
                job_id=self.spec.job_id, lane=self.lanes[record.lane],
                task=record.task, dispatch_id=record.id, inputs=record.inputs,
            )
            handle = await self._run_lane(
                request, store=self.store, event_log=self.events
            )
        record.handle = handle
        record.status = TaskStatus.DONE
        await self._refresh_cost()
        return handle

    async def _refresh_cost(self) -> None:
        """Cost comes from the event log, not from a second tally.

        The stream already records what every dispatch and proxy call spent;
        keeping a parallel counter here would be a second source of truth that
        can disagree with the first.
        """
        self.state.spent_usd = total_cost_usd(await self.events.read(self.spec.job_id))

    # ---- collection ------------------------------------------------------

    async def await_tasks(
        self, task_ids: Sequence[str], *, mode: str = "all", timeout: float = 600.0
    ) -> CollectResult:
        wanted = [tid for tid in task_ids if tid in self._tasks]
        pending = {self._tasks[tid] for tid in wanted if not self._tasks[tid].done()}

        timed_out = False
        if pending:
            return_when = (
                asyncio.FIRST_COMPLETED if mode == "any" else asyncio.ALL_COMPLETED
            )
            _done, still = await asyncio.wait(pending, timeout=timeout,
                                             return_when=return_when)
            timed_out = bool(still) and mode != "any"

        handles: list[LaneHandle] = []
        running: list[str] = []
        for tid in wanted:
            record = self.state.tasks[tid]
            if self._tasks[tid].done() and record.handle is not None:
                record.collected = True
                handles.append(record.handle)
                await self._note_progress(record)
            else:
                running.append(tid)

        await self.check_limits()
        return CollectResult(tuple(handles), tuple(running), timed_out)

    async def _note_progress(self, record: TaskRecord) -> None:
        handle = record.handle
        artifact = handle.artifact if handle and handle.ok else None
        self.state.record_progress(artifact)
        # Emit once, on the run that crosses the threshold, not on every one after.
        if self.state.no_progress_streak == self.spec.no_progress_limit:
            await self.events.append(
                self.spec.job_id, NO_PROGRESS,
                streak=self.state.no_progress_streak, lane=record.lane,
            )

    @property
    def no_progress(self) -> bool:
        return self.state.no_progress_streak >= self.spec.no_progress_limit

    @property
    def systematically_failing(self) -> bool:
        """Far past "no progress": something is broken, not merely unproductive.

        The golden job's first run spent all fourteen dispatches on the same
        authentication error while the no-progress warning was advisory and the
        orchestrator kept going. Advice is not a guard.
        """
        return self.state.no_progress_streak >= self.spec.no_progress_limit * 2

    # ---- questions -------------------------------------------------------

    async def ask_user(self, question: Question) -> Answer:
        """Ask, and always come back with something.

        Both halves are logged: an ask with no matching answer is what turns
        into an "unconfirmed assumption" caveat, so a defaulted reply must not
        look like a real one in the stream.
        """
        if self.state.questions_remaining <= 0:
            answer = Answer(question.id, question.default, defaulted=True,
                            reason="job 的提問配額已用盡")
        else:
            self.state.questions_asked += 1
            await self.events.append(
                self.spec.job_id, ASK_USER, qid=question.id, text=question.text,
                kind=str(question.kind), default=question.default,
            )
            answer = await self.channel.ask(question)

        if not answer.defaulted:
            await self.events.append(
                self.spec.job_id, ASK_ANSWER, qid=question.id, text=answer.text,
            )
        return answer

    # ---- shutdown --------------------------------------------------------

    async def settle(self, *, timeout: float = 300.0) -> Sequence[LaneHandle]:
        """Collect whatever is still in flight before the job ends.

        Work already paid for should not be thrown away, and a task left running
        after the job ends leaks a process and keeps billing.
        """
        outstanding = list(self.state.running())
        if outstanding:
            await self.await_tasks([t.id for t in outstanding], timeout=timeout)
        for task_id, task in self._tasks.items():
            if not task.done():
                task.cancel()
                self.state.tasks[task_id].status = TaskStatus.CANCELLED
        return tuple(
            t.handle for t in self.state.tasks.values() if t.handle is not None
        )

    def status(self) -> dict[str, Any]:
        """Bounded status, whatever the job has done so far."""
        payload = self.state.to_dict()
        payload["elapsed_s"] = round(self.elapsed_s(), 1)
        payload["goal"] = self.spec.goal[:200]
        channel = self.channel
        payload["pending_questions"] = [
            q.to_dict() for q in getattr(channel, "pending", lambda: [])()
        ]
        return payload
