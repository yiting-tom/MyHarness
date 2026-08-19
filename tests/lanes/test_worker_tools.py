"""Worker tools: the grant model holds only if there is no way around it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from myharness.artifacts.local import LocalArtifactStore
from myharness.artifacts.types import GrantSet
from myharness.lanes.tools import WorkerToolbox
from myharness.lanes.types import LaneRegistry, LaneType

JOB = "j7"


def text_of(result: dict) -> str:
    return result["content"][0]["text"]


def error_of(result: dict) -> dict:
    body = text_of(result)
    assert body.startswith("ERROR "), f"expected a refusal, got {body[:120]!r}"
    return json.loads(body.removeprefix("ERROR "))


@pytest.fixture
async def bench(tmp_path: Path):
    store = LocalArtifactStore(tmp_path)
    await store.init_job(JOB)
    charter = tmp_path / "c.md"
    charter.write_text("charter", encoding="utf-8")
    registry = LaneRegistry(
        LaneType(
            name="ta", charter_path=charter, state_max_tokens=60,
            tools=("read_note", "write_finding", "update_state", "localize_blob"),
        )
    )
    lane = registry.create("txn-2024", "ta")
    granted = await store.put_note(JOB, "lanes/kyc/findings/001", "已授權的內容", produced_by="kyc")
    secret = await store.put_note(JOB, "lanes/kyc/state", "未授權的機密", produced_by="kyc")
    blob = await store.put_blob(JOB, "raw/a", data=b"ts,amt\n1,2\n",
                                produced_by="user", schema={"columns": ["ts", "amt"]})
    toolbox = WorkerToolbox(
        store=store, job_id=JOB, lane=lane,
        grants=GrantSet.for_lane(JOB, lane.namespace, [granted.id, blob.id]),
        read_budget=3000,
    )
    toolbox.build_server()
    return toolbox, {"granted": granted, "secret": secret, "blob": blob}


async def test_reads_explicitly_granted_artifact(bench):
    """Scenario: 讀取被授權的 input"""
    toolbox, ids = bench
    assert "已授權" in text_of(await toolbox.handlers["read_note"]({"artifact": str(ids["granted"].id)}))


async def test_refusal_is_visible_to_the_worker(bench):
    """Scenario: 讀取未授權的 artifact 失敗 -- 且該失敗對 worker 可見."""
    toolbox, ids = bench
    detail = error_of(await toolbox.handlers["read_note"]({"artifact": str(ids["secret"].id)}))
    assert detail["code"] == "not_granted"
    assert "機密" not in json.dumps(detail, ensure_ascii=False)


async def test_blob_cannot_be_read_as_a_note(bench):
    toolbox, ids = bench
    detail = error_of(await toolbox.handlers["read_note"]({"artifact": str(ids["blob"].id)}))
    assert detail["code"] == "blob_not_readable"
    assert detail["suggested_access"]


async def test_malformed_artifact_id_is_a_refusal_not_a_crash(bench):
    toolbox, _ = bench
    assert error_of(await toolbox.handlers["read_note"]({"artifact": "garbage"}))["code"] == "bad_artifact_id"


async def test_write_finding_records_what_was_produced(bench):
    toolbox, _ = bench
    await toolbox.handlers["write_finding"]({"name": "001", "text": "## 結論\n夜間高頻"})
    assert toolbox.findings == [f"{JOB}/note/lanes/txn-2024/findings/001"]
    assert toolbox.last_finding == toolbox.findings[0]


async def test_empty_finding_is_refused(bench):
    toolbox, _ = bench
    assert error_of(await toolbox.handlers["write_finding"]({"name": "x", "text": "  "}))["code"] == "empty_finding"


async def test_state_update_advances_revision(bench):
    """Scenario: Lane state 提供跨任務的連續性（寫入側）"""
    toolbox, _ = bench
    await toolbox.handlers["update_state"]({"text": "## 已確認結論\n夜間高頻"})
    assert toolbox.state_revision == 1
    await toolbox.handlers["update_state"]({"text": "## 已確認結論\n夜間高頻，且集中週末"})
    assert toolbox.state_revision == 2
    assert not toolbox.state_rejected


async def test_oversized_state_is_refused_and_old_state_kept(bench):
    """Scenario: 超過上限的 state 寫入被拒絕"""
    toolbox, _ = bench
    await toolbox.handlers["update_state"]({"text": "## 已確認結論\n原本的"})
    detail = error_of(await toolbox.handlers["update_state"]({"text": "長" * 500}))

    assert detail["code"] == "state_too_large"
    assert detail["est_tokens"] > detail["limit"]
    assert toolbox.state_rejected
    assert toolbox.state_revision == 1, "the accepted revision must be unchanged"

    kept = await toolbox.store.read_note(
        toolbox.lane and __import__("myharness.artifacts.ids", fromlist=["ArtifactId"]).ArtifactId(
            JOB, "note", toolbox.lane.state_name
        ),
        grants=toolbox.grants, max_tokens=5000,
    )
    assert kept == "## 已確認結論\n原本的"


async def test_concurrent_state_write_is_detected(bench):
    """Scenario: 並行寫入被偵測"""
    toolbox, _ = bench
    await toolbox.handlers["update_state"]({"text": "v1"})

    # A second worker writes the same lane state from the same starting point.
    await toolbox.store.compare_and_set_note(
        JOB, toolbox.lane.state_name, "v2-from-elsewhere",
        produced_by="other", expected_revision=1,
    )

    detail = error_of(await toolbox.handlers["update_state"]({"text": "v2-from-me"}))
    assert detail["code"] == "revision_conflict"
    assert toolbox.state_rejected


async def test_localize_blob_returns_a_usable_path(bench):
    toolbox, ids = bench
    payload = json.loads(text_of(await toolbox.handlers["localize_blob"]({"artifact": str(ids["blob"].id)})))
    assert Path(payload["path"]).read_bytes() == b"ts,amt\n1,2\n"
    assert payload["schema"] == {"columns": ["ts", "amt"]}


async def test_localize_respects_grants(bench):
    toolbox, ids = bench
    assert error_of(await toolbox.handlers["localize_blob"]({"artifact": str(ids["secret"].id)}))["code"] in {
        "not_granted", "not_a_blob",
    }


async def test_only_declared_tools_are_exposed(tmp_path: Path):
    """A lane pays for every tool definition it declares, so it declares few."""
    store = LocalArtifactStore(tmp_path)
    await store.init_job(JOB)
    charter = tmp_path / "c.md"
    charter.write_text("c", encoding="utf-8")
    registry = LaneRegistry(LaneType(name="reader", charter_path=charter, tools=("read_note",)))
    lane = registry.create("r1", "reader")
    toolbox = WorkerToolbox(store=store, job_id=JOB, lane=lane,
                            grants=GrantSet.for_lane(JOB, lane.namespace), read_budget=1000)
    toolbox.build_server()
    assert list(toolbox.handlers) == ["read_note"]
    assert toolbox.tool_names() == ["mcp__lane__read_note"]
