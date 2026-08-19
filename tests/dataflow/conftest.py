"""Builders for synthetic flows, plus the real golden-run fixture."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from myharness.artifacts.ids import ArtifactId
from myharness.artifacts.types import ArtifactMeta
from myharness.events.types import Event

JOB = "j7"
FIXTURES = Path(__file__).parent / "fixtures"


class Stream:
    """Fluent event-stream builder, so a test reads as the story it tells."""

    def __init__(self, job_id: str = JOB) -> None:
        self.job_id = job_id
        self.events: list[Event] = []

    def _add(self, t: str, **data) -> "Stream":
        self.events.append(
            Event(t=t, seq=len(self.events), ts=datetime.now(UTC),
                  job_id=self.job_id, data=data)
        )
        return self

    def start(self, goal: str = "分析") -> "Stream":
        return self._add("job.start", goal=goal)

    def ingress(self, payload: str, nbytes: int = 1024) -> "Stream":
        return self._add("ingress", payload=payload, bytes=nbytes)

    def dispatch(self, did: str, lane: str, inputs=(), task: str = "t") -> "Stream":
        return self._add("dispatch.start", id=did, lane=lane,
                         inputs=list(inputs), task=task)

    def done(self, did: str, lane: str, artifact: str | None = None,
             status: str = "ok", usd: float = 0.1,
             tokens: dict | None = None, turns: int = 3) -> "Stream":
        return self._add("dispatch.end", id=did, lane=lane, artifact=artifact,
                         status=status, usd=usd, turns=turns,
                         tokens=tokens or {"in": 1000, "out": 200, "cache_read": 800})

    def read(self, did: str, artifact: str) -> "Stream":
        return self._add("artifact.read", dispatch=did, artifact=artifact)

    def finish(self, report: str | None = None, reason: str = "finished") -> "Stream":
        return self._add("job.finish", report=report, reason=reason)

    def unknown(self) -> "Stream":
        return self._add("some.future.event", whatever=1)


def meta(artifact_id: str, *, est_tokens: int = 100, nbytes: int = 400,
         produced_by: str = "lane:a", revision: int = 1) -> ArtifactMeta:
    aid = ArtifactId.parse(artifact_id)
    return ArtifactMeta(
        id=aid, kind=aid.kind, bytes=nbytes, produced_by=produced_by,
        created_at=datetime.now(UTC), est_tokens=est_tokens, revision=revision,
    )


@pytest.fixture
def golden5():
    """The real fifth golden run: it passed every discipline check and still
    delivered a report from a dispatch that had been granted nothing."""
    events = [
        Event.from_json(line)
        for line in (FIXTURES / "golden5-events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rows = json.loads((FIXTURES / "golden5-artifacts.json").read_text(encoding="utf-8"))
    artifacts = [
        ArtifactMeta(
            id=ArtifactId.parse(r["id"]), kind=r["kind"], bytes=r["bytes"],
            produced_by=r["produced_by"], created_at=datetime.now(UTC),
            est_tokens=r["est_tokens"], revision=r["revision"],
        )
        for r in rows
    ]
    return events, artifacts
