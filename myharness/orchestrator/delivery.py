"""Handing the finished job back without blowing up the caller's context.

Everything upstream protects the orchestrator; this protects whoever asked. The
report itself can be tens of thousands of tokens, and the caller is often an
agent partway through other work — so what comes back is a summary plus a menu
with prices, and the caller decides what else it wants (DESIGN.md decision #14).

Caveats are computed from the event stream rather than taken from the report,
because what a model most reliably forgets to mention is what it could not do.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from myharness.artifacts.errors import ArtifactError
from myharness.artifacts.ids import ArtifactId
from myharness.artifacts.store import ArtifactStore
from myharness.artifacts.types import GrantSet
from myharness.events.query import Caveat, summarize
from myharness.events.types import Event

#: The executive summary is truncated to this; the rest is a drill away.
MAX_SUMMARY_CHARS = 1500
MAX_KEY_FINDINGS = 5


@dataclass(frozen=True, slots=True)
class Section:
    """One drillable part of the report, with its price."""

    id: str
    title: str
    est_tokens: int

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "est_tokens": self.est_tokens}


@dataclass(frozen=True, slots=True)
class Delivery:
    """What the caller gets: a summary, a priced menu, and what went wrong."""

    job_id: str
    status: str
    executive_summary: str
    key_findings: tuple[str, ...] = ()
    confidence: str = "medium"
    sections: tuple[Section, ...] = ()
    caveats: tuple[Caveat, ...] = ()
    report_artifact: str | None = None
    cost_usd: float = 0.0
    dispatches: int = 0
    throttle_seconds: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "executive_summary": self.executive_summary,
            "key_findings": list(self.key_findings),
            "confidence": self.confidence,
            "caveats": [
                {"kind": c.kind, "detail": c.detail, **c.context} for c in self.caveats
            ],
            "sections": [s.to_dict() for s in self.sections],
            "report_artifact": self.report_artifact,
            "cost": {"usd": round(self.cost_usd, 5), "dispatches": self.dispatches,
                     "throttle_s": self.throttle_seconds},
        }


def _first_section_text(text: str, sections: Sequence[Any]) -> str:
    """The summary is the report's first section, or its opening lines."""
    if sections:
        marker = f"## {sections[0].title}"
        start = text.find(marker)
        if start >= 0:
            rest = text[start + len(marker):]
            end = rest.find("\n## ")
            return rest[: end if end >= 0 else len(rest)].strip()
    return "\n".join(text.strip().splitlines()[:12])


def _bullets(text: str, limit: int) -> tuple[str, ...]:
    found = [
        line.lstrip("-*• ").strip()
        for line in text.splitlines()
        if line.lstrip().startswith(("-", "*", "•"))
    ]
    return tuple(f for f in found if f)[:limit]


async def build_delivery(
    *,
    store: ArtifactStore,
    events: Sequence[Event],
    job_id: str,
    status: str,
    report_artifact: str | None,
    confidence: str = "medium",
) -> Delivery:
    summary_stats = summarize(events)
    base = dict(
        job_id=job_id, status=status, report_artifact=report_artifact,
        caveats=tuple(summary_stats.caveats), cost_usd=summary_stats.total_usd,
        dispatches=summary_stats.dispatches,
        throttle_seconds=summary_stats.throttle_seconds,
    )

    if not report_artifact:
        return Delivery(
            executive_summary="此 job 未產出報告。", confidence="low", **base
        )

    grants = GrantSet.unrestricted(job_id)
    try:
        aid = ArtifactId.parse(report_artifact)
        meta = await store.stat(aid, grants=grants)
    except (ValueError, ArtifactError):
        return Delivery(
            executive_summary="報告 artifact 無法讀取。", confidence="low", **base
        )

    sections = tuple(
        Section(s.id, s.title, s.est_tokens) for s in meta.sections
    )
    try:
        text = await store.read_note(
            aid, grants=grants, max_tokens=(meta.est_tokens or 0) + 1
        )
    except ArtifactError:
        text = ""

    summary = _first_section_text(text, meta.sections)[:MAX_SUMMARY_CHARS]
    findings = _bullets(text, MAX_KEY_FINDINGS)

    # A degraded lane anywhere caps the confidence of the whole delivery.
    if summary_stats.failures or summary_stats.caveats:
        confidence = "low" if confidence == "high" else confidence

    return Delivery(
        executive_summary=summary or "（報告沒有摘要章節）",
        key_findings=findings, confidence=confidence, sections=sections, **base,
    )


async def drill(
    store: ArtifactStore, job_id: str, report_artifact: str, section_id: str,
    *, max_tokens: int = 20_000,
) -> str:
    """Fetch one section of the report on demand."""
    return await store.read_note(
        ArtifactId.parse(report_artifact), grants=GrantSet.unrestricted(job_id),
        max_tokens=max_tokens, section=section_id,
    )
