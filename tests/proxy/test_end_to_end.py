"""Declare a routing table, hand over two payloads, watch them sort themselves.

The unit tests cover each seam. This one is about the loop closing: the
orchestrator's declaration reaches the classifier, two different payloads land
in two different lanes, and each decision arrives at the orchestrator as a
suggestion it still has to act on.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from myharness.lanes.types import LaneRegistry, LaneType
from myharness.mcp.service import AnalysisService
from myharness.orchestrator.tools import OrchestratorTools

TXNS = "txn_id,ts,account,amount,channel\nT001,2024-03-02,A1,1200,atm\n"
DOCS = "doc_id,holder,id_number,verified_at\nD001,陳小明,A12****89,2024-02-11\n"


class Block:
    def __init__(self, text: str) -> None:
        self.text = text


class Msg:
    def __init__(self, text: str) -> None:
        self.content = [Block(text)]
        self.total_cost_usd = 0.0003
        self.usage = {"input_tokens": 400, "output_tokens": 25}


class KeywordClassifier:
    """Stands in for the model: routes on a word in the sample.

    Deliberately not a fixed answer -- the point is that the two payloads take
    different paths, which a canned reply could not show.
    """

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def stream(self, prompt: str, options: Any):
        self.prompts.append(prompt)
        if "id_number" in prompt:
            reply = '{"lane": "kyc", "confidence": "high", "reason": "有身分證欄位"}'
        elif "amount" in prompt:
            reply = '{"lane": "txn", "confidence": "high", "reason": "有金額與通路"}'
        else:
            reply = '{"lane": null, "confidence": "low", "reason": "看不出來"}'

        async def gen():
            yield Msg(reply)

        return gen()


class FakeLoop:
    instances: list["FakeLoop"] = []

    def __init__(self, *, runner, lanes, backend):
        self.runner = runner
        self.lanes = lanes
        self.released = asyncio.Event()
        FakeLoop.instances.append(self)

    async def run(self):
        await self.released.wait()
        return "outcome"


@pytest.fixture(autouse=True)
def _reset():
    FakeLoop.instances.clear()
    yield
    for loop in FakeLoop.instances:
        loop.released.set()
    FakeLoop.instances.clear()


@pytest.fixture
async def svc(tmp_path: Path):
    charter = tmp_path / "c.md"
    charter.write_text("charter", encoding="utf-8")
    lanes = LaneRegistry(LaneType(name="ta", charter_path=charter, state_max_tokens=200))
    service = AnalysisService(
        tmp_path / "root", lanes=lanes, backend="anthropic",
        loop_factory=FakeLoop, proxy_transport=KeywordClassifier(),
    )
    try:
        yield service
    finally:
        await service.aclose()


async def test_two_payloads_sort_into_two_lanes(svc):
    job_id = (await svc.start("分析交易與 KYC 資料"))["job_id"]
    runner = svc.manager.get(job_id).runner

    # The orchestrator declares what each lane takes -- the proxy's whole view.
    tools = OrchestratorTools(runner=runner, lanes=FakeLoop.instances[0].lanes)
    tools.build_server()
    declared = json.loads((await tools.handlers["plan_update"]({
        "plan": "# 目標\n分析交易與 KYC\n",
        "lanes": [{"id": "txn", "type": "ta"}, {"id": "kyc", "type": "ta"}],
        "routing_table": [
            {"lane": "txn", "accepts": "交易明細、金流紀錄"},
            {"lane": "kyc", "accepts": "身分與 KYC 文件"},
        ],
    }))["content"][0]["text"])
    assert declared["routing_open"] == ["txn", "kyc"]
    runner.take_notices()  # clear the plan_update turn

    first = await svc.provide(job_id, TXNS, name="txn.csv")
    second = await svc.provide(job_id, DOCS, name="kyc.csv")

    assert first["routed_to"] == "txn", first
    assert second["routed_to"] == "kyc", second

    notices = runner.take_notices()
    assert len(notices) == 2
    assert "txn" in notices[0] and "kyc" in notices[1]
    assert all("建議" in n and "inputs" in n for n in notices)

    events = await svc._events.read(job_id)
    routes = {e.get("payload"): e.get("lane")
              for e in events if e.t == "proxy.route"}
    assert routes[first["artifact"]] == "txn"
    assert routes[second["artifact"]] == "kyc"


async def test_a_closed_lane_never_receives_anything(svc):
    job_id = (await svc.start("t"))["job_id"]
    runner = svc.manager.get(job_id).runner
    tools = OrchestratorTools(runner=runner, lanes=FakeLoop.instances[0].lanes)
    tools.build_server()
    await tools.handlers["plan_update"]({
        "plan": "p",
        "lanes": [{"id": "txn", "type": "ta"}],
        "routing_table": [
            {"lane": "txn", "accepts": "交易", "status": "closed"},
            {"lane": "kyc", "accepts": "身分文件"},
        ],
    })
    out = await svc.provide(job_id, TXNS, name="txn.csv")
    assert out["routed"] is False, "a closed lane was routed to"


async def test_the_classifier_never_saw_the_plan(svc):
    """The end-to-end version of the property: even with a real plan written
    to the store, none of it reaches the classifier."""
    job_id = (await svc.start("找出 2024 年的異常交易樣態"))["job_id"]
    runner = svc.manager.get(job_id).runner
    tools = OrchestratorTools(runner=runner, lanes=FakeLoop.instances[0].lanes)
    tools.build_server()
    await tools.handlers["plan_update"]({
        "plan": "# 目標\n找出 2024 年的異常交易樣態\n\n## 決策與理由\n先做通路分佈。\n",
        "lanes": [{"id": "txn", "type": "ta"}],
        "routing_table": [{"lane": "txn", "accepts": "交易明細"}],
    })
    await svc.provide(job_id, TXNS, name="txn.csv")

    prompt = svc._proxy_transport.prompts[-1]
    for leaked in ("異常樣態", "決策與理由", "先做通路分佈"):
        assert leaked not in prompt, f"{leaked!r} reached the classifier"
    assert "交易明細" in prompt, "the routing table did not reach it either"


class TestTheOrchestratorIsToldRoutingExists:
    """A feature nothing mentions is a feature nobody uses.

    The tool description explains routing_table, but a model reads the kickoff
    to decide what to do first. Without a mention there, a routing table gets
    declared only by luck -- and with no table the classifier short-circuits, so
    the whole path stays dark.
    """

    def test_the_kickoff_mentions_routing_table(self):
        from myharness.orchestrator.loop import KICKOFF

        assert "routing_table" in KICKOFF

    def test_it_says_when_to_bother(self):
        from myharness.orchestrator.loop import KICKOFF

        assert "中途還會提供資料" in KICKOFF

    def test_it_repeats_that_routing_is_not_authorisation(self):
        """The single most expensive misunderstanding available here."""
        from myharness.orchestrator.loop import KICKOFF

        assert "不會給任何 lane 讀取權限" in KICKOFF
        assert "inputs" in KICKOFF
