"""Fixtures shared by the backend-agnostic ArtifactStore contract suite.

Every backend supplies a small harness alongside the store so that the suite can
verify negative behaviour ("content was not read") without knowing how that
backend stores anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pytest

from myharness.artifacts.ids import ArtifactId
from myharness.artifacts.local import LocalArtifactStore
from myharness.artifacts.store import ArtifactStore
from myharness.local_layout import JobLayout

JOB = "j7"


class StoreHarness(Protocol):
    """Backend-specific hooks the contract suite needs."""

    store: ArtifactStore

    def destroy_content(self, artifact_id: ArtifactId) -> None:
        """Make the artifact's content unreadable, leaving the index intact.

        The suite uses this to prove that a rejected read never touches content:
        if the call still fails with the *expected* error after the content is
        gone, it cannot have read it.
        """


@dataclass
class LocalHarness:
    store: LocalArtifactStore
    root: Path

    def destroy_content(self, artifact_id: ArtifactId) -> None:
        layout = JobLayout(self.root, artifact_id.job_id)
        path = (
            layout.blob_path(artifact_id.name)
            if artifact_id.is_blob
            else layout.note_path(artifact_id.name)
        )
        path.unlink()


@pytest.fixture(params=["local"])
def harness(request, tmp_path: Path) -> StoreHarness:
    if request.param == "local":
        return LocalHarness(LocalArtifactStore(tmp_path), tmp_path)
    raise AssertionError(f"unknown backend {request.param}")


@pytest.fixture
def store(harness: StoreHarness) -> ArtifactStore:
    return harness.store
