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

#: Longer than the job's own `max_wall_clock_s` (1,800s) on purpose. A client
#: that gives up at the same moment the harness would can never tell "the
#: analysis was stopped" from "I stopped waiting" -- the third live run ended at
#: exactly 1,800s in WORKING, which is both of those at once.
DEADLINE_S = 2_100.0

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
    csv = next(Path("jobs-scratch").rglob("blobs/raw/txn-2024"), None)
    if csv is None:
        pytest.skip("no txn-2024 fixture in jobs-scratch; run a golden job first")

    async def seed(job_id: str) -> None:
        provided = await service.provide(
            job_id, csv.read_text(encoding="utf-8"), name="txn-2024"
        )
        assert provided.get("ok"), provided

    answered: list[str] = []

    async def keep_answering(job_id: str) -> None:
        """Answer whatever the orchestrator asks, the way an MCP client would.

        There is no `analysis_answer` over A2A either, and a question nobody
        answers is a job that sits in place until the question times out -- the
        third live run spent its whole thirty minutes in WORKING, which is what
        that looks like from outside.
        """
        while True:
            progress = await service.poll(job_id, wait=20.0)
            if not progress.get("ok"):
                return
            for question in progress.get("pending_questions") or []:
                answered.append(question["id"])
                await service.answer(job_id, question["id"], "否")
            if progress.get("state") != "running":
                return

    port = free_port()
    app = build_app(service, url=endpoint_url("127.0.0.1", port))
    try:
        async with running(app, port):
            result = await drive(port, TASK, after_start=seed,
                                 during=keep_answering, timeout_s=DEADLINE_S)
    finally:
        await service.aclose()

    assert result.started_state in (TaskState.TASK_STATE_SUBMITTED,
                                    TaskState.TASK_STATE_WORKING), \
        "the start must return before the analysis does"

    assert result.skills == [SKILL_PRICE_LIST, SKILL_FULL_TEXT]
    assert result.extensions == [PRICE_LIST_EXTENSION]
    assert result.state == TaskState.TASK_STATE_COMPLETED, (
        f"state={result.state} refusal={result.refusal} answered={answered}"
    )
    assert result.marked_artifacts == 1, "the price list, marked as one"
    assert result.price_list.get("sections"), "a finished report has sections to price"
    assert "text" not in result.price_list, "the report body must not ride along"

    assert result.revisions == sorted(result.revisions), result.revisions
    assert len(result.revisions) >= 2, "a real analysis moves more than once"

    assert result.section["text"].strip(), "an empty section is not a section"
    assert not result.section_marked, "content must not be marked as a price list"
