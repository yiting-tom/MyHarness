"""Append-only event log: the single source of truth for a job's history."""

from myharness.events.log import EventLog, LocalEventLog
from myharness.events.query import (
    Caveat,
    JobSummary,
    cache_hit_ratio,
    context_peak,
    cost_by_lane,
    derive_caveats,
    duplicate_dispatches,
    failures,
    finish_reason,
    of_type,
    peek_tokens_spent,
    summarize,
    throttle_seconds,
    throttled_backends,
    tokens_by_lane,
    total_cost_usd,
)
from myharness.events.types import Event, MalformedEvent

__all__ = [
    "Caveat", "Event", "EventLog", "JobSummary", "LocalEventLog", "MalformedEvent",
    "cache_hit_ratio", "context_peak", "cost_by_lane", "derive_caveats", "duplicate_dispatches",
    "failures", "finish_reason", "of_type", "peek_tokens_spent", "summarize", "throttle_seconds", "throttled_backends",
    "tokens_by_lane", "total_cost_usd",
]
