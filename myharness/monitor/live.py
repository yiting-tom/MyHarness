"""`monitor`: what a running job is doing right now.

A job takes tens of minutes, and during that time "thinking", "waiting out a
rate limit" and "wedged" look identical from outside. Telling them apart is the
whole point: 29% of the fifth golden run's wall clock went to throttle waiting,
and nothing said so while it happened.

Polls and redraws rather than watching for filesystem events (design.md D3): the
stream is a few kilobytes of append-only JSONL, so re-reading costs nothing a
person could perceive, and inotify would buy platform differences instead.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from myharness.dataflow import DataFlow, build_dataflow, detect
from myharness.events.query import summarize
from myharness.events.types import (
    ASK_USER,
    CTX,
    HANDOFF_RESTART,
    JOB_FINISH,
    LIMIT_REACHED,
    NO_PROGRESS,
    THROTTLE_COOLDOWN,
    THROTTLE_WAIT,
    Event,
)
from myharness.monitor.render import (
    bar,
    human_duration,
    human_tokens,
    pad,
    rule,
    style,
    truncate,
)

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


@dataclass(frozen=True, slots=True)
class Activity:
    """What the job is doing, as far as the stream can tell."""

    state: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.state}{f' — {self.detail}' if self.detail else ''}"


def current_activity(events: Sequence[Event], flow: DataFlow) -> Activity:
    """Distinguish waiting from working, which is the reason this exists."""
    if flow.finished:
        return Activity("已結束", flow.finish_reason)

    tail = list(events)[-6:]
    for event in reversed(tail):
        if event.t in (THROTTLE_COOLDOWN, THROTTLE_WAIT):
            waited = event.get("seconds")
            return Activity(
                "等待限流",
                f"{event.get('backend')}"
                + (f"，已等 {human_duration(float(waited))}" if waited else ""),
            )
        if event.t == ASK_USER:
            return Activity("等待使用者回答", truncate(str(event.get("text") or ""), 44))
        if event.t == HANDOFF_RESTART:
            return Activity("交接重啟", f"context {human_tokens(event.get('used'))}")
        if event.t == LIMIT_REACHED:
            return Activity("收尾中", f"觸及 {event.get('limit')}")
        if event.t == NO_PROGRESS:
            return Activity("無進展", f"連續 {event.get('streak')} 次沒有新產出")

    running = flow.running()
    if running:
        lanes = ", ".join(sorted({d.lane for d in running}))
        return Activity(f"{len(running)} 條 lane 執行中", lanes)
    return Activity("orchestrator 思考中")


@dataclass
class LiveView:
    """Renders one frame of a running job."""

    job_id: str
    started: float = field(default_factory=time.monotonic)
    frame: int = 0

    def render(
        self, events: Sequence[Event], artifacts=(), *,
        colour: bool = True, width: int = 78,
    ) -> str:
        self.frame += 1
        flow = build_dataflow(events, artifacts, job_id=self.job_id)
        summary = summarize(events)
        activity = current_activity(events, flow)
        spin = "✓" if flow.finished else SPINNER[self.frame % len(SPINNER)]

        lines = [
            style(rule(f"job {self.job_id}", width), "bold", enabled=colour),
            f"  {spin} {style(str(activity), 'cyan' if not flow.finished else 'green', enabled=colour)}",
            "",
            f"  派工 {len(flow.dispatches)}"
            f"（執行中 {len(flow.running())}）"
            f"   成本 ${summary.total_usd:.4f}"
            f"   context {human_tokens(summary.context_peak)}"
            + (f"   限流 {human_duration(summary.throttle_seconds)}"
               if summary.throttle_seconds else ""),
        ]

        if flow.dispatches:
            lines.append("")
            for dispatch in list(flow.dispatches.values())[-8:]:
                lines.append(self._dispatch_row(dispatch, colour, width))

        anomalies = detect(flow)
        if anomalies:
            lines.append("")
            for anomaly in anomalies[:3]:
                lines.append(style(f"  ✖ {truncate(anomaly.detail, width - 6)}",
                                   "red", enabled=colour))

        if flow.finished:
            lines += ["", self._final(flow, summary, colour, width)]
        return "\n".join(lines)

    def _dispatch_row(self, dispatch, colour: bool, width: int) -> str:
        if dispatch.running:
            mark, colours = "▸", ("cyan",)
        elif dispatch.ok:
            mark, colours = "✓", ("green",)
        else:
            mark, colours = "✗", ("yellow",)
        status = style(f"{mark} {pad(dispatch.id, 4)}{pad(dispatch.lane, 9)}",
                       *colours, enabled=colour)
        detail = (dispatch.task if dispatch.running
                  else (dispatch.produced[0].split("/")[-1] if dispatch.produced
                        else dispatch.status))
        return f"  {status}{style(truncate(detail.replace(chr(10), ' '), width - 22), 'dim', enabled=colour)}"

    def _final(self, flow: DataFlow, summary, colour: bool, width: int) -> str:
        bits = [
            style(rule("完成", width), "bold", enabled=colour),
            f"  報告   {flow.report_artifact or '（無）'}",
            f"  成本   ${summary.total_usd:.4f}"
            f"   派工 {summary.dispatches}"
            f"   caveats {len(summary.caveats)}",
        ]
        if summary.cache_hit_ratio:
            bits.append(f"  cache  {bar(summary.cache_hit_ratio, 16)} "
                        f"{summary.cache_hit_ratio:.0%}")
        return "\n".join(bits)
