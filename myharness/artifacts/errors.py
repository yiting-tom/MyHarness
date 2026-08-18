"""Structured artifact-store errors.

These are exceptions so that callers cannot ignore them, but each carries a
``detail`` mapping so the tool layer can turn one into a handle for the LLM
without re-deriving the reason (DESIGN.md decision #12).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from myharness.artifacts.ids import ArtifactId
from myharness.artifacts.types import Section


class ArtifactError(Exception):
    """Base class. ``detail`` is what the tool layer serialises."""

    code = "artifact_error"

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.message = message
        self.detail: dict[str, Any] = {"code": self.code, "message": message, **detail}

    def to_dict(self) -> dict[str, Any]:
        return dict(self.detail)


class ArtifactNotFound(ArtifactError):
    code = "artifact_not_found"

    def __init__(self, artifact_id: ArtifactId) -> None:
        super().__init__(f"no such artifact: {artifact_id}", artifact=str(artifact_id))


class NotGranted(ArtifactError):
    """Read attempted outside the worker's grant set.

    Deliberately reveals nothing about the target beyond its id -- not even
    whether it exists.
    """

    code = "not_granted"

    def __init__(self, artifact_id: ArtifactId, grants: Sequence[str]) -> None:
        super().__init__(
            f"not authorized to read {artifact_id}",
            artifact=str(artifact_id),
            grants=list(grants),
        )


class BlobNotReadable(ArtifactError):
    """A blob was asked for as if it were text.

    Raised before any content byte is touched. The detail tells the caller how
    to get at the data properly (design.md D1).
    """

    code = "blob_not_readable"

    def __init__(
        self,
        artifact_id: ArtifactId,
        *,
        bytes: int,
        schema: dict[str, Any] | None,
        suggested_access: Sequence[str],
    ) -> None:
        super().__init__(
            f"{artifact_id} is a blob and cannot be read into context",
            artifact=str(artifact_id),
            kind="blob",
            bytes=bytes,
            schema=schema,
            suggested_access=list(suggested_access),
        )


class TokenBudgetExceeded(ArtifactError):
    """The note is larger than the caller's budget.

    Raised from the index alone, before the note is opened (design.md D3).
    """

    code = "token_budget_exceeded"

    def __init__(
        self,
        artifact_id: ArtifactId,
        *,
        est_tokens: int,
        max_tokens: int,
        sections: Sequence[Section],
    ) -> None:
        super().__init__(
            f"{artifact_id} needs ~{est_tokens} tokens, budget is {max_tokens}",
            artifact=str(artifact_id),
            est_tokens=est_tokens,
            max_tokens=max_tokens,
            sections=[
                {"id": s.id, "title": s.title, "est_tokens": s.est_tokens}
                for s in sections
            ],
        )


class SectionNotFound(ArtifactError):
    code = "section_not_found"

    def __init__(self, artifact_id: ArtifactId, section: str, available: Sequence[Section]) -> None:
        super().__init__(
            f"{artifact_id} has no section {section!r}",
            artifact=str(artifact_id),
            section=section,
            sections=[{"id": s.id, "title": s.title, "est_tokens": s.est_tokens} for s in available],
        )


class RevisionConflict(ArtifactError):
    """compare-and-set lost a race -- the note changed underneath the writer."""

    code = "revision_conflict"

    def __init__(self, artifact_id: ArtifactId, *, expected: int, actual: int) -> None:
        super().__init__(
            f"{artifact_id} is at revision {actual}, writer expected {expected}",
            artifact=str(artifact_id),
            expected_revision=expected,
            actual_revision=actual,
        )
