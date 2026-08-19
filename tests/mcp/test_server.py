"""The protocol layer, driven by a real MCP client session.

Everything below this file is tested without MCP, which is deliberate. What is
left to check here is exactly what a hand-rolled fake would get wrong: that the
schemas survive validation, that a client can list and call the tools, and that
a failing tool comes back as a readable result rather than a transport error.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp.server.models import InitializationOptions
from mcp.shared.memory import create_connected_server_and_client_session

from myharness.lanes.types import LaneRegistry, LaneType
from myharness.mcp.server import build_server, default_lanes
from myharness.mcp.service import AnalysisService
from myharness.mcp.tools import TOOL_DESCRIPTIONS, TOOL_SCHEMAS


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


@asynccontextmanager
async def connected(tmp_path: Path):
    """A client wired to a server over an in-memory transport.

    Not a fixture: anyio's cancel scope has to be entered and exited in the same
    task, and a yielding fixture puts the two halves in different ones.
    """
    charter = tmp_path / "c.md"
    charter.write_text("charter", encoding="utf-8")
    lanes = LaneRegistry(
        LaneType(name="analyst", charter_path=charter, state_max_tokens=100)
    )
    service = AnalysisService(tmp_path / "root", lanes=lanes, loop_factory=FakeLoop)
    try:
        async with create_connected_server_and_client_session(
            build_server(service)
        ) as client:
            yield client
    finally:
        await service.aclose()


def payload_of(result) -> dict:
    return json.loads(result.content[0].text)


async def test_a_client_can_list_the_tools(tmp_path: Path):
    async with connected(tmp_path) as session:
        listed = await session.list_tools()
        assert {t.name for t in listed.tools} == set(TOOL_SCHEMAS)


async def test_every_tool_has_a_description_and_a_valid_schema(tmp_path: Path):
    async with connected(tmp_path) as session:
        """A schema the protocol rejects would fail at connect time, not at call."""
        listed = await session.list_tools()
        for tool in listed.tools:
            assert tool.description == TOOL_DESCRIPTIONS[tool.name]
            assert tool.inputSchema["type"] == "object"
            assert "required" in tool.inputSchema


async def test_optional_arguments_stay_optional_through_the_protocol(tmp_path: Path):
    async with connected(tmp_path) as session:
        """The shorthand-schema mistake from the last change, checked on the wire:
        a client must be able to poll before it has ever seen a revision."""
        listed = await session.list_tools()
        poll = next(t for t in listed.tools if t.name == "analysis_poll")
        assert poll.inputSchema["required"] == ["job_id"]
        assert {"wait", "since"} <= set(poll.inputSchema["properties"])


async def test_start_then_poll_over_the_wire(tmp_path: Path):
    async with connected(tmp_path) as session:
        started = payload_of(await session.call_tool("analysis_start", {"task": "分析交易"}))
        assert started["ok"]
        job_id = started["job_id"]

        progress = payload_of(
            await session.call_tool("analysis_poll", {"job_id": job_id, "wait": 0.05})
        )
        assert progress["ok"] and progress["state"] == "running"
        assert progress["job_id"] == job_id


async def test_provide_returns_an_id_and_not_the_data(tmp_path: Path):
    async with connected(tmp_path) as session:
        job_id = payload_of(await session.call_tool("analysis_start", {"task": "t"}))["job_id"]
        out = payload_of(await session.call_tool(
            "analysis_provide",
            {"job_id": job_id, "payload": "account,amount\nA1,999999\n", "name": "extra.csv"},
        ))
        assert out["ok"] and out["artifact"].endswith("raw/extra.csv")
        assert "999999" not in json.dumps(out)
        assert out["routed"] is False


async def test_a_refusal_comes_back_as_a_readable_result(tmp_path: Path):
    async with connected(tmp_path) as session:
        """Not a protocol error: the client is an agent and has to read it."""
        out = payload_of(await session.call_tool("analysis_poll", {"job_id": "nope"}))
        assert out["ok"] is False and out["error"] == "no_such_job"


async def test_an_unknown_tool_lists_the_real_ones(tmp_path: Path):
    async with connected(tmp_path) as session:
        from myharness.mcp.tools import build_handlers, call

        text = await call({}, "analysis_nope", {})
        assert json.loads(text)["error"] == "unknown_tool"


async def test_a_handler_that_raises_becomes_a_refusal(tmp_path: Path):
    async with connected(tmp_path) as session:
        from myharness.mcp.tools import call

        async def boom(args):
            raise RuntimeError("unexpected")

        out = json.loads(await call({"x": boom}, "x", {}))
        assert out["ok"] is False and out["error"] == "tool_failed"
        assert "unexpected" in out["message"]


async def test_the_protocol_enforces_required_arguments(tmp_path: Path):
    """The `required` lists are not documentation -- MCP validates against them
    and the handler is never reached. Which means getting them wrong (the
    shorthand-schema mistake) would reject legitimate calls at the protocol
    layer, where the error is hardest to attribute."""
    async with connected(tmp_path) as session:
        result = await session.call_tool("analysis_start", {})
        assert result.isError
        assert "task" in result.content[0].text


async def test_an_argument_the_schema_allows_still_reaches_our_refusals(
    tmp_path: Path,
):
    """A blank task satisfies the schema, so this one is ours to reject."""
    async with connected(tmp_path) as session:
        out = payload_of(await session.call_tool("analysis_start", {"task": "   "}))
        assert out["ok"] is False and out["error"] == "empty_task"


async def test_optional_arguments_are_genuinely_optional_on_the_wire(tmp_path: Path):
    """Poll with neither wait nor since -- what a first poll looks like."""
    async with connected(tmp_path) as session:
        started = payload_of(
            await session.call_tool("analysis_start", {"task": "t"})
        )
        out = payload_of(
            await session.call_tool("analysis_poll",
                                    {"job_id": started["job_id"], "wait": 0.01})
        )
        assert out["ok"]


def test_the_shipped_lane_types_point_at_charters_that_exist():
    """A registry naming a missing charter fails at dispatch, deep inside a job."""
    registry = default_lanes(Path("charters"))
    for name in ("tabular-analyst", "synthesizer"):
        assert registry.get_type(name).charter_path.exists(), name


def test_the_shipped_analyst_can_actually_query_data():
    """The whole point of the previous change; easy to lose in a tool list."""
    tools = default_lanes(Path("charters")).get_type("tabular-analyst").tools
    assert "duckdb_query" in tools and "inspect_blob" in tools


async def test_a_long_poll_does_not_block_other_calls(tmp_path: Path):
    """If it did, a client could not answer a question while waiting for one.

    The long-poll is the whole interaction model here -- a client is expected to
    sit in a 30-second wait most of the time. A server that serialises requests
    would make analysis_answer unreachable exactly when it is needed, and the
    job would time the question out while the answer sat in a queue.
    """
    async with connected(tmp_path) as session:
        started = payload_of(await session.call_tool("analysis_start", {"task": "t"}))
        job_id = started["job_id"]

        blocked = asyncio.create_task(
            session.call_tool("analysis_poll", {"job_id": job_id, "wait": 20.0})
        )
        await asyncio.sleep(0.2)
        assert not blocked.done(), "the poll returned instead of waiting"

        # A different call must get through while that one is parked.
        other = payload_of(
            await asyncio.wait_for(
                session.call_tool("analysis_provide",
                                  {"job_id": job_id, "payload": "a\n1\n"}),
                timeout=5.0,
            )
        )
        assert other["ok"], other

        # And the parked poll wakes on the change that call produced.
        woken = payload_of(await asyncio.wait_for(blocked, timeout=5.0))
        assert woken["ok"]
