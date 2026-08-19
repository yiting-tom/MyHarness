"""What flowed where: nodes, edges, and the provenance of a report.

This harness deliberately hides data — blobs cannot enter a context, a lane
reads only what it was granted, a worker vanishes when it is done. Those
properties are why it fits in 196k, and the price is that when something goes
wrong nobody can see what happened.

The model here is a projection of the event stream and nothing else. Granted and
actually-read are separate edges because they carry information only when they
disagree (design.md D1): a dispatch granted nothing that still produced a report
looks, in a read-only view, exactly like one that chose not to read.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class NodeKind(StrEnum):
    BLOB = "blob"
    FINDING = "finding"
    REPORT = "report"
    STATE = "state"
    PLAN = "plan"
    LANE = "lane"
    DISPATCH = "dispatch"


class EdgeKind(StrEnum):
    #: The dispatch was permitted to read this artifact.
    GRANTED = "granted"
    #: The dispatch wrote this artifact.
    PRODUCED = "produced"
    #: The dispatch actually opened this artifact. Absent until workers emit
    #: artifact.read events -- reported as unavailable, never inferred.
    READ = "read"
    #: The dispatch ran on this lane.
    RAN_ON = "ran_on"
    #: The proxy classified this artifact as belonging to this lane. Neither an
    #: authorisation nor an action -- a suggestion the orchestrator may ignore
    #: (design.md D1, D2). Kept distinct so "suggested but never granted" is
    #: answerable.
    SUGGESTED = "suggested"


@dataclass(frozen=True, slots=True)
class Node:
    id: str
    kind: NodeKind
    label: str = ""
    est_tokens: int | None = None
    bytes: int | None = None
    produced_by: str = ""
    revision: int = 1

    @property
    def is_artifact(self) -> bool:
        return self.kind in {NodeKind.BLOB, NodeKind.FINDING, NodeKind.REPORT,
                             NodeKind.STATE, NodeKind.PLAN}


@dataclass(frozen=True, slots=True)
class Edge:
    src: str
    dst: str
    kind: EdgeKind

    def __str__(self) -> str:
        return f"{self.src} -{self.kind}-> {self.dst}"


@dataclass(frozen=True, slots=True)
class DispatchInfo:
    """One lane execution, as the flow sees it."""

    id: str
    lane: str
    task: str
    status: str = "running"
    granted: tuple[str, ...] = ()
    produced: tuple[str, ...] = ()
    usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    turns: int = 0
    transcript: str | None = None

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class DataFlow:
    """The whole picture, derived and read-only."""

    job_id: str
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    dispatches: dict[str, DispatchInfo] = field(default_factory=dict)
    report_artifact: str | None = None
    #: False until workers emit artifact.read events. The UI must say so rather
    #: than let granted stand in for read.
    read_edges_available: bool = False
    finished: bool = False
    finish_reason: str = ""

    # ---- queries ---------------------------------------------------------

    def of_kind(self, kind: NodeKind) -> list[Node]:
        return [n for n in self.nodes.values() if n.kind is kind]

    def edges_from(self, node_id: str, kind: EdgeKind | None = None) -> list[Edge]:
        return [e for e in self.edges
                if e.src == node_id and (kind is None or e.kind is kind)]

    def edges_to(self, node_id: str, kind: EdgeKind | None = None) -> list[Edge]:
        return [e for e in self.edges
                if e.dst == node_id and (kind is None or e.kind is kind)]

    def producer_of(self, artifact_id: str) -> DispatchInfo | None:
        """Which dispatch wrote this artifact last -- the version that survives."""
        writers = [e.src for e in self.edges_to(artifact_id, EdgeKind.PRODUCED)]
        return self.dispatches.get(writers[-1]) if writers else None

    def writers_of(self, artifact_id: str) -> list[DispatchInfo]:
        return [self.dispatches[e.src]
                for e in self.edges_to(artifact_id, EdgeKind.PRODUCED)
                if e.src in self.dispatches]

    def running(self) -> list[DispatchInfo]:
        return [d for d in self.dispatches.values() if d.running]

    def provenance(self, artifact_id: str) -> list[DispatchInfo]:
        """Every dispatch that contributed to this artifact, upstream first.

        Walks back through produced and granted edges, so "what was this report
        based on" has an answer rather than an assumption.
        """
        seen_artifacts: set[str] = set()
        chain: list[DispatchInfo] = []
        frontier = [artifact_id]

        while frontier:
            current = frontier.pop()
            if current in seen_artifacts:
                continue
            seen_artifacts.add(current)
            for dispatch in self.writers_of(current):
                if dispatch not in chain:
                    chain.append(dispatch)
                frontier.extend(dispatch.granted)

        chain.reverse()
        return chain

    def provenance_cost(self, artifact_id: str) -> dict[str, Any]:
        chain = self.provenance(artifact_id)
        return {
            "dispatches": [d.id for d in chain],
            "usd": round(sum(d.usd for d in chain), 6),
            "tokens_in": sum(d.tokens_in for d in chain),
            "tokens_out": sum(d.tokens_out for d in chain),
        }

    def cost_by_lane(self) -> dict[str, float]:
        totals: dict[str, float] = defaultdict(float)
        for dispatch in self.dispatches.values():
            totals[dispatch.lane] += dispatch.usd
        return {k: round(v, 6) for k, v in sorted(totals.items())}

    def tokens_by_lane(self) -> dict[str, dict[str, int]]:
        totals: dict[str, dict[str, int]] = defaultdict(
            lambda: {"in": 0, "out": 0, "cache_read": 0})
        for dispatch in self.dispatches.values():
            bucket = totals[dispatch.lane]
            bucket["in"] += dispatch.tokens_in
            bucket["out"] += dispatch.tokens_out
            bucket["cache_read"] += dispatch.cache_read
        return {k: dict(v) for k, v in sorted(totals.items())}

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "finished": self.finished,
            "finish_reason": self.finish_reason,
            "report": self.report_artifact,
            "read_edges_available": self.read_edges_available,
            "nodes": [
                {"id": n.id, "kind": str(n.kind), "label": n.label,
                 "est_tokens": n.est_tokens, "bytes": n.bytes,
                 "produced_by": n.produced_by, "revision": n.revision}
                for n in self.nodes.values()
            ],
            "edges": [{"src": e.src, "dst": e.dst, "kind": str(e.kind)}
                      for e in self.edges],
            "dispatches": [
                {"id": d.id, "lane": d.lane, "status": d.status,
                 "granted": list(d.granted), "produced": list(d.produced),
                 "usd": d.usd, "tokens_in": d.tokens_in,
                 "tokens_out": d.tokens_out, "turns": d.turns}
                for d in self.dispatches.values()
            ],
            "cost_by_lane": self.cost_by_lane(),
        }
