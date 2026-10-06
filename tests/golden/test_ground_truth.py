"""The ground truth itself, checked offline.

An assertion that the golden report contains 765 is worthless if 765 is wrong,
and the live test that uses it costs money to run -- so the fact is verified
here, where it is free.
"""

from __future__ import annotations

import pytest

from myharness.goldens import GOLDEN_CSV, ComplaintsTruth, GroundTruth, ground_truth

pytestmark = pytest.mark.skipif(
    not GOLDEN_CSV.exists(), reason=f"{GOLDEN_CSV} missing"
)


def test_matches_the_fixture():
    truth = ground_truth(GOLDEN_CSV)
    assert truth.rows == 2940
    assert truth.accounts == 765
    assert truth.cheapest_channel == "app"


def test_a_report_that_analysed_nothing_is_caught():
    truth = ground_truth(GOLDEN_CSV)
    hollow = "由於權限限制，未能讀取交易資料，因此無法提供具體數字。"
    assert len(truth.missing_from(hollow)) == 2


def test_a_report_with_the_figures_passes():
    truth = ground_truth(GOLDEN_CSV)
    real = "資料共有 765 個不重複帳戶；app 通路的平均金額最低。"
    assert truth.missing_from(real) == []


def test_thousands_separators_are_accepted():
    """A report writing 1,234 has still done the work."""
    truth = GroundTruth(rows=2940, accounts=1234, cheapest_channel="app")
    assert truth.missing_from("1,234 個帳戶，app 最低") == []


def test_a_near_miss_is_not_accepted():
    truth = ground_truth(GOLDEN_CSV)
    assert "distinct accounts (765)" in " ".join(
        truth.missing_from("約 700 多個帳戶，app 最低")
    )


# --- the complaints task -------------------------------------------------


def test_the_complaints_answer_key():
    from myharness.goldens import complaints_truth

    assert complaints_truth() == ComplaintsTruth(569, "high", "web")


def test_a_keyword_count_is_a_wrong_answer():
    """Counting 盜/冒用 gives 485: the trap is the point of the task."""
    truth = ComplaintsTruth(569, "high", "web")
    assert truth.missing_from("ANSWER: count=485; riskiest=high; channel=web")
    assert not truth.missing_from("……\nANSWER: count=560；riskiest=high；channel=web")


def test_naming_every_channel_is_not_an_answer():
    truth = ComplaintsTruth(569, "high", "web")
    assert truth.missing_from("web、app、atm、branch 都有，high 風險最多，共 569 則") == [
        "ANSWER line"]
