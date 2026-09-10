"""The A2A executor: one shell over the same `AnalysisService`.

`myharness/mcp/server.py` says everything above it is protocol-free, and that is
what makes a second boundary cheap: this module is the same width as that one.
It routes an A2A request onto `result()` and `drill_section()` and does not
touch the service (expose-over-a2a D3). No `mode=` parameter went into
`AnalysisService` -- the difference between the two skills lives here, because
the moment the service knows there are two protocols, the layering that made
this possible is gone.

What is here is the read path. Starting an analysis is a lifecycle question --
non-blocking start, progress events carrying `revision`, reconnect without
missed events -- and it is section 6 of the change, not this one. A request to
start is refused with something a caller can act on rather than half-served.
"""

from __future__ import annotations

import json
from typing import Any

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, Task, TaskState, TaskStatus

from myharness.a2a.card import PRICE_LIST_EXTENSION, SKILL_FULL_TEXT, SKILL_PRICE_LIST
from myharness.mcp.service import AnalysisService

#: What a price-list artifact says about itself. The section ids alone would be
#: a list of names with prices and no way to spend them, and the change's spec
#: 5.2 asks for the price list to carry its own instructions.
PRICE_LIST_GUIDANCE = (
    "這是章節價目表，不是章節內容。每一節的 est_tokens 是把它讀進 context 要花的"
    f"token。要全文請用 skill `{SKILL_FULL_TEXT}` 逐節取，指名 section id。"
)


class AnalysisExecutor(AgentExecutor):
    """Reads a finished analysis out to an A2A caller."""

    def __init__(self, service: AnalysisService) -> None:
        self._service = service

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
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

        request = _read_request(context)
        job_id = request.get("job_id")
        if not job_id:
            await _refuse(updater, "missing_job_id",
                          "請在 message 裡指名 job_id。啟動新分析尚未開放 —— "
                          "目前 A2A 邊界只讀已存在的分析。")
            return

        section_id = request.get("section_id")
        if section_id:
            await self._answer_section(updater, str(job_id), str(section_id))
        else:
            await self._answer_price_list(updater, str(job_id))

    async def _answer_price_list(self, updater: TaskUpdater, job_id: str) -> None:
        answer = await self._service.result(job_id)
        if not answer.get("ok"):
            await _refuse(updater, answer.get("code", "error"),
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

    async def _answer_section(
        self, updater: TaskUpdater, job_id: str, section_id: str
    ) -> None:
        answer = await self._service.drill_section(job_id, section_id)
        if not answer.get("ok"):
            await _refuse(updater, answer.get("code", "error"),
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


__all__ = ["AnalysisExecutor", "PRICE_LIST_GUIDANCE"]
