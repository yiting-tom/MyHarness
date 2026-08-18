"""Append-only event log: interface plus the local JSONL backend.

JSONL rather than a database so a job's history can be ``cat``-ed, tarred up and
handed to a person unchanged. Cost reports, dashboards and OpenTelemetry spans
are all projections of this file, never a second record (design.md D7).
"""

from __future__ import annotations

import abc
import asyncio
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from myharness.events.types import Event, MalformedEvent, now
from myharness.local_layout import JobLayout


class EventLog(abc.ABC):
    """Append and read; deliberately no update or delete."""

    @abc.abstractmethod
    async def append(self, job_id: str, t: str, **data: Any) -> Event:
        """Append one event and return it, with its assigned sequence number."""

    @abc.abstractmethod
    async def read(self, job_id: str) -> Sequence[Event]:
        """All events for a job, in append order."""


class LocalEventLog(EventLog):
    """One ``events.jsonl`` per job, flushed line by line."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._lock = threading.Lock()
        self._next_seq: dict[str, int] = {}

    def _path(self, job_id: str) -> Path:
        return JobLayout(self._root, job_id).events_path

    async def append(self, job_id: str, t: str, **data: Any) -> Event:
        return await asyncio.to_thread(self._append_sync, job_id, t, data)

    def _append_sync(self, job_id: str, t: str, data: dict[str, Any]) -> Event:
        path = self._path(job_id)
        with self._lock:
            seq = self._next_seq.get(job_id)
            if seq is None:
                seq = self._scan_next_seq(path)
            event = Event(t=t, seq=seq, ts=now(), job_id=job_id, data=dict(data))
            path.parent.mkdir(parents=True, exist_ok=True)
            # One write() per line under O_APPEND, then flush to the OS: a killed
            # process cannot leave a half line behind (spec: 寫入的耐久性).
            with path.open("a", encoding="utf-8") as fh:
                fh.write(event.to_json() + "\n")
                fh.flush()
            self._next_seq[job_id] = seq + 1
            return event

    @staticmethod
    def _scan_next_seq(path: Path) -> int:
        """Resume numbering after a restart by counting what is already there."""
        if not path.exists():
            return 0
        highest = -1
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    highest = max(highest, Event.from_json(line).seq)
                except MalformedEvent:
                    continue
        return highest + 1

    async def read(self, job_id: str) -> Sequence[Event]:
        return await asyncio.to_thread(self._read_sync, job_id)

    def _read_sync(self, job_id: str) -> Sequence[Event]:
        path = self._path(job_id)
        if not path.exists():
            return ()
        events: list[Event] = []
        raw_lines = path.read_text(encoding="utf-8").split("\n")
        for i, line in enumerate(raw_lines):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(Event.from_json(line))
            except MalformedEvent:
                # Only a truncated final line is tolerable; anything else means
                # the log was corrupted and must not be silently half-read.
                if i == len(raw_lines) - 1:
                    continue
                raise
        return tuple(events)
