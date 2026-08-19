"""The two views, and the promise that neither of them writes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from myharness.dataflow import build_dataflow, detect
from myharness.monitor.cli import discover, main, resolve_root
from myharness.monitor.inspect import render_inspect
from myharness.monitor.live import LiveView, current_activity

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


# --- Requirement: 事後模式展開完整資料流 ----------------------------------


def test_inspect_shows_the_flow():
    """Scenario: 顯示流向"""
    events = healthy().events
    out = render_inspect(build_dataflow(events), events, colour=False)
    assert "d1" in out and "d2" in out
    assert "←授權" in out and "→產出" in out
    assert "raw/txns" in out


def test_inspect_highlights_anomalies_distinctly():
    """Scenario: 異常被凸顯"""
    events = ungranted().events
    out = render_inspect(build_dataflow(events), events, colour=False)
    assert "資料流異常" in out
    assert "CRITICAL" in out
    assert "沒有被授權任何輸入" in out


def test_inspect_says_so_when_a_job_is_clean():
    events = healthy().events
    out = render_inspect(build_dataflow(events), events, colour=False)
    assert "資料流異常" in out and "無" in out
    assert "CRITICAL" not in out


def test_inspect_shows_cost_attribution():
    """Scenario: 顯示成本歸屬"""
    events = healthy().events
    out = render_inspect(build_dataflow(events), events, colour=False)
    assert "成本歸屬" in out
    assert "syn" in out and "$" in out


def test_inspect_admits_read_data_is_unavailable():
    """Authorisation must not be allowed to look like reading (design.md D1)."""
    events = healthy().events
    out = render_inspect(build_dataflow(events), events, colour=False)
    assert "不可得" in out

    with_reads = healthy()
    with_reads.events.insert(3, Stream().read("d1", BLOB).events[0])
    out2 = render_inspect(build_dataflow(with_reads.events), with_reads.events,
                          colour=False)
    assert "不可得" not in out2


def test_inspect_marks_a_dispatch_with_no_grants():
    events = ungranted().events
    out = render_inspect(build_dataflow(events), events, colour=False)
    assert "←授權 （無）" in out


# --- Requirement: 即時模式顯示 job 正在做什麼 -----------------------------


def test_live_shows_running_and_finished_dispatches():
    """Scenario: 顯示進行中的派工"""
    stream = (Stream().start().ingress(BLOB)
              .dispatch("d1", "a", [BLOB]).done("d1", "a", F1)
              .dispatch("d2", "a", [BLOB]).dispatch("d3", "b", [BLOB]))
    out = LiveView(JOB).render(stream.events, colour=False)
    assert "d1" in out and "d2" in out and "d3" in out
    assert "執行中 2" in out


def test_live_distinguishes_throttling_from_thinking():
    """Scenario: 區分等待與運算

    29% of the fifth golden run went to throttle waiting and nothing said so.
    """
    waiting = Stream().start().dispatch("d1", "a")._add(
        "throttle.wait", backend="openrouter", seconds=42.0)
    flow = build_dataflow(waiting.events)
    activity = current_activity(waiting.events, flow)
    assert activity.state == "等待限流"
    assert "openrouter" in activity.detail

    thinking = Stream().start().dispatch("d1", "a")
    assert current_activity(thinking.events, build_dataflow(thinking.events)).state.endswith("執行中")


def test_live_reports_waiting_on_the_user():
    stream = Stream().start()._add("ask.user", qid="q1", text="要納入 2023 嗎？")
    activity = current_activity(stream.events, build_dataflow(stream.events))
    assert activity.state == "等待使用者回答"


def test_live_reports_a_handoff():
    stream = Stream().start()._add("handoff.restart", used=118_000, pct=0.6)
    assert current_activity(stream.events, build_dataflow(stream.events)).state == "交接重啟"


def test_live_updates_as_events_arrive():
    """Scenario: 新事件出現時更新"""
    view = LiveView(JOB)
    stream = Stream().start().dispatch("d1", "a", [BLOB])
    first = view.render(stream.events, colour=False)
    stream.done("d1", "a", F1).dispatch("d2", "syn", [F1])
    second = view.render(stream.events, colour=False)
    assert "d2" not in first and "d2" in second


def test_live_shows_a_final_summary_when_the_job_ends():
    """Scenario: Job 結束時停止"""
    out = LiveView(JOB).render(healthy().events, colour=False)
    assert "完成" in out and "已結束" in out
    assert REPORT in out


def test_live_surfaces_critical_anomalies_immediately():
    out = LiveView(JOB).render(ungranted().events, colour=False)
    assert "✖" in out


# --- Requirement: 輸出同時供人閱讀與供機器解析 ----------------------------


def test_structured_output_parses(tmp_path: Path, capsys):
    """Scenario: 結構化輸出可被解析"""
    _write_job(tmp_path, ungranted())
    assert main(["--root", str(tmp_path), "inspect", JOB, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["job_id"] == JOB
    assert payload["nodes"] and payload["edges"]
    assert payload["anomalies"]


def test_both_formats_report_the_same_anomalies(tmp_path: Path, capsys):
    """Scenario: 兩種格式內容一致"""
    _write_job(tmp_path, ungranted())
    events = ungranted().events
    human = render_inspect(build_dataflow(events), events, colour=False)

    main(["--root", str(tmp_path), "inspect", JOB, "--json"])
    machine = json.loads(capsys.readouterr().out)

    for anomaly in machine["anomalies"]:
        assert anomaly["detail"] in human
    assert len(machine["anomalies"]) == len(detect(build_dataflow(events)))


# --- Requirement: Monitor 不影響被觀察的 job ------------------------------


def _write_job(root: Path, stream: Stream) -> Path:
    path = root / "jobs" / JOB / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(e.to_json() for e in stream.events) + "\n",
                    encoding="utf-8")
    return path


def test_monitoring_leaves_the_stream_untouched(tmp_path: Path, capsys):
    """Scenario: 監控不改變事件流"""
    path = _write_job(tmp_path, healthy())
    before = path.read_bytes()

    main(["--root", str(tmp_path), "inspect", JOB])
    main(["--root", str(tmp_path), "monitor", JOB, "--once"])
    capsys.readouterr()

    assert path.read_bytes() == before


def test_monitoring_needs_only_the_files(tmp_path: Path, capsys):
    """Scenario: 監控不需要 job 存在於同一程序"""
    _write_job(tmp_path, healthy())
    assert main(["--root", str(tmp_path), "inspect", JOB]) == 0
    assert "d1" in capsys.readouterr().out


# --- Requirement: 找得到可觀察的 job --------------------------------------


def test_jobs_are_listed_with_their_state(tmp_path: Path, capsys):
    """Scenario: 列出 job"""
    _write_job(tmp_path, healthy())
    (tmp_path / "jobs" / "other").mkdir(parents=True)
    (tmp_path / "jobs" / "other" / "events.jsonl").write_text(
        "\n".join(e.to_json() for e in Stream("other").start().dispatch("d1", "a").events) + "\n",
        encoding="utf-8")

    assert main(["--root", str(tmp_path), "jobs"]) == 0
    out = capsys.readouterr().out
    assert JOB in out and "other" in out
    assert "完成" in out and "執行中" in out


def test_a_nested_store_is_still_found(tmp_path: Path):
    """The golden runner nests its store; guessing the directory is not the job."""
    _write_job(tmp_path / "run-1", healthy())
    jobs = discover(tmp_path)
    assert [j.job_id for j in jobs] == [JOB]
    assert resolve_root(tmp_path, JOB) == tmp_path / "run-1"


def test_an_unknown_job_lists_what_exists(tmp_path: Path, capsys):
    _write_job(tmp_path, healthy())
    assert main(["--root", str(tmp_path), "inspect", "ghost"]) == 1
    assert JOB in capsys.readouterr().out


def test_inspect_exits_nonzero_when_something_is_critical(tmp_path: Path, capsys):
    """So CI can fail on a data-flow problem without parsing anything."""
    _write_job(tmp_path, ungranted())
    assert main(["--root", str(tmp_path), "inspect", JOB]) == 2
    capsys.readouterr()

    _write_job(tmp_path, healthy())
    assert main(["--root", str(tmp_path), "inspect", JOB]) == 0
