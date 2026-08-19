"""The six operations, against a real store and event log, with a fake loop.

No model and no stdio session: every rule these tests care about -- the bounds,
the refusals, and the distinction between "not running here" and "no such job"
-- is a property of the service, and mixing a protocol into the test would only
make the failures harder to read.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from myharness.events.types import DISPATCH_END, DISPATCH_START, JOB_FINISH
from myharness.jobs.channel import Question
from myharness.lanes.types import LaneRegistry, LaneType
from myharness.mcp.service import AnalysisService


class FakeLoop:
    """Stands in for OrchestratorLoop. Driven by the test, not by a model."""

    instances: list[FakeLoop] = []

    def __init__(self, *, runner, lanes, backend):
        self.runner = runner
        self.released = asyncio.Event()
        self.crash: BaseException | None = None
        self.report: str | None = None
        FakeLoop.instances.append(self)

    async def run(self):
        await self.released.wait()
        if self.crash is not None:
            raise self.crash
        if self.report:
            await self.runner.events.append(
                self.runner.spec.job_id, JOB_FINISH,
                report=self.report, phase="complete", usd=0.1, dispatches=1,
            )
        return "outcome"


@pytest.fixture(autouse=True)
def _reset():
    FakeLoop.instances.clear()
    yield
    FakeLoop.instances.clear()


@pytest.fixture
async def service(tmp_path: Path):
    charter = tmp_path / "c.md"
    charter.write_text("charter", encoding="utf-8")
    lanes = LaneRegistry(
        LaneType(name="analyst", charter_path=charter, state_max_tokens=100)
    )
    svc = AnalysisService(tmp_path / "root", lanes=lanes, loop_factory=FakeLoop)
    try:
        yield svc
    finally:
        for loop in FakeLoop.instances:
            loop.released.set()
        await svc.aclose()


def loop_of(job_id: str) -> FakeLoop:
    return next(x for x in FakeLoop.instances if x.runner.spec.job_id == job_id)


class TestStart:
    async def test_returns_before_the_analysis_finishes(self, service):
        out = await service.start("分析 2024 年交易")
        assert out["ok"] and out["job_id"]
        assert out["state"] == "running"
        assert loop_of(out["job_id"]).runner is not None

    async def test_an_empty_task_is_refused(self, service):
        out = await service.start("   ")
        assert not out["ok"] and out["error"] == "empty_task"

    async def test_capacity_is_enforced_with_a_useful_message(self, tmp_path: Path):
        charter = tmp_path / "c.md"
        charter.write_text("c", encoding="utf-8")
        lanes = LaneRegistry(
            LaneType(name="a", charter_path=charter, state_max_tokens=10)
        )
        from myharness.mcp.manager import JobManager

        svc = AnalysisService(tmp_path / "r", lanes=lanes, loop_factory=FakeLoop,
                              manager=JobManager(max_concurrent=1))
        try:
            first = await svc.start("one")
            await asyncio.sleep(0)
            out = await svc.start("two")
            assert not out["ok"] and out["error"] == "at_capacity"
            assert out["limit"] == 1 and first["job_id"] in out["running"]
        finally:
            for loop in FakeLoop.instances:
                loop.released.set()
            await svc.aclose()

    async def test_a_crash_is_reachable_through_poll(self, service):
        out = await service.start("crash me")
        job_id = out["job_id"]
        loop = loop_of(job_id)
        loop.crash = RuntimeError("model exploded")
        loop.released.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        progress = await service.poll(job_id, wait=0.0)
        assert progress["state"] == "failed"
        assert "model exploded" in progress["note"]


class TestPoll:
    async def test_returns_when_something_happens(self, service):
        job_id = (await service.start("t"))["job_id"]
        runner = loop_of(job_id).runner
        waiter = asyncio.create_task(service.poll(job_id, wait=30.0))
        await asyncio.sleep(0)
        await runner.events.append(job_id, DISPATCH_START, id="d1", lane="analyst")
        out = await asyncio.wait_for(waiter, 2.0)
        assert out["ok"] and out["dispatches"] >= 0
        assert any("dispatch.start" in line for line in out["recent"])

    async def test_a_quiet_wait_times_out_without_an_error(self, service):
        job_id = (await service.start("t"))["job_id"]
        out = await service.poll(job_id, wait=0.05)
        assert out["ok"] and out["state"] == "running"

    async def test_the_wait_is_capped(self, service):
        from myharness.mcp.service import MAX_POLL_WAIT_S

        job_id = (await service.start("t"))["job_id"]
        started = asyncio.get_running_loop().time()
        await service.poll(job_id, wait=0.01)
        assert asyncio.get_running_loop().time() - started < MAX_POLL_WAIT_S

    async def test_a_change_between_polls_is_not_missed(self, service):
        job_id = (await service.start("t"))["job_id"]
        first = await service.poll(job_id, wait=0.0)
        runner = loop_of(job_id).runner
        await runner.events.append(job_id, DISPATCH_START, id="d1", lane="a")
        out = await service.poll(job_id, wait=0.05, since=first["revision"])
        assert out["revision"] > first["revision"]

    async def test_ctx_alone_does_not_end_the_wait(self, service):
        from myharness.events.types import CTX

        job_id = (await service.start("t"))["job_id"]
        runner = loop_of(job_id).runner
        waiter = asyncio.create_task(service.poll(job_id, wait=0.3))
        await asyncio.sleep(0)
        await runner.events.append(job_id, CTX, who="orchestrator", used=1)
        out = await asyncio.wait_for(waiter, 2.0)
        assert out["ok"]

    async def test_an_unknown_job_says_so(self, service):
        out = await service.poll("nope", wait=0.0)
        assert not out["ok"] and out["error"] == "no_such_job"

    async def test_pending_questions_reach_the_client(self, service):
        job_id = (await service.start("t"))["job_id"]
        channel = service.manager.get(job_id).channel
        asyncio.create_task(channel.ask(Question(id="q1", text="要含 2023 嗎？")))
        await asyncio.sleep(0)
        out = await service.poll(job_id, wait=0.05)
        assert [q["id"] for q in out["pending_questions"]] == ["q1"]


class TestAnswer:
    async def test_an_answer_reaches_the_job(self, service):
        job_id = (await service.start("t"))["job_id"]
        channel = service.manager.get(job_id).channel
        asking = asyncio.create_task(channel.ask(Question(id="q1", text="?")))
        await asyncio.sleep(0)
        out = await service.answer(job_id, "q1", "是")
        assert out["ok"]
        assert (await asyncio.wait_for(asking, 2.0)).text == "是"

    async def test_an_unknown_question_is_refused_with_the_real_ids(self, service):
        job_id = (await service.start("t"))["job_id"]
        channel = service.manager.get(job_id).channel
        asyncio.create_task(channel.ask(Question(id="q1", text="?")))
        await asyncio.sleep(0)
        out = await service.answer(job_id, "q9", "x")
        assert not out["ok"] and out["error"] == "unknown_question"
        assert out["pending"] == ["q1"]

    async def test_answering_an_unknown_job(self, service):
        out = await service.answer("nope", "q1", "x")
        assert not out["ok"] and out["error"] == "no_such_job"


class TestProvide:
    async def test_data_becomes_a_blob_and_the_content_does_not_come_back(
        self, service
    ):
        job_id = (await service.start("t"))["job_id"]
        out = await service.provide(job_id, "a,b\n1,2\n", name="extra.csv")
        assert out["ok"] and out["artifact"].endswith("raw/extra.csv")
        assert "1,2" not in str(out)

    async def test_the_absence_of_routing_is_stated(self, service):
        """Silence would let a client assume the data reached a lane."""
        job_id = (await service.start("t"))["job_id"]
        out = await service.provide(job_id, "x,y\n1,2\n")
        assert out["routed"] is False
        assert "not routed" in out["note"]

    async def test_an_empty_payload_is_refused(self, service):
        job_id = (await service.start("t"))["job_id"]
        out = await service.provide(job_id, "")
        assert not out["ok"] and out["error"] == "empty_payload"

    async def test_an_illegal_name_is_refused(self, service):
        job_id = (await service.start("t"))["job_id"]
        out = await service.provide(job_id, "x", name="../escape")
        assert not out["ok"] and out["error"] == "bad_name"

    async def test_providing_to_an_unknown_job(self, service):
        out = await service.provide("nope", "x")
        assert not out["ok"] and out["error"] == "no_such_job"


class TestResultsOutliveTheProcess:
    """design.md D4: result and drill are projections of the event log and the
    store, both on disk. A restart loses the running task, not the answer.

    Written as tests rather than left as a happy accident, because the tempting
    optimisation -- keeping the delivery in memory next to the handle -- would
    break it silently.
    """

    async def _finished_job(self, service, tmp_path: Path) -> str:
        job_id = (await service.start("分析"))["job_id"]
        loop = loop_of(job_id)
        report = await service._store.put_note(
            job_id, "report",
            "## 摘要\n共 765 個帳戶。\n\n## 方法\n以 SQL 聚合。\n\n## 限制\n無。",
            produced_by="lane:syn1",
        )
        loop.report = str(report.id)
        loop.released.set()
        await service.manager.get(job_id).task
        return job_id

    async def test_result_is_a_priced_menu(self, service, tmp_path: Path):
        job_id = await self._finished_job(service, tmp_path)
        out = await service.result(job_id)
        assert out["ok"]
        ids = [s["id"] for s in out["sections"]]
        assert ids and all(s["est_tokens"] > 0 for s in out["sections"])
        assert "analysis_drill" in out["hint"]
        assert "以 SQL 聚合" not in str(out), "the menu must not include the body"

    async def test_result_works_from_a_service_that_never_ran_the_job(
        self, service, tmp_path: Path
    ):
        job_id = await self._finished_job(service, tmp_path)
        charter = tmp_path / "c.md"
        lanes = LaneRegistry(
            LaneType(name="analyst", charter_path=charter, state_max_tokens=100)
        )
        fresh = AnalysisService(tmp_path / "root", lanes=lanes, loop_factory=FakeLoop)
        out = await fresh.result(job_id)
        assert out["ok"] and out["sections"]

    async def test_drill_returns_one_section(self, service, tmp_path: Path):
        job_id = await self._finished_job(service, tmp_path)
        menu = await service.result(job_id)
        section = menu["sections"][0]["id"]
        out = await service.drill_section(job_id, section)
        assert out["ok"] and out["text"] and not out["truncated"]

    async def test_drill_on_an_unknown_section_points_at_the_menu(
        self, service, tmp_path: Path
    ):
        job_id = await self._finished_job(service, tmp_path)
        out = await service.drill_section(job_id, "not-a-section")
        assert not out["ok"] and out["error"] == "no_such_section"
        assert "analysis_result" in out["hint"]

    async def test_a_running_job_has_no_result_yet(self, service):
        job_id = (await service.start("t"))["job_id"]
        out = await service.result(job_id)
        assert not out["ok"] and out["error"] == "not_finished"

    async def test_result_for_an_unknown_job(self, service):
        out = await service.result("nope")
        assert not out["ok"] and out["error"] == "no_such_job"

    async def test_not_running_is_distinct_from_no_such_job(
        self, service, tmp_path: Path
    ):
        """The client's next move differs: one is readable, the other a mistake."""
        job_id = await self._finished_job(service, tmp_path)
        charter = tmp_path / "c.md"
        lanes = LaneRegistry(
            LaneType(name="analyst", charter_path=charter, state_max_tokens=100)
        )
        fresh = AnalysisService(tmp_path / "root", lanes=lanes, loop_factory=FakeLoop)

        known = await fresh.poll(job_id, wait=0.0)
        assert not known["ok"] and known["error"] == "not_running"
        assert "analysis_result" in known["message"]

        unknown = await fresh.poll("jobdoesnotexist", wait=0.0)
        assert not unknown["ok"] and unknown["error"] == "no_such_job"
