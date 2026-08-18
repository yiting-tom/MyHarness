"""The one module that knows where things live on a local disk.

design.md D6 requires that the on-disk layout be owned by the local backend and
by nothing else. Both the local artifact store and the local event log need it,
so "the local backend" is this module rather than a single class -- and
``tests/unit/test_layout_is_private.py`` enforces that no other module composes
these paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class JobLayout:
    """Absolute paths for one job's storage."""

    root: Path
    job_id: str

    @property
    def job_dir(self) -> Path:
        return self.root / "jobs" / self.job_id

    @property
    def blobs_dir(self) -> Path:
        return self.job_dir / "blobs"

    @property
    def notes_dir(self) -> Path:
        return self.job_dir / "notes"

    @property
    def traces_dir(self) -> Path:
        return self.job_dir / "traces"

    @property
    def index_path(self) -> Path:
        return self.job_dir / "index.sqlite"

    @property
    def events_path(self) -> Path:
        return self.job_dir / "events.jsonl"

    def blob_path(self, name: str) -> Path:
        return self.blobs_dir / name

    def note_path(self, name: str) -> Path:
        return self.notes_dir / f"{name}.md"

    def trace_path(self, dispatch_id: str) -> Path:
        return self.traces_dir / f"{dispatch_id}.jsonl"

    def ensure_dirs(self) -> None:
        for d in (self.blobs_dir, self.notes_dir, self.traces_dir):
            d.mkdir(parents=True, exist_ok=True)
