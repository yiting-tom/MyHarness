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


_JOBS_DIR = "jobs"


@dataclass(frozen=True, slots=True)
class JobLayout:
    """Absolute paths for one job's storage."""

    root: Path
    job_id: str

    @property
    def job_dir(self) -> Path:
        return self.root / _JOBS_DIR / self.job_id

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

    @property
    def exists(self) -> bool:
        return self.events_path.exists()


#: How far below a given directory to look for job stores. Callers put their
#: store at different depths, and a monitor whose first obstacle is "guess the
#: directory" is not much of a monitor.
SEARCH_DEPTH = 2


def find_stores(root: Path, depth: int = SEARCH_DEPTH) -> list[Path]:
    """Directories at or below `root` that hold a job store."""
    stores: list[Path] = []
    frontier = [(root, 0)]
    while frontier:
        current, level = frontier.pop(0)
        if not current.is_dir():
            continue
        if (current / _JOBS_DIR).is_dir():
            stores.append(current)
        if level < depth:
            frontier += [
                (child, level + 1)
                for child in sorted(current.iterdir())
                if child.is_dir() and child.name != _JOBS_DIR
            ]
    return stores


def find_jobs(root: Path, depth: int = SEARCH_DEPTH) -> list[JobLayout]:
    """Every job layout at or below `root`."""
    return [
        layout
        for store in find_stores(root, depth)
        for entry in sorted((store / _JOBS_DIR).iterdir())
        if entry.is_dir() and (layout := JobLayout(store, entry.name)).exists
    ]
