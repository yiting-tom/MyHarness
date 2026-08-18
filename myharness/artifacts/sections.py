"""Split a note into ``##`` sections so oversized notes stay partially readable."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

from myharness.artifacts.tokens import estimate_tokens
from myharness.artifacts.types import Section

_H2 = re.compile(r"^##\s+(?P<title>.+?)\s*$", re.MULTILINE)


def slugify(title: str) -> str:
    """Readable id for a heading.

    Unicode word characters are kept rather than stripped: section ids surface
    to the caller (``analysis_drill(section_id)``), and folding every Chinese
    heading down to "section" would make them useless for exactly the reports
    this harness is built to produce.
    """
    normalized = unicodedata.normalize("NFKC", title).lower()
    out: list[str] = []
    for ch in normalized:
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-") or "section"


def split_sections(text: str) -> tuple[Sequence[Section], dict[str, str]]:
    """Return (section metadata, section id -> body text).

    The body of a section includes its own heading line, so a section read back
    on its own is still self-describing. Text before the first ``##`` is not a
    section; it is only ever returned by a whole-note read.
    """
    matches = list(_H2.finditer(text))
    if not matches:
        return (), {}

    metas: list[Section] = []
    bodies: dict[str, str] = {}
    seen: dict[str, int] = {}

    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.start() : end].rstrip("\n")
        title = m.group("title")

        base = slugify(title)
        seen[base] = seen.get(base, 0) + 1
        sid = base if seen[base] == 1 else f"{base}-{seen[base]}"

        bodies[sid] = body
        metas.append(Section(id=sid, title=title, est_tokens=estimate_tokens(body)))

    return tuple(metas), bodies
