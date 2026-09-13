"""Turning whatever a worker said into a handle.

Two paths, chosen from the backend's declared capabilities:

* **enforced** -- the backend was given the schema and returns a validated
  object in ``ResultMessage.structured_output``; we only clamp its size;
* **degraded** -- the backend cannot enforce a schema, so we extract JSON from
  the text, validate it ourselves, and re-prompt on failure.

The event log records which path ran, so "was this run's contract enforced or
merely requested?" is answerable after the fact (spec: Backend capability 的宣告與降級).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from jsonschema import Draft202012Validator

from myharness.lanes.handle import (
    HANDLE_SCHEMA,
    HandleStatus,
    LaneHandle,
    clamp_handle,
)

_VALIDATOR = Draft202012Validator(HANDLE_SCHEMA)
_FENCE = re.compile(r"```(?:json)?\s*(?P<body>\{.*?\})\s*```", re.DOTALL)

#: How many times to re-prompt a backend that cannot enforce the schema itself.
#: One. A re-prompt gets its own allowance on top of the lane's budget
#: (LaneType.retry_budget), so each one is a real increase in what a dispatch
#: can cost; golden #18 found what happens when that is unbounded. Two attempts
#: put the worst case at twice the budget, which is a number that can be
#: reasoned about rather than a loop that cannot.
MAX_SCHEMA_RETRIES = 1


class ContractPath(StrEnum):
    ENFORCED = "enforced"
    DEGRADED = "degraded"


class HandleContractError(ValueError):
    """The worker's output could not be turned into a handle."""

    def __init__(self, message: str, *, raw: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.raw = raw


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    handle: LaneHandle | None
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.handle is not None


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort recovery of a JSON object from free-form model output."""
    if not text:
        return None
    candidate = text.strip()
    for attempt in (candidate, None):
        if attempt is None:
            fenced = _FENCE.search(text)
            if not fenced:
                break
            attempt = fenced.group("body")
        try:
            parsed = json.loads(attempt)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    # Last resort: the outermost braces in the text.
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def validate_payload(payload: dict[str, Any]) -> ValidationOutcome:
    """Check a candidate handle against the schema, then clamp it."""
    problems = tuple(
        f"{'.'.join(str(p) for p in e.path) or '(root)'}: {e.message}"
        for e in sorted(_VALIDATOR.iter_errors(payload), key=lambda e: list(e.path))
    )
    if problems:
        return ValidationOutcome(None, problems)

    handle = LaneHandle(
        artifact=str(payload["artifact"]),
        headline=str(payload["headline"]),
        confidence=str(payload["confidence"]),
        metrics={str(k): float(v) for k, v in (payload.get("metrics") or {}).items()},
        followups=tuple(str(f) for f in (payload.get("followups") or ())),
    )
    return ValidationOutcome(clamp_handle(handle), ())


#: An instance, not a schema. The re-prompt used to send the schema itself, and
#: golden runs #21 and #22 re-prompted four lanes: three replied with
#: ``{"type": "object", "properties": {"artifact": "...", ...}}`` -- their real
#: answer, correct in every field, nested inside the envelope the re-prompt had
#: just shown them. A backend that cannot be handed a schema is being asked to
#: imitate what it sees, so what it sees has to be the thing we want back.
#: Every value here is a placeholder except ``confidence``, which has to be a
#: real one: the example must itself validate, because verbatim imitation is
#: the failure mode this is correcting and it has to land somewhere harmless.
_HANDLE_EXAMPLE: Final[dict[str, Any]] = {
    "artifact": "lanes/<your lane>/findings/<the name you gave it>",
    "headline": "One sentence saying what you found.",
    "confidence": "medium",
    "metrics": {"rows_examined": 0},
    "followups": ["What the orchestrator should look at next."],
}


def reprompt_text(problems: tuple[str, ...]) -> str:
    """What to send back when the model's output did not validate."""
    bullets = "\n".join(f"- {p}" for p in problems)
    allowed = ", ".join(HANDLE_SCHEMA["properties"]["confidence"]["enum"])
    return (
        "Your last message was not a valid handle. Problems:\n"
        f"{bullets}\n\n"
        "Reply with ONLY a JSON object of exactly this shape -- the same keys, "
        "your values. No prose, no code fence, no wrapper around it:\n"
        f"{json.dumps(_HANDLE_EXAMPLE, ensure_ascii=False)}\n\n"
        f"artifact, headline and confidence are required; confidence is one of "
        f"{allowed}. Every value in metrics must be a number. Leave metrics and "
        "followups out if you have none."
    )


def failure_handle(
    status: HandleStatus,
    *,
    headline: str,
    lane: str | None = None,
    dispatch_id: str | None = None,
    partial: str | None = None,
    suggest: str | None = None,
    detail: str | None = None,
    transcript: str | None = None,
    metrics: dict[str, float] | None = None,
) -> LaneHandle:
    """Build the handle that represents a failure. Failure is a value."""
    return clamp_handle(
        LaneHandle(
            artifact=partial or "",
            headline=headline,
            confidence="low",
            status=status,
            metrics=metrics or {},
            lane=lane,
            dispatch_id=dispatch_id,
            transcript=transcript,
            partial=partial,
            suggest=suggest,
            detail=detail,
        )
    )
