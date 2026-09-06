"""Offline scaffolding for the job runner.

A lane that does not call a model: it records which dispatches ran, sleeps for
a controllable span so overlap is observable, and hands back whatever handle
the test wants. Every guard in the runner -- ceilings, no-progress, wrap-up --
is about *when* work happens rather than what it produces, so the work itself
is the one part that can be faked outright.

RECONSTRUCTED, NOT ORIGINAL
===========================
The original file was destroyed on 2026-09-06 by a careless overwrite, and
being caught by an unanchored `jobs/` in .gitignore it had never been
committed, so there was nothing to restore. What follows was rebuilt from what
test_job_runner.py requires of it. All 41 tests pass against it -- but the
tests pin the *interface* this module exposes, not its *meaning*, and the four
places below are where a wrong guess would leave a test passing while checking
something weaker than its name claims. Read them before trusting this file.

1. DEFAULT_ARTIFACT is a constant, and that has a side effect.
   JobState.record_progress dedupes on artifact id, so with one constant
   default every dispatch after the first counts as *no progress*. Three
   default dispatches leave no_progress_streak at 2 against a limit of 3 --
   passing, but one step from the threshold. Whether the original varied the
   artifact per dispatch is not recoverable. The hint pointing at "no, it was
   constant" is that test_a_productive_job_never_trips_the_futility_guard sets
   default_artifact explicitly each iteration, which would be redundant
   otherwise; the hint pointing the other way is that nothing else does.

2. overlapped() decides for itself what it means.
   test_background_tasks_actually_overlap asserts only `overlapped()`, so this
   function could return True unconditionally and the test would still pass.
   It is written here as a real pairwise interval intersection. The original
   may have counted peak concurrency instead, which would also exercise
   max_lane_concurrency -- this version does not.

3. delay defaults to 0.01, which sets how strict one test is.
   test_dispatch_returns_before_the_work_finishes asserts three dispatches
   return in under `delay`. At 10ms that is strict rather than lax, so it will
   not hide a regression -- but it may flake on a loaded machine, and a larger
   original default would have been deliberate headroom now removed.

4. Lane registration happens here, in the fixture.
   The runner refuses to dispatch to a lane nobody created, so something had
   to register a/b/c. Doing it directly means these tests no longer touch the
   plan_update path that tests/orchestrator/conftest.py uses to create lanes.

Everything else is pinned by the tests: JOB, failing()'s signature, FakeLane's
calls/delay/default_artifact/handles, Bench.stream()/kinds(), and the fixture
taking indirect params as JobSpec overrides. Getting those wrong fails loudly.

runner_factory was added afterwards for test_cost_ceiling.py and is not part
of the reconstruction.
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

#: See point 1 in the module docstring before changing this: it is constant,
#: and record_progress dedupes on artifact id.
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
        """True if any two runs were in flight at the same moment.

        Reconstructed -- see point 2 in the module docstring. The only test
        using this asserts nothing about *how* overlap is decided.
        """
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
