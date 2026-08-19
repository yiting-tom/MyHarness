"""Filesystem + SQLite implementation of ArtifactStore.

Paths come from ``myharness.local_layout``; no other module composes them
(design.md D6). SQL is hand-written rather than routed through an ORM so that the dialect
differences that matter for the eventual MariaDB/Oracle backend stay visible
(design.md D5).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from myharness.artifacts.errors import (
    ArtifactNotFound,
    BlobNotReadable,
    NotGranted,
    RevisionConflict,
    SectionNotFound,
    TokenBudgetExceeded,
)
from myharness.artifacts.ids import KIND_BLOB, KIND_NOTE, ArtifactId
from myharness.artifacts.sections import split_sections
from myharness.artifacts.store import ArtifactStore
from myharness.artifacts.tokens import estimate_tokens
from myharness.artifacts.types import ArtifactMeta, GrantSet, Section
from myharness.local_layout import JobLayout

_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    id            TEXT PRIMARY KEY,
    job_id        TEXT NOT NULL,
    kind          TEXT NOT NULL,
    name          TEXT NOT NULL,
    namespace     TEXT NOT NULL,
    bytes         INTEGER NOT NULL,
    est_tokens    INTEGER,
    schema_json   TEXT,
    sections_json TEXT NOT NULL DEFAULT '[]',
    produced_by   TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    revision      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS artifacts_job_kind ON artifacts(job_id, kind);
CREATE INDEX IF NOT EXISTS artifacts_job_ns   ON artifacts(job_id, namespace);
"""

#: Named in refusals so a worker that reached for the wrong tool is told the
#: right one. Keep in step with myharness.lanes.tools.DEFAULT_TOOLS.
BLOB_ACCESS_HINT = (
    "inspect_blob(artifact) to see columns, types and row count",
    "duckdb_query(artifacts, sql, into) to query it",
    "store.localize(artifact) for a local file path",
)


class LocalArtifactStore(ArtifactStore):
    """Single-machine backend. One SQLite index per job."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    # ---- paths (delegated to the layout module; see design.md D6) --------

    def _layout(self, job_id: str) -> JobLayout:
        return JobLayout(self._root, job_id)

    def _index_path(self, job_id: str) -> Path:
        return self._layout(job_id).index_path

    def _content_path(self, artifact_id: ArtifactId) -> Path:
        layout = self._layout(artifact_id.job_id)
        if artifact_id.is_blob:
            return layout.blob_path(artifact_id.name)
        return layout.note_path(artifact_id.name)

    # ---- sqlite helpers --------------------------------------------------

    def _connect(self, job_id: str) -> sqlite3.Connection:
        conn = sqlite3.connect(self._index_path(job_id), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @staticmethod
    def _row_to_meta(row: sqlite3.Row) -> ArtifactMeta:
        return ArtifactMeta(
            id=ArtifactId(row["job_id"], row["kind"], row["name"]),
            kind=row["kind"],
            bytes=row["bytes"],
            produced_by=row["produced_by"],
            created_at=datetime.fromisoformat(row["created_at"]),
            est_tokens=row["est_tokens"],
            schema=json.loads(row["schema_json"]) if row["schema_json"] else None,
            sections=tuple(Section(**s) for s in json.loads(row["sections_json"])),
            revision=row["revision"],
        )

    def _fetch_sync(self, artifact_id: ArtifactId) -> ArtifactMeta:
        if not self._index_path(artifact_id.job_id).exists():
            raise ArtifactNotFound(artifact_id)
        with self._connect(artifact_id.job_id) as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE id = ?", (str(artifact_id),)
            ).fetchone()
        if row is None:
            raise ArtifactNotFound(artifact_id)
        return self._row_to_meta(row)

    # ---- lifecycle -------------------------------------------------------

    async def init_job(self, job_id: str) -> None:
        await asyncio.to_thread(self._init_job_sync, job_id)

    def _init_job_sync(self, job_id: str) -> None:
        self._layout(job_id).ensure_dirs()
        with self._connect(job_id) as conn:
            conn.executescript(_SCHEMA)

    # ---- writing ---------------------------------------------------------

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
        if (data is None) == (source is None):
            raise ValueError("put_blob requires exactly one of data= or source=")
        return await asyncio.to_thread(
            self._put_blob_sync, job_id, name, data, source, produced_by, schema
        )

    def _put_blob_sync(
        self,
        job_id: str,
        name: str,
        data: bytes | None,
        source: Path | None,
        produced_by: str,
        schema: dict[str, Any] | None,
    ) -> ArtifactMeta:
        self._init_job_sync(job_id)
        aid = ArtifactId(job_id, KIND_BLOB, name)
        dest = self._content_path(aid)
        dest.parent.mkdir(parents=True, exist_ok=True)

        tmp = dest.with_name(dest.name + ".tmp")
        if data is not None:
            tmp.write_bytes(data)
        else:
            shutil.copyfile(source, tmp)  # type: ignore[arg-type]
        os.replace(tmp, dest)

        return self._upsert_sync(
            aid,
            nbytes=dest.stat().st_size,
            est_tokens=None,
            schema=schema,
            sections=(),
            produced_by=produced_by,
        )

    async def put_note(
        self, job_id: str, name: str, text: str, *, produced_by: str
    ) -> ArtifactMeta:
        return await asyncio.to_thread(self._put_note_sync, job_id, name, text, produced_by, None)

    async def compare_and_set_note(
        self,
        job_id: str,
        name: str,
        text: str,
        *,
        produced_by: str,
        expected_revision: int,
    ) -> ArtifactMeta:
        return await asyncio.to_thread(
            self._put_note_sync, job_id, name, text, produced_by, expected_revision
        )

    def _put_note_sync(
        self,
        job_id: str,
        name: str,
        text: str,
        produced_by: str,
        expected_revision: int | None,
    ) -> ArtifactMeta:
        self._init_job_sync(job_id)
        aid = ArtifactId(job_id, KIND_NOTE, name)
        sections, _ = split_sections(text)

        with self._connect(job_id) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT revision FROM artifacts WHERE id = ?", (str(aid),)
                ).fetchone()
                actual = row["revision"] if row else 0
                if expected_revision is not None and actual != expected_revision:
                    raise RevisionConflict(aid, expected=expected_revision, actual=actual)

                dest = self._content_path(aid)
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_name(dest.name + ".tmp")
                tmp.write_text(text, encoding="utf-8")
                os.replace(tmp, dest)

                meta = self._upsert_sync(
                    aid,
                    nbytes=dest.stat().st_size,
                    est_tokens=estimate_tokens(text),
                    schema=None,
                    sections=sections,
                    produced_by=produced_by,
                    revision=actual + 1,
                    conn=conn,
                )
                conn.execute("COMMIT")
                return meta
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def _upsert_sync(
        self,
        aid: ArtifactId,
        *,
        nbytes: int,
        est_tokens: int | None,
        schema: dict[str, Any] | None,
        sections: Sequence[Section],
        produced_by: str,
        revision: int = 1,
        conn: sqlite3.Connection | None = None,
    ) -> ArtifactMeta:
        created = datetime.now(UTC)
        params = (
            str(aid), aid.job_id, aid.kind, aid.name, aid.namespace, nbytes,
            est_tokens,
            json.dumps(schema) if schema is not None else None,
            json.dumps([{"id": s.id, "title": s.title, "est_tokens": s.est_tokens}
                        for s in sections]),
            produced_by, created.isoformat(), revision,
        )
        sql = """
            INSERT INTO artifacts (id, job_id, kind, name, namespace, bytes,
                                   est_tokens, schema_json, sections_json,
                                   produced_by, created_at, revision)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                bytes=excluded.bytes, est_tokens=excluded.est_tokens,
                schema_json=excluded.schema_json, sections_json=excluded.sections_json,
                produced_by=excluded.produced_by, revision=excluded.revision
        """
        if conn is not None:
            conn.execute(sql, params)
        else:
            with self._connect(aid.job_id) as c:
                c.execute(sql, params)

        return ArtifactMeta(
            id=aid, kind=aid.kind, bytes=nbytes, produced_by=produced_by,
            created_at=created, est_tokens=est_tokens, schema=schema,
            sections=tuple(sections), revision=revision,
        )

    # ---- reading ---------------------------------------------------------

    def _authorize(self, artifact_id: ArtifactId, grants: GrantSet) -> None:
        if not grants.allows(artifact_id):
            raise NotGranted(artifact_id, grants.describe())

    async def stat(self, artifact_id: ArtifactId, *, grants: GrantSet) -> ArtifactMeta:
        self._authorize(artifact_id, grants)
        return await asyncio.to_thread(self._fetch_sync, artifact_id)

    async def read_note(
        self,
        artifact_id: ArtifactId,
        *,
        grants: GrantSet,
        max_tokens: int,
        section: str | None = None,
    ) -> str:
        # Every rejection below is decided from the index alone. No content byte
        # is read on a rejected call -- that is the whole point (design.md D1/D3).
        self._authorize(artifact_id, grants)
        meta = await asyncio.to_thread(self._fetch_sync, artifact_id)

        if meta.kind == KIND_BLOB:
            raise BlobNotReadable(
                artifact_id,
                bytes=meta.bytes,
                schema=meta.schema,
                suggested_access=BLOB_ACCESS_HINT,
            )

        if section is None:
            if (meta.est_tokens or 0) > max_tokens:
                raise TokenBudgetExceeded(
                    artifact_id,
                    est_tokens=meta.est_tokens or 0,
                    max_tokens=max_tokens,
                    sections=meta.sections,
                )
            return await asyncio.to_thread(
                self._content_path(artifact_id).read_text, "utf-8"
            )

        wanted = next((s for s in meta.sections if s.id == section), None)
        if wanted is None:
            raise SectionNotFound(artifact_id, section, meta.sections)
        if wanted.est_tokens > max_tokens:
            raise TokenBudgetExceeded(
                artifact_id,
                est_tokens=wanted.est_tokens,
                max_tokens=max_tokens,
                sections=meta.sections,
            )
        text = await asyncio.to_thread(self._content_path(artifact_id).read_text, "utf-8")
        _, bodies = split_sections(text)
        return bodies[section]

    @asynccontextmanager
    async def localize(
        self, artifact_id: ArtifactId, *, grants: GrantSet
    ) -> AsyncIterator[Path]:
        self._authorize(artifact_id, grants)
        meta = await asyncio.to_thread(self._fetch_sync, artifact_id)
        if meta.kind != KIND_BLOB:
            raise ValueError(f"{artifact_id} is a note; use read_note")
        # Local backend: the blob already is a local file. Copy nothing.
        yield self._content_path(artifact_id)

    async def list(
        self,
        job_id: str,
        *,
        kind: str | None = None,
        namespace: str | None = None,
    ) -> Sequence[ArtifactMeta]:
        return await asyncio.to_thread(self._list_sync, job_id, kind, namespace)

    def _list_sync(
        self, job_id: str, kind: str | None, namespace: str | None
    ) -> Sequence[ArtifactMeta]:
        if not self._index_path(job_id).exists():
            return ()
        sql = "SELECT * FROM artifacts WHERE job_id = ?"
        params: list[Any] = [job_id]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        if namespace is not None:
            sql += " AND (namespace = ? OR namespace LIKE ?)"
            params += [namespace, f"{namespace}/%"]
        sql += " ORDER BY id"
        with self._connect(job_id) as conn:
            return tuple(self._row_to_meta(r) for r in conn.execute(sql, params))


__all__ = ["LocalArtifactStore", "KIND_BLOB", "KIND_NOTE"]
