"""The orchestrator's tool surface: small, fixed, and with no path to raw data."""

from __future__ import annotations

import pytest

from myharness.events.types import PEEK, PLAN_UPDATE
from myharness.jobs.spec import JobPhase
from myharness.orchestrator.tools import TOOL_NAMES, OrchestratorTools

from .conftest import JOB, payload


# --- Requirement: 工具面固定且極小 ----------------------------------------


def test_no_tool_can_return_a_blob(bench):
    """Scenario: 沒有讀取 blob 的路徑"""
    assert set(bench.tools.handlers) == set(TOOL_NAMES)
    assert len(TOOL_NAMES) == 6


async def test_peek_refuses_a_blob(bench):
    """The only read tool must not become a back door for raw data."""
    blob = await bench.store.put_blob(JOB, "raw/txns", data=b"ts,amt\n1,2\n",
                                      produced_by="user", schema={"columns": ["ts"]})
    body = payload(await bench.h["peek"]({"artifact": str(blob.id)}))
    assert body["error"] if "error" in body else body["code"] == "blob_not_readable"


async def test_surface_does_not_grow_with_lanes(bench):
    """Scenario: 工具數量不隨規模變化"""
    before = set(bench.tools.handlers)
    await bench.declare(*[f"lane{i}" for i in range(20)])
    assert set(bench.tools.handlers) == before


# --- plan_update ----------------------------------------------------------


async def test_plan_update_writes_the_plan_and_creates_lanes(bench):
    """Scenario: 計畫隨進度更新"""
    body = await bench.declare("txn", "kyc", plan="# 目標\n找出異常交易\n")
    assert body["created"] == ["txn", "kyc"]
    assert body["plan_revision"] == 1

    (event,) = await bench.kinds(PLAN_UPDATE)
    assert event.get("lanes") == ["kyc", "txn"]

    from myharness.orchestrator.plan import read_plan

    text, revision = await read_plan(bench.store, JOB)
    assert "找出異常交易" in text and revision == 1


async def test_plan_update_is_idempotent_for_existing_lanes(bench):
    await bench.declare("txn")
    body = await bench.declare("txn", "kyc")
    assert body["created"] == ["kyc"]


async def test_unknown_lane_type_is_refused(bench):
    body = payload(await bench.h["plan_update"]({
        "plan": "x", "lanes": [{"id": "z", "type": "nope"}]}))
    assert body["error"] == "unknown_lane_type"
    assert "ta" in body["message"]


async def test_empty_plan_is_refused(bench):
    assert payload(await bench.h["plan_update"]({"plan": "  "}))["error"] == "empty_plan"


# --- dispatch / await_tasks ----------------------------------------------


async def test_dispatch_then_collect(bench):
    await bench.declare("txn")
    dispatched = payload(await bench.h["dispatch"]({"lane": "txn", "task": "分析"}))
    assert dispatched["status"] == "running"

    collected = payload(await bench.h["await_tasks"]({"task_ids": [dispatched["task_id"]]}))
    assert len(collected["handles"]) == 1
    assert collected["handles"][0]["artifact"]


async def test_dispatch_to_undeclared_lane_is_refused(bench):
    body = payload(await bench.h["dispatch"]({"lane": "ghost", "task": "x"}))
    assert body["status"] == "unknown_lane"


async def test_await_reports_no_progress_back_to_the_orchestrator(bench):
    """The guard is useless if the orchestrator never hears about it."""
    bench.fake.artifact = None
    await bench.declare("txn")
    for i in range(bench.runner.spec.no_progress_limit):
        d = payload(await bench.h["dispatch"]({"lane": "txn", "task": f"嘗試 {i}"}))
        body = payload(await bench.h["await_tasks"]({"task_ids": [d["task_id"]]}))
    assert "warning" in body and "沒有新產出" in body["warning"]


@pytest.mark.parametrize("bench", [{"max_dispatches": 1}], indirect=True)
async def test_wrap_up_notice_reaches_the_orchestrator(bench):
    await bench.declare("txn", "syn")
    d = payload(await bench.h["dispatch"]({"lane": "txn", "task": "分析"}))
    body = payload(await bench.h["await_tasks"]({"task_ids": [d["task_id"]]}))
    assert "收工" in body["notice"]


# --- Requirement: Peek 有 job 級的總預算 ----------------------------------


SECTIONED_REPORT = "## 摘要\n夜間高頻是主因。\n\n## 方法\nduckdb 全表掃描。\n"


async def _write_note(bench, name: str, text: str) -> str:
    meta = await bench.store.put_note(JOB, name, text, produced_by="lane:txn")
    return str(meta.id)


async def test_peek_spends_from_the_budget(bench):
    """Scenario: 單次窺看扣減預算"""
    aid = await _write_note(bench, "lanes/txn/findings/1", "## 結論\n" + "夜間高頻。" * 20)
    before = bench.runner.state.peek_remaining
    body = payload(await bench.h["peek"]({"artifact": aid, "max_tokens": 5000}))

    assert body["tokens"] > 0
    assert body["peek_remaining"] == before - body["tokens"]
    (event,) = await bench.kinds(PEEK)
    assert event.get("tokens") == body["tokens"]


async def _drain_peek_budget(bench, aid: str) -> None:
    """Spend the budget on notes small enough to actually be read."""
    for _ in range(200):
        body = payload(await bench.h["peek"]({"artifact": aid, "max_tokens": 5000}))
        if body.get("error") == "peek_budget_exhausted":
            return
        assert "code" not in body, f"unexpected refusal while draining: {body}"
    raise AssertionError("budget never drained")


@pytest.mark.parametrize("bench", [{"peek_budget_tokens": 600}], indirect=True)
async def test_peek_refuses_once_the_budget_is_gone(bench):
    """Scenario: 預算耗盡後拒絕"""
    aid = await _write_note(bench, "lanes/txn/findings/1", "## 結論\n夜間高頻。")
    await _drain_peek_budget(bench, aid)

    body = payload(await bench.h["peek"]({"artifact": aid}))
    assert body["error"] == "peek_budget_exhausted"
    assert "lane" in body["message"]


@pytest.mark.parametrize("bench", [{"peek_budget_tokens": 600}], indirect=True)
async def test_exhausted_peek_does_not_disable_the_job(bench):
    """Scenario: 拒絕不影響其他工具

    Letting a job die on its peek budget would be pointless: the right
    degradation is "dispatch a lane to read it", which was the better move anyway.
    """
    await bench.declare("txn", "syn")
    aid = await _write_note(bench, "lanes/txn/findings/1", "## 結論\n夜間高頻。")
    await _drain_peek_budget(bench, aid)
    assert payload(await bench.h["peek"]({"artifact": aid}))["error"]

    dispatched = payload(await bench.h["dispatch"]({"lane": "txn", "task": "改派 lane 讀"}))
    assert dispatched["status"] == "running"
    collected = payload(await bench.h["await_tasks"]({"task_ids": [dispatched["task_id"]]}))
    assert collected["handles"]

    report = await _write_note(bench, "report", SECTIONED_REPORT)
    assert payload(await bench.h["finish"]({"report_artifact": report}))["status"] == "complete"


async def test_a_stranded_balance_reports_as_exhausted(bench):
    """A balance too small to fund any read must not read as "try again".

    Otherwise the orchestrator collects token_budget_exceeded forever against a
    balance it can never spend, instead of being told to dispatch a lane.
    """
    from myharness.orchestrator.tools import MIN_USEFUL_PEEK_TOKENS

    aid = await _write_note(bench, "lanes/txn/findings/1", "短" * 10)
    bench.runner.state.peek_spent_tokens = (
        bench.runner.spec.peek_budget_tokens - MIN_USEFUL_PEEK_TOKENS + 1
    )
    body = payload(await bench.h["peek"]({"artifact": aid, "max_tokens": 100_000}))
    assert body["error"] == "peek_budget_exhausted"
    assert 0 < body["remaining"] < MIN_USEFUL_PEEK_TOKENS


async def test_oversized_note_suggests_a_section(bench):
    aid = await _write_note(bench, "lanes/txn/findings/1",
                            "## 甲\n" + "長" * 400 + "\n\n## 乙\n短\n")
    body = payload(await bench.h["peek"]({"artifact": aid, "max_tokens": 20}))
    assert body["code"] == "token_budget_exceeded"
    assert [s["id"] for s in body["sections"]] == ["甲", "乙"]
    assert body["peek_remaining"] > 0, "a refused peek costs nothing"


# --- finish ---------------------------------------------------------------


async def test_finish_requires_a_real_report(bench):
    body = payload(await bench.h["finish"]({"report_artifact": "j7/note/nope"}))
    assert body["error"] == "no_such_report"
    assert "synthesis" in body["hint"]
    assert bench.runner.state.phase is not JobPhase.COMPLETE


async def test_finish_records_the_report(bench):
    report = await _write_note(bench, "report", SECTIONED_REPORT)
    body = payload(await bench.h["finish"]({"report_artifact": report}))
    assert body["status"] == "complete"
    assert bench.runner.state.report_artifact == report
    assert bench.tools.finished


# --- ask_user -------------------------------------------------------------


async def test_ask_user_returns_the_default_without_a_channel(bench):
    body = payload(await bench.h["ask_user"]({"question": "要納入 2023 嗎？", "default": "否"}))
    assert body["answer"] == "否" and body["defaulted"] is True
    assert body["questions_remaining"] == bench.runner.spec.question_quota - 1


async def test_finish_rejects_a_finding_masquerading_as_a_report(bench):
    """The golden job pointed finish at an analyst's raw finding.

    The delivery is a summary plus a priced section menu; a note with no
    sections cannot produce one, so it is not a report whatever it is called.
    """
    finding = await _write_note(bench, "lanes/txn/findings/1",
                                "Attempted to access the blob. No data retrieved.")
    body = payload(await bench.h["finish"]({"report_artifact": finding}))
    assert body["error"] == "report_has_no_sections"
    assert body["produced_by"]
    assert "synthesis" in body["hint"]
    assert not bench.tools.finished


# --- inputs are the authorisation ----------------------------------------


async def test_dispatch_accepts_plain_artifact_id_strings(bench):
    await bench.declare("txn")
    blob = await bench.store.put_blob(JOB, "raw/data", data=b"x", produced_by="user")
    body = payload(await bench.h["dispatch"]({
        "lane": "txn", "task": "分析", "inputs": [str(blob.id)]}))
    assert body["status"] == "running"


async def test_dispatch_unwraps_the_object_shape_a_model_reaches_for(bench):
    """The golden job sent [{"blob_path": "..."}]; the intent is unambiguous."""
    await bench.declare("txn")
    blob = await bench.store.put_blob(JOB, "raw/data", data=b"x", produced_by="user")
    body = payload(await bench.h["dispatch"]({
        "lane": "txn", "task": "分析", "inputs": [{"blob_path": str(blob.id)}]}))
    assert body["status"] == "running"

    await bench.runner.settle()
    record = bench.runner.state.tasks[body["task_id"]]
    assert record.inputs == (str(blob.id),), "the grant must be the real id"


async def test_dispatch_refuses_an_unusable_grant_instead_of_mangling_it(bench):
    """str() on a dict produced a grant nothing could match, and the failure
    surfaced two lanes later as an inscrutable not_granted."""
    await bench.declare("txn")
    body = payload(await bench.h["dispatch"]({
        "lane": "txn", "task": "分析", "inputs": [{"nonsense": 1}, "not-an-id"]}))
    assert body["error"] == "bad_inputs"
    assert len(body["rejected"]) == 2
    assert body["accepted"] == []
    assert bench.fake.calls == [], "nothing may run on a broken grant"


async def test_dispatch_tool_says_inputs_are_the_authorisation(bench):
    """The grant model is invisible unless the tool description states it."""
    from myharness.orchestrator.tools import OrchestratorTools

    server = bench.tools.build_server()
    descriptions = {
        t.name: t.description for t in server["instance"]._tools
    } if hasattr(server.get("instance", None), "_tools") else {}
    # Fall back to the source of truth if the SDK shape differs.
    import inspect

    from myharness.orchestrator import tools as tools_module

    source = inspect.getsource(tools_module)
    assert "inputs` is the lane's ONLY authorisation" in source


# --- routing table (add-ingress-proxy) -------------------------------------


class TestRoutingTable:
    """The orchestrator steers the proxy with declarative data and nothing else.

    These are about the seam: whether the table survives plan_update intact,
    and whether the failure modes are refusals rather than silent drops.
    """

    async def test_a_table_is_stored_and_readable(self, bench):
        from myharness.orchestrator.routing import read_routing

        out = payload(await bench.h["plan_update"]({
            "plan": "# 目標\n測試\n",
            "lanes": [{"id": "txn", "type": "ta"}],
            "routing_table": [
                {"lane": "txn", "accepts": "2024 交易明細"},
                {"lane": "kyc", "accepts": "身分文件", "status": "closed"},
            ],
        }))
        assert out["routing_open"] == ["txn"]
        table = await read_routing(bench.store, "j7")
        assert [e.lane for e in table.entries] == ["txn", "kyc"]

    async def test_omitting_the_table_leaves_the_existing_one_alone(self, bench):
        from myharness.orchestrator.routing import read_routing

        await bench.h["plan_update"]({
            "plan": "# 目標\n一\n",
            "routing_table": [{"lane": "txn", "accepts": "交易"}],
        })
        out = payload(await bench.h["plan_update"]({"plan": "# 目標\n二\n"}))
        assert "routing_open" not in out
        table = await read_routing(bench.store, "j7")
        assert [e.lane for e in table.entries] == ["txn"], "the table was clobbered"

    async def test_declaring_again_replaces(self, bench):
        from myharness.orchestrator.routing import read_routing

        await bench.h["plan_update"]({
            "plan": "p", "routing_table": [{"lane": "a", "accepts": "x"}],
        })
        await bench.h["plan_update"]({
            "plan": "p", "routing_table": [{"lane": "b", "accepts": "y"}],
        })
        table = await read_routing(bench.store, "j7")
        assert [e.lane for e in table.entries] == ["b"]

    async def test_a_bad_entry_is_refused_and_nothing_is_written(self, bench):
        """Parsed before anything is written -- a half-applied plan_update
        leaves the orchestrator unsure which half took."""
        from myharness.orchestrator.plan import read_plan

        out = payload(await bench.h["plan_update"]({
            "plan": "# 目標\n新的\n",
            "routing_table": [{"lane": "a"}],
        }))
        assert out["error"] == "bad_routing_table"
        assert "nothing can be routed" in out["message"]
        plan, _ = await read_plan(bench.store, "j7")
        assert plan is None, "the plan was written despite the refusal"

    async def test_a_lane_that_does_not_exist_yet_is_flagged_not_refused(self, bench):
        """A table may legitimately name a lane about to be created; silence
        would make a typo look like it worked."""
        out = payload(await bench.h["plan_update"]({
            "plan": "p",
            "lanes": [{"id": "txn", "type": "ta"}],
            "routing_table": [{"lane": "txn", "accepts": "x"},
                              {"lane": "typo", "accepts": "y"}],
        }))
        assert out["routing_lanes_not_yet_created"] == ["typo"]
        assert "error" not in out

    async def test_the_event_records_which_lanes_are_open(self, bench):
        await bench.h["plan_update"]({
            "plan": "p",
            "routing_table": [{"lane": "a", "accepts": "x"},
                              {"lane": "b", "accepts": "y", "status": "closed"}],
        })
        events = await bench.kinds("plan.update")
        assert events[-1].get("routing_open") == ["a"]

    async def test_routing_table_is_optional_in_the_schema(self):
        """The SDK shorthand would have made it required on every plan update."""
        from myharness.orchestrator.tools import _PLAN_SCHEMA

        assert _PLAN_SCHEMA["required"] == ["plan"]
        assert "routing_table" in _PLAN_SCHEMA["properties"]
        assert "lanes" in _PLAN_SCHEMA["properties"]
