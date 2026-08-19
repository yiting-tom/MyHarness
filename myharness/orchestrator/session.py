"""The seam between the orchestrator loop and a live conversation.

The orchestrator keeps one conversation for the whole job (DESIGN.md decision
#7), so unlike a lane worker it needs multi-turn send/receive rather than a
single stream. Everything interesting in the loop -- the handoff threshold,
wrap-up injection, the guards -- has to be testable without a network, so the
conversation sits behind this protocol.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient


@dataclass(frozen=True, slots=True)
class ContextUsage:
    used: int
    limit: int

    @property
    def ratio(self) -> float:
        return self.used / self.limit if self.limit else 0.0


class OrchestratorSession(abc.ABC):
    """One live conversation with the orchestrator."""

    @abc.abstractmethod
    def send(self, text: str) -> AsyncIterator[Any]:
        """Send a turn and yield the messages it produces."""

    @abc.abstractmethod
    async def context_usage(self) -> ContextUsage:
        """How full the conversation is, for the handoff threshold."""


class SdkSession(OrchestratorSession):
    def __init__(self, client: ClaudeSDKClient, fallback_limit: int) -> None:
        self._client = client
        self._fallback_limit = fallback_limit

    def send(self, text: str) -> AsyncIterator[Any]:
        async def stream() -> AsyncIterator[Any]:
            await self._client.query(text)
            async for message in self._client.receive_response():
                yield message

        return stream()

    async def context_usage(self) -> ContextUsage:
        try:
            usage = await self._client.get_context_usage()
        except Exception:
            return ContextUsage(0, self._fallback_limit)
        return ContextUsage(
            int(usage.get("totalTokens", 0)),
            int(usage.get("maxTokens") or self._fallback_limit),
        )


class SessionFactory(abc.ABC):
    @abc.abstractmethod
    def open(self, options: ClaudeAgentOptions, *, limit: int):
        """Async context manager yielding a session."""


class SdkSessionFactory(SessionFactory):
    @asynccontextmanager
    async def open(self, options: ClaudeAgentOptions, *, limit: int):
        async with ClaudeSDKClient(options=options) as client:
            yield SdkSession(client, limit)


# --- test doubles ---------------------------------------------------------


@dataclass
class ScriptedSession(OrchestratorSession):
    """Replays canned turns; reports whatever context usage the script says."""

    turns: list[Sequence[Any]]
    usage_series: list[int] = field(default_factory=list)
    limit: int = 196_000
    sent: list[str] = field(default_factory=list)
    _usage_index: int = 0

    def send(self, text: str) -> AsyncIterator[Any]:
        self.sent.append(text)
        script = self.turns.pop(0) if self.turns else []

        async def stream() -> AsyncIterator[Any]:
            for item in script:
                if isinstance(item, BaseException):
                    raise item
                yield item

        return stream()

    async def context_usage(self) -> ContextUsage:
        if not self.usage_series:
            return ContextUsage(0, self.limit)
        index = min(self._usage_index, len(self.usage_series) - 1)
        self._usage_index += 1
        return ContextUsage(self.usage_series[index], self.limit)


@dataclass
class ScriptedSessionFactory(SessionFactory):
    """Hands out one scripted session per open(); records how many were opened."""

    sessions: list[ScriptedSession]
    opened: list[ScriptedSession] = field(default_factory=list)

    @asynccontextmanager
    async def open(self, options: ClaudeAgentOptions, *, limit: int):
        session = self.sessions.pop(0) if self.sessions else ScriptedSession([])
        session.limit = limit
        self.opened.append(session)
        yield session

    @property
    def open_count(self) -> int:
        return len(self.opened)
