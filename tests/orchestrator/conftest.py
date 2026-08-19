"""Offline scaffolding for the orchestrator tool surface."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from myharness.artifacts.local import LocalArtifactStore
from myharness.events.log import LocalEventLog
from myharness.jobs.runner import JobRunner
from myharness.jobs.spec import JobSpec
from myharness.lanes.handle import LaneHandle
from myharness.lanes.types import LaneRegistry, LaneType
from myharness.orchestrator.tools import OrchestratorTools

JOB = "j7"


def payload(result: dict) -> Any:
    return json.loads(result["content"][0]["text"])


@dataclass
class FakeLane:
    delay: float = 0.01
    artifact: str | None = "j7/note/lanes/txn/findings/1"
    calls: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.calls = []

    async def __call__(self, request, *, store, event_log) -> LaneHandle:
        import asyncio

        self.calls.append(request.dispatch_id)
        await asyncio.sleep(self.delay)
        if self.artifact:
            await store.put_note(request.job_id, self.artifact.split("/note/", 1)[1],
                                 "## 結論\n夜間高頻交易佔多數。\n\n## 方法\nduckdb 全表掃描。\n",
                                 produced_by=f"lane:{request.lane.id}")
        return LaneHandle(artifact=self.artifact or "", headline="ok", confidence="high",
                          lane=request.lane.id, dispatch_id=request.dispatch_id)


@dataclass
class Bench:
    tools: OrchestratorTools
    runner: JobRunner
    fake: FakeLane
    events: LocalEventLog
    store: LocalArtifactStore

    @property
    def h(self) -> dict[str, Any]:
        return self.tools.handlers

    async def stream(self):
        return await self.events.read(JOB)

    async def kinds(self, t: str):
        return [e for e in await self.stream() if e.t == t]

    async def declare(self, *lane_ids: str, plan: str = "# 目標\n測試\n"):
        return payload(await self.h["plan_update"]({
            "plan": plan,
            "lanes": [{"id": i, "type": "ta", "scope": f"{i} 的範圍"} for i in lane_ids],
        }))


@pytest.fixture
async def bench(tmp_path: Path, request) -> Bench:
    overrides = getattr(request, "param", {}) or {}
    store = LocalArtifactStore(tmp_path)
    await store.init_job(JOB)
    events = LocalEventLog(tmp_path)
    charter = tmp_path / "c.md"
    charter.write_text("charter", encoding="utf-8")

    registry = LaneRegistry(
        LaneType(name="ta", charter_path=charter),
        LaneType(name="synthesizer", charter_path=charter),
    )
    fake = FakeLane(delay=overrides.pop("delay", 0.01))
    runner = JobRunner(JobSpec(job_id=JOB, goal="分析交易", **overrides),
                       store=store, event_log=events, lane_runner=fake)
    tools = OrchestratorTools(runner=runner, lanes=registry)
    tools.build_server()
    return Bench(tools, runner, fake, events, store)
