"""Rendering primitives, chiefly that CJK alignment is not len()."""

from __future__ import annotations

import pytest

from myharness.monitor.render import (
    bar,
    colour_enabled,
    display_width,
    human_duration,
    human_tokens,
    pad,
    rule,
    style,
    truncate,
)


@pytest.mark.parametrize(
    ("text", "width"),
    [("txn", 3), ("交易明細", 8), ("KYC 比對", 8), ("", 0), ("日本語abc", 9)],
)
def test_full_width_characters_count_double(text: str, width: int):
    assert display_width(text) == width


def test_padding_aligns_mixed_scripts():
    """This harness runs on Chinese data; len() would misalign every column."""
    rows = [pad("交易", 10), pad("txn", 10), pad("KYC 比對", 10)]
    assert len({display_width(r) for r in rows}) == 1


def test_truncation_never_splits_a_wide_character_over_the_limit():
    out = truncate("這是一段很長的中文標題", 9)
    assert display_width(out) <= 9
    assert out.endswith("…")


def test_short_text_is_left_alone():
    assert truncate("短", 10) == "短"


def test_width_ignores_ansi_sequences():
    assert display_width(style("交易", "red")) == 4


def test_styling_can_be_switched_off():
    assert style("x", "red", enabled=False) == "x"
    assert "\033" in style("x", "red", enabled=True)


def test_colour_is_off_when_nobody_is_watching(monkeypatch):
    monkeypatch.delenv("FORCE_COLOR", raising=False)

    class NotATty:
        def isatty(self) -> bool:
            return False

    assert not colour_enabled(NotATty())


def test_no_color_is_respected(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")

    class Tty:
        def isatty(self) -> bool:
            return True

    assert not colour_enabled(Tty())


@pytest.mark.parametrize(
    ("value", "shown"),
    [(None, "—"), (0, "—"), (453, "453"), (19039, "19.0k"), (1_200_000, "1.2M")],
)
def test_token_formatting(value, shown):
    assert human_tokens(value) == shown


@pytest.mark.parametrize(
    ("seconds", "shown"), [(45, "45s"), (534.9, "8m54s"), (1863, "31m03s"), (7200, "2h00m")]
)
def test_duration_formatting(seconds, shown):
    assert human_duration(seconds) == shown


def test_bar_is_the_width_asked_for():
    assert len(bar(0.5, 20)) == 20
    assert bar(0.0, 4) == "░░░░"
    assert bar(1.0, 4) == "████"
    assert len(bar(2.0, 4)) == 4, "an out-of-range fraction must not overflow"


def test_rule_fills_to_width():
    assert display_width(rule("資料流", 40)) == 40
    assert display_width(rule("", 40)) == 40
