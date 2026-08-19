"""The six operations behind the MCP tools, without any MCP in them.

Keeping the logic here and the protocol in ``server.py`` means every rule -- the
bounds, the "not running" versus "no such job" distinction, the refusals -- is
testable by calling a method, with no stdio session in the way.

Every method returns a dict. Failures are values, exactly as they are for lane
and orchestrator tools (design.md D5): the client is an agent, and an
explanation it can act on beats a stack trace that costs it a turn.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from myharness.artifacts.ids import ArtifactId
from myharness.artifacts.local import LocalArtifactStore
from myharness.artifacts.store import ArtifactStore
from myharness.events.log import EventLog, LocalEventLog
from myharness.events.query import summarize
from myharness.events.types import INGRESS
from myharness.jobs.channel import QueueChannel
from myharness.jobs.runner import JobRunner
from myharness.jobs.spec import JobSpec
from myharness.lanes.types import LaneRegistry
from myharness.local_layout import find_jobs
from myharness.mcp.manager import JobHandle, JobManager, NotifyingEventLog, RunState
from myharness.mcp.payload import (
    MAX_SECTION_TOKENS,
    bound_section,
    build_progress,
    build_result,
)
from myharness.orchestrator.delivery import build_delivery, drill
from myharness.orchestrator.loop import OrchestratorLoop

#: Long-poll ceiling. In-process tool calls blocking 180s and 600s both passed
#: (DESIGN §8 Q5) and MCP_TOOL_TIMEOUT defaults to ~27.8h, so this sits well
#: inside what is known to work.
MAX_POLL_WAIT_S = 300.0
DEFAULT_POLL_WAIT_S = 30.0

#: Events the progress payload draws its "recent" lines from.
_RECENT_WINDOW = 40


def _err(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": code, "message": message, **extra}


def _new_job_id() -> str:
    return f"job{uuid.uuid4().hex[:10]}"


class AnalysisService:
    """One root directory, one lane registry, many jobs."""

    def __init__(
        self,
        root: Path | str,
        *,
        lanes: LaneRegistry,
        backend: str = "openrouter",
        manager: JobManager | None = None,
        store: ArtifactStore | None = None,
        event_log: EventLog | None = None,
        loop_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._root = Path(root)
        self._lanes = lanes
        self._backend = backend
        self._manager = manager or JobManager()
        self._store = store or LocalArtifactStore(self._root)
        self._events = event_log or LocalEventLog(self._root)
        self._loop_factory = loop_factory or OrchestratorLoop

    @property
    def manager(self) -> JobManager:
        return self._manager

    async def aclose(self) -> None:
        await self._manager.aclose()

    # ---- start -----------------------------------------------------------

    async def start(
        self, task: str, *, job_id: str | None = None, **spec_overrides: Any
    ) -> dict[str, Any]:
        task = (task or "").strip()
        if not task:
            return _err("empty_task", "task must describe what to analyse")
        if self._manager.at_capacity():
            return _err(
                "at_capacity",
                f"{len(self._manager.running_ids())} analyses are already running "
                f"and the limit is {self._manager.max_concurrent}. Wait for one to "
                "finish, or poll the ones in flight.",
                running=self._manager.running_ids(),
                limit=self._manager.max_concurrent,
            )
        job_id = job_id or _new_job_id()
        if self._manager.get(job_id) is not None:
            return _err("duplicate_job", f"{job_id} already exists")

        await self._store.init_job(job_id)
        channel = QueueChannel()
        spec = JobSpec(job_id=job_id, goal=task, **spec_overrides)
        runner = JobRunner(
            spec, store=self._store, event_log=self._events, channel=channel
        )
        handle = self._manager.register(job_id, runner=runner, channel=channel)
        # Wired before launch: the runner writes through a log that wakes *this*
        # handle's waiters, so a long-poll returns on real progress rather than
        # on a timer (design.md D2).
        runner.events = NotifyingEventLog(self._events, handle)
        loop = self._loop_factory(
            runner=runner, lanes=self._lanes, backend=self._backend
        )
        self._manager.launch(handle, loop.run)
        return {"ok": True, "job_id": job_id, "state": str(handle.state),
                "revision": handle.revision}

    # ---- poll ------------------------------------------------------------

    async def poll(
        self, job_id: str, *, wait: float = DEFAULT_POLL_WAIT_S, since: int | None = None
    ) -> dict[str, Any]:
        handle = self._manager.get(job_id)
        if handle is None:
            return await self._poll_finished_elsewhere(job_id)
        wait = max(0.0, min(float(wait), MAX_POLL_WAIT_S))
        await handle.wait_for_change(wait, since=since)
        events = await self._events.read(job_id)
        progress = build_progress(
            job_id=job_id,
            state=str(handle.state),
            revision=handle.revision,
            status=handle.runner.status(),
            recent_events=events[-_RECENT_WINDOW:],
            note=handle.error,
        )
        return {"ok": True, **progress.to_dict()}

    async def _poll_finished_elsewhere(self, job_id: str) -> dict[str, Any]:
        """A job this process did not run is not the same as no job at all.

        The two need different handling by the client -- one can still be read,
        the other is a mistake -- so they must not share an error (design.md D4).
        """
        if job_id not in self._known_job_ids():
            return _err("no_such_job", f"no analysis with id {job_id}")
        return _err(
            "not_running",
            f"{job_id} is not running in this process. Its result is still "
            "available: call analysis_result.",
            job_id=job_id,
        )

    def _known_job_ids(self) -> set[str]:
        """Every job this service can say anything about.

        On-disk jobs plus the ones running here. Disk alone is not enough: a
        job that has just started may not have written an event yet, and
        answering "no such job" for something we are actively running is worse
        than any other wrong answer this layer can give.
        """
        return {j.job_id for j in find_jobs(self._root)} | set(self._manager.ids())

    # ---- provide ---------------------------------------------------------

    async def provide(
        self, job_id: str, payload: str, *, name: str = "", schema: dict | None = None
    ) -> dict[str, Any]:
        if job_id not in self._known_job_ids() and self._manager.get(job_id) is None:
            return _err("no_such_job", f"no analysis with id {job_id}")
        if not payload:
            return _err("empty_payload", "payload must not be empty")
        leaf = (name or f"provided-{uuid.uuid4().hex[:8]}").strip()
        try:
            ArtifactId(job_id=job_id, kind="blob", name=f"raw/{leaf}")
        except ValueError as exc:
            return _err("bad_name", str(exc))
        meta = await self._store.put_blob(
            job_id, f"raw/{leaf}", data=payload.encode("utf-8"),
            produced_by="client", schema=schema,
        )
        handle = self._manager.get(job_id)
        log = handle.runner.events if handle else self._events
        await log.append(
            job_id, INGRESS, payload=str(meta.id), bytes=meta.bytes, routed=False,
        )
        # An event in the log is not an announcement -- nothing in the
        # orchestrator reads the log. Without this the payload is stored
        # somewhere nobody will look, and the client is told otherwise.
        announced = False
        if handle is not None and handle.running:
            handle.runner.notify(
                f"【系統】使用者提供了新資料：{meta.id}（{meta.bytes:,} bytes"
                + (f"，schema {schema}" if schema else "")
                + "）。尚未路由到任何 lane —— 由你決定要不要用、給誰用。"
                " 記得在 dispatch 的 inputs 帶上這個 id，否則 lane 讀不到。"
            )
            handle.notify()
            announced = True
        return {
            "ok": True,
            "artifact": str(meta.id),
            "bytes": meta.bytes,
            # Both stated: a client that assumes its data reached a lane -- or
            # reached anyone -- will not understand the report it gets.
            "routed": False,
            "announced": announced,
            "note": (
                "stored as a blob and announced to the orchestrator, which "
                "decides whether to use it. Not routed to a lane -- no proxy "
                "exists yet."
                if announced else
                "stored as a blob, but this analysis is not running here, so "
                "nothing was announced. Only the stored artifact remains."
            ),
        }

    # ---- answer ----------------------------------------------------------

    async def answer(self, job_id: str, question_id: str, text: str) -> dict[str, Any]:
        handle = self._manager.get(job_id)
        if handle is None:
            return await self._poll_finished_elsewhere(job_id)
        if not handle.running:
            return _err("not_running", f"{job_id} has already ended ({handle.state})")
        if not handle.channel.answer(question_id, text):
            pending = [q.id for q in handle.channel.pending()]
            return _err(
                "unknown_question",
                f"no pending question with id {question_id}",
                pending=pending,
            )
        handle.notify()
        return {"ok": True, "question_id": question_id}

    # ---- result ----------------------------------------------------------

    async def result(self, job_id: str) -> dict[str, Any]:
        """Reads only the event log and the store, so it answers for jobs this
        process never ran (design.md D4)."""
        if job_id not in self._known_job_ids():
            return _err("no_such_job", f"no analysis with id {job_id}")
        events = await self._events.read(job_id)
        handle = self._manager.get(job_id)
        report = _report_artifact(events)
        # Order matters: "still running" is the useful answer for a job that has
        # not written anything yet, and "no events" would send the client
        # looking for a problem that is really just elapsed time.
        if report is None and handle is not None and handle.running:
            return _err(
                "not_finished",
                f"{job_id} is still running and has produced no report yet.",
                state=str(handle.state),
                dispatches=summarize(events).dispatches if events else 0,
            )
        if not events:
            return _err("no_events", f"{job_id} has no recorded events")
        delivery = await build_delivery(
            store=self._store, events=events, job_id=job_id,
            status=_phase(events, handle), report_artifact=report,
        )
        return {"ok": True, **build_result(delivery.to_dict()).to_dict()}

    # ---- drill -----------------------------------------------------------

    async def drill_section(
        self, job_id: str, section_id: str, *, max_tokens: int = MAX_SECTION_TOKENS
    ) -> dict[str, Any]:
        if job_id not in self._known_job_ids():
            return _err("no_such_job", f"no analysis with id {job_id}")
        events = await self._events.read(job_id)
        report = _report_artifact(events)
        if report is None:
            return _err("no_report", f"{job_id} has no report to drill into")
        try:
            text = await drill(self._store, job_id, report, section_id,
                               max_tokens=max_tokens * 4)
        except Exception as exc:  # noqa: BLE001 - a bad section id is a value
            return _err(
                "no_such_section", f"{type(exc).__name__}: {exc}",
                hint="call analysis_result for the list of section ids",
            )
        bounded, truncated = bound_section(text, max_tokens=max_tokens)
        return {"ok": True, "section_id": section_id, "text": bounded,
                "truncated": truncated}


def _report_artifact(events: Any) -> str | None:
    for event in reversed(list(events)):
        if event.t == "job.finish":
            report = event.data.get("report")
            if report:
                return str(report)
    return None


def _phase(events: Any, handle: JobHandle | None) -> str:
    if handle is not None and handle.state is not RunState.FINISHED:
        return str(handle.state)
    for event in reversed(list(events)):
        if event.t == "job.finish":
            return str(event.data.get("phase", "complete"))
    return "unknown"


__all__ = ["DEFAULT_POLL_WAIT_S", "MAX_POLL_WAIT_S", "AnalysisService"]
