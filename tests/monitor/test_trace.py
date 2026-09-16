"""一次派工裡實際發生了什麼：把 transcript 解析成依序的步驟。

這一層的每一條斷言都對應規格裡的一句話，而規格裡最重要的那一條是
「未留存與非精確的內容不得被冒充」—— 推理文字沒有被留存，所以這裡驗的
不只是「有解析出來」，還有「沒有解析出不存在的東西」。
"""

from __future__ import annotations

import json
from pathlib import Path

from myharness.monitor.trace import StepKind, parse_trace

FIXTURES = Path(__file__).parent / "fixtures"


def rows(*items: dict) -> list[dict]:
    return list(items)


def assistant(*blocks: dict) -> dict:
    return {"role": "assistant", "content": list(blocks)}


def user(*blocks: dict) -> dict:
    return {"role": "user", "content": list(blocks)}


def call(name: str = "mcp__lane__duckdb_query", **args) -> dict:
    return {"type": "tool_use", "name": name, "input": args}


def result(content: str, *, error: bool = False) -> dict:
    return {"type": "tool_result", "tool_use_id": "call_1",
            "is_error": error, "content": content}


# --- Requirement: 一次派工的逐輪軌跡可被檢視 ------------------------------


def test_calls_and_results_are_paired():
    """Scenario: 工具呼叫與其結果成對顯示"""
    trace = parse_trace(rows(
        assistant(call("mcp__lane__inspect_blob", artifact="a")),
        user(result("2,940 rows")),
        assistant(call("mcp__lane__duckdb_query", sql="SELECT 1")),
        user(result("1")),
    ))
    calls = [s for s in trace.steps if s.kind is StepKind.CALL]
    results = [s for s in trace.steps if s.kind is StepKind.RESULT]
    assert [c.name for c in calls] == [
        "mcp__lane__inspect_blob", "mcp__lane__duckdb_query"]
    assert [trace.steps[r.answers].name for r in results] == [  # type: ignore[index]
        "mcp__lane__inspect_blob", "mcp__lane__duckdb_query"]


def test_order_is_the_order_it_happened():
    """Scenario: 順序保留"""
    trace = parse_trace(rows(
        assistant({"type": "thinking", "chars": 40}),
        assistant(call()),
        user(result("ok")),
    ))
    assert [s.kind for s in trace.steps] == [
        StepKind.THINK, StepKind.CALL, StepKind.RESULT]


def test_a_failed_call_is_marked_as_failed():
    """Scenario: 失敗的工具呼叫可被辨識"""
    trace = parse_trace(rows(
        assistant(call()), user(result("Binder Error: no such column", error=True))))
    failed = [s for s in trace.steps if s.kind is StepKind.RESULT and s.error]
    assert len(failed) == 1


def test_turns_are_numbered_per_assistant_message():
    trace = parse_trace(rows(
        assistant({"type": "thinking", "chars": 1}, call()),
        user(result("ok")),
        assistant(call()),
    ))
    assert trace.turns == 2
    assert [s.turn for s in trace.steps if s.kind is StepKind.CALL] == [1, 2]


def test_attempts_are_counted():
    trace = parse_trace(rows(
        {"role": "system", "subtype": "init"},
        assistant(call()),
        {"role": "result", "subtype": "error_during_execution",
         "is_error": True, "turns": 1},
        {"role": "system", "subtype": "init"},
        assistant(call()),
    ))
    assert trace.attempts == 2


def test_an_unknown_block_type_is_kept_rather_than_dropped():
    """事件流對未知型別寬容，transcript 也該一樣 —— 丟掉等於這一步沒發生過。"""
    trace = parse_trace(rows(assistant({"type": "something_new_in_the_sdk"})))
    assert [s.kind for s in trace.steps] == [StepKind.OTHER]
    assert "something_new_in_the_sdk" in trace.steps[0].detail


# --- Requirement: 未留存與非精確的內容不得被冒充 --------------------------


def test_reasoning_records_its_length_and_nothing_else():
    """Scenario: 推理內容不可得

    `_block_to_dict` 只留字元數。解析出來的步驟必須也只有字元數 ——
    這裡驗的是它沒有從別處生出一段文字來。
    """
    trace = parse_trace(rows(assistant({"type": "thinking", "chars": 812})))
    step = trace.steps[0]
    assert step.kind is StepKind.THINK
    assert step.chars == 812
    assert step.text == ""


def test_an_empty_thinking_block_is_not_the_same_as_an_unrecorded_one():
    """Scenario: 後端回傳空的推理區塊

    自架後端回的是 53 bytes 的空殼。「後端根本沒想」與「想了但我們沒留」
    是兩件事，合併它們就是在編造其中一件。
    """
    empty = parse_trace(rows(assistant({"type": "thinking", "chars": 0}))).steps[0]
    kept = parse_trace(rows(assistant({"type": "thinking", "chars": 300}))).steps[0]
    assert empty.chars == 0
    assert kept.chars == 300
    assert empty.empty_reasoning
    assert not kept.empty_reasoning


def test_an_excerpted_result_says_how_much_was_skipped():
    """Scenario: 被截斷的工具結果"""
    trace = parse_trace(rows(
        assistant(call()),
        user(result("頭\n…[略過 4821 字元]…\n尾")),
    ))
    step = next(s for s in trace.steps if s.kind is StepKind.RESULT)
    assert step.skipped == 4821


def test_a_complete_result_claims_nothing_was_skipped():
    trace = parse_trace(rows(assistant(call()), user(result("短短的結果"))))
    assert next(s for s in trace.steps if s.kind is StepKind.RESULT).skipped == 0


def test_the_harness_note_is_separated_from_what_the_tool_said():
    """Harness 把自己的指示附在工具結果之後。兩者混在一起，讀的人會以為
    工具回了這句話 —— 而它是 harness 說的。"""
    trace = parse_trace(rows(
        assistant(call()),
        user(result(
            "wrote lanes/a/findings/x (1548 est tokens)\n\n"
            "[harness] token 預算已用 89%，已經付不起下一次請求。")),
    ))
    step = next(s for s in trace.steps if s.kind is StepKind.RESULT)
    assert step.body == "wrote lanes/a/findings/x (1548 est tokens)"
    assert "[harness]" not in step.body
    assert "已經付不起下一次請求" in step.harness


def test_the_only_per_turn_budget_signal_is_read_out():
    """Scenario: 警告的位置

    transcript 不帶任何 token 計數。Harness 附加的那句話是唯一的逐輪訊號。
    """
    trace = parse_trace(rows(
        assistant(call()), user(result("ok\n\n[harness] token 預算已用 57%，只夠再 5 次請求")),
        assistant(call()), user(result("ok")),
        assistant(call()), user(result("ok\n\n[harness] token 預算已用 89%，已經付不起下一次請求")),
    ))
    assert [s.budget_pct for s in trace.budget_marks] == [57, 89]


def test_a_result_with_no_call_is_counted_not_hidden():
    """配對是照順序的，因為留存的 tool_use 沒有 id。配不到就得說。"""
    trace = parse_trace(rows(user(result("孤兒"))))
    assert trace.unpaired_results == 1
    assert next(s for s in trace.steps if s.kind is StepKind.RESULT).answers is None


# --- 真實資料的回歸 -------------------------------------------------------


def test_the_real_golden24_analyst_transcript():
    """golden #24 的 d1：21 turns、14 次工具呼叫、7 個推理標記、3 個預算量測點。

    而 7 個推理標記的字元數**全部是 0** —— 這一行是整個視圖存在理由的一半。
    """
    rows_ = [json.loads(line) for line
             in (FIXTURES / "golden24-d1.jsonl").read_text(encoding="utf-8").splitlines()
             if line.strip()]
    trace = parse_trace(rows_)

    calls = [s for s in trace.steps if s.kind is StepKind.CALL]
    results = [s for s in trace.steps if s.kind is StepKind.RESULT]
    thinks = [s for s in trace.steps if s.kind is StepKind.THINK]

    assert len(calls) == 14
    assert len(results) == 14
    assert len(thinks) == 7
    assert all(s.empty_reasoning for s in thinks)
    assert trace.unpaired_results == 0
    assert [s.budget_pct for s in trace.budget_marks] == [57, 77, 89]
