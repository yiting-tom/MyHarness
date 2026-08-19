"""Ingress with the proxy in the path.

Two properties dominate: the blob lands whatever the classifier does, and a
routing decision is a suggestion -- it neither dispatches nor grants. Both are
the kind of thing that is true today and quietly stops being true later, so
they are asserted rather than assumed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from myharness.lanes.types import LaneRegistry, LaneType
from myharness.mcp.service import AnalysisService
from myharness.orchestrator.routing import RoutingTable, write_routing
from myharness.proxy.classify import Unrouted

CSV = "txn_id,ts,account,amount\nT001,2024-01-01,A1,100\nT002,2024-01-02,A2,250\n"


class Block:
    def __init__(self, text: str) -> None:
        self.text = text


class Msg:
    def __init__(self, text: str) -> None:
        self.content = [Block(text)]
        self.total_cost_usd = 0.0002
        self.usage = {"input_tokens": 420, "output_tokens": 26}


class FakeTransport:
    def __init__(self, text: str = '{"lane": "txn", "confidence": "high", '
                                   '"reason": "欄位是交易明細"}',
                 *, raises: BaseException | None = None) -> None:
        self._text, self._raises = text, raises
        self.prompts: list[str] = []

    def stream(self, prompt: str, options: Any):
        self.prompts.append(prompt)
        text, raises = self._text, self._raises

        async def gen():
            if raises is not None:
                raise raises
            yield Msg(text)

        return gen()


class FakeLoop:
    instances: list["FakeLoop"] = []

    def __init__(self, *, runner, lanes, backend):
        self.runner = runner
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
async def bench(tmp_path: Path):
    charter = tmp_path / "c.md"
    charter.write_text("charter", encoding="utf-8")
    lanes = LaneRegistry(LaneType(name="ta", charter_path=charter, state_max_tokens=100))
    transport = FakeTransport()
    svc = AnalysisService(tmp_path / "root", lanes=lanes, backend="anthropic",
                          loop_factory=FakeLoop, proxy_transport=transport)
    try:
        yield svc, transport
    finally:
        await svc.aclose()


async def with_table(svc, job_id: str, *entries) -> None:
    table = RoutingTable.from_raw(list(entries) or [
        {"lane": "txn", "accepts": "交易明細"},
        {"lane": "kyc", "accepts": "身分文件"},
    ])
    await write_routing(svc._store, job_id, table)


def notices_of(svc, job_id: str) -> list[str]:
    return svc.manager.get(job_id).runner.take_notices()


class TestRoutingHappens:
    async def test_a_matching_payload_is_routed(self, bench):
        svc, _ = bench
        job_id = (await svc.start("t"))["job_id"]
        await with_table(svc, job_id)
        out = await svc.provide(job_id, CSV, name="txn.csv")
        assert out["routed"] is True and out["routed_to"] == "txn"
        assert "交易" in out["routing_reason"]

    async def test_the_orchestrator_is_told_the_suggestion_and_that_it_is_one(
        self, bench
    ):
        svc, _ = bench
        job_id = (await svc.start("t"))["job_id"]
        await with_table(svc, job_id)
        await svc.provide(job_id, CSV, name="txn.csv")
        notice = notices_of(svc, job_id)[0]
        assert "txn" in notice and "建議" in notice
        assert "inputs" in notice, "the grant rule must travel with the suggestion"

    async def test_a_proxy_route_event_is_recorded(self, bench):
        svc, _ = bench
        job_id = (await svc.start("t"))["job_id"]
        await with_table(svc, job_id)
        await svc.provide(job_id, CSV, name="txn.csv")
        events = await svc._events.read(job_id)
        routes = [e for e in events if e.t == "proxy.route"]
        assert len(routes) == 1
        assert routes[0].get("lane") == "txn"
        assert routes[0].get("payload").endswith("raw/txn.csv")

    async def test_the_classifier_saw_the_sample_not_the_whole_blob(self, bench):
        svc, transport = bench
        job_id = (await svc.start("t"))["job_id"]
        await with_table(svc, job_id)
        big = "a,b\n" + "\n".join(f"{i},{i}" for i in range(50_000))
        await svc.provide(job_id, big, name="big.csv")
        prompt = transport.prompts[0]
        assert "a,b" in prompt
        assert "49999" not in prompt, "the whole blob reached the classifier"
        assert len(prompt) < 4_000


class TestRoutingIsOnlyASuggestion:
    async def test_it_does_not_dispatch(self, bench):
        """A misclassification should be rejectable, not already paid for."""
        svc, _ = bench
        job_id = (await svc.start("t"))["job_id"]
        await with_table(svc, job_id)
        before = svc.manager.get(job_id).runner.state.dispatches
        await svc.provide(job_id, CSV, name="txn.csv")
        assert svc.manager.get(job_id).runner.state.dispatches == before
        events = await svc._events.read(job_id)
        assert not [e for e in events if e.t == "dispatch.start"]

    async def test_it_does_not_grant(self, bench):
        """Authorisation stays at dispatch(inputs=...). A routed lane that was
        never granted the artifact still cannot read it."""
        from myharness.artifacts.errors import ArtifactError
        from myharness.artifacts.ids import ArtifactId
        from myharness.artifacts.types import GrantSet

        svc, _ = bench
        job_id = (await svc.start("t"))["job_id"]
        await with_table(svc, job_id)
        out = await svc.provide(job_id, CSV, name="txn.csv")
        assert out["routed_to"] == "txn"

        # Exactly the grant set the routed lane would run with, having been
        # given no inputs.
        grants = GrantSet.for_lane(job_id, "lanes/txn", [])
        with pytest.raises(ArtifactError):
            await svc._store.stat(ArtifactId.parse(out["artifact"]), grants=grants)


class TestFailuresStillLand:
    async def _provide(self, svc, transport=None):
        job_id = (await svc.start("t"))["job_id"]
        await with_table(svc, job_id)
        return job_id, await svc.provide(svc.manager.get(job_id).job_id, CSV,
                                         name="txn.csv")

    async def test_no_routing_table_is_not_a_failure(self, bench):
        svc, _ = bench
        job_id = (await svc.start("t"))["job_id"]
        out = await svc.provide(job_id, CSV, name="txn.csv")
        assert out["ok"] and out["routed"] is False
        assert out["unrouted_because"] == str(Unrouted.NO_TABLE)

    async def test_a_dead_classifier_still_stores_the_blob(self, tmp_path: Path):
        charter = tmp_path / "c.md"
        charter.write_text("c", encoding="utf-8")
        svc = AnalysisService(
            tmp_path / "root",
            lanes=LaneRegistry(LaneType(name="ta", charter_path=charter,
                                        state_max_tokens=10)),
            backend="anthropic", loop_factory=FakeLoop,
            proxy_transport=FakeTransport(raises=RuntimeError("endpoint down")),
        )
        try:
            job_id = (await svc.start("t"))["job_id"]
            await with_table(svc, job_id)
            out = await svc.provide(job_id, CSV, name="txn.csv")
            assert out["ok"] and out["routed"] is False
            assert out["unrouted_because"] == str(Unrouted.FAILED)
            listed = await svc._store.list(job_id, kind="blob")
            assert any(a.id.name == "raw/txn.csv" for a in listed), "the blob was lost"
        finally:
            await svc.aclose()

    async def test_a_hallucinated_lane_does_not_route(self, tmp_path: Path):
        charter = tmp_path / "c.md"
        charter.write_text("c", encoding="utf-8")
        svc = AnalysisService(
            tmp_path / "root",
            lanes=LaneRegistry(LaneType(name="ta", charter_path=charter,
                                        state_max_tokens=10)),
            backend="anthropic", loop_factory=FakeLoop,
            proxy_transport=FakeTransport('{"lane": "does-not-exist"}'),
        )
        try:
            job_id = (await svc.start("t"))["job_id"]
            await with_table(svc, job_id)
            out = await svc.provide(job_id, CSV, name="txn.csv")
            assert out["routed"] is False
            assert out["unrouted_because"] == str(Unrouted.NO_MATCH)
        finally:
            await svc.aclose()

    async def test_the_notice_says_why_when_unrouted(self, bench):
        svc, _ = bench
        job_id = (await svc.start("t"))["job_id"]
        out = await svc.provide(job_id, CSV, name="txn.csv")
        notice = notices_of(svc, job_id)[0]
        assert "未分流" in notice and "routing table" in notice
        assert out["announced"] is True
