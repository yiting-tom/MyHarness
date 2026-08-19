"""`inspect`: what a finished job actually did.

Built to answer one question in a single screen — *what was this report based
on?* — because that is the question five golden runs were spent answering by
hand (proposal: Why).
"""

from __future__ import annotations

from collections.abc import Sequence

from myharness.dataflow import (
    Anomaly,
    DataFlow,
    DispatchInfo,
    EdgeKind,
    NodeKind,
    Severity,
    detect,
)
from myharness.events.query import JobSummary, summarize
from myharness.events.types import Event
from myharness.monitor.render import (
    bar,
    display_width,
    human_duration,
    human_tokens,
    pad,
    rule,
    style,
    truncate,
)

STATUS_STYLE = {
    "ok": ("green",), "running": ("cyan",), "duplicate": ("dim",),
}
DEFAULT_STATUS_STYLE = ("yellow",)

KIND_MARK = {
    NodeKind.BLOB: "▣", NodeKind.FINDING: "▪", NodeKind.REPORT: "★",
    NodeKind.STATE: "◇", NodeKind.PLAN: "◆",
}


def render_inspect(
    flow: DataFlow,
    events: Sequence[Event],
    *,
    colour: bool = True,
    width: int = 78,
) -> str:
    summary = summarize(events)
    anomalies = detect(flow)
    blocks = [
        _header(flow, summary, colour, width),
        _anomalies(anomalies, colour, width),
        _flow(flow, colour, width),
        _dispatch_table(flow, colour, width),
        _cost(flow, summary, colour, width),
    ]
    return "\n".join(b for b in blocks if b)


# --- sections -------------------------------------------------------------


def _header(flow: DataFlow, summary: JobSummary, colour: bool, width: int) -> str:
    state = "完成" if flow.finished else "執行中"
    lines = [
        style(rule(f"job {flow.job_id}", width), "bold", enabled=colour),
        f"  狀態      {state}"
        + (f"（{flow.finish_reason}）" if flow.finish_reason else ""),
        f"  派工      {summary.dispatches} 次"
        + (f"，{summary.failures} 次未成功" if summary.failures else "")
        + (f"，{summary.duplicates} 次重複" if summary.duplicates else ""),
        f"  成本      ${summary.total_usd:.4f}",
        f"  context   峰值 {human_tokens(summary.context_peak)}"
        + (f"，peek 用了 {human_tokens(summary.peek_tokens)}" if summary.peek_tokens else "，未使用 peek"),
    ]
    if summary.cache_hit_ratio:
        lines.append(
            f"  cache     {bar(summary.cache_hit_ratio, 16)} {summary.cache_hit_ratio:.0%}"
        )
    if summary.throttle_seconds:
        lines.append(
            f"  限流等待  {human_duration(summary.throttle_seconds)}"
        )
    if flow.report_artifact:
        cost = flow.provenance_cost(flow.report_artifact)
        lines.append(
            f"  報告      {flow.report_artifact}"
            f"（來源鏈 {len(cost['dispatches'])} 次派工，${cost['usd']:.4f}）"
        )
    return "\n".join(lines)


def _anomalies(anomalies: Sequence[Anomaly], colour: bool, width: int) -> str:
    if not anomalies:
        return style(rule("資料流異常", width), "bold", enabled=colour) + "\n" + style(
            "  無", "green", enabled=colour
        )
    lines = [style(rule("資料流異常", width), "bold", enabled=colour)]
    for anomaly in anomalies:
        marks, colours = (
            ("✖", ("red", "bold")) if anomaly.severity is Severity.CRITICAL
            else ("▲", ("yellow",))
        )
        tag = style(f"  {marks} {anomaly.severity.upper():<8}", *colours, enabled=colour)
        lines.append(f"{tag} {anomaly.detail}")
    return "\n".join(lines)


def _flow(flow: DataFlow, colour: bool, width: int) -> str:
    """Raw data → dispatch → output, with the grants on the arrows."""
    lines = [style(rule("資料流向", width), "bold", enabled=colour)]
    if not flow.read_edges_available:
        lines.append(style(
            "  （實際讀取資訊不可得 —— worker 尚未記錄 artifact.read 事件；"
            "以下顯示的是授權）", "dim", enabled=colour))

    for dispatch in flow.dispatches.values():
        lines.append("")
        lines.append(_dispatch_line(flow, dispatch, colour, width))
        granted = list(dispatch.granted)
        produced = list(dispatch.produced)
        for i, artifact in enumerate(granted):
            last = (i == len(granted) - 1) and not produced
            lines.append(
                "  " + ("└─" if last else "├─")
                + style(" ←授權 ", "dim", enabled=colour)
                + _artifact_label(flow, artifact, colour, width - 16)
            )
        for i, artifact in enumerate(produced):
            lines.append(
                "  " + ("└─" if i == len(produced) - 1 else "├─")
                + style(" →產出 ", "cyan", enabled=colour)
                + _artifact_label(flow, artifact, colour, width - 16)
            )
        if not granted:
            lines.append("  " + style("   ←授權 （無）", "red", enabled=colour))
    return "\n".join(lines)


def _dispatch_line(flow: DataFlow, d: DispatchInfo, colour: bool, width: int) -> str:
    marks = style(d.status, *STATUS_STYLE.get(d.status, DEFAULT_STATUS_STYLE),
                  enabled=colour)
    head = style(f"{d.id} · {d.lane}", "bold", enabled=colour)
    task = truncate(d.task.replace("\n", " "), max(20, width - 34))
    return f"{head}  [{marks}]  {style(task, 'dim', enabled=colour)}"


def _artifact_label(flow: DataFlow, artifact_id: str, colour: bool, width: int) -> str:
    node = flow.nodes.get(artifact_id)
    if node is None:
        return artifact_id
    mark = KIND_MARK.get(node.kind, "·")
    size = (f" {human_tokens(node.est_tokens)}t" if node.est_tokens
            else f" {node.bytes:,}B" if node.bytes else "")
    label = truncate(node.label, width)
    return f"{mark} {label}{style(size, 'dim', enabled=colour)}"


def _dispatch_table(flow: DataFlow, colour: bool, width: int) -> str:
    if not flow.dispatches:
        return ""
    lines = [style(rule("派工明細", width), "bold", enabled=colour)]
    header = (f"  {pad('id', 5)}{pad('lane', 9)}{pad('status', 12)}"
              f"{pad('in', 8, 'right')}{pad('out', 8, 'right')}"
              f"{pad('cache', 8, 'right')}{pad('usd', 9, 'right')}")
    lines.append(style(header, "dim", enabled=colour))
    for d in flow.dispatches.values():
        lines.append(
            f"  {pad(d.id, 5)}{pad(d.lane, 9)}"
            f"{pad(style(d.status, *STATUS_STYLE.get(d.status, DEFAULT_STATUS_STYLE), enabled=colour), 12)}"
            f"{pad(human_tokens(d.tokens_in), 8, 'right')}"
            f"{pad(human_tokens(d.tokens_out), 8, 'right')}"
            f"{pad(human_tokens(d.cache_read), 8, 'right')}"
            f"{pad(f'${d.usd:.4f}', 9, 'right')}"
        )
    return "\n".join(lines)


def _cost(flow: DataFlow, summary: JobSummary, colour: bool, width: int) -> str:
    by_lane = flow.cost_by_lane()
    if not by_lane:
        return ""
    total = sum(by_lane.values()) or 1.0
    tokens = flow.tokens_by_lane()
    lines = [style(rule("成本歸屬", width), "bold", enabled=colour)]
    for lane, usd in sorted(by_lane.items(), key=lambda kv: -kv[1]):
        share = usd / total
        counts = tokens.get(lane, {})
        lines.append(
            f"  {pad(lane, 10)}{bar(share, 18)} {pad(f'${usd:.4f}', 9, 'right')}"
            + style(f"  in {human_tokens(counts.get('in'))}"
                    f" · out {human_tokens(counts.get('out'))}", "dim", enabled=colour)
        )
    if summary.caveats:
        lines.append("")
        lines.append(style("  交付的已知限制", "bold", enabled=colour))
        for caveat in summary.caveats:
            lines.append(f"    · {truncate(caveat.detail, width - 6)}")
    return "\n".join(lines)
