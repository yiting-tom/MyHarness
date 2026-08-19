"""Data-flow anomalies: the things a line-by-line log will not show you.

Pure functions over the model, because the golden job has to be able to assert
on them (design.md D2). The fifth golden run passed every discipline check and
still delivered a report written by a dispatch that had been granted nothing —
found by a human, by luck. A lucky observation is not a line of defence.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from myharness.dataflow.model import DataFlow, EdgeKind, NodeKind


class Severity(StrEnum):
    #: The delivery may not mean what it appears to mean.
    CRITICAL = "critical"
    #: Something was wasted or is inconsistent, but the result stands.
    WARNING = "warning"


class AnomalyKind(StrEnum):
    UNGRANTED_PRODUCTION = "ungranted_production"
    OVERWRITTEN_OUTPUT = "overwritten_output"
    UNUSED_INPUT = "unused_input"
    ORPHAN_OUTPUT = "orphan_output"


@dataclass(frozen=True, slots=True)
class Anomaly:
    kind: AnomalyKind
    severity: Severity
    detail: str
    context: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"kind": str(self.kind), "severity": str(self.severity),
                "detail": self.detail, **self.context}


def detect(flow: DataFlow) -> list[Anomaly]:
    """Every anomaly in one flow, most severe first."""
    found = [
        *_ungranted_productions(flow),
        *_overwritten_outputs(flow),
        *_unused_inputs(flow),
        *_orphan_outputs(flow),
    ]
    found.sort(key=lambda a: (a.severity is not Severity.CRITICAL, str(a.kind)))
    return found


def _ungranted_productions(flow: DataFlow) -> list[Anomaly]:
    """A dispatch granted nothing that still wrote an analysis.

    It cannot have based that output on anything in the job. Either the
    orchestrator forgot the grants or the lane invented the content; both are
    reasons not to trust the result.
    """
    out = []
    for dispatch in flow.dispatches.values():
        if dispatch.granted or not dispatch.produced:
            continue
        produced = [
            a for a in dispatch.produced
            if flow.nodes.get(a) and flow.nodes[a].kind is not NodeKind.STATE
        ]
        if not produced:
            continue
        out.append(Anomaly(
            AnomalyKind.UNGRANTED_PRODUCTION, Severity.CRITICAL,
            f"{dispatch.id}（{dispatch.lane}）沒有被授權任何輸入，卻產出了 "
            f"{', '.join(produced)}",
            {"dispatch": dispatch.id, "lane": dispatch.lane, "produced": produced},
        ))
    return out


def _overwritten_outputs(flow: DataFlow) -> list[Anomaly]:
    """Two dispatches wrote the same artifact; only the later one survives."""
    out = []
    for node in flow.nodes.values():
        if not node.is_artifact:
            continue
        writers = flow.writers_of(node.id)
        if len(writers) < 2:
            continue
        winner = writers[-1]
        severity = (
            Severity.CRITICAL
            if node.kind is NodeKind.REPORT or node.id == flow.report_artifact
            else Severity.WARNING
        )
        out.append(Anomaly(
            AnomalyKind.OVERWRITTEN_OUTPUT, severity,
            f"{node.label} 被 {len(writers)} 次派工寫入"
            f"（{', '.join(w.id for w in writers)}），最終版本來自 {winner.id}",
            {"artifact": node.id, "writers": [w.id for w in writers],
             "winner": winner.id},
        ))
    return out


def _unused_inputs(flow: DataFlow) -> list[Anomaly]:
    """Raw data that entered the job and was never granted to anyone."""
    out = []
    for node in flow.of_kind(NodeKind.BLOB):
        if node.label.startswith("traces/"):
            continue  # transcripts are bookkeeping, not job input
        if flow.edges_from(node.id, EdgeKind.GRANTED):
            continue
        out.append(Anomaly(
            AnomalyKind.UNUSED_INPUT, Severity.WARNING,
            f"{node.label} 進入了 job 但從未被授權給任何 lane",
            {"artifact": node.id, "bytes": node.bytes},
        ))
    return out


def _orphan_outputs(flow: DataFlow) -> list[Anomaly]:
    """Analysis nobody read and that did not become the report.

    Lane state is excluded: it exists for the lane's own next run, so having no
    reader is its normal condition rather than a symptom.
    """
    out = []
    for node in flow.of_kind(NodeKind.FINDING):
        if node.id == flow.report_artifact:
            continue
        if flow.edges_from(node.id, EdgeKind.GRANTED):
            continue
        if not flow.writers_of(node.id):
            continue
        out.append(Anomaly(
            AnomalyKind.ORPHAN_OUTPUT, Severity.WARNING,
            f"{node.label} 被產出但沒有任何後續派工讀到它，也不是最終報告",
            {"artifact": node.id, "est_tokens": node.est_tokens},
        ))
    return out


def critical(anomalies: Sequence[Anomaly]) -> list[Anomaly]:
    return [a for a in anomalies if a.severity is Severity.CRITICAL]


def counts(anomalies: Sequence[Anomaly]) -> dict[str, int]:
    tally: dict[str, int] = {}
    for anomaly in anomalies:
        tally[str(anomaly.kind)] = tally.get(str(anomaly.kind), 0) + 1
    return tally
