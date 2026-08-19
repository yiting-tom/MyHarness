"""Artifact id in, table name out. The worker never sees a path.

design.md D2: the worker names artifacts and the harness decides what they are
called in SQL. That keeps authorisation on artifact ids -- the same place every
other read is checked -- instead of needing a second, parser-level check on
paths embedded in SQL. Two authorisation paths means the weaker one decides.

The binding is echoed on every response, success and failure alike, so a worker
that guesses a table name gets the right answer back in the refusal rather than
burning its one context on a second guess.

``ArtifactId`` already validates every name segment as
``[A-Za-z0-9][A-Za-z0-9._-]*`` (``ids.py``), so there is no empty leaf, no
non-ASCII, and no leading underscore to defend against here. Only ``.`` and
``-`` need replacing, and only a leading digit needs a prefix.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from myharness.artifacts.ids import ArtifactId

_NEEDS_REPLACING = re.compile(r"[.\-]")

MAX_TABLE_CHARS = 48


@dataclass(frozen=True, slots=True)
class Binding:
    artifact: ArtifactId
    table: str


def bind_name(artifact: ArtifactId, *, taken: Iterable[str] = ()) -> str:
    """A legal, readable, collision-free table name for one artifact.

    Derived from the artifact's last name segment so ``txn-2024`` reads as
    ``txn_2024`` in SQL rather than ``t1``.
    """
    leaf = artifact.name.rsplit("/", 1)[-1]
    candidate = _NEEDS_REPLACING.sub("_", leaf)[:MAX_TABLE_CHARS]
    if candidate[0].isdigit():
        candidate = f"t_{candidate}"[:MAX_TABLE_CHARS]
    used = set(taken)
    if candidate not in used:
        return candidate
    for n in range(2, 1000):
        alt = f"{candidate}_{n}"
        if alt not in used:
            return alt
    raise ValueError(f"could not find a free table name for {artifact}")


def bind_all(artifacts: Sequence[ArtifactId]) -> tuple[Binding, ...]:
    """Bind in the order given, so the mapping is reproducible for one call."""
    out: list[Binding] = []
    for artifact in artifacts:
        out.append(Binding(artifact, bind_name(artifact, taken=[b.table for b in out])))
    return tuple(out)


def describe(bindings: Sequence[Binding]) -> str:
    """The line that goes on every response so the worker never has to guess."""
    if not bindings:
        return "(no tables bound)"
    return ", ".join(f"{b.table} = {b.artifact}" for b in bindings)


__all__ = ["Binding", "MAX_TABLE_CHARS", "bind_all", "bind_name", "describe"]
