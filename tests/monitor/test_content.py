"""Artifact content and its recorded versions.

The store keeps only the latest copy of a note. Every earlier version is in the
transcript of the dispatch that wrote it, so history is rebuilt from what the
lanes actually sent -- and where the record does not cover it, the view says so.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from myharness.artifacts.local import LocalArtifactStore
from myharness.monitor.content import line_diff, writes_in
from myharness.monitor.serve import build_artifact, build_state
from tests.dataflow.conftest import JOB, Stream

NOTE = f"{JOB}/note/lanes/a/findings/f"
V1 = "# 分析\n- 帳戶 765\n- app 最低\n- 附錄：原始查詢"
V2 = "# 分析\n- 帳戶 765\n- app 最低（修正）"


def _call(tool: str, **inp) -> dict:
    return {"role": "assistant", "content": [
        {"type": "tool_use", "name": f"mcp__lane__{tool}", "input": inp}]}


def _result(body: str, error: bool = False) -> dict:
    return {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "x", "is_error": error, "content": body}]}


def test_a_refused_write_is_not_a_version():
    """Scenario: 失敗的寫入不算一個版本"""
    rows = [_call("write_finding", name=f"{JOB}/note/x", text="壞的"),
            _result('ERROR {"code":"bad_name"}'),
            _call("duckdb_query", sql="SELECT 1"), _result("n\n1"),
            _call("write_finding", name="f", text=V1), _result(f"wrote {NOTE} (9 est tokens)"),
            _call("update_state", text="結論"), _result("state updated (revision 1, ~3 tokens)")]
    ws = writes_in(rows, job_id=JOB, lane_namespace="lanes/a")
    assert [(w.artifact, w.text) for w in ws] == [
        (NOTE, V1), (f"{JOB}/note/lanes/a/state", "結論")]


def test_the_diff_names_what_was_removed():
    d = line_diff(V1, V2)
    assert d["removed"] == 2 and d["added"] == 1
    removed = [t for op, t in d["lines"] if op == "del"]
    assert "- 附錄：原始查詢" in removed


def test_long_unchanged_runs_fold():
    old = "\n".join(f"line {i}" for i in range(40))
    new = old.replace("line 20", "LINE 20")
    ops = [op for op, _ in line_diff(old, new)["lines"]]
    assert "skip" in ops and len(ops) < 12


# --- through the server ------------------------------------------------------


def _job(tmp_path: Path, *, second_running: bool = False, store_text: str = V2) -> None:
    store = LocalArtifactStore(tmp_path)

    async def setup() -> None:
        await store.init_job(JOB)
        for did, text in (("d1", V1), ("d2", V2)):
            rows = [_call("write_finding", name="f", text=text),
                    _result(f"wrote {NOTE} (9 est tokens)")]
            await store.put_blob(JOB, f"traces/{did}",
                                 data="\n".join(json.dumps(r, ensure_ascii=False)
                                                for r in rows).encode(),
                                 produced_by="harness")
        await store.put_note(JOB, "lanes/a/findings/f", store_text, produced_by="lane:a")
    asyncio.run(setup())

    s = Stream().start()
    s.dispatch("d1", "a")._add("dispatch.end", id="d1", lane="a", artifact=NOTE, status="ok",
                               turns=2, transcript=f"{JOB}/blob/traces/d1")
    s.dispatch("d2", "a")._add("dispatch.end", id="d2", lane="a", artifact=None, status="ok",
                               turns=2, transcript=f"{JOB}/blob/traces/d2")
    if second_running:
        s.dispatch("d3", "a")
    path = tmp_path / "jobs" / JOB / "events.jsonl"
    path.write_text("\n".join(e.to_json() for e in s.events) + "\n", encoding="utf-8")


def test_a_rewritten_finding_shows_both_versions(tmp_path: Path):
    """Scenario: 被改寫的產出顯示刪除的內容"""
    _job(tmp_path)
    view = build_artifact(tmp_path, JOB, NOTE)
    assert view["content"] == V2 and view["matches_last"]
    v1, v2 = view["versions"]
    assert (v1["dispatch"], v1["first"]) == ("d1", True)
    assert v2["dispatch"] == "d2" and v2["diff"]["removed"] == 2
    assert view["history_why"] == ""


def test_an_unreported_write_still_reaches_the_graph(tmp_path: Path):
    """Scenario: 沒回報的產出仍在圖上 -- d2 wrote but returned no artifact."""
    _job(tmp_path)
    writes = build_state(tmp_path, JOB)["writes"]
    assert {"dispatch": "d2", "artifact": NOTE} in [
        {"dispatch": w["dispatch"], "artifact": w["artifact"]} for w in writes]


def test_a_running_lane_is_named_as_a_gap(tmp_path: Path):
    """Scenario: 執行中的派工"""
    _job(tmp_path, second_running=True)
    assert "還在跑" in build_artifact(tmp_path, JOB, NOTE)["history_why"]


def test_a_store_that_disagrees_with_the_record_says_so(tmp_path: Path):
    _job(tmp_path, store_text="別的內容")
    view = build_artifact(tmp_path, JOB, NOTE)
    assert not view["matches_last"] and "不同" in view["history_why"]


def test_ids_outside_the_job_are_refused(tmp_path: Path):
    """Scenario: 不屬於這個 job 的識別被拒絕"""
    _job(tmp_path)
    assert build_artifact(tmp_path, JOB, "other/note/lanes/a/findings/f")["error"] == "other_job"
    assert build_artifact(tmp_path, JOB, f"{JOB}/note/../../etc/passwd")["error"] == "bad_id"
    assert build_artifact(tmp_path, JOB, f"{JOB}/note/lanes/nope")["error"] == "not_found"


def test_a_text_blob_is_previewed(tmp_path: Path):
    _job(tmp_path)
    asyncio.run(LocalArtifactStore(tmp_path).put_blob(
        JOB, "raw/t.csv", data=b"a,b\n1,2\n", produced_by="user"))
    view = build_artifact(tmp_path, JOB, f"{JOB}/blob/raw/t.csv")
    assert view["preview"] == ["a,b", "1,2"]


def test_a_binary_blob_says_it_is_binary(tmp_path: Path):
    _job(tmp_path)
    asyncio.run(LocalArtifactStore(tmp_path).put_blob(
        JOB, "raw/p", data=b"PAR1\xff\xfe\x00", produced_by="user"))
    view = build_artifact(tmp_path, JOB, f"{JOB}/blob/raw/p")
    assert view["preview"] is None and "二進位" in view["preview_why"]
