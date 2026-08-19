"""Bounds on everything that crosses back to the client.

This layer protects the host agent's context -- the first row of DESIGN.md's
recursion table, and the only one that had no bounds until now. Every layer
below it caps what it hands upward: a lane handle is clamped, a query result is
gated on rows and on characters, a note read is refused above a token estimate.
There is no reason for the outermost boundary to be the leaky one.

The shape follows DESIGN #14: a summary plus a *priced menu*. The client sees
what each section would cost before spending context on it, and drills only
into what it needs.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from myharness.artifacts.tokens import estimate_tokens

#: One line per recent event, and only the recent ones. Progress must not grow
#: with the length of the job -- that is the whole point.
MAX_RECENT_EVENTS = 8
MAX_EVENT_CHARS = 120
#: A question the client has to read and answer. Longer than this is a note,
#: not a question.
MAX_QUESTION_CHARS = 400
MAX_PENDING_QUESTIONS = 5
#: The whole progress payload. Enforced, not hoped for: eight 120-character
#: event lines plus five 400-character questions already come to ~3,000, so
#: per-item caps do not bound the total. Same lesson as the query gates.
MAX_PROGRESS_CHARS = 2_000
#: Executive summary plus findings. The menu is what carries the detail.
MAX_SUMMARY_CHARS = 1_500
MAX_FINDINGS = 8
MAX_FINDING_CHARS = 200
#: One section, fetched deliberately. Generous because the client asked for it
#: after seeing the price -- but not unbounded, because the price is an estimate.
MAX_SECTION_TOKENS = 20_000


def clip(text: str, limit: int) -> str:
    """Shorten and say so. A silent truncation is a confident half-truth."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


@dataclass(frozen=True, slots=True)
class Progress:
    """What a client learns from one poll. Bounded regardless of job size."""

    job_id: str
    state: str
    phase: str
    revision: int
    dispatches: int
    running: int
    spent_usd: float
    elapsed_s: float
    recent: tuple[str, ...] = ()
    questions: tuple[dict[str, Any], ...] = ()
    report_ready: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "job_id": self.job_id,
            "state": self.state,
            "phase": self.phase,
            # The client passes this back on the next poll so a change landing
            # between two polls is not lost.
            "revision": self.revision,
            "dispatches": self.dispatches,
            "running": self.running,
            "spent_usd": round(self.spent_usd, 5),
            "elapsed_s": round(self.elapsed_s, 1),
            "recent": list(self.recent),
            "pending_questions": list(self.questions),
            "report_ready": self.report_ready,
        }
        if self.note:
            payload["note"] = self.note
        return payload


def build_progress(
    *,
    job_id: str,
    state: str,
    revision: int,
    status: dict[str, Any],
    recent_events: Sequence[Any] = (),
    note: str = "",
) -> Progress:
    """Assemble a progress payload from a job's status and its last events."""
    questions = tuple(
        {
            "id": q.get("id", ""),
            "text": clip(str(q.get("text", "")), MAX_QUESTION_CHARS),
            "kind": q.get("kind", ""),
        }
        for q in list(status.get("pending_questions", []))[:MAX_PENDING_QUESTIONS]
    )
    recent = tuple(
        clip(describe_event(e), MAX_EVENT_CHARS)
        for e in list(recent_events)[-MAX_RECENT_EVENTS:]
    )
    progress = Progress(
        job_id=job_id,
        state=state,
        phase=str(status.get("phase", "")),
        revision=revision,
        dispatches=int(status.get("dispatches", 0) or 0),
        running=int(status.get("running", 0) or 0),
        spent_usd=float(status.get("spent_usd", 0.0) or 0.0),
        elapsed_s=float(status.get("elapsed_s", 0.0) or 0.0),
        recent=recent,
        questions=questions,
        report_ready=bool(status.get("report")),
        note=note,
    )
    return _fit(progress)


def _encoded_size(progress: Progress) -> int:
    return len(json.dumps(progress.to_dict(), ensure_ascii=False))


def _fit(progress: Progress, limit: int = MAX_PROGRESS_CHARS) -> Progress:
    """Shed content until the whole payload fits, least useful first.

    Recent events go before questions: an unanswered question blocks the job,
    while a missed log line costs the client nothing it cannot poll again for.
    The counters and the revision are never dropped -- they are what makes the
    payload worth sending at all.
    """
    if _encoded_size(progress) <= limit:
        return progress

    recent = list(progress.recent)
    while recent and _encoded_size(replace(progress, recent=tuple(recent))) > limit:
        recent.pop(0)
    progress = replace(progress, recent=tuple(recent))
    if _encoded_size(progress) <= limit:
        return progress

    questions = list(progress.questions)
    while len(questions) > 1 and _encoded_size(
        replace(progress, questions=tuple(questions))
    ) > limit:
        questions.pop()
    progress = replace(progress, questions=tuple(questions))
    if _encoded_size(progress) <= limit:
        return progress

    # One question, still too large: clip its text rather than drop the only
    # thing the job is waiting on.
    if questions:
        room = max(40, MAX_QUESTION_CHARS - (_encoded_size(progress) - limit))
        questions[0] = {**questions[0], "text": clip(questions[0]["text"], room)}
    return replace(progress, questions=tuple(questions))


def describe_event(event: Any) -> str:
    """One line for one event. Never the event itself.

    The event stream is the system's record and it is large; a client that
    wanted it would be paying context for the thing this layer exists to keep
    out. Reading is deliberately tolerant -- an unknown kind renders as its
    name rather than breaking the poll (events/types.py's additive rule).
    """
    data = getattr(event, "data", None) or {}
    kind = getattr(event, "t", None) or data.get("t") or "?"
    if kind == "dispatch.start":
        return f"{kind} {data.get('id', '')} → {data.get('lane', '')}: {data.get('task', '')}"
    if kind == "dispatch.end":
        return (f"{kind} {data.get('id', '')} {data.get('status', '')}: "
                f"{data.get('headline', '')}")
    if kind == "ask.user":
        return f"{kind}: {data.get('question', '')}"
    if kind == "limit.reached":
        return f"{kind}: {data.get('kind', '')}"
    if kind == "job.finish":
        return f"{kind}: {data.get('report', '')}"
    return str(kind)


@dataclass(frozen=True, slots=True)
class ResultPayload:
    """A summary and a priced menu -- never the report itself (DESIGN #14)."""

    body: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return self.body


def build_result(delivery_dict: dict[str, Any]) -> ResultPayload:
    """Bound a delivery for the wire.

    ``Delivery`` is already small by construction, but "already small" is a
    property of today's synthesizer prompt, not a guarantee. The bound belongs
    on the boundary.
    """
    body = dict(delivery_dict)
    body["executive_summary"] = clip(
        str(body.get("executive_summary", "")), MAX_SUMMARY_CHARS
    )
    body["key_findings"] = [
        clip(str(f), MAX_FINDING_CHARS)
        for f in list(body.get("key_findings", []))[:MAX_FINDINGS]
    ]
    sections = list(body.get("sections", []))
    body["sections"] = sections
    body["total_section_tokens"] = sum(
        int(s.get("est_tokens", 0) or 0) for s in sections
    )
    body["hint"] = (
        "sections lists what is available and what each would cost. "
        "Call analysis_drill(job_id, section_id) for the ones you need."
    )
    return ResultPayload(body)


#: Appended to a cut section. Its own cost comes out of the budget, not on top
#: of it -- a bound the notice pushes you past is not a bound.
_CUT_NOTICE = "\n\n…（本節超出單次上限，以上為前段）"


def bound_section(text: str, *, max_tokens: int = MAX_SECTION_TOKENS) -> tuple[str, bool]:
    """Return the section and whether it had to be cut.

    The menu's price is an estimate, so a section can be larger than advertised.
    Cutting silently would make the estimate look reliable when it was not.
    """
    if estimate_tokens(text) <= max_tokens:
        return text, False
    budget = max(0, max_tokens - estimate_tokens(_CUT_NOTICE))
    # Estimation is not reversible, so walk back by characters until it fits.
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + _CUT_NOTICE, True


__all__ = [
    "MAX_EVENT_CHARS",
    "MAX_FINDINGS",
    "MAX_FINDING_CHARS",
    "MAX_PENDING_QUESTIONS",
    "MAX_PROGRESS_CHARS",
    "MAX_QUESTION_CHARS",
    "MAX_RECENT_EVENTS",
    "MAX_SECTION_TOKENS",
    "MAX_SUMMARY_CHARS",
    "Progress",
    "ResultPayload",
    "bound_section",
    "build_progress",
    "build_result",
    "clip",
    "describe_event",
]
