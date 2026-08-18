"""Append-only event log: the single source of truth for a job's history."""

from myharness.events.log import EventLog, LocalEventLog
from myharness.events.query import (
    Caveat,
    JobSummary,
    context_peak,
    cost_by_lane,
    derive_caveats,
    duplicate_dispatches,
    failures,
    of_type,
    summarize,
    tokens_by_lane,
    total_cost_usd,
)
from myharness.events.types import Event, MalformedEvent

__all__ = [
    "Caveat", "Event", "EventLog", "JobSummary", "LocalEventLog", "MalformedEvent",
    "context_peak", "cost_by_lane", "derive_caveats", "duplicate_dispatches",
    "failures", "of_type", "summarize", "tokens_by_lane", "total_cost_usd",
]
