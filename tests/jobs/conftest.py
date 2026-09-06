"""Offline scaffolding for the job runner.

A lane that does not call a model: it records which dispatches ran, sleeps for
a controllable span so overlap is observable, and hands back whatever handle
the test wants. Every guard in the runner -- ceilings, no-progress, wrap-up --
is about *when* work happens rather than what it produces, so the work itself
is the one part that can be faked outright.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from myharness.artifacts.local import LocalArtifactStore
from myharness.events.log import LocalEventLog
from myharness.jobs.runner import JobRunner
from myharness.jobs.spec import JobSpec
from myharness.lanes.handle import HandleStatus, LaneHandle
from myharness.lanes.types import LaneInstance, LaneType

JOB = "j7"

DEFAULT_ARTIFACT = f"{JOB}/note/lanes/a/findings/1"


def failing(status: HandleStatus, *, headline: str = "工具呼叫失敗") -> LaneHandle:
    """A handle that did not deliver. Failure is a value here too."""
    return LaneHandle(
        artifact="", headline=headline, confidence="low", status=status,
        partial=None, suggest="縮小任務範圍重派",
    )


@dataclass
class FakeLane:
    """Runs no model. Records spans so overlap can be asserted, not assumed."""

    delay: float = 0.01
    #: Artifact the lane claims to have written. None means a barren dispatch,
    #: which is what the no-progress guard is watching for.
    default_artifact: str | None = DEFAULT_ARTIFACT
    #: Per-dispatch overrides, keyed by dispatch id.
    handles: dict[str, LaneHandle] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    spans: list[tuple[float, float]] = field(default_factory=list)

    async def __call__(self, request, *, store, event_log) -> LaneHandle:
        self.calls.append(request.dispatch_id)
        started = asyncio.get_running_loop().time()
        await asyncio.sleep(self.delay)
        self.spans.append((started, asyncio.get_running_loop().time()))

        override = self.handles.get(request.dispatch_id)
        if override is not None:
            # replace, not **__dict__: LaneHandle is slots=True and has none.
            return replace(override, lane=request.lane.id,
                           dispatch_id=request.dispatch_id)

        artifact = self.default_artifact
        if artifact:
            await store.put_note(
                request.job_id, artifact.split("/note/", 1)[1],
                "## 結論\n夜間高頻交易佔多數。\n\n## 方法\nduckdb 全表掃描。\n",
                produced_by=f"lane:{request.lane.id}",
            )
        return LaneHandle(
            artifact=artifact or "", headline="ok", confidence="high",
            lane=request.lane.id, dispatch_id=request.dispatch_id,
        )

    def overlapped(self) -> bool:
        """True if any two runs were in flight at the same moment."""
        for i, (start_a, end_a) in enumerate(self.spans):
            for start_b, end_b in self.spans[i + 1:]:
                if start_a < end_b and start_b < end_a:
                    return True
        return False


@dataclass
class Bench:
    runner: JobRunner
    fake: FakeLane
    events: LocalEventLog
    store: LocalArtifactStore

    async def stream(self):
        return await self.events.read(JOB)

    async def kinds(self, t: str):
        return [e for e in await self.stream() if e.t == t]


@pytest.fixture
async def bench(tmp_path: Path, request) -> Bench:
    overrides: dict[str, Any] = dict(getattr(request, "param", {}) or {})
    store = LocalArtifactStore(tmp_path)
    await store.init_job(JOB)
    events = LocalEventLog(tmp_path)
    fake = FakeLane(delay=overrides.pop("delay", 0.01))
    runner = JobRunner(
        JobSpec(job_id=JOB, goal="分析交易", **overrides),
        store=store, event_log=events, lane_runner=fake,
    )
    # The runner refuses to dispatch to a lane nobody created, which is a
    # guard of its own -- so the bench registers the ones the tests use rather
    # than letting every case open with the same three lines.
    charter = tmp_path / "c.md"
    charter.write_text("charter", encoding="utf-8")
    lane_type = LaneType(name="ta", charter_path=charter)
    for lane_id in ("a", "b", "c"):
        runner.register_lane(LaneInstance(id=lane_id, type=lane_type,
                                          scope=f"{lane_id} 的範圍"))
    return Bench(runner, fake, events, store)


@pytest.fixture
def runner_factory(tmp_path: Path):
    """A runner whose ceilings can be poked at without running anything."""

    def build(*, spec: JobSpec | None = None, **overrides) -> JobRunner:
        return JobRunner(
            spec or JobSpec(job_id=JOB, goal="g", **overrides),
            store=LocalArtifactStore(tmp_path),
            event_log=LocalEventLog(tmp_path),
        )

    return build
