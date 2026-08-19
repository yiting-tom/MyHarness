"""The seam between the worker loop and the SDK.

Everything interesting in ``run_lane_worker`` -- budget classification, the
degraded contract path, transient retry, partial-result recovery -- has to be
testable without spending money or needing a network. So the SDK call sits
behind this one protocol, and the offline suite injects a scripted stand-in.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, Protocol

from claude_agent_sdk import ClaudeAgentOptions, query


class WorkerTransport(Protocol):
    """Runs one agent turn and yields its messages."""

    def stream(
        self, prompt: str, options: ClaudeAgentOptions
    ) -> AsyncIterator[Any]:  # pragma: no cover - protocol
        ...


class SdkTransport:
    """The real thing: one ``query()`` per worker run."""

    def stream(self, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        return query(prompt=prompt, options=options)


class ScriptedTransport:
    """Replays canned message sequences, one per call.

    Raising is expressed by putting an exception instance in a script: the SDK
    raises mid-stream when ``task_budget`` runs out, and the worker loop has to
    cope with messages already consumed before that point (design.md D1).
    """

    def __init__(self, *scripts: Sequence[Any]) -> None:
        self._scripts = list(scripts)
        self.calls: list[tuple[str, ClaudeAgentOptions]] = []

    def stream(self, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        self.calls.append((prompt, options))
        script = self._scripts.pop(0) if self._scripts else []

        async def gen() -> AsyncIterator[Any]:
            for item in script:
                if isinstance(item, BaseException):
                    raise item
                yield item

        return gen()

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def last_prompt(self) -> str:
        return self.calls[-1][0] if self.calls else ""
