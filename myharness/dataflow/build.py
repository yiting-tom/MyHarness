"""Deriving the flow from the event stream.

Nothing here writes. If a fact about the data flow cannot be derived from the
existing events, that is a gap in the event types and belongs there — inventing
a second record would give the two a chance to disagree (proposal: Capabilities).
"""

from __future__ import annotations

from collections.abc import Sequence

from myharness.artifacts.types import ArtifactMeta
from myharness.events.types import (
    ARTIFACT_READ,
    DISPATCH_END,
    DISPATCH_START,
    INGRESS,
    PROXY_ROUTE,
    JOB_FINISH,
    Event,
)
from myharness.dataflow.model import (
    DataFlow,
    DispatchInfo,
    Edge,
    EdgeKind,
    Node,
    NodeKind,
)


def classify(artifact_id: str) -> NodeKind:
    """What kind of thing an artifact id names."""
    parts = artifact_id.split("/", 2)
    if len(parts) < 3:
        return NodeKind.FINDING
    kind, name = parts[1], parts[2]
    if kind == "blob":
        return NodeKind.BLOB
    if name.endswith("/state"):
        return NodeKind.STATE
    if name == "plan":
        return NodeKind.PLAN
    if name == "report" or name.endswith("/report"):
        return NodeKind.REPORT
    return NodeKind.FINDING


def short_label(artifact_id: str) -> str:
    """A name a person can read, without losing which lane it belongs to."""
    parts = artifact_id.split("/", 2)
    name = parts[2] if len(parts) >= 3 else artifact_id
    if name.startswith("lanes/"):
        segments = name.split("/")
        if len(segments) >= 4:  # lanes/<lane>/findings/<n>
            return f"{segments[1]}:{segments[-1]}"
        return "/".join(segments[1:])
    return name


def build_dataflow(
    events: Sequence[Event],
    artifacts: Sequence[ArtifactMeta] = (),
    *,
    job_id: str | None = None,
) -> DataFlow:
    """Project a data flow out of one job's events."""
    flow = DataFlow(job_id=job_id or (events[0].job_id if events else ""))

    for meta in artifacts:
        _touch(flow, str(meta.id), meta=meta)

    for event in events:
        if event.t == INGRESS:
            if payload := event.get("payload"):
                _touch(flow, str(payload))

        elif event.t == PROXY_ROUTE:
            payload, lane = event.get("payload"), event.get("lane")
            if payload and lane:
                _touch(flow, str(payload))
                flow.edges.append(
                    Edge(str(payload), _lane_node(flow, str(lane)), EdgeKind.SUGGESTED)
                )

        elif event.t == DISPATCH_START:
            _start(flow, event)

        elif event.t == DISPATCH_END:
            _end(flow, event)

        elif event.t == ARTIFACT_READ:
            flow.read_edges_available = True
            dispatch_id, artifact = event.get("dispatch"), event.get("artifact")
            if dispatch_id and artifact:
                _touch(flow, str(artifact))
                flow.edges.append(Edge(str(dispatch_id), str(artifact), EdgeKind.READ))

        elif event.t == JOB_FINISH:
            flow.finished = True
            flow.finish_reason = str(event.get("reason") or "")
            if report := event.get("report"):
                flow.report_artifact = str(report)
                _touch(flow, str(report))

    return flow


def _touch(flow: DataFlow, artifact_id: str, meta: ArtifactMeta | None = None) -> None:
    """Record an artifact node, enriching it if the index knows more."""
    existing = flow.nodes.get(artifact_id)
    if existing is not None and meta is None:
        return
    flow.nodes[artifact_id] = Node(
        id=artifact_id,
        kind=classify(artifact_id),
        label=short_label(artifact_id),
        est_tokens=meta.est_tokens if meta else (existing.est_tokens if existing else None),
        bytes=meta.bytes if meta else (existing.bytes if existing else None),
        produced_by=meta.produced_by if meta else (existing.produced_by if existing else ""),
        revision=meta.revision if meta else (existing.revision if existing else 1),
    )


def _lane_node(flow: DataFlow, lane: str) -> str:
    node_id = f"lane:{lane}"
    if node_id not in flow.nodes:
        flow.nodes[node_id] = Node(id=node_id, kind=NodeKind.LANE, label=lane)
    return node_id


def _start(flow: DataFlow, event: Event) -> None:
    dispatch_id = str(event.get("id") or "")
    if not dispatch_id:
        return
    lane = str(event.get("lane") or "")
    granted = tuple(str(i) for i in (event.get("inputs") or []))

    flow.dispatches[dispatch_id] = DispatchInfo(
        id=dispatch_id, lane=lane, task=str(event.get("task") or ""), granted=granted,
    )
    flow.nodes[dispatch_id] = Node(
        id=dispatch_id, kind=NodeKind.DISPATCH, label=f"{dispatch_id} ({lane})"
    )
    flow.edges.append(Edge(dispatch_id, _lane_node(flow, lane), EdgeKind.RAN_ON))
    for artifact in granted:
        _touch(flow, artifact)
        flow.edges.append(Edge(artifact, dispatch_id, EdgeKind.GRANTED))


def _end(flow: DataFlow, event: Event) -> None:
    dispatch_id = str(event.get("id") or "")
    current = flow.dispatches.get(dispatch_id)
    if current is None:
        # An end without a start: still worth showing rather than dropping.
        current = DispatchInfo(id=dispatch_id, lane=str(event.get("lane") or ""), task="")
        flow.nodes[dispatch_id] = Node(id=dispatch_id, kind=NodeKind.DISPATCH,
                                       label=dispatch_id)

    tokens = event.get("tokens") or {}
    artifact = event.get("artifact")
    produced = (str(artifact),) if artifact else ()

    flow.dispatches[dispatch_id] = DispatchInfo(
        id=current.id, lane=current.lane or str(event.get("lane") or ""),
        task=current.task, status=str(event.get("status") or "ok"),
        granted=current.granted, produced=produced,
        usd=float(event.get("usd") or 0.0),
        tokens_in=int(tokens.get("in") or 0),
        tokens_out=int(tokens.get("out") or 0),
        cache_read=int(tokens.get("cache_read") or 0),
        turns=int(event.get("turns") or 0),
        transcript=event.get("transcript"),
    )
    for artifact_id in produced:
        _touch(flow, artifact_id)
        flow.edges.append(Edge(dispatch_id, artifact_id, EdgeKind.PRODUCED))
