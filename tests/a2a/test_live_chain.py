"""The same chain, with a real model behind it.

tests/a2a/test_socket_chain.py already drives this over a real socket with a
real client; what this adds is the only part that cannot be faked -- a real
analysis, really running, while a remote agent watches it. It costs money and
needs a key, so it is marked live and deselected by default.

    set -a && . ./.env && set +a && pytest -m live tests/a2a
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("a2a", reason="the a2a extra is optional (D6)")

from myharness.a2a.card import (
    PRICE_LIST_EXTENSION, SKILL_FULL_TEXT, SKILL_PRICE_LIST,
)
from myharness.a2a.server import build_app, endpoint_url
from myharness.backends.profile import registry, self_hosted_from_env
from myharness.mcp.server import default_lanes
from myharness.mcp.service import AnalysisService

from tests.a2a.chain import drive, free_port, running

pytestmark = pytest.mark.live

TASK = (
    "分析這份交易資料，找出異常樣態。報告中必須給出不重複帳戶的總數，"
    "以及平均交易金額最低的 channel。"
)


async def test_a_remote_agent_can_run_and_read_an_analysis(tmp_path: Path):
    from a2a.types import TaskState

    profile = self_hosted_from_env()
    if profile is None:
        pytest.skip("HARNESS_PROXY_BASE_URL / _MODEL unset")
    registry.register(profile)

    service = AnalysisService(
        tmp_path / "root",
        lanes=default_lanes(Path("charters"), backend=profile.name),
        backend=profile.name,
    )
    port = free_port()
    app = build_app(service, url=endpoint_url("127.0.0.1", port))
    try:
        async with running(app, port):
            result = await drive(port, TASK)
    finally:
        await service.aclose()

    assert result.skills == [SKILL_PRICE_LIST, SKILL_FULL_TEXT]
    assert result.extensions == [PRICE_LIST_EXTENSION]
    assert result.state == TaskState.TASK_STATE_COMPLETED, result.state

    assert result.marked_artifacts == 1, "the price list, marked as one"
    assert result.price_list.get("sections"), "a finished report has sections to price"
    assert "text" not in result.price_list, "the report body must not ride along"

    assert result.revisions == sorted(result.revisions), result.revisions
    assert len(result.revisions) >= 2, "a real analysis moves more than once"

    assert result.section["text"].strip(), "an empty section is not a section"
    assert not result.section_marked, "content must not be marked as a price list"
