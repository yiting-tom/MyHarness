"""How much of a blob the classifier is allowed to see.

The proxy is an LLM, so whatever it reads is context -- the same problem this
whole system exists to bound, arriving at the one place data enters. So the
sample gets the same two gates as everything else: lines for shape, characters
for length (design.md D4).

Classification needs remarkably little. For a CSV the header alone almost
decides it; a few rows confirm the shape. The limits are small because more
would not classify better, not because small happens to be cheap.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from myharness.artifacts.types import ArtifactMeta

#: Enough to see a header and the shape of a row.
MAX_SAMPLE_LINES = 12
#: Roughly 300 tokens of CJK, less of ASCII. A classifier does not need a page.
MAX_SAMPLE_CHARS = 1_200
#: One line longer than this is a record, not a line; cut it and say so.
MAX_LINE_CHARS = 300
#: Never read more than this off disk, whatever the blob's size claims.
MAX_READ_BYTES = 64 * 1024

_TEXTUAL_SUFFIXES = frozenset({
    ".csv", ".tsv", ".txt", ".json", ".jsonl", ".ndjson", ".log", ".md", ".xml", ".yaml", ".yml",
})


@dataclass(frozen=True, slots=True)
class Sample:
    """What the classifier sees of one blob."""

    text: str
    lines: int
    line_limited: bool
    char_limited: bool
    binary: bool = False

    @property
    def truncated(self) -> bool:
        return self.line_limited or self.char_limited


def describe_meta(meta: ArtifactMeta) -> str:
    """The metadata line. Cheap, and often decisive on its own."""
    parts = [f"id: {meta.id}", f"bytes: {meta.bytes:,}"]
    schema = meta.schema or {}
    if fmt := schema.get("format"):
        parts.append(f"format: {fmt}")
    if columns := schema.get("columns"):
        shown = [str(c) for c in list(columns)[:20]]
        more = "" if len(columns) <= 20 else f" (+{len(columns) - 20})"
        parts.append(f"columns: {', '.join(shown)}{more}")
    return "\n".join(parts)


def read_sample(
    path: Path,
    *,
    max_lines: int = MAX_SAMPLE_LINES,
    max_chars: int = MAX_SAMPLE_CHARS,
) -> Sample:
    """The first few lines of a blob, bounded on both axes.

    Binary content is described rather than dumped: a classifier gains nothing
    from mojibake, and decoding it would be the one place a blob's bytes reach
    a prompt unfiltered.
    """
    try:
        raw = path.read_bytes()[:MAX_READ_BYTES]
    except OSError as exc:
        return Sample(f"(unreadable: {type(exc).__name__})", 0, False, False)

    if _looks_binary(raw, path):
        return Sample("(binary content, not sampled)", 0, False, False, binary=True)

    text = raw.decode("utf-8", errors="replace")
    all_lines = text.splitlines()
    kept = all_lines[:max_lines]
    line_limited = len(all_lines) > max_lines

    trimmed = [
        line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + "…"
        for line in kept
    ]

    out: list[str] = []
    used = 0
    char_limited = False
    for line in trimmed:
        cost = len(line) + 1
        if used + cost > max_chars:
            char_limited = True
            break
        out.append(line)
        used += cost

    if not out and trimmed:
        # One line already over budget: keep a prefix rather than nothing.
        out = [trimmed[0][:max_chars]]
        char_limited = True

    return Sample("\n".join(out), len(out), line_limited, char_limited)


def _looks_binary(raw: bytes, path: Path) -> bool:
    if path.suffix.lower() in _TEXTUAL_SUFFIXES:
        return False
    if b"\x00" in raw[:4096]:
        return True
    try:
        raw[:4096].decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


__all__ = [
    "MAX_LINE_CHARS",
    "MAX_READ_BYTES",
    "MAX_SAMPLE_CHARS",
    "MAX_SAMPLE_LINES",
    "Sample",
    "describe_meta",
    "read_sample",
]
