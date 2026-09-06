"""Asking the user something, without knowing where the user is.

The orchestrator must not know whether it is talking to a terminal, an MCP
client or nothing at all. It asks; a channel answers. The default channel
answers with the question's own default, which is what lets this whole layer run
end to end before any outward-facing service exists (design.md D6).
"""

from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class QuestionKind(StrEnum):
    #: The host agent can usually answer this itself (a path, a parameter).
    ANSWERABLE_BY_HOST = "answerable_by_host"
    #: Needs a person: a business judgement or an authorisation.
    NEEDS_HUMAN = "needs_human"


@dataclass(frozen=True, slots=True)
class Question:
    id: str
    text: str
    kind: QuestionKind = QuestionKind.ANSWERABLE_BY_HOST
    options: tuple[str, ...] = ()
    default: str = ""
    timeout_s: float = 600.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "text": self.text, "kind": str(self.kind),
            "options": list(self.options), "default": self.default,
        }


@dataclass(frozen=True, slots=True)
class Answer:
    question_id: str
    text: str
    #: True when nobody actually answered -- the default stood in. Such answers
    #: become "unconfirmed assumption" caveats in the final delivery.
    defaulted: bool = False
    reason: str = ""


class UserChannel(abc.ABC):
    @abc.abstractmethod
    async def ask(self, question: Question) -> Answer:
        """Put a question to the user and return an answer, always."""


class DefaultingChannel(UserChannel):
    """Never actually asks. Used when no interactive surface is attached."""

    async def ask(self, question: Question) -> Answer:
        return Answer(question.id, question.default, defaulted=True,
                      reason="no interactive channel attached")


class ScriptedChannel(UserChannel):
    """Test double: canned answers, falling back to the default."""

    def __init__(self, *answers: str, delay_s: float = 0.0) -> None:
        self._answers = list(answers)
        self._delay = delay_s
        self.asked: list[Question] = []

    async def ask(self, question: Question) -> Answer:
        self.asked.append(question)
        if self._delay:
            await asyncio.sleep(self._delay)
        if not self._answers:
            return Answer(question.id, question.default, defaulted=True,
                          reason="script exhausted")
        return Answer(question.id, self._answers.pop(0))


class QueueChannel(UserChannel):
    """Parks the question and waits for someone outside to answer it.

    This is the shape the MCP layer will use: the question surfaces in a job
    status poll and an answer call resolves the future.
    """

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[str]] = {}
        self.questions: dict[str, Question] = {}

    def pending(self) -> list[Question]:
        return [q for qid, q in self.questions.items() if qid in self._pending]

    def answer(self, question_id: str, text: str) -> bool:
        future = self._pending.pop(question_id, None)
        if future is None or future.done():
            return False
        future.set_result(text)
        return True

    async def ask(self, question: Question) -> Answer:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending[question.id] = future
        self.questions[question.id] = question
        try:
            text = await asyncio.wait_for(future, timeout=question.timeout_s)
        except (TimeoutError, asyncio.TimeoutError):
            self._pending.pop(question.id, None)
            return Answer(question.id, question.default, defaulted=True,
                          reason=f"no answer within {question.timeout_s:.0f}s")
        return Answer(question.id, text)
