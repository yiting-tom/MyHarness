"""Artifact identity.

An artifact id is ``<job_id>/<kind>/<name>``. The name may itself contain
slashes (``lanes/txn-2024/state``); everything after the kind segment is the
name, and its leading segments form the artifact's namespace.

Ids are globally unique even though v1 only ever grants access within a single
job -- see DESIGN.md decision #10 and design.md D2.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

KIND_BLOB: Final = "blob"
KIND_NOTE: Final = "note"
KINDS: Final = frozenset({KIND_BLOB, KIND_NOTE})

_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class InvalidArtifactId(ValueError):
    """Raised when an id cannot be parsed or is built from illegal parts."""


def _check_segment(value: str, *, what: str) -> str:
    if not _SEGMENT.match(value):
        raise InvalidArtifactId(f"illegal {what}: {value!r}")
    return value


def _check_name(name: str) -> str:
    if not name:
        raise InvalidArtifactId("name must not be empty")
    if name.startswith("/") or name.endswith("/") or "//" in name:
        raise InvalidArtifactId(f"illegal name: {name!r}")
    for seg in name.split("/"):
        if seg in (".", ".."):
            raise InvalidArtifactId(f"path traversal in name: {name!r}")
        _check_segment(seg, what="name segment")
    return name


@dataclass(frozen=True, slots=True, order=True)
class ArtifactId:
    """A parsed ``<job_id>/<kind>/<name>`` identifier."""

    job_id: str
    kind: str
    name: str

    def __post_init__(self) -> None:
        _check_segment(self.job_id, what="job_id")
        if self.kind not in KINDS:
            raise InvalidArtifactId(f"illegal kind: {self.kind!r}")
        _check_name(self.name)

    @classmethod
    def parse(cls, raw: str) -> ArtifactId:
        parts = raw.split("/", 2)
        if len(parts) != 3:
            raise InvalidArtifactId(f"expected '<job>/<kind>/<name>', got {raw!r}")
        return cls(parts[0], parts[1], parts[2])

    @property
    def namespace(self) -> str:
        """The name's immediate parent path, or ``""`` for a flat name.

        This is the *directory*, not the owning lane: an artifact named
        ``lanes/txn-2024/findings/003`` has namespace ``lanes/txn-2024/findings``.
        Ownership is therefore always a prefix comparison against
        ``lane_namespace(...)``, never equality -- see ``GrantSet.allows``.
        """
        head, _, tail = self.name.rpartition("/")
        return head if tail else ""

    @property
    def is_blob(self) -> bool:
        return self.kind == KIND_BLOB

    @property
    def is_note(self) -> bool:
        return self.kind == KIND_NOTE

    def __str__(self) -> str:
        return f"{self.job_id}/{self.kind}/{self.name}"


def lane_namespace(lane_id: str) -> str:
    """The namespace every artifact owned by ``lane_id`` lives under."""
    return f"lanes/{_check_segment(lane_id, what='lane_id')}"
