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
from typing import Any

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
MAX_SCHEMA_RETRIES = 2


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


def reprompt_text(problems: tuple[str, ...]) -> str:
    """What to send back when the model's output did not validate."""
    bullets = "\n".join(f"- {p}" for p in problems)
    return (
        "Your last message was not a valid handle. Problems:\n"
        f"{bullets}\n\n"
        "Reply with ONLY a JSON object matching this schema, no prose, no code fence:\n"
        f"{json.dumps(HANDLE_SCHEMA, ensure_ascii=False)}"
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
