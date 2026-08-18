"""Event types.

The event log is the single source of truth for what happened inside a job
(design.md D7). Every event carries ``t``/``seq``/``ts``/``job_id``; everything
else lives in ``data`` so that adding a field -- or a whole new event type -- is
purely additive and never breaks an existing reader.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

# --- event type names ----------------------------------------------------

JOB_START: Final = "job.start"
JOB_FINISH: Final = "job.finish"
PLAN_UPDATE: Final = "plan.update"
INGRESS: Final = "ingress"
PROXY_ROUTE: Final = "proxy.route"
DISPATCH_START: Final = "dispatch.start"
DISPATCH_END: Final = "dispatch.end"
ARTIFACT_READ: Final = "artifact.read"
CTX: Final = "ctx"
ASK_USER: Final = "ask.user"
ASK_ANSWER: Final = "ask.answer"

KNOWN_TYPES: Final = frozenset({
    JOB_START, JOB_FINISH, PLAN_UPDATE, INGRESS, PROXY_ROUTE,
    DISPATCH_START, DISPATCH_END, ARTIFACT_READ, CTX, ASK_USER, ASK_ANSWER,
})

# --- dispatch outcomes ---------------------------------------------------

STATUS_OK: Final = "ok"
STATUS_BUDGET_EXCEEDED: Final = "budget_exceeded"
STATUS_TOOL_FAILURE: Final = "tool_failure"
STATUS_MAX_TURNS: Final = "max_turns"
STATUS_DUPLICATE: Final = "duplicate"

#: Outcomes that mean the lane did not deliver what it was asked for. These are
#: what ``derive_caveats`` turns into "what we did not do" in the final report.
DEGRADED_STATUSES: Final = frozenset({
    STATUS_BUDGET_EXCEEDED, STATUS_TOOL_FAILURE, STATUS_MAX_TURNS,
})


class MalformedEvent(ValueError):
    """A line in the log could not be parsed as an event."""


@dataclass(frozen=True, slots=True)
class Event:
    """One append-only record."""

    t: str
    seq: int
    ts: datetime
    job_id: str
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def is_known_type(self) -> bool:
        return self.t in KNOWN_TYPES

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def to_json(self) -> str:
        payload = {
            "t": self.t,
            "seq": self.seq,
            "ts": self.ts.isoformat(),
            "job_id": self.job_id,
            **self.data,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, line: str) -> Event:
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MalformedEvent(str(exc)) from exc
        if not isinstance(raw, dict):
            raise MalformedEvent("event must be a JSON object")
        try:
            t, seq, ts, job_id = raw.pop("t"), raw.pop("seq"), raw.pop("ts"), raw.pop("job_id")
        except KeyError as exc:
            raise MalformedEvent(f"missing common field {exc}") from exc
        return cls(
            t=str(t), seq=int(seq), ts=datetime.fromisoformat(str(ts)),
            job_id=str(job_id), data=raw,
        )


def now() -> datetime:
    return datetime.now(UTC)
