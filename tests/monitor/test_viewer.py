"""第三種 render：同一個模型、同一組異常，換一個表面。

它只要在任何一件事上跟 ASCII 或 JSON 說得不一樣，就是缺陷而不是特性。
"""

from __future__ import annotations

import json
import re

from myharness.dataflow import build_dataflow, detect
from myharness.monitor.inspect import render_inspect
from myharness.monitor.trace import parse_trace
from myharness.monitor.viewer import render_html
from tests.dataflow.conftest import JOB, Stream

BLOB = f"{JOB}/blob/raw/txns"
F1 = f"{JOB}/note/lanes/a/findings/1"
REPORT = f"{JOB}/note/report"


def healthy() -> Stream:
    return (Stream().start().ingress(BLOB)
            .dispatch("d1", "a", [BLOB]).done("d1", "a", F1)
            .dispatch("d2", "syn", [F1]).done("d2", "syn", REPORT)
            .finish(REPORT))


def ungranted() -> Stream:
    return (Stream().start().ingress(BLOB)
            .dispatch("d1", "syn", []).done("d1", "syn", REPORT).finish(REPORT))


def html_of(stream: Stream, traces=None) -> str:
    events = stream.events
    flow = build_dataflow(events)
    return render_html(flow, events, traces or {})


# --- Requirement: 視覺輸出是同一個模型的投影，且有文字等價物 --------------


def test_the_three_renders_name_the_same_anomalies():
    """Scenario: 三種輸出一致"""
    events = ungranted().events
    flow = build_dataflow(events)
    kinds = [str(a.kind) for a in detect(flow)]
    assert kinds, "這個 stream 本來就該有異常，否則這個測試什麼都沒驗到"

    ascii_out = render_inspect(flow, events, colour=False)
    html_out = render_html(flow, events, {})
    for kind in kinds:
        assert kind in html_out
    assert ("CRITICAL" in ascii_out) == ("critical" in html_out)


def test_a_clean_job_says_so_in_html_too():
    out = html_of(healthy())
    assert "ungranted_production" not in out


def test_edges_carry_their_direction_and_kind_as_text():
    """Scenario: 圖形關係有文字等價物

    授權與產出的差別不能只靠位置或顏色表達 —— 終端機那份用的是
    ←授權 / →產出，這裡沿用同一組字，讀者才不用翻譯。
    """
    out = html_of(healthy())
    assert "←授權" in out
    assert "→產出" in out


def test_the_golden_fifth_run_still_shows_its_critical_anomaly(golden5):
    """那一次在所有紀律指標上都是綠的，報告卻來自一次沒有授權的派工。"""
    events, artifacts = golden5
    flow = build_dataflow(events, artifacts)
    out = render_html(flow, events, {})
    assert "ungranted_production" in out
    assert "critical" in out


# --- Requirement: 視覺輸出唯讀、離線、且不預設留在 job 目錄 ---------------


def test_nothing_is_fetched_from_the_network():
    """Scenario: 離線開啟

    一份要被帶去沒有網路的地方讀的檔案。字型、圖表函式庫、追蹤像素
    —— 一個都不能有。
    """
    out = html_of(healthy())
    external = re.findall(r"""(?:src|href)\s*=\s*["']https?://[^"']+""", out)
    assert not external, external


def test_the_page_carries_its_own_data():
    """沒有 fetch、沒有 XHR：資料就在檔案裡，否則離線打開是一片空白。"""
    out = html_of(healthy())
    assert "fetch(" not in out
    assert "XMLHttpRequest" not in out


# --- Requirement: 未留存與非精確的內容不得被冒充 --------------------------


def test_the_page_says_reasoning_was_never_kept():
    """Scenario: 推理內容不可得"""
    trace = parse_trace([
        {"role": "assistant", "content": [{"type": "thinking", "chars": 0}]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "name": "mcp__lane__duckdb_query", "input": {}}]},
    ])
    out = html_of(healthy(), {"d1": trace})
    assert "未留存" in out


def test_estimated_token_counts_are_marked_as_estimates():
    """Scenario: 估計的 token 數"""
    stream = (Stream().start().ingress(BLOB)
              .dispatch("d1", "a", [BLOB])
              .done("d1", "a", F1, status="budget_exceeded",
                    tokens={"in": 57197, "out": 3908, "estimated": True})
              .finish(F1))
    out = html_of(stream)
    assert "估計" in out


# --- 它必須是安全的 HTML，因為 task 與工具參數都是模型寫的 -----------------


def test_model_written_text_cannot_close_the_script_element():
    """task、SQL、工具結果全都是模型產生的字串，而它們就坐在 <script> 裡面。

    一個 </script> 就把元素關掉，後面的東西變成瀏覽器樂意執行的標記。
    所以驗的是「頁面上沒有多出任何元素」—— 而不是那串字有沒有消失：
    `<` 被轉義之後，`onerror=alert(1)` 只是 JS 字串裡的一段死字。
    """
    nasty = "</script><img src=x onerror=alert(1)>"
    stream = (Stream().start().ingress(BLOB)
              .dispatch("d1", "a", [BLOB], task=nasty).done("d1", "a", F1)
              .finish(F1))
    out = html_of(stream)

    assert out.count("</script>") == out.count("<script>")
    assert "<img" not in out
    assert "\\u003c/script" in out, "那段字必須還在，只是不能是標記"


def test_the_payload_survives_being_escaped():
    """轉義不能把資料弄壞 —— 頁面打開要看得到原本那串字。"""
    nasty = "</script>\u2028<b>"
    stream = (Stream().start().ingress(BLOB)
              .dispatch("d1", "a", [BLOB], task=nasty).done("d1", "a", F1)
              .finish(F1))
    payload = json.loads(
        html_of(stream).split("const DATA = ", 1)[1].split(";\n", 1)[0])
    assert payload["dispatches"][0]["task"] == nasty
