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
    out = await toolbox.handlers["read_note"]({"artifact": str(ids["granted"].id)})
    assert "已授權" in text_of(out)


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
    out = await toolbox.handlers["read_note"]({"artifact": "garbage"})
    assert error_of(out)["code"] == "bad_artifact_id"


async def test_write_finding_records_what_was_produced(bench):
    toolbox, _ = bench
    await toolbox.handlers["write_finding"]({"name": "001", "text": "## 結論\n夜間高頻"})
    assert toolbox.findings == [f"{JOB}/note/lanes/txn-2024/findings/001"]
    assert toolbox.last_finding == toolbox.findings[0]


async def test_empty_finding_is_refused(bench):
    toolbox, _ = bench
    out = await toolbox.handlers["write_finding"]({"name": "x", "text": "  "})
    assert error_of(out)["code"] == "empty_finding"


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
    out = await toolbox.handlers["localize_blob"]({"artifact": str(ids["blob"].id)})
    payload = json.loads(text_of(out))
    assert Path(payload["path"]).read_bytes() == b"ts,amt\n1,2\n"
    assert payload["schema"] == {"columns": ["ts", "amt"]}


async def test_localize_respects_grants(bench):
    toolbox, ids = bench
    out = await toolbox.handlers["localize_blob"]({"artifact": str(ids["secret"].id)})
    assert error_of(out)["code"] in {
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


# --- the lane cannot see its own consumption ------------------------------
#
# Golden run #9: a lane spent its entire 60k budget on 24 queries, never once
# called write_finding, and the analysis vanished with it. It was not being
# careless -- nothing told it how much was left.


def _text(result) -> str:
    return result["content"][0]["text"]


async def test_a_result_carries_no_warning_below_the_threshold(bench):
    from myharness.lanes.tools import BUDGET_WARN_AT

    toolbox, _ = bench
    toolbox.budget_used = BUDGET_WARN_AT - 0.01
    assert "[harness]" not in _text(toolbox._result("rows: 12"))


async def test_past_the_threshold_a_lane_with_nothing_written_is_told_to_write(bench):
    toolbox, _ = bench
    toolbox.budget_used = 0.8
    text = _text(toolbox._result("rows: 12"))

    assert text.startswith("rows: 12")  # the result itself is never displaced
    assert "80%" in text
    assert "write_finding" in text
    # The consequence, not just the instruction: an unfiled analysis does not
    # come back truncated, it does not come back at all.
    assert "全部消失" in text


async def test_a_lane_that_has_written_is_told_to_finish_instead(bench):
    toolbox, _ = bench
    toolbox.budget_used = 0.9
    toolbox.findings.append("j/note/lanes/a/findings/1")
    text = _text(toolbox._result("rows: 12"))

    assert "不要再開新的查詢" in text
    assert "write_finding" not in text, \
        "it already has one; telling it to write again invites a duplicate"


async def test_the_warning_repeats_on_every_call(bench):
    """Said once, thirty messages back, is not what the model is attending to
    when it decides whether to run one more query."""
    toolbox, _ = bench
    toolbox.budget_used = 0.8
    assert all("[harness]" in _text(toolbox._result(f"r{i}")) for i in range(3))


# --- a name is a label, not a path ----------------------------------------


async def test_a_finding_name_that_is_an_artifact_id_is_refused(bench):
    """Golden run #10.

    A worker reads artifact ids all run and reaches for one here. The harness
    nested it under the lane's own namespace and produced
    lanes/critic/findings/<job>/note/lanes/analyst/findings/critique -- a path
    nothing looks for, which the flow graph then reported as an orphan.
    """
    toolbox, _ids = bench
    result = text_of(await toolbox.handlers["write_finding"]({
        "name": "j/note/lanes/analyst/findings/critique", "text": "## 結論\nok\n",
    }))
    assert result.startswith("ERROR")
    assert "'/'" in result
    # A refusal the model can act on beats one it can only be confused by.
    assert "artifact id" in result
    assert not toolbox.findings, "nothing may be written on a refusal"


async def test_an_overlong_name_is_refused(bench):
    toolbox, _ = bench
    result = text_of(await toolbox.handlers["write_finding"]({
        "name": "a" * 61, "text": "x",
    }))
    assert result.startswith("ERROR")
    assert "60" in result


async def test_a_chinese_name_is_refused_rather_than_raised(bench):
    """The charters are in Chinese, so this is what a worker reaches for.

    ArtifactId already rejects it, but by raising from inside put_note -- a
    stack trace where the caller is a model that needs a sentence.
    """
    toolbox, _ = bench
    result = text_of(await toolbox.handlers["write_finding"]({
        "name": "交易異常分析", "text": "## 結論\nok\n",
    }))
    assert result.startswith("ERROR")
    assert "中文請放在 finding 的內容裡" in result


async def test_an_ordinary_name_still_works(bench):
    toolbox, _ = bench
    result = text_of(await toolbox.handlers["write_finding"]({
        "name": "txn-stats", "text": "## 結論\nok\n",
    }))
    assert not result.startswith("ERROR")
    assert toolbox.findings and toolbox.findings[-1].endswith("/findings/txn-stats")


# --- the gate, because the warning is a request and not a construction -------
#
# Golden #17's d1 read "預算已用 82%" and then 92%, made fifteen queries after
# the first one, and never called write_finding. #18's d1 read the same string
# at 77% and did call it. Same model, same threshold, two outcomes. Every other
# ceiling in this harness holds by construction; this one asked nicely.


async def test_below_the_gate_a_content_tool_still_answers(bench):
    from myharness.lanes.tools import BUDGET_GATE_AT

    toolbox, ids = bench
    toolbox.budget_used = BUDGET_GATE_AT - 0.01
    result = await toolbox.handlers["read_note"]({"artifact": str(ids["granted"].id)})
    assert "已授權的內容" in _text(result)


async def test_above_the_gate_a_content_tool_is_refused(bench):
    toolbox, ids = bench
    toolbox.budget_used = 0.93
    body = error_of(await toolbox.handlers["read_note"]({"artifact": str(ids["granted"].id)}))

    assert body["code"] == "budget_gate"
    assert body["budget_used"] == 93
    assert "write_finding" in body["message"]
    assert "全部消失" in body["message"], "the consequence, not just the instruction"


async def test_the_gate_leaves_somewhere_to_put_the_work(bench):
    """Closing everything would only change what the lane loses its work to."""
    toolbox, _ = bench
    toolbox.budget_used = 0.95

    wrote = await toolbox.handlers["write_finding"]({"name": "partial", "text": "結論"})
    assert "wrote" in _text(wrote)
    state = await toolbox.handlers["update_state"]({"text": "還沒查完"})
    assert "state updated" in _text(state)


async def test_a_lane_that_already_wrote_is_told_to_finish_not_to_write_again(bench):
    toolbox, ids = bench
    toolbox.budget_used = 0.95
    toolbox.findings.append("j/note/lanes/a/findings/1")
    body = error_of(await toolbox.handlers["read_note"]({"artifact": str(ids["granted"].id)}))

    assert "handle" in body["message"]
    assert "write_finding" in body["message"], "補上新結論仍然要靠它"


async def test_localize_stays_open_because_it_returns_a_path_not_content(bench):
    """Refusing it would cost a lane its working file and save nothing."""
    toolbox, ids = bench
    toolbox.budget_used = 0.97
    result = await toolbox.handlers["localize_blob"]({"artifact": str(ids["blob"].id)})
    # The warning rides along on the same result, so the document is the first line.
    body, _, warning = _text(result).partition("\n\n[harness]")
    assert json.loads(body)["path"]
    assert warning, "and the lane is still being told where it stands"


async def test_the_gate_is_counted_so_a_run_shows_it_fired(bench):
    toolbox, ids = bench
    toolbox.budget_used = 0.95
    for _ in range(3):
        await toolbox.handlers["read_note"]({"artifact": str(ids["granted"].id)})
    assert toolbox.gated == 3
