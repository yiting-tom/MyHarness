"""Terminal rendering primitives.

No rich, no textual (design.md D4): this layer earns its keep by projecting the
facts correctly, not by looking good, and a TUI framework would add a runtime
dependency and layout complexity in exchange for appearance.

CJK width is handled properly because the harness is used on Chinese data and
``len()`` is wrong for every label it will ever show.
"""

from __future__ import annotations

import os
import sys
import unicodedata
from typing import Final

RESET: Final = "\033[0m"
STYLES: Final = {
    "dim": "\033[2m", "bold": "\033[1m", "red": "\033[31m",
    "yellow": "\033[33m", "green": "\033[32m", "cyan": "\033[36m",
    "magenta": "\033[35m", "blue": "\033[34m",
}


def colour_enabled(stream=None) -> bool:
    """Colour only when someone is watching, and never when told not to."""
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return bool(getattr(stream, "isatty", lambda: False)())


def style(text: str, *names: str, enabled: bool = True) -> str:
    if not enabled or not names:
        return text
    prefix = "".join(STYLES.get(n, "") for n in names)
    return f"{prefix}{text}{RESET}" if prefix else text


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


def rule(label: str = "", width: int = 72, char: str = "─") -> str:
    if not label:
        return char * width
    head = f"{char * 2} {label} "
    return head + char * max(0, width - display_width(head))


def tree_prefix(depth: int, last: bool, parents_last: tuple[bool, ...] = ()) -> str:
    """Box-drawing prefix for one line of a tree."""
    if depth == 0:
        return ""
    stem = "".join("   " if p else "│  " for p in parents_last[: depth - 1])
    return stem + ("└─ " if last else "├─ ")


def human_tokens(n: int | None) -> str:
    if not n:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def bar(fraction: float, width: int = 20, fill: str = "█", empty: str = "░") -> str:
    filled = max(0, min(width, round(fraction * width)))
    return fill * filled + empty * (width - filled)
