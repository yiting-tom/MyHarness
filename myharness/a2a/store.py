"""A task store that can answer for jobs this process never ran.

`JobManager` keeps an `asyncio.Task` in memory, so a job in flight belongs to one
process (expose-over-a2a D5). Reading one does not: `AnalysisService.result` and
`drill_section` touch only the event log and the store, which is why the MCP
boundary answers for a job started by somebody else.

An in-memory task store would throw that away -- a `GetTask` for a job this
process did not run would be a 404 for something that is sitting on disk,
finished and readable. So this keeps the SDK's in-memory store for live tasks
and falls back to the event log for everything else.

The state it reports comes from spike #23, which found that D5's "exists but is
not running here" splits in two and only one half is homeless:

- finished elsewhere -> COMPLETED. The event log says so and the harness can
  still answer in full.
- abandoned in flight -> nothing in TaskState fits. Not WORKING, because nothing
  is processing it; not COMPLETED, because it did not finish; not FAILED or
  REJECTED, because the agent never decided anything; not CANCELED, because
  nobody asked. FAILED with a message saying so is wrong in kind but terminal,
  and terminal is what lets a caller stop waiting. WORKING would be a lie the
  caller could not detect: it would wait forever for a stream nothing will ever
  write to.
"""

from __future__ import annotations

from typing import Any

from a2a.server.tasks import InMemoryTaskStore
from a2a.types import Task, TaskState, TaskStatus

from myharness.mcp.service import AnalysisService

#: Said in the status message of an abandoned task, because the state alone
#: cannot say it and a caller that sees FAILED would otherwise assume the
#: analysis itself failed.
ABANDONED_DETAIL = (
    "這個分析不在任何執行中的程序裡，而且沒有跑完 —— 啟動它的程序消失了。"
    "A2A 的 task 狀態沒有一個表示這件事，所以用 FAILED：它是終局狀態，"
    "呼叫方應該停止等待。已經寫下的部分仍然讀得到。"
)


async def state_of(service: AnalysisService, job_id: str) -> tuple[Any, str] | None:
    """(state, detail) for a job that exists and is not running here.

    None for both of the cases this must not answer for: an id no job has ever
    had, and a job running in this process. The second matters as much as the
    first -- a fabricated task for a live run carries a context id that nothing
    else agrees with, and the framework rejects the next event as belonging to
    another task.

    Kept apart from the store so the mapping spike #23 argued for is one
    function with one answer, rather than a branch inside a cache.
    """
    progress = await service.poll(job_id, wait=0.0)
    if progress.get("ok"):
        return None
    if progress.get("code") == "no_such_job":
        return None

    answer = await service.result(job_id)
    if answer.get("ok"):
        return TaskState.TASK_STATE_COMPLETED, ""
    if answer.get("code") == "no_such_job":
        # Both halves have to agree before this store invents a task. Deciding
        # on poll alone would fabricate a terminal task for an id nobody has
        # ever used -- and a task id is a client-supplied string.
        return None
    return TaskState.TASK_STATE_FAILED, ABANDONED_DETAIL


class EventLogTaskStore(InMemoryTaskStore):
    """In memory for what is live, the event log for what is finished or gone."""

    def __init__(self, service: AnalysisService) -> None:
        super().__init__()
        self._service = service

    async def get(self, task_id: str, context: Any = None) -> Task | None:
        live = await super().get(task_id, context) if context is not None else None
        if live is not None:
            return live
        mapped = await state_of(self._service, task_id)
        if mapped is None:
            return None
        state, detail = mapped
        status = TaskStatus(state=state)
        task = Task(id=task_id, context_id=task_id, status=status)
        if detail:
            # The detail rides in metadata rather than in a Message: a status
            # message is part of the conversation, and nobody said this.
            task.metadata.update({"detail": detail})
        return task


__all__ = ["ABANDONED_DETAIL", "EventLogTaskStore", "state_of"]
