"""The same analysis, through both boundaries, answering the same.

Two shells over one `AnalysisService` drift by degrees: a field renamed on one
side, a section list built twice. What holds them together is that neither
builds anything -- both call `result()` and `drill_section()` and pass on what
comes back (D3). This asserts the consequence rather than the intent.

The orchestrator is scripted, reusing the MCP suite's loop so both boundaries
are measured against the same job. Everything under it is real: a real
JobRunner, a real store, a real event log.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("a2a", reason="the a2a extra is optional (D6)")

from myharness.a2a.server import RPC_PATH, build_app
from myharness.lanes.types import LaneRegistry, LaneType
from myharness.mcp.service import AnalysisService

from tests.mcp.test_full_flow import ScriptedLoop

VERSION_HEADERS = {"A2A-Version": "1.0"}


@pytest.fixture
def _reset_loops():
    ScriptedLoop.instances.clear()
    yield
    ScriptedLoop.instances.clear()


@pytest.fixture
async def finished_job(tmp_path: Path, _reset_loops):
    """One analysis, run to completion, readable by both boundaries."""
    charter = tmp_path / "c.md"
    charter.write_text("charter", encoding="utf-8")
    service = AnalysisService(
        tmp_path / "root",
        lanes=LaneRegistry(
            LaneType(name="analyst", charter_path=charter, state_max_tokens=100)
        ),
        loop_factory=ScriptedLoop,
    )
    started = await service.start("分析 2024 交易資料")
    job_id = started["job_id"]
    # The scripted loop asks one question and will not finish until it is
    # answered; the analysis is not the point here, a finished job is.
    for _ in range(200):
        await asyncio.sleep(0.01)
        progress = await service.poll(job_id, wait=0.0)
        if progress.get("ok") and (progress.get("pending_questions") or []):
            await service.answer(
                job_id, progress["pending_questions"][0]["id"], "否"
            )
        if progress.get("ok") and progress.get("state") != "running":
            break
    try:
        yield service, job_id
    finally:
        await service.aclose()


def ask(app, payload: dict) -> dict:
    from starlette.testclient import TestClient

    body = {
        "jsonrpc": "2.0", "id": 1, "method": "SendMessage",
        "params": {"message": {
            "messageId": "m1", "role": "ROLE_USER",
            "parts": [{"text": json.dumps(payload, ensure_ascii=False)}],
        }},
    }
    with TestClient(app) as client:
        response = client.post(RPC_PATH, json=body, headers=VERSION_HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def artifacts_of(answer: dict) -> list[dict]:
    return ((answer.get("result") or {}).get("task") or {}).get("artifacts") or []


async def test_both_boundaries_report_the_same_summary(finished_job):
    service, job_id = finished_job
    over_mcp = await service.result(job_id)
    assert over_mcp.get("ok"), over_mcp

    app = build_app(service, url=f"http://127.0.0.1:8973{RPC_PATH}")
    over_a2a = artifacts_of(ask(app, {"job_id": job_id}))[0]["parts"][0]["data"]

    assert over_a2a["executive_summary"] == over_mcp["executive_summary"]


async def test_both_boundaries_list_the_same_sections(finished_job):
    """Section ids are the currency of the price list: if the two boundaries
    disagree about them, a price quoted on one cannot be spent on the other."""
    service, job_id = finished_job
    over_mcp = await service.result(job_id)
    app = build_app(service, url=f"http://127.0.0.1:8973{RPC_PATH}")
    over_a2a = artifacts_of(ask(app, {"job_id": job_id}))[0]["parts"][0]["data"]

    assert ([s["id"] for s in over_a2a["sections"]]
            == [s["id"] for s in over_mcp["sections"]])


async def test_a_price_quoted_on_one_boundary_is_spendable_on_the_other(finished_job):
    service, job_id = finished_job
    over_mcp = await service.result(job_id)
    section_id = over_mcp["sections"][0]["id"]

    app = build_app(service, url=f"http://127.0.0.1:8973{RPC_PATH}")
    answer = ask(app, {"job_id": job_id, "section_id": section_id})
    body = artifacts_of(answer)[0]["parts"][0]["data"]

    direct = await service.drill_section(job_id, section_id)
    assert body["text"] == direct["text"]


async def test_neither_boundary_hands_over_the_report_unasked(finished_job):
    """The outermost gate, stated as a property of both doors."""
    service, job_id = finished_job
    over_mcp = await service.result(job_id)
    app = build_app(service, url=f"http://127.0.0.1:8973{RPC_PATH}")
    over_a2a = artifacts_of(ask(app, {"job_id": job_id}))[0]["parts"][0]["data"]

    section_bodies = await asyncio.gather(*[
        service.drill_section(job_id, s["id"]) for s in over_mcp["sections"]
    ])
    for section in section_bodies:
        assert section["text"] not in json.dumps(over_mcp, ensure_ascii=False)
        assert section["text"] not in json.dumps(over_a2a, ensure_ascii=False)
