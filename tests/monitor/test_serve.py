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
    root, url = running_job
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
