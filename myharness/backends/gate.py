"""Per-backend throttling.

A provider's rate limit is a property of the *backend*, not of the worker that
happened to discover it. With the orchestrator about to fan out several lanes at
once, per-worker retry is actively harmful: five lanes each independently
rediscovering the same 429 is five times the load on a provider already refusing
us. So every worker on a backend passes through one shared gate.

Two things the gate owns that a retry loop cannot:

* **shared cooldown** -- the first worker to be refused parks the whole backend,
  so the others wait instead of piling on;
* **a time budget** -- rate limits recover on the order of minutes, and a fixed
  count of short backoffs just gives up before recovery.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Final

#: Total wall-clock a single worker may spend waiting on one backend before it
#: gives up and returns a handle. Rate limits outlast short retry counts.
DEFAULT_RETRY_BUDGET_S: Final = 300.0
DEFAULT_MAX_CONCURRENCY: Final = 4
BASE_BACKOFF_S: Final = 4.0
MAX_BACKOFF_S: Final = 60.0


def backoff_delay(attempt: int, *, rng: random.Random | None = None) -> float:
    """Exponential backoff with full jitter.

    Full jitter rather than a fixed schedule because several workers are
    typically refused at the same instant; without it they would all wake up
    together and reproduce the burst that got them limited.
    """
    ceiling = min(MAX_BACKOFF_S, BASE_BACKOFF_S * (2**attempt))
    return (rng or random).uniform(0.0, ceiling)


@dataclass
class ThrottleReport:
    """What waiting cost, so it can be told apart from the model being slow."""

    waited_s: float = 0.0
    waits: int = 0
    cooldowns_triggered: int = 0
    gave_up: bool = False

    def record_wait(self, seconds: float) -> None:
        self.waited_s += seconds
        self.waits += 1


class BackendGate:
    """Concurrency limit plus a shared cooldown for one backend."""

    def __init__(
        self,
        name: str,
        *,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        retry_budget_s: float = DEFAULT_RETRY_BUDGET_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.name = name
        self.retry_budget_s = retry_budget_s
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._cooldown_until: float = 0.0
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._rng = rng or random.Random()

    # ---- cooldown --------------------------------------------------------

    @property
    def cooling_down(self) -> bool:
        return self._clock() < self._cooldown_until

    def remaining_cooldown(self) -> float:
        return max(0.0, self._cooldown_until - self._clock())

    def trigger_cooldown(self, seconds: float) -> float:
        """Park the whole backend. Extends an existing cooldown, never shortens it."""
        until = self._clock() + seconds
        self._cooldown_until = max(self._cooldown_until, until)
        return self.remaining_cooldown()

    async def wait_for_clearance(self, report: ThrottleReport) -> None:
        """Block until the backend is out of cooldown."""
        while (remaining := self.remaining_cooldown()) > 0:
            await self._sleep(remaining)
            report.record_wait(remaining)

    # ---- retry -----------------------------------------------------------

    def acquire(self):
        """Limit how many workers hit this backend at once."""
        return self._semaphore

    async def back_off(self, attempt: int, report: ThrottleReport) -> bool:
        """Sleep before the next attempt, and park the backend while doing so.

        Returns False when the worker's time budget is spent.
        """
        if report.waited_s >= self.retry_budget_s:
            report.gave_up = True
            return False

        delay = min(
            backoff_delay(attempt, rng=self._rng),
            self.retry_budget_s - report.waited_s,
        )
        self.trigger_cooldown(delay)
        report.cooldowns_triggered += 1
        await self._sleep(delay)
        report.record_wait(delay)
        return True


class GateRegistry:
    """One gate per backend name, created on first use."""

    def __init__(self, **defaults) -> None:
        self._gates: dict[str, BackendGate] = {}
        self._defaults = defaults

    def for_backend(self, name: str) -> BackendGate:
        if name not in self._gates:
            self._gates[name] = BackendGate(name, **self._defaults)
        return self._gates[name]

    def reset(self) -> None:
        self._gates.clear()


gates: Final = GateRegistry()
