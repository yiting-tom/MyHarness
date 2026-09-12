"""The A2A executor: one shell over the same `AnalysisService`.

`myharness/mcp/server.py` says everything above it is protocol-free, and that is
what makes a second boundary cheap: this module is the same width as that one.
It routes an A2A request onto `result()` and `drill_section()` and does not
touch the service (expose-over-a2a D3). No `mode=` parameter went into
`AnalysisService` -- the difference between the two skills lives here, because
the moment the service knows there are two protocols, the layering that made
this possible is gone.

Starting an analysis does not block. A2A has a word for that -- a client sets
`configuration.return_immediately` and the framework hands back the task while
the executor keeps running -- so non-blocking start is the protocol's own
mechanism rather than something bolted on. A client that does not ask for it
gets the finished task, which is also a reasonable thing to want.

Progress events carry `revision` in their metadata, because reconnecting to an
A2A stream does not replay: `SubscribeToTaskRequest` carries only `id` and
`tenant`, so a new stream starts from now and whatever happened while the client
was away is simply gone (spike #22). The cursor is what closes that -- a client
that comes back with the revision it last saw can be told it is behind, and
`Task.history` is where it catches up.
"""

from __future__ import annotations

import json
from typing import Any

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, Task, TaskState, TaskStatus

from myharness.a2a.card import PRICE_LIST_EXTENSION, SKILL_FULL_TEXT
from myharness.mcp.service import AnalysisService

#: What a price-list artifact says about itself. The section ids alone would be
#: a list of names with prices and no way to spend them, and the change's spec
#: 5.2 asks for the price list to carry its own instructions.
PRICE_LIST_GUIDANCE = (
    "這是章節價目表，不是章節內容。每一節的 est_tokens 是把它讀進 context 要花的"
    f"token。要全文請用 skill `{SKILL_FULL_TEXT}` 逐節取，指名 section id。"
)

#: How long each progress poll waits for the job to do something. Long enough
#: that a quiet job does not produce a stream of identical updates, short enough
#: that a cancelled request is noticed. `AnalysisService.poll` clamps it anyway.
PROGRESS_WAIT_S = 20.0


class AnalysisExecutor(AgentExecutor):
    """Reads a finished analysis out to an A2A caller."""

    def __init__(self, service: AnalysisService) -> None:
        self._service = service

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        request = _read_request(context)
        job_id = request.get("job_id")
        goal = request.get("task") or request.get("goal")

        # Started before the task is announced, not after. A client that asked
        # for `return_immediately` gets the task id the moment the Task event
        # goes out, and anything it does with that id -- providing data, polling
        # -- would otherwise race the job into existence and be told there is no
        # such job.
        started = None
        if not job_id and goal:
            started = await self._service.start(str(goal), job_id=context.task_id)

        # The task itself goes on the queue before any update to it. The
        # framework refuses a status event for a task it has never seen --
        # "Agent should enqueue Task before TaskStatusUpdateEvent event" -- and
        # a first request has no current_task to inherit.
        if context.current_task is None:
            await event_queue.enqueue_event(Task(
                id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
            ))
        await updater.start_work()

        if job_id:
            section_id = request.get("section_id")
            if section_id:
                await self._answer_section(updater, str(job_id), str(section_id))
            elif request.get("sections") or request.get("full_text"):
                await self._answer_every_section(updater, str(job_id))
            else:
                await self._answer_price_list(updater, str(job_id))
            return

        if started is not None:
            # The A2A task id becomes the job id, so every later GetTask,
            # SubscribeToTask and result read addresses the same thing by the
            # same name -- including from a process that never ran it.
            await self._watch(updater, started, job_id=context.task_id)
            return

        await _refuse(updater, "empty_request",
                      "請指名 job_id 來讀一份已完成的分析，"
                      "或給 task 來啟動一個新的。")

    async def _watch(
        self, updater: TaskUpdater, started: dict[str, Any], *, job_id: str
    ) -> None:
        if not started.get("ok"):
            # at_capacity carries its own limit and running count, and the
            # refusal keeps them: "too many" without the numbers is not
            # actionable (change spec 7.2).
            await _refuse(updater, _code(started),
                          started.get("message", ""),
                          **{k: v for k, v in started.items()
                             if k not in ("ok", "error", "message")})
            return

        revision = int(started.get("revision", 0))
        await self._progress(updater, _cursor(revision, job_id))
        while True:
            progress = await self._service.poll(
                job_id, wait=PROGRESS_WAIT_S, since=revision
            )
            if not progress.get("ok"):
                await _refuse(updater, _code(progress),
                              progress.get("message", ""), job_id=job_id)
                return
            revision = int(progress.get("revision", revision))
            if progress.get("state") != "running":
                break
            await self._progress(updater, _cursor(revision, job_id, progress))
        await self._answer_price_list(updater, job_id)

    async def _progress(self, updater: TaskUpdater, cursor: dict[str, Any]) -> None:
        """One progress tick, said twice on purpose.

        The metadata is for whoever is listening right now. The message is for
        whoever is not: the SDK moves a status message into `Task.history` when
        the next status arrives, and history is the only way back for a client
        that dropped its stream -- `SubscribeToTask` replays nothing, and its
        request has nowhere to put a cursor (spike #22). Saying it once, in
        metadata, would leave a reconnecting client with no way to find out what
        it missed.
        """
        await updater.update_status(
            TaskState.TASK_STATE_WORKING,
            message=updater.new_agent_message([_data_part(cursor)]),
            metadata=cursor,
        )

    async def _answer_price_list(self, updater: TaskUpdater, job_id: str) -> None:
        answer = await self._service.result(job_id)
        if not answer.get("ok"):
            await _refuse(updater, _code(answer),
                          answer.get("message", ""), job_id=job_id)
            return
        body = {k: v for k, v in answer.items() if k != "ok"}
        await updater.add_artifact(
            [_data_part({**body, "hint": PRICE_LIST_GUIDANCE})],
            name="section price list",
            # The mark, on the artifact itself. Declared on the card as an
            # extension with required=true, so a client that does not know the
            # convention is told so rather than reading a menu as a meal.
            extensions=[PRICE_LIST_EXTENSION],
        )
        await updater.complete()

    async def _answer_every_section(self, updater: TaskUpdater, job_id: str) -> None:
        """Full text, assembled from the same per-section reads.

        Not a second route that returns the whole report: `drill_section`
        already carries a token limit, and a second path would mean a second
        limit -- and the limits in this project are the ones that actually run,
        not the ones that are declared (D2). "Full text" therefore means the
        endpoint walks the price list on the caller's behalf.

        One artifact per section rather than one concatenation, so a caller can
        stop reading partway and so a section that had to be cut says so where
        it was cut.
        """
        listing = await self._service.result(job_id)
        if not listing.get("ok"):
            await _refuse(updater, _code(listing),
                          listing.get("message", ""), job_id=job_id)
            return

        sections = listing.get("sections") or []
        if not sections:
            await _refuse(updater, "no_sections",
                          "這份報告沒有可逐節取得的章節。", job_id=job_id)
            return

        for section in sections:
            section_id = str(section.get("id", ""))
            answer = await self._service.drill_section(job_id, section_id)
            if answer.get("ok"):
                await updater.add_artifact(
                    [_data_part({k: v for k, v in answer.items() if k != "ok"})],
                    name=f"section {section_id}",
                )
                continue
            # One bad section does not take the rest with it: the caller asked
            # for the report, and the readable part of it is still worth having.
            # Outward, a refusal's kind is `code`: `error` is what JSON-RPC
            # calls its own envelope failure, and two meanings for one word on
            # the same wire is how a caller ends up handling neither.
            await updater.add_artifact(
                [_data_part({
                    "section_id": section_id, "code": _code(answer),
                    **{k: v for k, v in answer.items() if k not in ("ok", "error")},
                })],
                name=f"section {section_id} (unavailable)",
            )
        await updater.complete()

    async def _answer_section(
        self, updater: TaskUpdater, job_id: str, section_id: str
    ) -> None:
        answer = await self._service.drill_section(job_id, section_id)
        if not answer.get("ok"):
            await _refuse(updater, _code(answer),
                          answer.get("message", ""), job_id=job_id, section=section_id)
            return
        # No extension mark here: this artifact IS the content, and marking it
        # would say the opposite of what it is.
        await updater.add_artifact(
            [_data_part({k: v for k, v in answer.items() if k != "ok"})],
            name=f"section {section_id}",
        )
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        # Reading is not long enough to be worth interrupting, and a cancel that
        # silently did nothing would be worse than one that says so.
        await updater.cancel()


def _code(answer: dict[str, Any]) -> str:
    """The service spells a refusal's kind `error`, not `code`.

    Reading the wrong key does not raise -- it quietly yields a default, so
    every refusal came out labelled "error" and the task store could not tell a
    job that never existed from one that was abandoned.
    """
    return str(answer.get("error") or "error")


def _cursor(revision: int, job_id: str, progress: dict[str, Any] | None = None):
    """What a reconnecting client needs, on every event that says anything.

    `revision` rather than an event count: it is the number the harness already
    bumps on every meaningful change and already accepts back through
    `wait_for_change(since=)`. A second sequence would be a second thing to keep
    correct. `ctx` does not bump it, so an orchestrator turn produces no push.
    """
    cursor: dict[str, Any] = {"revision": revision, "job_id": job_id}
    if progress:
        for key in ("phase", "dispatches", "state"):
            if key in progress:
                cursor[key] = progress[key]
    return cursor


def _read_request(context: RequestContext) -> dict[str, Any]:
    """job_id and section_id, whether they arrive as data or as text.

    A caller with a structured client sends a DataPart. A caller driving this by
    hand sends a line of text. Refusing the second would make the boundary
    harder to try than the MCP one, which takes both.
    """
    message = context.message
    for part in getattr(message, "parts", None) or []:
        # Part is a oneof; `data` is set only when the caller sent structured
        # content, and WhichOneof is the only honest way to ask.
        if part.WhichOneof("content") == "data":
            return _value_to_dict(part.data)
    text = (context.get_user_input() or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {"job_id": text.split()[0]}
    return parsed if isinstance(parsed, dict) else {}


def _data_part(payload: dict[str, Any]) -> Part:
    """Part.data is a protobuf Value, which takes JSON-shaped input only.

    The round trip through json is not decoration: the service's answers carry
    tuples and dataclass leftovers that Value refuses, and finding that out at
    serialisation time would surface as a protobuf error rather than as ours.
    """
    from google.protobuf.struct_pb2 import Value

    value = Value()
    value.struct_value.update(json.loads(json.dumps(payload, ensure_ascii=False)))
    return Part(data=value, media_type="application/json")


def _value_to_dict(value: Any) -> dict[str, Any]:
    from google.protobuf.json_format import MessageToDict

    if value is None:
        return {}
    parsed = MessageToDict(value)
    return parsed if isinstance(parsed, dict) else {}


async def _refuse(updater: TaskUpdater, code: str, message: str, **detail: Any) -> None:
    """A refusal is a readable result, not a stack trace (design.md D5).

    It goes out as a failed task rather than a completed one with bad news in
    it: failure is terminal, and terminal is what lets a caller stop waiting.
    """
    await updater.failed(
        updater.new_agent_message(
            [_data_part({"code": code, "message": message, **detail})]
        )
    )


__all__ = ["PRICE_LIST_GUIDANCE", "AnalysisExecutor"]
