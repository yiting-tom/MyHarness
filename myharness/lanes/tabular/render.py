"""Turning a result set into text a worker can afford to read.

A query result is data, and data entering an orchestrated worker's context is
the exact thing this system exists to prevent. So results go through the same
two gates as a handle: one on shape, one on length (design.md D4).

Both are needed and neither implies the other. Fifty rows of one integer is
nothing; one row holding a 40,000-character JSON column will blow a context on
its own. ``clamp_handle()`` learned this already -- a schema bounds the shape,
only a character count bounds the size.

Truncation is always stated. A silently shortened result is a worker confidently
reporting a maximum that was not the maximum.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from myharness.monitor.render import display_width, pad, truncate

#: Enough to see a distribution, not enough to be a data transfer.
DEFAULT_MAX_ROWS = 50
#: Roughly 1-2k tokens. A worker gets several queries inside its budget.
DEFAULT_MAX_CHARS = 4000
#: Past this a single cell is a document, not a value.
MAX_CELL_CHARS = 200

_NULL = "NULL"


@dataclass(frozen=True, slots=True)
class Rendered:
    text: str
    rows_shown: int
    row_limited: bool
    char_limited: bool

    @property
    def truncated(self) -> bool:
        return self.row_limited or self.char_limited


def render_rows(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_chars: int = DEFAULT_MAX_CHARS,
    more_available: bool = False,
) -> Rendered:
    """A column-aligned table, bounded on rows and on characters.

    ``more_available`` is the caller's answer to "was there a row after the last
    one you fetched" -- fetching ``max_rows + 1`` answers it without running the
    query to completion just to print a total (design.md D4).
    """
    if not columns:
        return Rendered("(no columns)", 0, False, False)

    kept = list(rows[:max_rows])
    row_limited = more_available or len(rows) > max_rows

    cells = [[_cell(v) for v in row] for row in kept]
    widths = [
        max(display_width(str(col)), *(display_width(r[i]) for r in cells))
        if cells else display_width(str(col))
        for i, col in enumerate(columns)
    ]

    lines = [
        "  ".join(pad(str(c), widths[i]) for i, c in enumerate(columns)),
        "  ".join("-" * widths[i] for i in range(len(columns))),
    ]
    lines += ["  ".join(pad(c, widths[i]) for i, c in enumerate(row)) for row in cells]

    if not kept:
        lines.append("(0 rows)")

    text = "\n".join(lines)
    char_limited = len(text) > max_chars
    if char_limited:
        text = _clamp_lines(lines, max_chars)

    if row_limited:
        text += f"\n... more rows exist; only the first {len(kept)} are shown."
    if char_limited:
        text += "\n... output truncated at the character limit."
    return Rendered(text, len(kept), row_limited, char_limited)


def _cell(value: Any) -> str:
    if value is None:
        return _NULL
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    text = str(value).replace("\n", "\\n").replace("\t", " ")
    return truncate(text, MAX_CELL_CHARS)


def _clamp_lines(lines: Sequence[str], max_chars: int) -> str:
    """Cut on a line boundary: half a row of a table reads as a real row."""
    out: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + 1
        if used + cost > max_chars:
            break
        out.append(line)
        used += cost
    if not out:
        return lines[0][:max_chars]
    return "\n".join(out)


__all__ = [
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MAX_ROWS",
    "MAX_CELL_CHARS",
    "Rendered",
    "render_rows",
]
