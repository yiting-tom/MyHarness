"""The same chain a remote agent runs, over a real socket, with no model.

tests/a2a/test_boundary.py drives the app in-process, which skips uvicorn, HTTP,
card resolution over the wire and the real client's own serialisation. Those are
where the live test's first run died, twenty minutes and one analysis too late.
This costs nothing and fails in seconds.
"""

from __future__ import annotations

import pytest

pytest.importorskip("a2a", reason="the a2a extra is optional (D6)")

from myharness.a2a.card import (
    PRICE_LIST_EXTENSION, SKILL_FULL_TEXT, SKILL_PRICE_LIST,
)
from myharness.a2a.server import build_app, endpoint_url

from tests.a2a.chain import drive, free_port, running
from tests.a2a.test_boundary import FakeService


@pytest.fixture
async def chain():
    from a2a.types import TaskState

    service = FakeService()
    service.progress = [
        {"ok": True, "state": "running", "revision": 2, "phase": "planning"},
        {"ok": True, "state": "running", "revision": 6, "phase": "synthesising"},
        {"ok": True, "state": "finished", "revision": 8},
    ]
    port = free_port()
    app = build_app(service, url=endpoint_url("127.0.0.1", port))
    async with running(app, port):
        yield await drive(port, "分析這份交易資料", timeout_s=30.0), TaskState


async def test_a_remote_agent_discovers_both_skills_before_asking(chain):
    result, _ = chain
    assert result.skills == [SKILL_PRICE_LIST, SKILL_FULL_TEXT]
    assert result.extensions == [PRICE_LIST_EXTENSION]


async def test_the_analysis_reaches_a_terminal_state(chain):
    """`str(state)` is "3": the protobuf enum is an int, and a string comparison
    against the name can never pass. That is what this test exists for."""
    result, TaskState = chain
    assert result.state == TaskState.TASK_STATE_COMPLETED


async def test_what_comes_back_is_the_price_list_and_is_marked(chain):
    result, _ = chain
    assert result.marked_artifacts == 1
    assert [s["id"] for s in result.price_list["sections"]] == ["方法", "限制"]
    assert "text" not in result.price_list, "the report body must not ride along"


async def test_the_history_carries_every_revision_in_order(chain):
    result, _ = chain
    assert result.revisions == [0, 2, 6]


async def test_one_section_can_be_bought_with_an_id_from_the_list(chain):
    result, _ = chain
    assert result.section_id == "方法"
    assert result.section["text"].strip()
    assert not result.section_marked, "content must not be marked as a price list"
