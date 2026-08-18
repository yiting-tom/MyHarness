"""Artifact metadata, section index, and the capability grant model."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from myharness.artifacts.ids import ArtifactId


@dataclass(frozen=True, slots=True)
class Section:
    """One ``##`` section of a note, with its own pre-read token estimate."""

    id: str
    title: str
    est_tokens: int


@dataclass(frozen=True, slots=True)
class ArtifactMeta:
    """Everything the index knows about an artifact without opening it."""

    id: ArtifactId
    kind: str
    bytes: int
    produced_by: str
    created_at: datetime
    est_tokens: int | None = None
    schema: dict[str, Any] | None = None
    sections: tuple[Section, ...] = ()
    revision: int = 1

    @property
    def section_ids(self) -> tuple[str, ...]:
        return tuple(s.id for s in self.sections)


@dataclass(frozen=True, slots=True)
class GrantSet:
    """What one lane worker execution is allowed to read.

    A worker may read anything under its own namespace plus the ids explicitly
    listed in the ``dispatch`` call that started it. There is no third source of
    access -- see design.md D2.
    """

    job_id: str
    own_namespace: str = ""
    granted: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def for_lane(
        cls, job_id: str, own_namespace: str, granted: Iterable[ArtifactId | str] = ()
    ) -> GrantSet:
        return cls(job_id, own_namespace, frozenset(str(g) for g in granted))

    @classmethod
    def unrestricted(cls, job_id: str) -> GrantSet:
        """For the harness itself (orchestrator plumbing, tests, reporting)."""
        return cls(job_id, own_namespace="", granted=frozenset({"*"}))

    def allows(self, artifact_id: ArtifactId) -> bool:
        if artifact_id.job_id != self.job_id:
            return False
        if "*" in self.granted:
            return True
        if str(artifact_id) in self.granted:
            return True
        ns = self.own_namespace
        if not ns:
            return False
        return artifact_id.name == ns or artifact_id.name.startswith(ns + "/")

    def describe(self) -> Sequence[str]:
        """Human-readable summary, used in authorization error messages."""
        out = [f"own namespace: {self.own_namespace or '(none)'}"]
        if "*" in self.granted:
            out.append("granted: (unrestricted)")
        else:
            out.append(f"granted: {sorted(self.granted) or '(none)'}")
        return out
