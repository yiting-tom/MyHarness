"""即時模式的瀏覽器表面。

這是整個 package 裡**唯一會聽 port 的東西** —— MCP 走 stdio，終端機 monitor
什麼都不聽。所以下面關於 loopback、GET-only 與路徑的斷言不是形式，
它們是這個 change 唯一真正新增的攻擊面上的欄杆。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from myharness.dataflow import build_dataflow
from myharness.loopback import NotLoopback
from myharness.monitor.live import LiveView, current_activity
from myharness.monitor.serve import (
    build_state,
    build_trace,
    make_server,
    safe_id,
    serve,
)
from tests.dataflow.conftest import JOB, Stream

BLOB = f"{JOB}/blob/raw/txns"
F1 = f"{JOB}/note/lanes/a/findings/1"


def write_job(root: Path, stream: Stream) -> Path:
    path = root / "jobs" / JOB / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(e.to_json() for e in stream.events) + "\n",
                    encoding="utf-8")
    return path


def mid_flight() -> Stream:
    """一次派工開始了、讀了一份資料，還沒結束。"""
    return (Stream().start().ingress(BLOB)
            .dispatch("d1", "a", [BLOB]).read("d1", BLOB))


def finished() -> Stream:
    return (Stream().start().ingress(BLOB)
            .dispatch("d1", "a", [BLOB]).read("d1", BLOB).done("d1", "a", F1)
            .finish(F1))


@pytest.fixture
def running_job(tmp_path: Path):
    write_job(tmp_path, mid_flight())
    httpd, url = serve(tmp_path, JOB)
    yield tmp_path, url
    httpd.shutdown()
    httpd.server_close()


def get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return int(r.status), r.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8")


# --- Requirement: 即時視圖的 server 唯讀且只在本機可達 ---------------------


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.5", "example.com", "::"])
def test_a_non_loopback_bind_is_refused(tmp_path: Path, host: str):
    """Scenario: 拒絕非 loopback 的繫結

    主機名不算證據。位址才算。
    """
    write_job(tmp_path, finished())
    with pytest.raises(NotLoopback) as caught:
        make_server(tmp_path, JOB, host=host)
    assert host in str(caught.value)
    assert "loopback" in str(caught.value)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_is_allowed(tmp_path: Path, host: str):
    write_job(tmp_path, finished())
    httpd = make_server(tmp_path, JOB, host=host)
    httpd.server_close()


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_there_is_no_write_path(running_job, method: str):
    """Scenario: 沒有寫入路徑"""
    root, url = running_job
    path = root / "jobs" / JOB / "events.jsonl"
    before = path.read_bytes()
    listing = sorted(p.name for p in path.parent.iterdir())

    request = urllib.request.Request(url + "state", method=method, data=b"{}")
    try:
        with urllib.request.urlopen(request, timeout=5):
            pass
        served = True
    except urllib.error.HTTPError as exc:
        served = False
        assert exc.code in (400, 405, 501), exc.code

    assert not served, f"{method} 不該被服務"
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == listing


@pytest.mark.parametrize("bad", [
    "../../../etc/passwd", "..%2f..%2fetc", "/etc/passwd", "d1/../../x",
    ".hidden", "a" * 200, "d1;rm",
])
def test_an_identifier_from_the_path_is_not_trusted(bad: str):
    """Scenario: 路徑上的識別不被信任"""
    assert safe_id(bad) is None


def test_a_real_dispatch_id_still_passes():
    assert safe_id("d1") == "d1"
    assert safe_id("d12.a-b_c") == "d12.a-b_c"


def test_a_traversal_on_the_wire_is_refused(running_job):
    _, url = running_job
    status, body = get(url + "trace/..%2F..%2Fetc%2Fpasswd")
    assert status == 400
    assert "bad_id" in body


# --- Requirement: 即時模式有一個瀏覽器的表面 -------------------------------


def test_the_two_surfaces_say_the_same_thing(tmp_path: Path):
    """Scenario: 兩種表面說法一致

    終端機與瀏覽器讀的是同一個 `current_activity`。它們要是說得不一樣，
    那是缺陷不是特性。
    """
    write_job(tmp_path, mid_flight())
    events = mid_flight().events
    flow = build_dataflow(events, job_id=JOB)
    terminal = current_activity(events, flow)

    state = build_state(tmp_path, JOB)
    assert state["activity"]["state"] == terminal.state
    assert state["activity"]["detail"] == terminal.detail
    assert state["running"] == ["d1"]

    frame = LiveView(JOB).render(events, colour=False)
    assert terminal.state in frame


def test_a_new_dispatch_shows_up_on_the_next_poll(running_job):
    """Scenario: 不重新載入就反映新事件"""
    root, url = running_job
    first = json.loads(get(url + "state")[1])
    assert [d["id"] for d in first["dispatches"]] == ["d1"]

    grown = mid_flight().dispatch("d2", "b", [F1])
    write_job(root, grown)

    second = json.loads(get(url + "state")[1])
    assert [d["id"] for d in second["dispatches"]] == ["d1", "d2"]
    assert second["seq"] > first["seq"]


def test_waiting_is_not_reported_as_working(tmp_path: Path):
    """Scenario: 執行中的等待可被辨識"""
    stream = mid_flight()
    stream._add("throttle.wait", backend="or", seconds=42)
    write_job(tmp_path, stream)

    state = build_state(tmp_path, JOB)
    assert state["activity"]["state"] == "等待限流"
    assert "42" in state["activity"]["detail"] or "42s" in state["activity"]["detail"]


def test_a_finished_job_is_marked_finished(tmp_path: Path):
    """Scenario: 結束後停下來 —— 頁面靠這個欄位決定停止輪詢。"""
    write_job(tmp_path, finished())
    assert build_state(tmp_path, JOB)["finished"] is True
    assert build_state(tmp_path, JOB)["report"] == F1


# --- Requirement: 還沒被寫下的紀錄不得被冒充 -------------------------------


def test_a_running_dispatch_has_no_turns_and_says_why(tmp_path: Path):
    """Scenario: 執行中的派工沒有逐輪紀錄

    transcript 是派工**結束時**才落檔的。所以這裡不是「還沒載入」，
    是「還不存在」—— 而回一個空的步驟清單，讀者會以為它什麼都沒做。
    """
    write_job(tmp_path, mid_flight())
    answer = build_trace(tmp_path, JOB, "d1")
    assert answer["state"] == "running"
    assert "還不存在" in answer["why"]
    assert "steps" not in answer


def test_a_finished_dispatch_with_no_transcript_says_so(tmp_path: Path):
    write_job(tmp_path, finished())
    answer = build_trace(tmp_path, JOB, "d1")
    assert answer["state"] == "absent"
    assert answer["why"]


def test_what_a_running_dispatch_has_already_opened_is_visible(tmp_path: Path):
    """Scenario: 執行中仍看得到已讀取的東西

    artifact.read 是執行期間就寫下的，所以它是唯一會在派工活著的時候動的訊號。
    """
    write_job(tmp_path, mid_flight())
    d1 = build_state(tmp_path, JOB)["dispatches"][0]
    assert d1["running"] is True
    assert d1["opened"] == [BLOB]
    assert d1["produced"] == []


def test_an_unknown_dispatch_is_an_error_not_an_empty_trace(tmp_path: Path):
    write_job(tmp_path, finished())
    assert build_trace(tmp_path, JOB, "d99")["error"] == "no_such_dispatch"


def test_an_unknown_job_is_an_error(tmp_path: Path):
    assert build_state(tmp_path, "nope")["error"] == "no_such_job"


# --- 頁面本身 -------------------------------------------------------------


def test_the_page_pulls_nothing_from_the_network(running_job):
    """Scenario: （沿用）離線 —— 只有同源的 state / trace，沒有第三方資源。"""
    _, url = running_job
    status, body = get(url)
    assert status == 200
    assert "<!doctype html>" in body.lower()
    assert not re.findall(r"""(?:src|href)\s*=\s*["']https?://[^"']+""", body)


def test_the_page_arrives_with_the_first_frame_already_in_it(running_job):
    """開頁不該先看到一片空白再等一秒。"""
    _, url = running_job
    _, body = get(url)
    assert '"job_id":"' + JOB in body.replace("\\u003c", "<")


def test_an_unknown_route_is_a_404(running_job):
    _, url = running_job
    assert get(url + "wat")[0] == 404


# --- 空的地方要說為什麼 ---------------------------------------------------
#
# 監看頁上一塊空白至少有四種原因，而它們要的反應正好相反：job 還沒開始、
# 開始了但還沒派工、結束了卻一次都沒派、或者 monitor 指錯了地方。
# 空白一種都沒說。


def test_a_missing_job_says_where_it_looked_and_what_is_there(tmp_path: Path):
    write_job(tmp_path, finished())
    state = build_state(tmp_path, "typo")
    assert state["error"] == "no_such_job"
    assert str(tmp_path) in state["why"]
    assert "--root" in state["why"], "最常見的原因要被點名"
    assert state["known"] == [JOB]


def test_an_empty_stream_says_the_job_has_not_started(tmp_path: Path):
    path = tmp_path / "jobs" / JOB / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")
    assert "還沒寫下任何事件" in build_state(tmp_path, JOB)["flow_empty_why"]


def test_no_dispatch_yet_is_explained(tmp_path: Path):
    write_job(tmp_path, Stream().start())
    why = build_state(tmp_path, JOB)["flow_empty_why"]
    assert "還沒派出任何工作" in why


def test_finishing_without_a_single_dispatch_is_not_the_same_as_not_yet(tmp_path: Path):
    write_job(tmp_path, Stream().start().finish(None, reason="limit"))
    why = build_state(tmp_path, JOB)["flow_empty_why"]
    assert "已經結束" in why and "一次派工都沒有" in why
    assert "還沒" not in why


def test_a_flow_with_dispatches_has_nothing_to_explain(tmp_path: Path):
    write_job(tmp_path, mid_flight())
    assert build_state(tmp_path, JOB)["flow_empty_why"] == ""


@pytest.mark.parametrize(("status", "says"), [
    ("running", "還在跑"),
    ("ok", "回報完成，但沒有寫出"),
    ("budget_exceeded", "預算"),
    ("max_turns", "來回次數"),
    ("tool_failure", "工具出錯"),
    ("something_new", "something_new"),
])
def test_no_output_says_which_kind_of_no_output(status: str, says: str):
    """「沒有產出」有好幾種，而預算用完跟回報完成卻沒寫，對讀者是相反的事。"""
    from myharness.monitor.serve import explain_no_output
    assert says in explain_no_output(status)


def test_a_dispatch_that_wrote_nothing_carries_its_reason(tmp_path: Path):
    stream = (Stream().start().ingress(BLOB).dispatch("d1", "a", [BLOB])
              .done("d1", "a", None, status="budget_exceeded"))
    write_job(tmp_path, stream)
    d1 = build_state(tmp_path, JOB)["dispatches"][0]
    assert "預算" in d1["nothing_written_why"]
    assert d1["nothing_granted_why"] == ""


def test_every_trace_answer_that_is_not_ready_has_a_reason(tmp_path: Path):
    write_job(tmp_path, mid_flight())
    for answer in (build_trace(tmp_path, JOB, "d1"),
                   build_trace(tmp_path, JOB, "d99"),
                   build_trace(tmp_path, "typo", "d1")):
        assert answer["state"] != "ready"
        assert answer["why"], answer


def test_an_empty_transcript_is_explained_not_rendered_as_nothing(tmp_path: Path):
    stream = (Stream().start().ingress(BLOB).dispatch("d1", "a", [BLOB]))
    stream._add("dispatch.end", id="d1", lane="a", status="tool_failure",
                transcript=f"{JOB}/blob/traces/d1", tokens={})
    write_job(tmp_path, stream)
    blob = tmp_path / "jobs" / JOB / "blobs" / "traces" / "d1"
    blob.parent.mkdir(parents=True)
    blob.write_text('{"role": "system", "subtype": "init"}\n', encoding="utf-8")

    # One init row is one ATTEMPT step, so this one is not empty -- write a
    # truly empty file for the case under test.
    blob.write_text("", encoding="utf-8")
    answer = build_trace(tmp_path, JOB, "d1")
    assert answer["state"] == "absent"
    assert "一輪都沒有" in answer["why"]


def test_without_read_records_grants_are_not_drawn_as_unopened(tmp_path: Path):
    """沒有 artifact.read 的舊事件流：把每個授權都畫成「沒打開」是冒充。"""
    stream = (Stream().start().ingress(BLOB).dispatch("d1", "a", [BLOB])
              .done("d1", "a", F1))
    write_job(tmp_path, stream)
    assert build_state(tmp_path, JOB)["read_edges_available"] is False


def test_the_cli_refuses_to_serve_a_job_that_is_not_there(tmp_path: Path, capsys):
    """一個只會說「找不到」的頁面，比終端機上一行字更糟。"""
    from myharness.monitor.cli import main
    write_job(tmp_path, finished())
    assert main(["--root", str(tmp_path), "monitor", "typo", "--web"]) == 1
    out = capsys.readouterr().out
    assert JOB in out and "--wait" in out


def test_the_page_never_shows_a_blank_where_a_reason_belongs(running_job):
    """頁面上每一種空的狀態都有對應的文字。"""
    _, url = running_job
    _, body = get(url)
    for reason in ("renderMissing", "flow_empty_why", "nothing_written_why",
                   "nothing_granted_why", "事件流還是空的", "沒有說明原因",
                   "沒有讀取紀錄"):
        assert reason in body, reason


# --- steps: what an agent is doing while it runs --------------------------


def _step(stream: Stream, did: str, turn: int, pct: float) -> Stream:
    return stream._add("lane.step", dispatch=did, lane="a", phase="turn", attempt=1,
                       turn=turn, spent=int(pct * 1000), pct=pct,
                       calls=[{"tool": "run_query", "arg": f"sql=SELECT {turn}"}],
                       thinking_chars=0, text_chars=4)


def test_a_running_agent_carries_its_steps(tmp_path: Path):
    """Scenario: 執行中的 agent 顯示當下的步驟"""
    write_job(tmp_path, _step(_step(mid_flight(), "d1", 1, 0.1), "d1", 2, 0.2))
    state = build_state(tmp_path, JOB)
    (d1,) = state["dispatches"]
    assert state["steps_recorded"]
    assert [s["turn"] for s in d1["steps"]] == [1, 2] and d1["step_count"] == 2
    assert d1["steps"][-1]["calls"][0]["arg"] == "sql=SELECT 2"
    assert d1["steps_why"] == ""


def test_steps_stay_off_the_event_column(tmp_path: Path):
    stream = mid_flight()
    for i in range(30):
        stream = _step(stream, "d1", i + 1, i / 40)
    write_job(tmp_path, stream)
    lines = [e["t"] for e in build_state(tmp_path, JOB)["events"]]
    assert "lane.step" not in lines and "dispatch.start" in lines


def test_an_old_stream_is_not_drawn_as_an_idle_agent(tmp_path: Path):
    """Scenario: 舊的事件流"""
    write_job(tmp_path, finished())
    state = build_state(tmp_path, JOB)
    assert not state["steps_recorded"]
    why = state["dispatches"][0]["steps_why"]
    assert "lane.step" in why and "不代表它閒著" in why


def test_a_dispatch_that_just_started_is_waiting_for_its_first_answer(tmp_path: Path):
    """Scenario: 剛開始的派工"""
    stream = _step(finished(), "d1", 1, 0.1).dispatch("d2", "b", [BLOB])
    write_job(tmp_path, stream)
    d2 = next(d for d in build_state(tmp_path, JOB)["dispatches"] if d["id"] == "d2")
    assert "等第一輪回應" in d2["steps_why"]


def test_a_running_dispatch_on_a_stepless_stream_admits_both_causes(tmp_path: Path):
    write_job(tmp_path, mid_flight())
    why = build_state(tmp_path, JOB)["dispatches"][0]["steps_why"]
    assert "第一輪回應" in why and "舊版" in why


def test_a_dispatch_that_ended_before_its_first_turn_says_so(tmp_path: Path):
    stream = (Stream().start().ingress(BLOB).dispatch("d1", "a", [BLOB])
              .done("d1", "a", None, status="tool_failure", turns=0))
    write_job(tmp_path, stream)
    assert "第一輪回應之前" in build_state(tmp_path, JOB)["dispatches"][0]["steps_why"]
