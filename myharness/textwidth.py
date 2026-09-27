"""Display width for terminal text, counting CJK as two columns and ANSI as none.

A base-layer module: both the views (monitor/) and the lane tools that render
tables for a worker (lanes/tabular/) need it, and the execution path must not
depend on the view layer. ``len()`` is wrong for every Chinese label the
harness will ever show.
"""

from __future__ import annotations

import unicodedata


def char_width(ch: str) -> int:
    """Display columns one character occupies."""
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(text: str) -> int:
    return sum(char_width(c) for c in _strip_ansi(text))


def _strip_ansi(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        if text[i] == "\033":
            j = text.find("m", i)
            if j == -1:
                break
            i = j + 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def pad(text: str, width: int, align: str = "left") -> str:
    """Pad to a display width, counting full-width characters as two."""
    gap = max(0, width - display_width(text))
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def truncate(text: str, width: int, marker: str = "…") -> str:
    if display_width(text) <= width:
        return text
    budget = width - display_width(marker)
    out, used = [], 0
    for ch in text:
        w = char_width(ch)
        if used + w > budget:
            break
        out.append(ch)
        used += w
    return "".join(out) + marker
