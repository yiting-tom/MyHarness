"""The classifier: one question, one call, and no view of the job.

The test that matters most here is the one asserting the prompt contains no
plan and no goal. Everything else can be re-derived from the code; that
property is a design commitment that will otherwise be traded away the first
time someone wants better accuracy.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from myharness.backends.profile import registry
from myharness.orchestrator.routing import RoutingTable
from myharness.proxy.classify import (
    SYSTEM_PROMPT,
    Routing,
    Unrouted,
    build_prompt,
    classify,
)
from myharness.proxy.sample import Sample

PROFILE = registry.get("anthropic")


def table(*entries) -> RoutingTable:
    return RoutingTable.from_raw(list(entries) or [
        {"lane": "txn-2024", "accepts": "2024 年交易明細與金流紀錄"},
        {"lane": "kyc-docs", "accepts": "身分與 KYC 文件"},
    ])


def sample(text="txn_id,ts,amount\nT001,2024-01-01,100\n") -> Sample:
    return Sample(text, 2, False, False)


class Block:
    def __init__(self, text: str) -> None:
        self.text = text


class Msg:
    def __init__(self, text: str, *, usd=0.001, tin=400, tout=30) -> None:
        self.content = [Block(text)]
        self.total_cost_usd = usd
        self.usage = {"input_tokens": tin, "output_tokens": tout}


class FakeTransport:
    """Replays one answer and records what it was asked."""

    def __init__(self, *messages: Any, raises: BaseException | None = None,
                 delay: float = 0.0) -> None:
        self._messages = messages
        self._raises = raises
        self._delay = delay
        self.prompts: list[str] = []
        self.options: list[Any] = []

    def stream(self, prompt: str, options: Any):
        self.prompts.append(prompt)
        self.options.append(options)
        messages, raises, delay = self._messages, self._raises, self._delay

        async def gen():
            if delay:
                await asyncio.sleep(delay)
            if raises is not None:
                raise raises
            for m in messages:
                yield m

        return gen()


async def run(transport, tbl=None, **kw) -> Routing:
    return await classify(tbl or table(), "id: j/blob/raw/txn.csv\nbytes: 1,472",
                          sample(), profile=PROFILE, transport=transport, **kw)


class TestZeroContextSharing:
    """DESIGN §4.2: the orchestrator steers the proxy with declarative data.

    If this stops holding, the proxy is a second planner with no budget
    controls -- and nothing else in the system would notice.
    """

    def test_the_prompt_contains_no_plan_and_no_goal(self):
        prompt = build_prompt(table(), "id: j/blob/raw/txn.csv", sample())
        forbidden = [
            "# 目標", "已確認結論", "決策與理由", "開放問題",   # plan template
            "找出異常樣態",                                     # a goal
        ]
        assert not [f for f in forbidden if f in prompt], prompt

    def test_the_prompt_is_only_the_table_the_metadata_and_the_sample(self):
        tbl = table({"lane": "only-lane", "accepts": "只有這個"})
        prompt = build_prompt(tbl, "id: j/blob/raw/x.csv\nbytes: 9", sample("h1,h2\n1,2"))
        assert "only-lane" in prompt and "只有這個" in prompt
        assert "j/blob/raw/x.csv" in prompt and "h1,h2" in prompt

    def test_closed_lanes_are_not_even_offered(self):
        tbl = table({"lane": "open-one", "accepts": "x"},
                    {"lane": "shut", "accepts": "y", "status": "closed"})
        assert "shut" not in build_prompt(tbl, "m", sample())

    def test_the_system_prompt_says_it_cannot_see_the_goal(self):
        assert "看不到" in SYSTEM_PROMPT

    def test_classify_takes_no_job_handle(self):
        """No parameter through which the plan could later be reached."""
        import inspect

        params = set(inspect.signature(classify).parameters)
        assert not (params & {"job_id", "store", "runner", "plan", "goal"})


class TestAnswers:
    async def test_a_clean_answer_routes(self):
        t = FakeTransport(Msg('{"lane": "txn-2024", "confidence": "high", '
                              '"reason": "欄位是交易明細"}'))
        out = await run(t)
        assert out.routed and out.lane == "txn-2024"
        assert out.confidence == "high" and "交易" in out.reason

    async def test_json_wrapped_in_a_code_fence(self):
        t = FakeTransport(Msg('```json\n{"lane": "kyc-docs", "confidence": "low"}\n```'))
        assert (await run(t)).lane == "kyc-docs"

    async def test_json_buried_in_prose(self):
        t = FakeTransport(Msg('我認為這是交易資料。\n{"lane": "txn-2024"}\n希望有幫助。'))
        assert (await run(t)).lane == "txn-2024"

    async def test_an_explicit_null_is_no_match_not_a_failure(self):
        t = FakeTransport(Msg('{"lane": null, "reason": "看不出來"}'))
        out = await run(t)
        assert not out.routed and out.unrouted is Unrouted.NO_MATCH
        assert "看不出來" in out.reason

    async def test_cost_and_tokens_are_carried(self):
        t = FakeTransport(Msg('{"lane": "txn-2024"}', usd=0.0004, tin=512, tout=24))
        out = await run(t)
        assert out.usd == pytest.approx(0.0004)
        assert out.tokens_in == 512 and out.tokens_out == 24

    async def test_null_usage_values_do_not_crash(self):
        """Providers send nulls where the schema says integer."""
        m = Msg('{"lane": "txn-2024"}')
        m.usage = {"input_tokens": None, "output_tokens": None}
        m.total_cost_usd = None
        out = await run(FakeTransport(m))
        assert out.routed and out.tokens_in == 0


class TestFailuresDoNotRoute:
    async def test_no_open_lanes_short_circuits_without_calling_the_model(self):
        t = FakeTransport(Msg('{"lane": "anything"}'))
        out = await classify(RoutingTable(), "m", sample(), profile=PROFILE, transport=t)
        assert out.unrouted is Unrouted.NO_TABLE
        assert t.prompts == [], "the model was called with nothing to choose from"

    async def test_all_lanes_closed_is_also_no_table(self):
        tbl = table({"lane": "a", "accepts": "x", "status": "closed"})
        t = FakeTransport(Msg('{"lane": "a"}'))
        out = await run(t, tbl)
        assert out.unrouted is Unrouted.NO_TABLE and t.prompts == []

    async def test_a_hallucinated_lane_is_refused(self):
        t = FakeTransport(Msg('{"lane": "invented-lane", "confidence": "high"}'))
        out = await run(t)
        assert not out.routed and out.unrouted is Unrouted.NO_MATCH
        assert "invented-lane" in out.reason

    async def test_a_closed_lane_is_refused(self):
        tbl = table({"lane": "open-one", "accepts": "x"},
                    {"lane": "shut", "accepts": "y", "status": "closed"})
        t = FakeTransport(Msg('{"lane": "shut"}'))
        out = await run(t, tbl)
        assert not out.routed and out.unrouted is Unrouted.NO_MATCH

    async def test_unparseable_output_is_a_failure_not_a_crash(self):
        t = FakeTransport(Msg("我不確定，可能是交易資料吧。"))
        out = await run(t)
        assert out.unrouted is Unrouted.FAILED and "JSON" in out.reason

    async def test_empty_output(self):
        out = await run(FakeTransport(Msg("")))
        assert out.unrouted is Unrouted.FAILED

    async def test_a_transport_error_is_a_failure_not_a_raise(self):
        t = FakeTransport(raises=RuntimeError("endpoint is down"))
        out = await run(t)
        assert out.unrouted is Unrouted.FAILED and "endpoint is down" in out.reason

    async def test_a_timeout_is_a_failure_naming_the_limit(self):
        t = FakeTransport(Msg('{"lane": "txn-2024"}'), delay=5.0)
        out = await run(t, timeout_s=0.05)
        assert out.unrouted is Unrouted.FAILED and "逾時" in out.reason

    async def test_the_three_unrouted_reasons_are_distinguishable(self):
        """They mean different things to the orchestrator: wait, look again,
        or nobody wants it."""
        assert len({Unrouted.NO_TABLE, Unrouted.NO_MATCH, Unrouted.FAILED}) == 3


class TestOptions:
    async def test_the_classifier_gets_the_cheap_model(self):
        t = FakeTransport(Msg('{"lane": "txn-2024"}'))
        await run(t)
        assert t.options[0].model == PROFILE.resolve_model("cheap")

    async def test_it_gets_one_turn_and_no_tools(self):
        t = FakeTransport(Msg('{"lane": "txn-2024"}'))
        await run(t)
        options = t.options[0]
        assert options.max_turns == 1
        assert options.allowed_tools == []
        assert options.disallowed_tools, "builtin definitions were left in the prompt"


def test_the_event_payload_is_small():
    routed = Routing("txn-2024", "high", "理由" * 500, usd=0.0004,
                     tokens_in=500, tokens_out=20)
    event = routed.to_event()
    assert len(event["reason"]) <= 200
    assert event["lane"] == "txn-2024" and event["unrouted"] is None
