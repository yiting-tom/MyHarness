"""Job state: plain, serialisable data.

DESIGN.md decision #2 keeps jobs in memory but insists the state be plain data
from day one, so adding persistence later is a new writer rather than a rewrite.
Nothing here holds an SDK session or a coroutine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from myharness.jobs.spec import JobPhase, JobSpec, LimitKind


class TaskStatus(StrEnum):
    RUNNING = "running"
    DONE = "done"
    CANCELLED = "cancelled"


@dataclass
class TaskRecord:
    """One dispatched lane task, from launch to collection."""

    id: str
    lane: str
    task: str
    inputs: tuple[str, ...]
    fingerprint: str
    status: TaskStatus = TaskStatus.RUNNING
    handle: Any = None
    collected: bool = False

    @property
    def done(self) -> bool:
        return self.status is not TaskStatus.RUNNING


@dataclass
class JobState:
    """Everything about a job that survives the orchestrator's context."""

    spec: JobSpec
    phase: JobPhase = JobPhase.PLANNING
    tasks: dict[str, TaskRecord] = field(default_factory=dict)
    by_fingerprint: dict[str, str] = field(default_factory=dict)
    dispatches: int = 0
    spent_usd: float = 0.0
    peek_spent_tokens: int = 0
    questions_asked: int = 0
    no_progress_streak: int = 0
    wrap_up_used: int = 0
    limit_hit: LimitKind | None = None
    report_artifact: str | None = None
    handoffs: int = 0
    findings_seen: set[str] = field(default_factory=set)

    # ---- budgets ---------------------------------------------------------

    @property
    def peek_remaining(self) -> int:
        return max(0, self.spec.peek_budget_tokens - self.peek_spent_tokens)

    @property
    def questions_remaining(self) -> int:
        return max(0, self.spec.question_quota - self.questions_asked)

    @property
    def wrap_up_remaining(self) -> int:
        return max(0, self.spec.wrap_up_grace - self.wrap_up_used)

    @property
    def wrapping_up(self) -> bool:
        return self.phase is JobPhase.WRAPPING_UP

    # ---- task views ------------------------------------------------------

    def running(self) -> list[TaskRecord]:
        return [t for t in self.tasks.values() if t.status is TaskStatus.RUNNING]

    def uncollected(self) -> list[TaskRecord]:
        return [t for t in self.tasks.values() if t.done and not t.collected]

    def record_progress(self, artifact: str | None) -> None:
        """A dispatch counts as progress only if it produced something new."""
        if artifact and artifact not in self.findings_seen:
            self.findings_seen.add(artifact)
            self.no_progress_streak = 0
        else:
            self.no_progress_streak += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.spec.job_id,
            "phase": str(self.phase),
            "dispatches": self.dispatches,
            "spent_usd": round(self.spent_usd, 6),
            "peek_remaining": self.peek_remaining,
            "questions_remaining": self.questions_remaining,
            "running": len(self.running()),
            "uncollected": len(self.uncollected()),
            "no_progress_streak": self.no_progress_streak,
            "limit_hit": str(self.limit_hit) if self.limit_hit else None,
            "handoffs": self.handoffs,
            "report": self.report_artifact,
        }
