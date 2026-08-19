"""The routing table: how the orchestrator steers the proxy without talking to it.

DESIGN §4.2 calls this "declarative data" and says the two share no context.
That is the whole point -- the proxy is a single-shot classifier, and the moment
it can see the plan it becomes a second planner with none of the budget controls
the first one has (design.md D3).

Stored as a note artifact beside the plan for the same reason the plan is: it
inherits revisions, the pre-read token check, and the event trail, instead of
inventing a second kind of state.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from myharness.artifacts.errors import ArtifactError
from myharness.artifacts.ids import ArtifactId
from myharness.artifacts.store import ArtifactStore
from myharness.artifacts.types import GrantSet

ROUTING_NAME = "routing"
ROUTING_MAX_TOKENS = 4_000

#: A table longer than this is not a routing table, it is a plan.
MAX_ENTRIES = 30
MAX_ACCEPTS_CHARS = 200


class LaneStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class RoutingError(ValueError):
    """A table the orchestrator wrote that cannot be used."""


@dataclass(frozen=True, slots=True)
class RoutingEntry:
    """One lane and the kind of data it accepts."""

    lane: str
    accepts: str
    status: LaneStatus = LaneStatus.OPEN

    @property
    def open(self) -> bool:
        return self.status is LaneStatus.OPEN

    def to_dict(self) -> dict[str, Any]:
        return {"lane": self.lane, "accepts": self.accepts, "status": str(self.status)}

    @classmethod
    def from_dict(cls, raw: Any) -> RoutingEntry:
        if not isinstance(raw, dict):
            raise RoutingError(f"routing entries must be objects, got {raw!r}")
        lane = str(raw.get("lane", "")).strip()
        if not lane:
            raise RoutingError(f"routing entry has no lane: {raw!r}")
        accepts = str(raw.get("accepts", "")).strip()
        if not accepts:
            raise RoutingError(
                f"lane {lane!r} has no `accepts`. Without a description of what "
                "it takes, nothing can be routed to it."
            )
        raw_status = str(raw.get("status", LaneStatus.OPEN)).strip().lower()
        try:
            status = LaneStatus(raw_status)
        except ValueError:
            raise RoutingError(
                f"lane {lane!r} has status {raw_status!r}; expected "
                f"{' or '.join(s.value for s in LaneStatus)}"
            ) from None
        return cls(lane=lane, accepts=accepts[:MAX_ACCEPTS_CHARS], status=status)


@dataclass(frozen=True, slots=True)
class RoutingTable:
    """What the proxy is allowed to know."""

    entries: tuple[RoutingEntry, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.entries)

    @property
    def open_entries(self) -> tuple[RoutingEntry, ...]:
        return tuple(e for e in self.entries if e.open)

    def get(self, lane: str) -> RoutingEntry | None:
        for entry in self.entries:
            if entry.lane == lane:
                return entry
        return None

    def accepts_from(self, lane: str) -> bool:
        """Whether ``lane`` may receive routed data right now."""
        entry = self.get(lane)
        return entry is not None and entry.open

    def to_json(self) -> str:
        return json.dumps([e.to_dict() for e in self.entries], ensure_ascii=False, indent=2)

    def describe(self) -> str:
        """The proxy's whole view of the job. Nothing else reaches it."""
        if not self.open_entries:
            return "(no open lanes)"
        return "\n".join(f"- {e.lane}: {e.accepts}" for e in self.open_entries)

    @classmethod
    def from_raw(cls, raw: Any) -> RoutingTable:
        """Parse what the orchestrator passed, refusing rather than dropping.

        A silently discarded entry is a lane that never receives anything and
        no message saying why -- the same shape of failure as the fourth golden
        run's mangled inputs.
        """
        if raw is None:
            return cls()
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RoutingError(f"routing_table is not valid JSON: {exc}") from None
        if not isinstance(raw, list):
            raise RoutingError(
                f"routing_table must be a list of objects, got {type(raw).__name__}"
            )
        if len(raw) > MAX_ENTRIES:
            raise RoutingError(
                f"routing_table has {len(raw)} entries; the limit is {MAX_ENTRIES}. "
                "A table this long is a plan, not a routing table."
            )
        entries = tuple(RoutingEntry.from_dict(item) for item in raw)
        seen: set[str] = set()
        for entry in entries:
            if entry.lane in seen:
                raise RoutingError(f"lane {entry.lane!r} appears twice")
            seen.add(entry.lane)
        return cls(entries)

    @classmethod
    def from_entries(cls, entries: Iterable[RoutingEntry]) -> RoutingTable:
        return cls(tuple(entries))


def routing_id(job_id: str) -> ArtifactId:
    return ArtifactId(job_id, "note", ROUTING_NAME)


async def read_routing(store: ArtifactStore, job_id: str) -> RoutingTable:
    """The current table, or an empty one. Never raises for absence."""
    aid = routing_id(job_id)
    grants = GrantSet.unrestricted(job_id)
    try:
        text = await store.read_note(aid, grants=grants, max_tokens=ROUTING_MAX_TOKENS)
    except ArtifactError:
        return RoutingTable()
    try:
        return RoutingTable.from_raw(json.loads(text))
    except (json.JSONDecodeError, RoutingError):
        # A corrupt table must not stop ingress; it degrades to "no table"
        # (design.md D5).
        return RoutingTable()


async def write_routing(
    store: ArtifactStore, job_id: str, table: RoutingTable,
    *, produced_by: str = "orchestrator",
) -> int:
    meta = await store.put_note(job_id, ROUTING_NAME, table.to_json(),
                                produced_by=produced_by)
    return meta.revision


__all__ = [
    "MAX_ACCEPTS_CHARS",
    "MAX_ENTRIES",
    "ROUTING_NAME",
    "LaneStatus",
    "RoutingEntry",
    "RoutingError",
    "RoutingTable",
    "read_routing",
    "routing_id",
    "write_routing",
]
