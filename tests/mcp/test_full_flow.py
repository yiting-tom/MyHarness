"""One client, one analysis, start to drill -- with a scripted orchestrator.

The individual operations are covered elsewhere. What this asserts is that they
compose: the job_id from start works in poll, poll's question id works in
answer, result's section ids work in drill. Those seams are where an API stops
being usable without any single test failing.

The loop is scripted rather than real so the test is free and deterministic.
What it fakes is the model's judgement, not the plumbing: a real JobRunner, a
real store, a real event log, a real MCP session.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from myharness.events.types import DISPATCH_END, DISPATCH_START, JOB_FINISH
from myharness.jobs.channel import Question
from myharness.lanes.types import LaneRegistry, LaneType
from myharness.mcp.server import build_server
from myharness.mcp.service import AnalysisService

REPORT = """## 摘要
2024 年交易資料共 765 個帳戶，app 通路平均金額最低。

## 方法
以 duckdb_query 對授權的 blob 聚合，未將原始資料讀入 context。

## 發現
app 13,981.81 為四個通路中最低，其餘三者介於 20,612 與 21,655 之間。

## 限制
未檢視 2023 年資料。
"""


class ScriptedLoop:
    """Plays out a plausible job: dispatch, ask, dispatch, finish."""

    instances: list["ScriptedLoop"] = []

    def __init__(self, *, runner, lanes, backend):
        self.runner = runner
        self.store = runner.store
        ScriptedLoop.instances.append(self)

    async def run(self):
        job = self.runner.spec.job_id
        log = self.runner.events
        await log.append(job, DISPATCH_START, id="d1", lane="analyst", task="count")
        await log.append(job, DISPATCH_END, id="d1", lane="analyst", status="ok",
                         headline="765 個帳戶", usd=0.1)
        answer = await self.runner.channel.ask(
            Question(id="q1", text="要含 2023 年嗎？", default="否", timeout_s=30.0)
        )
        report = await self.store.put_note(
            job, "lanes/syn1/report", REPORT, produced_by="lane:syn1"
        )
        await log.append(job, DISPATCH_START, id="d2", lane="syn1", task="write")
        await log.append(job, DISPATCH_END, id="d2", lane="syn1", status="ok",
                         artifact=str(report.id), headline="報告已寫入", usd=0.1)
        await log.append(job, JOB_FINISH, report=str(report.id), phase="complete",
                         usd=0.2, dispatches=2, answered=answer.text)
        return "outcome"


@asynccontextmanager
async def connected(tmp_path: Path):
    charter = tmp_path / "c.md"
    charter.write_text("charter", encoding="utf-8")
    service = AnalysisService(
        tmp_path / "root",
        lanes=LaneRegistry(
            LaneType(name="analyst", charter_path=charter, state_max_tokens=100)
        ),
        loop_factory=ScriptedLoop,
    )
    try:
        async with create_connected_server_and_client_session(
            build_server(service)
        ) as client:
            yield client, service
    finally:
        await service.aclose()


def body(result) -> dict:
    return json.loads(result.content[0].text)


@pytest.fixture(autouse=True)
def _reset():
    ScriptedLoop.instances.clear()
    yield
    ScriptedLoop.instances.clear()


async def test_start_poll_answer_result_drill(tmp_path: Path):
    async with connected(tmp_path) as (client, service):
        started = body(await client.call_tool(
            "analysis_start", {"task": "分析 2024 年交易資料"}
        ))
        assert started["ok"]
        job_id = started["job_id"]

        # Poll until the job asks something. The ids have to survive the wire.
        question_id = None
        for _ in range(20):
            progress = body(await client.call_tool(
                "analysis_poll", {"job_id": job_id, "wait": 1.0}
            ))
            assert progress["ok"], progress
            if progress["pending_questions"]:
                question_id = progress["pending_questions"][0]["id"]
                break
        assert question_id == "q1", "the job's question never reached the client"

        answered = body(await client.call_tool(
            "analysis_answer",
            {"job_id": job_id, "question_id": question_id, "text": "否，只看 2024"},
        ))
        assert answered["ok"]

        await asyncio.wait_for(service.manager.get(job_id).task, 5.0)

        result = body(await client.call_tool("analysis_result", {"job_id": job_id}))
        assert result["ok"]
        assert "765" in json.dumps(result, ensure_ascii=False)
        section_ids = [s["id"] for s in result["sections"]]
        assert "方法" in section_ids
        # The menu is a menu: it prices the sections without spending them.
        assert "duckdb_query" not in json.dumps(result, ensure_ascii=False)

        section = body(await client.call_tool(
            "analysis_drill", {"job_id": job_id, "section_id": "方法"}
        ))
        assert section["ok"] and "duckdb_query" in section["text"]
        assert not section["truncated"]


async def test_provided_data_reaches_the_job_as_a_blob(tmp_path: Path):
    async with connected(tmp_path) as (client, service):
        job_id = body(await client.call_tool("analysis_start", {"task": "t"}))["job_id"]
        out = body(await client.call_tool(
            "analysis_provide",
            {"job_id": job_id, "payload": "a,b\n1,2\n", "name": "extra.csv"},
        ))
        assert out["ok"]
        listed = await service._store.list(job_id, kind="blob")
        assert any(a.id.name == "raw/extra.csv" for a in listed)
        assert all(a.kind == "blob" for a in listed), "provided data became a note"


async def test_the_client_never_receives_the_report_body_unasked(tmp_path: Path):
    """The property this whole layer exists for."""
    async with connected(tmp_path) as (client, service):
        job_id = body(await client.call_tool("analysis_start", {"task": "t"}))["job_id"]
        for _ in range(20):
            progress = body(await client.call_tool(
                "analysis_poll", {"job_id": job_id, "wait": 1.0}
            ))
            if progress["pending_questions"]:
                await client.call_tool(
                    "analysis_answer",
                    {"job_id": job_id, "question_id": "q1", "text": "否"},
                )
                break
        await asyncio.wait_for(service.manager.get(job_id).task, 5.0)

        seen = json.dumps(
            body(await client.call_tool("analysis_poll", {"job_id": job_id, "wait": 0})),
            ensure_ascii=False,
        ) + json.dumps(
            body(await client.call_tool("analysis_result", {"job_id": job_id})),
            ensure_ascii=False,
        )
        assert "未檢視 2023" not in seen, "a section body arrived without being asked for"
        assert len(seen) < 4_000, f"{len(seen)} chars reached the client unasked"
