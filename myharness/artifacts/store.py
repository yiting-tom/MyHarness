"""The ArtifactStore interface.

Nothing outside an implementation of this class may touch an artifact by
filesystem path -- that is what makes the MinIO/MariaDB backend a new class
rather than a rewrite (design.md D6). ``tests/contract`` enforces it.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

from myharness.artifacts.ids import ArtifactId
from myharness.artifacts.types import ArtifactMeta, GrantSet


class ArtifactStore(abc.ABC):
    """Job-scoped storage for blobs (never readable into context) and notes."""

    # ---- lifecycle -------------------------------------------------------

    @abc.abstractmethod
    async def init_job(self, job_id: str) -> None:
        """Prepare storage for a job. Idempotent."""

    # ---- writing ---------------------------------------------------------

    @abc.abstractmethod
    async def put_blob(
        self,
        job_id: str,
        name: str,
        *,
        data: bytes | None = None,
        source: Path | None = None,
        produced_by: str,
        schema: dict[str, Any] | None = None,
    ) -> ArtifactMeta:
        """Store raw data. Exactly one of ``data`` or ``source`` must be given."""

    @abc.abstractmethod
    async def put_note(
        self, job_id: str, name: str, text: str, *, produced_by: str
    ) -> ArtifactMeta:
        """Store LLM-written text, indexing its size and ``##`` sections."""

    @abc.abstractmethod
    async def compare_and_set_note(
        self,
        job_id: str,
        name: str,
        text: str,
        *,
        produced_by: str,
        expected_revision: int,
    ) -> ArtifactMeta:
        """Write a note only if it is still at ``expected_revision``.

        Raises ``RevisionConflict`` otherwise. Use ``expected_revision=0`` to
        require that the note does not yet exist.
        """

    # ---- reading ---------------------------------------------------------

    @abc.abstractmethod
    async def stat(self, artifact_id: ArtifactId, *, grants: GrantSet) -> ArtifactMeta:
        """Metadata only -- never opens the artifact's content."""

    @abc.abstractmethod
    async def read_note(
        self,
        artifact_id: ArtifactId,
        *,
        grants: GrantSet,
        max_tokens: int,
        section: str | None = None,
    ) -> str:
        """Read a note's text, or a single section of it.

        Raises ``NotGranted`` outside the grant set, ``BlobNotReadable`` for a
        blob, and ``TokenBudgetExceeded`` when the indexed estimate exceeds
        ``max_tokens``. All three are decided from the index alone: no content
        byte is read on a rejected call.
        """

    @abc.abstractmethod
    def localize(
        self, artifact_id: ArtifactId, *, grants: GrantSet
    ) -> AbstractAsyncContextManager[Path]:
        """Materialise a blob as a local path for file-oriented tools.

        Local backends yield the real path and copy nothing. Object-store
        backends download to scratch and clean up on exit, including when the
        block exits via an exception.
        """

    @abc.abstractmethod
    async def list(
        self,
        job_id: str,
        *,
        kind: str | None = None,
        namespace: str | None = None,
    ) -> Sequence[ArtifactMeta]:
        """Enumerate artifacts, optionally filtered by kind and/or namespace."""


__all__ = ["ArtifactStore", "ArtifactId", "ArtifactMeta", "GrantSet", "AsyncIterator"]
