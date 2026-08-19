"""Projections over an event stream.

Cost reports, dashboards, regression assertions and the report's caveats are all
computed here from the same event sequence -- there is no second bookkeeping
(design.md D7).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from myharness.events.types import (
    ASK_ANSWER,
    ASK_USER,
    CTX,
    DEGRADED_STATUSES,
    DISPATCH_END,
    DISPATCH_START,
    INGRESS,
    JOB_FINISH,
    PROXY_ROUTE,
    STATUS_DUPLICATE,
    STATUS_OK,
    THROTTLE_COOLDOWN,
    THROTTLE_GAVE_UP,
    THROTTLE_WAIT,
    Event,
)


def of_type(events: Iterable[Event], *types: str) -> Sequence[Event]:
    wanted = frozenset(types)
    return tuple(e for e in events if e.t in wanted)


def context_peak(events: Iterable[Event], who: str = "orchestrator") -> int:
    """Highest recorded context usage for a role, or 0 if never recorded."""
    used = [
        int(e.get("used", 0)) for e in events if e.t == CTX and e.get("who") == who
    ]
    return max(used, default=0)


PROXY_BUCKET = "(proxy)"


def _bucket(event: Event) -> str:
    """Proxy spend is the proxy's, not the lane it happened to route to."""
    if event.t == PROXY_ROUTE:
        return PROXY_BUCKET
    return str(event.get("lane") or PROXY_BUCKET)


def cost_by_lane(events: Iterable[Event]) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for e in of_type(events, DISPATCH_END, PROXY_ROUTE):
        totals[_bucket(e)] += float(e.get("usd") or 0.0)
    return dict(totals)


def tokens_by_lane(events: Iterable[Event]) -> dict[str, dict[str, int]]:
    totals: dict[str, dict[str, int]] = defaultdict(lambda: {"in": 0, "out": 0})
    for e in of_type(events, DISPATCH_END, PROXY_ROUTE):
        bucket = _bucket(e)
        tok = e.get("tokens") or {}
        totals[bucket]["in"] += int(tok.get("in", 0))
        totals[bucket]["out"] += int(tok.get("out", 0))
    return {k: dict(v) for k, v in totals.items()}


def cache_hit_ratio(events: Iterable[Event]) -> float:
    """Share of input tokens served from the prompt cache, across a job.

    The ephemeral-worker design pays the charter prefix on every dispatch; this
    is the number that says whether caching is actually absorbing it.
    """
    cached = fresh = 0
    for e in of_type(events, DISPATCH_END):
        tok = e.get("tokens") or {}
        cached += int(tok.get("cache_read") or 0)
        fresh += int(tok.get("fresh_in") or 0) + int(tok.get("cache_write") or 0)
    total = cached + fresh
    return round(cached / total, 3) if total else 0.0


def total_cost_usd(events: Iterable[Event]) -> float:
    return sum(float(e.get("usd") or 0.0) for e in events)


def throttle_seconds(events: Iterable[Event]) -> float:
    """Total wall-clock a job spent waiting on rate limits.

    Without this, throttling delay is indistinguishable from the model simply
    being slow -- and the two call for completely different responses.
    """
    return round(
        sum(float(e.get("seconds") or 0.0) for e in of_type(events, THROTTLE_WAIT)), 3
    )


def throttled_backends(events: Iterable[Event]) -> dict[str, float]:
    per: dict[str, float] = defaultdict(float)
    for e in of_type(events, THROTTLE_WAIT):
        per[str(e.get("backend") or "(unknown)")] += float(e.get("seconds") or 0.0)
    return {k: round(v, 3) for k, v in per.items()}


def failures(events: Iterable[Event]) -> Sequence[Event]:
    """Dispatches that did not end successfully."""
    return tuple(
        e for e in of_type(events, DISPATCH_END) if e.get("status") != STATUS_OK
    )


def duplicate_dispatches(events: Iterable[Event]) -> Sequence[Event]:
    return tuple(
        e for e in of_type(events, DISPATCH_END) if e.get("status") == STATUS_DUPLICATE
    )


def dispatch_count(events: Iterable[Event]) -> int:
    return len(of_type(events, DISPATCH_START))


def finished(events: Sequence[Event]) -> bool:
    return bool(events) and events[-1].t == JOB_FINISH


@dataclass(frozen=True, slots=True)
class Caveat:
    """One thing the job did not deliver, derived from the stream."""

    kind: str
    detail: str
    context: dict[str, Any]


def derive_caveats(events: Sequence[Event]) -> Sequence[Caveat]:
    """What the job failed to do, computed rather than self-reported.

    An LLM reliably forgets to mention what it could not finish, so the
    framework works it out from the record instead (design.md D8).
    """
    caveats: list[Caveat] = []

    for e in failures(events):
        if e.get("status") in DEGRADED_STATUSES:
            caveats.append(
                Caveat(
                    kind=str(e.get("status")),
                    detail=str(e.get("headline") or f"lane {e.get('lane')} 未完成"),
                    context={
                        "lane": e.get("lane"),
                        "dispatch": e.get("id"),
                        "partial": e.get("partial"),
                        "suggest": e.get("suggest"),
                    },
                )
            )

    for e in of_type(events, THROTTLE_GAVE_UP):
        caveats.append(
            Caveat(
                kind="rate_limited",
                detail=(
                    f"backend {e.get('backend')} 持續限流，等待 "
                    f"{float(e.get('waited_s') or 0):.0f}s 後放棄"
                ),
                context={"backend": e.get("backend"), "lane": e.get("lane"),
                         "waited_s": e.get("waited_s")},
            )
        )

    answered = {e.get("qid") for e in of_type(events, ASK_ANSWER)}
    for e in of_type(events, ASK_USER):
        qid = e.get("qid")
        if qid in answered:
            continue
        caveats.append(
            Caveat(
                kind="unanswered_question",
                detail=str(e.get("text") or "未回答的提問"),
                context={"qid": qid, "default_applied": e.get("default")},
            )
        )

    routed = {e.get("payload") for e in of_type(events, PROXY_ROUTE) if e.get("lane")}
    consumed = {
        inp
        for e in of_type(events, DISPATCH_START)
        for inp in (e.get("inputs") or [])
    }
    for e in of_type(events, INGRESS):
        payload = e.get("payload")
        if payload not in routed and payload not in consumed:
            caveats.append(
                Caveat(
                    kind="unprocessed_payload",
                    detail=f"進入的資料未被任何 lane 使用：{payload}",
                    context={"payload": payload, "bytes": e.get("bytes")},
                )
            )

    return tuple(caveats)


@dataclass(frozen=True, slots=True)
class JobSummary:
    """Everything a regression assertion needs, in one object."""

    job_id: str
    finished: bool
    dispatches: int
    duplicates: int
    failures: int
    total_usd: float
    context_peak: int
    cost_by_lane: dict[str, float]
    throttle_seconds: float
    cache_hit_ratio: float
    caveats: Sequence[Caveat]


def summarize(events: Sequence[Event]) -> JobSummary:
    return JobSummary(
        job_id=events[0].job_id if events else "",
        finished=finished(events),
        dispatches=dispatch_count(events),
        duplicates=len(duplicate_dispatches(events)),
        failures=len(failures(events)),
        total_usd=round(total_cost_usd(events), 6),
        context_peak=context_peak(events),
        cost_by_lane=cost_by_lane(events),
        throttle_seconds=throttle_seconds(events),
        cache_hit_ratio=cache_hit_ratio(events),
        caveats=derive_caveats(events),
    )
