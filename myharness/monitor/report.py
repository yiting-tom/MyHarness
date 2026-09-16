"""The report for the person who handed over the data.

`inspect --html` is for whoever is debugging the harness: transcripts, token
ledgers, estimate error. This one is for whoever supplied the data and is being
asked to trust the conclusion. They are not the same reader and the two views
deliberately do not look alike.

Everything this architecture does -- raw data never entering the layer above, a
lane reading only what it was granted, granted and actually-read kept as
separate edges -- exists so that "where did my data go" has an answer. Until
now that answer lived only in a terminal command whose output assumes the
reader knows what a dispatch is.

So the translation table below is not copy. It is the content of this module:
every internal code becomes a sentence about what happened and what it means
for the conclusion, and `tests/monitor/test_report.py` fails if any code
reaches the page untranslated.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from myharness.dataflow import AnomalyKind, DataFlow, EdgeKind, NodeKind, Severity, detect
from myharness.events.query import summarize
from myharness.events.types import JOB_START, Event
from myharness.monitor.html import document, template

#: What each data-flow anomaly means to someone who did not write the harness.
#: (headline, what it means for the conclusion)
_ANOMALY_SAYS: Final[dict[AnomalyKind, tuple[str, str]]] = {
    AnomalyKind.UNGRANTED_PRODUCTION: (
        "這段結論沒有讀到任何資料就寫出來了",
        "它不可能是根據你交出的任何一份資料寫的。如果最終報告來自這裡，"
        "那份報告不該被當成對你資料的分析。",
    ),
    AnomalyKind.OVERWRITTEN_OUTPUT: (
        "同一份文件被寫了兩次",
        "你看到的是後寫的那一份；先寫的那一份已經被蓋掉，它的內容不在報告裡。",
    ),
    AnomalyKind.UNUSED_INPUT: (
        "這份資料進來了，但沒有任何一段分析用到它",
        "報告裡的結論都不是根據它寫的。如果你預期它會被用到，這次分析漏了它。",
    ),
    AnomalyKind.ORPHAN_OUTPUT: (
        "這段分析沒有被後面的人讀到",
        "它寫出來了，但既沒有進最終報告，也沒有被後續的分析參考 —— "
        "這段工作的結果沒有用上。",
    ),
    AnomalyKind.SUGGESTION_IGNORED: (
        "系統建議過這份資料的去向，但沒有照著做",
        "分流器判斷它屬於某一段分析，實際上沒有交過去。這不一定是錯的，"
        "但值得確認是不是漏了。",
    ),
}

#: What each framework-derived caveat means. These are computed from the record
#: rather than self-reported, precisely because a model reliably forgets to
#: mention what it could not finish -- which is also why they belong on the
#: page and not only in an API field nobody calls.
_CAVEAT_SAYS: Final[dict[str, tuple[str, str]]] = {
    "budget_exceeded": (
        "有一段分析在預算用完時還沒做完",
        "它已經寫下的部分還在，但它原本要做的事沒有做完。報告裡與這一段有關的"
        "結論，涵蓋範圍比原本規劃的小。",
    ),
    "max_turns": (
        "有一段分析用完了來回次數還沒做完",
        "它停在中途。與這一段有關的結論可能不完整。",
    ),
    "tool_failure": (
        "有一段分析因為工具出錯而中止",
        "它沒有跑完。與這一段有關的結論可能不完整。",
    ),
    "no_cost_ceiling": (
        "這次執行沒有金額上限",
        "後端沒有回報成本，所以這裡列出的金額不對應實際計費。"
        "把關的是派工次數與時間上限。",
    ),
    "limit_reached": (
        "這次分析是因為撞到上限才收工的",
        "不是因為做完了。可能還有沒做的事。",
    ),
    "rate_limited": (
        "後端持續限流，等到放棄",
        "有工作因此沒有送出去。",
    ),
    "unanswered_question": (
        "系統問過你一個問題，但沒有等到回答",
        "它用了預設值繼續做。那個假設沒有經過你確認。",
    ),
    "unprocessed_payload": (
        "有一份進來的資料沒有被送進任何分析",
        "報告裡的結論不是根據它寫的。",
    ),
}

#: What each dispatch outcome means. The status code is never the only thing
#: shown -- a reader who has to look up `budget_exceeded` has not been told.
_STATUS_SAYS: Final[dict[str, str]] = {
    "ok": "完成",
    "running": "還在進行",
    "budget_exceeded": "預算用完，沒做完",
    "max_turns": "來回次數用完，沒做完",
    "tool_failure": "工具出錯，中止",
    "duplicate": "與前一次重複，沒有重跑",
    "state_rejected": "分析完成，但這條線的記憶沒更新",
}

_KIND_SAYS: Final[dict[NodeKind, str]] = {
    NodeKind.BLOB: "你交出的資料",
    NodeKind.FINDING: "一段分析",
    NodeKind.REPORT: "最終報告",
    NodeKind.STATE: "這條線的工作記憶",
    NodeKind.PLAN: "計畫",
}

_SEVERITY_RANK: Final = {"critical": 0, "warning": 1, "note": 2}

_TEMPLATE: Final = "report.html"


def _label(flow: DataFlow, artifact_id: str) -> dict[str, Any]:
    node = flow.nodes.get(artifact_id)
    kind = node.kind if node else NodeKind.FINDING
    return {
        "id": artifact_id,
        "label": (node.label if node else artifact_id) or artifact_id,
        "kind": str(kind),
        "kind_says": _KIND_SAYS.get(kind, "一份文件"),
        "est_tokens": node.est_tokens if node else None,
        "bytes": node.bytes if node else None,
    }


def build_report(
    flow: DataFlow,
    events: Sequence[Event],
    delivery: Mapping[str, Any] | None = None,
    *,
    sections: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Everything the page shows, with nothing left in harness vocabulary."""
    delivery = delivery or {}
    summary = summarize(events)
    chain = [d.id for d in flow.provenance(flow.report_artifact)] if flow.report_artifact else []
    in_chain = set(chain)

    produced = {a for d in flow.dispatches.values() for a in d.produced}
    readers: dict[str, list[str]] = {}
    for dispatch in flow.dispatches.values():
        for artifact in dispatch.granted:
            readers.setdefault(artifact, []).append(dispatch.id)

    inputs = [
        {**_label(flow, node.id),
         "read_by": readers.get(node.id, []),
         "used": bool(readers.get(node.id))}
        for node in flow.of_kind(NodeKind.BLOB)
        if node.id not in produced and "/traces/" not in node.id
    ]

    stages = [
        {
            "id": d.id,
            "lane": d.lane,
            "task": d.task,
            "status": d.status,
            "status_says": _STATUS_SAYS.get(d.status, d.status),
            "ok": d.ok,
            "reads": [_label(flow, a) for a in d.granted],
            # Granted and actually-opened are separate edges because they carry
            # information only when they disagree. For this reader that
            # disagreement is the whole question, so it is surfaced per input.
            "opened": sorted({e.dst for e in flow.edges_from(d.id, EdgeKind.READ)}),
            "writes": [_label(flow, a) for a in d.produced],
            "in_chain": d.id in in_chain,
        }
        for d in flow.dispatches.values()
    ]

    return {
        "job_id": flow.job_id,
        "goal": next((str(e.get("goal") or "") for e in events if e.t == JOB_START), ""),
        "finished": flow.finished,
        "read_edges_available": flow.read_edges_available,
        "summary": str(delivery.get("executive_summary") or ""),
        "findings": list(delivery.get("key_findings") or []),
        "confidence": str(delivery.get("confidence") or ""),
        "inputs": inputs,
        "stages": stages,
        "chain": chain,
        "report": _label(flow, flow.report_artifact) if flow.report_artifact else None,
        "concerns": _concerns(flow, events),
        "sections": [dict(s) for s in sections],
        "cost_usd": summary.total_usd,
        "dispatch_count": summary.dispatches,
    }


def _concerns(flow: DataFlow, events: Sequence[Event]) -> list[dict[str, Any]]:
    """Anomalies and caveats as one list, because the reader does not care
    which subsystem noticed."""
    out: list[dict[str, Any]] = []

    for anomaly in detect(flow):
        headline, means = _ANOMALY_SAYS.get(
            anomaly.kind, (str(anomaly.kind), "這是系統偵測到的異常。"))
        out.append({
            "level": "critical" if anomaly.severity is Severity.CRITICAL else "warning",
            "headline": headline,
            "means": means,
            "where": _where(flow, anomaly.context),
            "source": "資料流檢查",
        })

    for caveat in summarize(events).caveats:
        headline, means = _CAVEAT_SAYS.get(
            caveat.kind, (caveat.detail, "這是系統自動記下的限制。"))
        out.append({
            "level": "note" if caveat.kind == "no_cost_ceiling" else "warning",
            "headline": headline,
            "means": means,
            "where": _where(flow, caveat.context),
            "source": "系統自動蒐集",
        })

    out.sort(key=lambda c: _SEVERITY_RANK.get(str(c["level"]), 3))
    return out


def _where(flow: DataFlow, context: Mapping[str, Any]) -> str:
    """Which part of the run this is about, named the way the page names it.

    Artifact ids are the harness's own vocabulary -- a full
    ``job/note/lanes/x/findings/y`` in front of this reader is the leak this
    module exists to prevent -- so they come back as the short label the flow
    already shows.
    """
    parts: list[str] = []
    for key in ("dispatch", "lane", "artifact", "payload"):
        value = context.get(key)
        if not value:
            continue
        node = flow.nodes.get(str(value))
        parts.append(node.label if node and node.label else str(value))
    return " · ".join(dict.fromkeys(parts))


def render_report(
    flow: DataFlow,
    events: Sequence[Event],
    delivery: Mapping[str, Any] | None = None,
    *,
    sections: Sequence[Mapping[str, Any]] = (),
) -> str:
    """One provenance report as a standalone page."""
    return document(template(_TEMPLATE),
                    build_report(flow, events, delivery, sections=sections))


__all__ = ["build_report", "render_report"]
