from __future__ import annotations

from myharness.lanes.tabular.render import (
    DEFAULT_MAX_CHARS,
    MAX_CELL_CHARS,
    render_rows,
)
from myharness.monitor.render import display_width


def test_columns_and_rows_appear():
    r = render_rows(["a", "b"], [(1, 2), (3, 4)])
    assert "a" in r.text and "3" in r.text
    assert r.rows_shown == 2 and not r.truncated


def test_empty_result_says_so():
    r = render_rows(["a"], [])
    assert "(0 rows)" in r.text and r.rows_shown == 0


def test_no_columns_is_not_a_crash():
    assert render_rows([], []).text == "(no columns)"


class TestRowGate:
    def test_rows_beyond_the_limit_are_dropped(self):
        r = render_rows(["n"], [(i,) for i in range(100)], max_rows=10)
        assert r.rows_shown == 10 and r.row_limited

    def test_truncation_is_stated_not_silent(self):
        """A silently shortened result is a confidently wrong maximum."""
        r = render_rows(["n"], [(i,) for i in range(100)], max_rows=10)
        assert "more rows exist" in r.text

    def test_more_available_flags_truncation_without_extra_rows(self):
        """The caller fetches limit+1 rather than counting the whole result."""
        r = render_rows(["n"], [(1,), (2,)], max_rows=2, more_available=True)
        assert r.row_limited and "more rows exist" in r.text

    def test_exactly_at_the_limit_is_not_truncated(self):
        r = render_rows(["n"], [(i,) for i in range(10)], max_rows=10)
        assert not r.row_limited


class TestCharGate:
    def test_rows_within_the_limit_can_still_exceed_the_char_limit(self):
        """The gate the row limit cannot do.

        A single monstrous cell is caught earlier by MAX_CELL_CHARS, so the
        case that actually reaches this gate is width: 40 columns of 200
        characters is 8,000 per row and every row is within the row limit.
        """
        cols = [f"c{i}" for i in range(40)]
        rows = [tuple("v" * 180 for _ in cols) for _ in range(20)]
        r = render_rows(cols, rows, max_rows=50)
        assert not r.row_limited, "the row gate cannot catch this"
        assert r.char_limited
        assert len(r.text) < 20 * 40 * 180

    def test_char_truncation_is_stated(self):
        cols = [f"c{i}" for i in range(40)]
        rows = [tuple("v" * 180 for _ in cols) for _ in range(20)]
        r = render_rows(cols, rows, max_rows=50)
        assert "truncated at the character limit" in r.text

    def test_cell_cap_is_a_third_bound_not_a_replacement(self):
        """It caps one value; it cannot bound a wide or tall result."""
        r = render_rows(["doc"], [("x" * 50_000,)], max_rows=50)
        assert not r.char_limited, "the cell cap handled this one alone"
        assert len(r.text) < 1000

    def test_both_gates_can_fire_together(self):
        r = render_rows(
            ["doc"], [("y" * 2000,) for _ in range(100)], max_rows=10, max_chars=500
        )
        assert r.row_limited and r.char_limited

    def test_neither_gate_fires_on_a_small_result(self):
        r = render_rows(["a"], [(1,)])
        assert not r.row_limited and not r.char_limited

    def test_output_stays_under_the_limit_plus_its_notices(self):
        r = render_rows(["d"], [("z" * 900,) for _ in range(50)], max_chars=1000)
        body = r.text.split("\n... ")[0]
        assert len(body) <= 1000


class TestCells:
    def test_null_is_distinguishable_from_empty_string(self):
        r = render_rows(["a", "b"], [(None, "")])
        assert "NULL" in r.text

    def test_long_cell_is_capped(self):
        r = render_rows(["a"], [("q" * 5000,)])
        assert "…" in r.text
        longest = max(len(line) for line in r.text.splitlines())
        assert longest < MAX_CELL_CHARS + 50

    def test_binary_is_described_not_dumped(self):
        r = render_rows(["blob"], [(b"\x00\x01\x02",)])
        assert "<3 bytes>" in r.text

    def test_newlines_do_not_break_the_table(self):
        r = render_rows(["a", "b"], [("one\ntwo", 1)])
        assert len(r.text.splitlines()) == 3
        assert "\\n" in r.text


class TestAlignment:
    def test_cjk_columns_line_up(self):
        """len() is not width: 交易 is 4 columns, not 2."""
        r = render_rows(["名稱", "n"], [("交易", 1), ("a", 22)])
        widths = {display_width(line) for line in r.text.splitlines()}
        assert len(widths) == 1, r.text

    def test_header_wider_than_data_still_aligns(self):
        r = render_rows(["a_very_long_header"], [(1,)])
        widths = {display_width(line) for line in r.text.splitlines()}
        assert len(widths) == 1


def test_default_char_limit_is_a_context_budget_not_a_page_size():
    assert 1000 <= DEFAULT_MAX_CHARS <= 8000
