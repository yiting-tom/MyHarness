from myharness.compare import Row, summary


def test_the_summary_gives_median_and_range_per_side():
    rows = [Row("H", tokens_in=n, run=i) for i, n in enumerate((90_000, 120_000, 100_000), 1)]
    rows.append(Row("B", note="放不進 context window", run=1))
    text = summary(rows)
    assert "H：3 次，答對 3" in text and "100,000（90,000–120,000）" in text
    assert "B：1 次，答對 0" in text
