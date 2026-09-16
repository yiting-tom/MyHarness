"""給交出資料的人看的那一份。

這一層的內容不是版面，是**翻譯**：每一個內部代碼都要變成一句關於「發生了什麼、
對你的結論有什麼影響」的話。所以下面大部分的斷言在驗字，不在驗標籤。
"""

from __future__ import annotations

import re

from myharness.dataflow import AnomalyKind, build_dataflow
from myharness.events.query import derive_caveats
from myharness.monitor.report import (
    _ANOMALY_SAYS,
    _CAVEAT_SAYS,
    _STATUS_SAYS,
    build_report,
    render_report,
)
from tests.dataflow.conftest import JOB, Stream

BLOB = f"{JOB}/blob/raw/txns"
SPARE = f"{JOB}/blob/raw/never-used"
F1 = f"{JOB}/note/lanes/a/findings/1"
F2 = f"{JOB}/note/lanes/b/findings/2"
REPORT = f"{JOB}/note/report"


def healthy() -> Stream:
    return (Stream().start().ingress(BLOB)
            .dispatch("d1", "a", [BLOB]).done("d1", "a", F1).read("d1", BLOB)
            .dispatch("d2", "syn", [F1]).done("d2", "syn", REPORT).read("d2", F1)
            .finish(REPORT))


def messy() -> Stream:
    """一份沒人讀的資料、一段沒進報告的分析、一次預算用完。"""
    return (Stream().start().ingress(BLOB).ingress(SPARE)
            .dispatch("d1", "a", [BLOB])
            .done("d1", "a", F1, status="budget_exceeded")
            .dispatch("d2", "b", [BLOB]).done("d2", "b", F2)
            .dispatch("d3", "syn", [F1]).done("d3", "syn", REPORT)
            .finish(REPORT))


def report_of(stream: Stream) -> dict:
    events = stream.events
    return build_report(build_dataflow(events), events)


def html_of(stream: Stream, **kw) -> str:
    events = stream.events
    return render_report(build_dataflow(events), events, **kw)


# --- Requirement: 資料提供者看得見自己的資料去了哪裡 ----------------------


def test_every_input_can_say_where_it_went():
    """Scenario: 每一份輸入都說得出去向"""
    data = report_of(messy())
    by_label = {i["label"]: i for i in data["inputs"]}
    assert set(by_label) == {"raw/txns", "raw/never-used"}
    assert by_label["raw/txns"]["used"] is True
    assert len(by_label["raw/txns"]["read_by"]) == 2
    assert by_label["raw/never-used"]["used"] is False
    assert by_label["raw/never-used"]["read_by"] == []


def test_an_untouched_input_is_called_out_on_the_page():
    out = html_of(messy())
    assert "沒有任何一段分析用到它" in out


def test_a_status_code_is_never_the_only_thing_said():
    """Scenario: 不以實作詞彙作為唯一說法"""
    data = report_of(messy())
    stopped = next(s for s in data["stages"] if s["status"] == "budget_exceeded")
    assert stopped["status_says"] == "預算用完，沒做完"
    assert stopped["status_says"] != stopped["status"]


def test_every_dispatch_outcome_has_a_sentence():
    """新增一種 status 而忘了翻譯，使用者就會看到一個英文代碼。"""
    from myharness.events.types import (
        DEGRADED_STATUSES,
        STATUS_DUPLICATE,
        STATUS_OK,
    )
    for status in {*DEGRADED_STATUSES, STATUS_OK, STATUS_DUPLICATE, "running"}:
        assert status in _STATUS_SAYS, status


# --- Requirement: 報告的來源鏈預設就被指出 --------------------------------


def test_the_chain_behind_the_report_is_named():
    """Scenario: 來源鏈被標示"""
    data = report_of(healthy())
    assert data["chain"] == ["d1", "d2"]
    assert all(s["in_chain"] for s in data["stages"])


def test_a_stage_that_did_not_contribute_is_distinguishable():
    """Scenario: 未貢獻的執行可被區分"""
    data = report_of(messy())
    off = [s["id"] for s in data["stages"] if not s["in_chain"]]
    assert off == ["d2"], "d2 寫的 finding 沒有被 synth 讀到"


def test_not_contributing_is_explained_in_words_not_only_in_style():
    """淡掉不是說明。淡掉的東西旁邊要有一句話。"""
    out = html_of(messy())
    assert "沒有進入報告" in out
    assert "這段工作的結果沒有被後面的人用到" in out


# --- Requirement: 沒做到的事與資料流異常以讀者能理解的語言呈現 ------------


def test_every_anomaly_kind_has_a_sentence():
    """Scenario: 異常被翻成讀者能理解的說法

    新增一種異常而忘了翻譯，使用者就會看到 `orphan_output` 這個字。
    """
    for kind in AnomalyKind:
        assert kind in _ANOMALY_SAYS, kind
        headline, means = _ANOMALY_SAYS[kind]
        assert str(kind) not in headline
        assert len(means) > 20, f"{kind} 只有標題沒有說明它代表什麼"


def test_every_caveat_kind_the_framework_emits_has_a_sentence():
    """`derive_caveats` 會發出的每一種，都要有人話。"""
    from myharness.events.types import DEGRADED_STATUSES
    emitted = {*DEGRADED_STATUSES, "no_cost_ceiling", "limit_reached",
               "rate_limited", "unanswered_question", "unprocessed_payload"}
    assert emitted <= set(_CAVEAT_SAYS), emitted - set(_CAVEAT_SAYS)


def test_no_internal_code_reaches_the_page():
    """整頁掃一遍：一個內部代碼都不准漏到使用者面前。"""
    out = html_of(messy())
    body = out.split("const DATA = ", 1)[0]  # 版面，不含 payload
    leaked = [code for code in (*(str(k) for k in AnomalyKind), *_CAVEAT_SAYS)
              if code in body]
    assert not leaked, leaked


def test_an_unfinished_analysis_is_as_visible_as_the_conclusions():
    """Scenario: 未完成的分析出現在讀者看得到的地方"""
    events = messy().events
    assert any(c.kind == "budget_exceeded" for c in derive_caveats(events))
    out = html_of(messy())
    assert "有一段分析在預算用完時還沒做完" in out
    assert out.index("這次沒做到的事") < out.index("結論"), "沒做到的事要在結論之前"


def test_a_clean_job_says_there_was_nothing_wrong():
    """Scenario: 沒有問題時也要說"""
    data = report_of(healthy())
    assert data["concerns"] == []
    assert "沒有發現問題" in html_of(healthy())


def test_the_golden_fifth_run_tells_the_user_the_report_had_no_basis(golden5):
    """那一次報告來自一次沒有授權的派工。使用者必須看得出來。"""
    events, artifacts = golden5
    out = render_report(build_dataflow(events, artifacts), events)
    assert "這段結論沒有讀到任何資料就寫出來了" in out
    assert "ungranted_production" not in out.split("const DATA = ", 1)[0]


# --- Requirement: 章節先標價再展開 ----------------------------------------


def test_prices_come_before_the_text():
    """Scenario: 價目表先於全文"""
    sections = [{"id": "s1", "title": "摘要", "est_tokens": 510, "text": "內容甲"},
                {"id": "s2", "title": "限制", "est_tokens": 108, "text": "內容乙"}]
    out = html_of(healthy(), sections=sections)
    assert '"est_tokens":510' in out and '"est_tokens":108' in out

    # 收合是這條需求要的：標題與價格看得到，全文要自己打開。
    # `<details open>` 會讓價目表失去意義 —— 全文已經在眼前了。
    assert "<details open" not in out
    # 價格在 <summary> 裡（一打開就看得到），全文在 .body 裡（要自己展開）。
    # 這一頁的 DOM 是在 script 裡組的，所以這裡驗的是組出來的順序。
    assert out.index('<span class="price">') < out.index('class="body"')


def test_a_report_with_no_sections_does_not_show_an_empty_menu():
    """Scenario: 沒有章節時"""
    out = html_of(healthy())
    assert "這份報告沒有分節" in out


# --- 離線、唯讀、不外洩 ---------------------------------------------------


def test_nothing_is_fetched_from_the_network():
    out = html_of(messy())
    assert not re.findall(r"""(?:src|href)\s*=\s*["']https?://[^"']+""", out)
    assert "fetch(" not in out


def test_model_written_text_cannot_close_the_script_element():
    nasty = "</script><img src=x onerror=alert(1)>"
    out = html_of(healthy(), delivery={"executive_summary": nasty})
    assert out.count("</script>") == out.count("<script>")
    assert "<img" not in out


# --- 接線 -----------------------------------------------------------------


def test_the_report_command_writes_where_it_was_told(tmp_path, capsys):
    from pathlib import Path

    from myharness.monitor.cli import main

    path = Path(tmp_path) / "jobs" / JOB / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(e.to_json() for e in healthy().events) + "\n",
                    encoding="utf-8")
    before = path.read_bytes()
    listing = sorted(p.name for p in path.parent.iterdir())

    target = Path(tmp_path) / "provenance.html"
    assert main(["--root", str(tmp_path), "report", JOB, "-o", str(target)]) == 0
    capsys.readouterr()

    assert "<!doctype html>" in target.read_text(encoding="utf-8").lower()
    # 唯讀：產生報告不能碰到 job
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == listing


def test_the_report_command_defaults_to_stdout(tmp_path, capsys):
    from pathlib import Path

    from myharness.monitor.cli import main

    path = Path(tmp_path) / "jobs" / JOB / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(e.to_json() for e in healthy().events) + "\n",
                    encoding="utf-8")

    assert main(["--root", str(tmp_path), "report", JOB]) == 0
    out = capsys.readouterr().out
    assert "<!doctype html>" in out.lower()
    assert not list(path.parent.glob("*.html"))
