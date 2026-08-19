"""The lane handle: what a worker is allowed to hand back.

This is where the harness's central claim lives. A worker may produce twenty
thousand tokens of analysis; what reaches the orchestrator is this object and
nothing else, and its size is bounded by code rather than by the model's
restraint (DESIGN.md decision #3, design.md D2).

Two mechanisms, deliberately stacked:

* the schema constrains the *shape* -- enforced by the backend where it can be,
  validated and re-prompted where it cannot;
* :func:`clamp_handle` constrains the *length* -- a schema cannot, and a model
  can happily return a schema-valid object with a three-thousand-word headline.

Either alone is "very likely". Both together is a guarantee.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

# --- size limits ---------------------------------------------------------

MAX_HEADLINE_CHARS: Final = 200
MAX_STRING_CHARS: Final = 300
MAX_FOLLOWUPS: Final = 5
MAX_METRICS: Final = 20
#: Hard ceiling on the whole serialised handle. Guards against a model smuggling
#: prose out through metric *keys* or a long list of short strings.
MAX_HANDLE_CHARS: Final = 2000

TRUNCATION_MARK: Final = "…[truncated]"


class HandleStatus(StrEnum):
    """How a lane task ended. Everything but OK is a value, never an exception."""

    OK = "ok"
    BUDGET_EXCEEDED = "budget_exceeded"
    TOOL_FAILURE = "tool_failure"
    MAX_TURNS = "max_turns"
    STATE_REJECTED = "state_rejected"
    SCHEMA_VIOLATION = "schema_violation"
    BACKEND_UNAVAILABLE = "backend_unavailable"


#: Statuses meaning the lane did not deliver what it was asked for. The event
#: log turns these into report caveats.
DEGRADED_STATUSES: Final = frozenset(HandleStatus) - {HandleStatus.OK}


HANDLE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "artifact": {
            "type": "string",
            "description": "Artifact id of the findings you wrote, e.g. lanes/<lane>/findings/003",
        },
        "headline": {
            "type": "string",
            "description": f"One sentence, at most {MAX_HEADLINE_CHARS} characters. Not a report.",
        },
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "metrics": {
            "type": "object",
            "description": "Quantitative results only. Numbers, not prose.",
            "additionalProperties": {"type": "number"},
        },
        "followups": {
            "type": "array",
            "description": "What the orchestrator should consider next.",
            "items": {"type": "string"},
        },
    },
    "required": ["artifact", "headline", "confidence"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class LaneHandle:
    """~120 tokens. The only thing that crosses back into the orchestrator."""

    artifact: str
    headline: str
    confidence: str
    status: HandleStatus = HandleStatus.OK
    metrics: dict[str, float] = field(default_factory=dict)
    followups: tuple[str, ...] = ()
    truncated: bool = False

    # Populated by the harness, never by the model.
    lane: str | None = None
    dispatch_id: str | None = None
    transcript: str | None = None
    partial: str | None = None
    suggest: str | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is HandleStatus.OK

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "artifact": self.artifact,
            "headline": self.headline,
            "confidence": self.confidence,
            "status": str(self.status),
        }
        if self.metrics:
            out["metrics"] = self.metrics
        if self.followups:
            out["followups"] = list(self.followups)
        for key in ("lane", "dispatch_id", "transcript", "partial", "suggest", "detail"):
            value = getattr(self, key)
            if value:
                out[key] = value
        if self.truncated:
            out["truncated"] = True
        return out

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[: max(0, limit - len(TRUNCATION_MARK))] + TRUNCATION_MARK, True


def clamp_handle(handle: LaneHandle) -> LaneHandle:
    """Force a handle under its size limits, marking it if anything was cut.

    Field limits first, then a whole-object ceiling, because a model can stay
    inside every field limit and still return something huge.
    """
    truncated = handle.truncated

    headline, cut = _clip(handle.headline, MAX_HEADLINE_CHARS)
    truncated |= cut
    artifact, cut = _clip(handle.artifact, MAX_STRING_CHARS)
    truncated |= cut

    followups: list[str] = []
    if len(handle.followups) > MAX_FOLLOWUPS:
        truncated = True
    for item in handle.followups[:MAX_FOLLOWUPS]:
        clipped, cut = _clip(str(item), MAX_STRING_CHARS)
        truncated |= cut
        followups.append(clipped)

    metrics: dict[str, float] = {}
    if len(handle.metrics) > MAX_METRICS:
        truncated = True
    for key, value in list(handle.metrics.items())[:MAX_METRICS]:
        clipped_key, cut = _clip(str(key), MAX_STRING_CHARS)
        truncated |= cut
        try:
            metrics[clipped_key] = float(value)
        except (TypeError, ValueError):
            truncated = True

    clamped = LaneHandle(
        artifact=artifact, headline=headline, confidence=handle.confidence,
        status=handle.status, metrics=metrics, followups=tuple(followups),
        truncated=truncated, lane=handle.lane, dispatch_id=handle.dispatch_id,
        transcript=handle.transcript, partial=handle.partial,
        suggest=handle.suggest, detail=handle.detail,
    )

    # Whole-object ceiling: shed the optional payload before the required fields.
    if len(clamped.to_json()) > MAX_HANDLE_CHARS:
        clamped = _shrink_to_ceiling(clamped)
    return clamped


def _shrink_to_ceiling(handle: LaneHandle) -> LaneHandle:
    metrics = dict(handle.metrics)
    followups = list(handle.followups)
    detail = handle.detail
    current = handle

    def rebuild() -> LaneHandle:
        return LaneHandle(
            artifact=current.artifact, headline=current.headline,
            confidence=current.confidence, status=current.status,
            metrics=metrics, followups=tuple(followups), truncated=True,
            lane=current.lane, dispatch_id=current.dispatch_id,
            transcript=current.transcript, partial=current.partial,
            suggest=current.suggest, detail=detail,
        )

    while followups and len(rebuild().to_json()) > MAX_HANDLE_CHARS:
        followups.pop()
    while metrics and len(rebuild().to_json()) > MAX_HANDLE_CHARS:
        metrics.popitem()
    current = rebuild()
    if len(current.to_json()) > MAX_HANDLE_CHARS and detail:
        detail = None
        current = rebuild()
    if len(current.to_json()) > MAX_HANDLE_CHARS:
        # Only the required fields are left; clip the headline until it fits.
        overflow = len(current.to_json()) - MAX_HANDLE_CHARS
        headline, _ = _clip(current.headline, max(20, len(current.headline) - overflow))
        current = LaneHandle(
            artifact=current.artifact, headline=headline, confidence=current.confidence,
            status=current.status, metrics=metrics, followups=tuple(followups),
            truncated=True, lane=current.lane, dispatch_id=current.dispatch_id,
            transcript=current.transcript, partial=current.partial,
            suggest=current.suggest, detail=detail,
        )
    return current
