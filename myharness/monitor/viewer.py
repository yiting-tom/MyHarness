"""The third render: the same flow and the same anomalies, in a browser.

`render.py` explains why this layer has no TUI framework -- its value is in
projecting the facts correctly, and a dependency buys appearance rather than
information. The same argument decides this file's shape: one self-contained
page, no chart library, no font host, nothing fetched at open time. What it
buys that ASCII cannot is the one thing the terminal view has no room for --
the turn-by-turn trace beside the flow that produced it, so "what was this
report based on" and "how was that step actually made" are answerable without
changing tools.

Everything here is a projection of `DataFlow`, the event stream and the stored
transcripts. If this render ever disagrees with `inspect` or with `--json`,
that is a defect in this file, not a feature of it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any, Final

from myharness.dataflow import DataFlow, EdgeKind, NodeKind, detect
from myharness.events.query import summarize
from myharness.events.types import (
    CTX,
    DISPATCH_END,
    DISPATCH_START,
    JOB_START,
    Event,
)
from myharness.monitor.html import document, template
from myharness.monitor.trace import Trace

#: The same marks the terminal view uses. Someone who reads `inspect` every day
#: should not have to translate between two symbol sets to read this.
_GLYPH: Final = {
    NodeKind.BLOB: "▣", NodeKind.FINDING: "▪", NodeKind.REPORT: "★",
    NodeKind.STATE: "◇", NodeKind.PLAN: "◆", NodeKind.LANE: "▸",
}

_TEMPLATE: Final = "viewer.html"


def render_html(
    flow: DataFlow,
    events: Sequence[Event],
    traces: Mapping[str, Trace],
) -> str:
    """One job as a standalone page. Writes nothing, fetches nothing."""
    return document(template(_TEMPLATE), _payload(flow, events, traces))


# --- payload --------------------------------------------------------------


def _payload(
    flow: DataFlow,
    events: Sequence[Event],
    traces: Mapping[str, Trace],
) -> dict[str, Any]:
    summary = summarize(events)
    extra = _per_dispatch(events)

    return {
        "job_id": flow.job_id,
        "goal": next((str(e.get("goal") or "") for e in events if e.t == JOB_START), ""),
        "finished": flow.finished,
        "finish_reason": flow.finish_reason,
        "report": flow.report_artifact,
        "read_edges_available": flow.read_edges_available,
        "usd": summary.total_usd,
        "context_peak": summary.context_peak,
        "dispatch_count": summary.dispatches,
        "failures": summary.failures,
        "nodes": [
            {"id": n.id, "kind": str(n.kind), "glyph": _GLYPH.get(n.kind, "·"),
             "label": n.label, "est_tokens": n.est_tokens, "bytes": n.bytes}
            for n in flow.nodes.values()
        ],
        "anomalies": [a.to_dict() for a in detect(flow)],
        "dispatches": [
            {
                "id": d.id, "lane": d.lane, "status": d.status,
                "granted": list(d.granted), "produced": list(d.produced),
                "read": sorted({e.dst for e in flow.edges_from(d.id, EdgeKind.READ)}),
                "usd": d.usd, "estimated": d.tokens_estimated,
                "turns": d.turns,
                **extra.get(d.id, {}),
            }
            for d in flow.dispatches.values()
        ],
        "traces": {k: _trace_json(v) for k, v in traces.items()},
    }


def _per_dispatch(events: Sequence[Event]) -> dict[str, dict[str, Any]]:
    """Figures the flow model does not carry, straight off the event stream.

    The lane's budget is in no event. Its spend and its share of that budget
    both are, and the one divides into the other -- the ``ctx`` row follows its
    own ``dispatch.end`` immediately, which is the only thing tying them.
    """
    out: dict[str, dict[str, Any]] = {}
    last = ""
    for event in events:
        if event.t == DISPATCH_START:
            out.setdefault(str(event.get("id") or ""), {}).update(
                task=str(event.get("task") or ""),
                model=str(event.get("model") or ""),
                backend=str(event.get("backend") or ""),
                contract=str(event.get("contract_path") or ""),
            )
        elif event.t == DISPATCH_END:
            last = str(event.get("id") or "")
            out.setdefault(last, {}).update(
                tokens=event.get("tokens") or {},
                estimate=event.get("estimate") or {},
            )
        elif event.t == CTX and str(event.get("who") or "").startswith("lane:"):
            spent, pct = event.get("spent"), event.get("pct")
            if last and spent and pct:
                out[last].update(spent=spent, pct=pct, budget=round(spent / pct))
    return out


def _trace_json(trace: Trace) -> dict[str, Any]:
    return {
        "turns": trace.turns,
        "attempts": trace.attempts,
        "unpaired_results": trace.unpaired_results,
        "steps": [
            {**asdict(step), "kind": str(step.kind),
             "empty_reasoning": step.empty_reasoning}
            for step in trace.steps
        ],
    }


__all__ = ["render_html"]
