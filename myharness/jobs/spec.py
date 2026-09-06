"""What a job is allowed to spend, and what phase it is in."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

#: Defaults are provisional pending golden-job calibration (design.md Open Questions).
DEFAULT_MAX_DISPATCHES: Final = 60
DEFAULT_MAX_BUDGET_USD: Final = 5.0
DEFAULT_MAX_WALL_CLOCK_S: Final = 1800.0
DEFAULT_PEEK_BUDGET_TOKENS: Final = 30_000
DEFAULT_QUESTION_QUOTA: Final = 5
DEFAULT_WRAP_UP_GRACE: Final = 3
DEFAULT_NO_PROGRESS_LIMIT: Final = 3
DEFAULT_HANDOFF_RATIO: Final = 0.6
DEFAULT_CONTEXT_WINDOW: Final = 196_000


class JobPhase(StrEnum):
    PLANNING = "planning"
    RUNNING = "running"
    WRAPPING_UP = "wrapping_up"
    COMPLETE = "complete"
    ABORTED = "aborted"


TERMINAL_PHASES: Final = frozenset({JobPhase.COMPLETE, JobPhase.ABORTED})


class LimitKind(StrEnum):
    DISPATCHES = "max_dispatches"
    BUDGET_USD = "max_budget_usd"
    WALL_CLOCK = "max_wall_clock_s"
    #: Not a ceiling on spending but on futility: sustained failure to produce
    #: anything means something is broken, and the budget should stop burning.
    NO_PROGRESS = "no_progress"


@dataclass(frozen=True, slots=True)
class JobSpec:
    """The goal plus every ceiling that bounds the run.

    Every ceiling exists because something in this layer would otherwise grow
    without bound -- see design.md D2 and D4.
    """

    job_id: str
    goal: str
    max_dispatches: int = DEFAULT_MAX_DISPATCHES
    max_budget_usd: float = DEFAULT_MAX_BUDGET_USD
    max_wall_clock_s: float = DEFAULT_MAX_WALL_CLOCK_S
    peek_budget_tokens: int = DEFAULT_PEEK_BUDGET_TOKENS
    question_quota: int = DEFAULT_QUESTION_QUOTA
    wrap_up_grace: int = DEFAULT_WRAP_UP_GRACE
    no_progress_limit: int = DEFAULT_NO_PROGRESS_LIMIT
    handoff_ratio: float = DEFAULT_HANDOFF_RATIO
    context_window: int = DEFAULT_CONTEXT_WINDOW
    max_lane_concurrency: int = 4
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def handoff_threshold_tokens(self) -> int:
        return int(self.context_window * self.handoff_ratio)
