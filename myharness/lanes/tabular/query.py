"""One query: authorise, localise, ingest, lock, run, shape the answer.

The order is the design. Authorisation is decided on artifact ids before any
byte is read, the granted files become tables, and only then does the worker's
SQL get to run -- against a connection that can no longer see a filesystem.

Nothing here raises at the worker. A wrong table name, a syntax error, a blob
in a format we cannot read: all of them come back as text the worker can act on
in its next turn (design.md D7). It has one context; "error" spends a whole
turn, "error, and here are the tables you actually have" does not.
"""

from __future__ import annotations

import asyncio
import csv
import time
from collections.abc import Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

import duckdb

from myharness.artifacts.errors import ArtifactError
from myharness.artifacts.ids import ArtifactId
from myharness.artifacts.store import ArtifactStore
from myharness.artifacts.types import ArtifactMeta, GrantSet
from myharness.lanes.tabular.binding import Binding, bind_all, describe
from myharness.lanes.tabular.guard import guard_sql
from myharness.lanes.tabular.ingest import IngestRefusal, check_size, choose_reader
from myharness.lanes.tabular.render import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_ROWS,
    Rendered,
    render_rows,
)
from myharness.lanes.tabular.sandbox import (
    Ingest,
    SandboxError,
    interruptible,
    run_guarded,
    sandboxed,
)

#: A query that has not answered in this long is not going to (design.md D5).
DEFAULT_TIMEOUT_S = 30.0
#: Rows written to a derived artifact. Generous -- it never enters a context --
#: but not unbounded, because disk is not free either.
MAX_INTO_ROWS = 5_000_000
#: Streamed in batches so a large `into` does not materialise in the harness.
_INTO_BATCH = 10_000
#: Sample rows shown by inspect_blob. Enough to see the shape of a value.
INSPECT_SAMPLE_ROWS = 5

#: Concurrent ingests across the whole process. Tool annotations stop one
#: worker running two queries at once, but a job runs several lanes in one
#: event loop, and each ingest can hold MAX_INGEST_BYTES. Without this the
#: memory ceiling is the cap times however many lanes happen to be in flight.
_INGEST_SLOTS = asyncio.Semaphore(2)


@dataclass(frozen=True, slots=True)
class QueryFailure:
    code: str
    message: str
    bindings: str = ""

    def text(self) -> str:
        line = f"ERROR {self.code}: {self.message}"
        return f"{line}\nTables in this call: {self.bindings}" if self.bindings else line


@dataclass(frozen=True, slots=True)
class QueryResult:
    rendered: Rendered
    bindings: str
    elapsed_s: float

    def text(self) -> str:
        return f"Tables: {self.bindings}\n\n{self.rendered.text}"


@dataclass(frozen=True, slots=True)
class IntoResult:
    """A derived artifact. The rows stayed on disk; only this comes back."""

    artifact: ArtifactMeta
    rows: int
    columns: tuple[str, ...]
    row_limited: bool

    def text(self) -> str:
        note = " (row limit reached)" if self.row_limited else ""
        return (
            f"wrote {self.artifact.id} -- {self.rows:,} rows{note}, "
            f"columns: {', '.join(self.columns)}\n"
            f"Query it by naming {self.artifact.id} in a later duckdb_query call."
        )


@dataclass(slots=True)
class _Prepared:
    bindings: tuple[Binding, ...]
    ingests: list[Ingest] = field(default_factory=list)


class QueryRunner:
    """Binds the store, the grant set and the caller's limits to one lane."""

    def __init__(
        self,
        store: ArtifactStore,
        *,
        job_id: str,
        grants: GrantSet,
        produced_by: str,
        derived_namespace: str,
        max_rows: int = DEFAULT_MAX_ROWS,
        max_chars: int = DEFAULT_MAX_CHARS,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._store = store
        self._job_id = job_id
        self._grants = grants
        self._produced_by = produced_by
        self._derived_namespace = derived_namespace
        self._max_rows = max_rows
        self._max_chars = max_chars
        self._timeout_s = timeout_s

    async def query(
        self, artifacts: Sequence[str], sql: str, *, into: str = ""
    ) -> QueryResult | IntoResult | QueryFailure:
        if not artifacts:
            return QueryFailure(
                "no_artifacts",
                "name at least one blob artifact to query. SQL cannot reach a "
                "file that was not named -- that is how authorisation works here.",
            )
        rejection = guard_sql(sql)
        if rejection is not None:
            return QueryFailure(rejection.code, rejection.message)

        async with AsyncExitStack() as stack:
            prepared = await self._prepare(artifacts, stack)
            if isinstance(prepared, QueryFailure):
                return prepared
            bindings = describe(prepared.bindings)
            scratch = Path(stack.enter_context(TemporaryDirectory(prefix="mh-into-")))
            try:
                async with _INGEST_SLOTS:
                    outcome = await asyncio.to_thread(
                        self._execute, prepared, sql, bindings, into, scratch
                    )
            except SandboxError as exc:
                return QueryFailure("ingest_failed", str(exc), bindings)
            if isinstance(outcome, _PendingInto):
                # The artifact write is async and belongs outside the worker
                # thread; the scratch file lives until this stack unwinds.
                return await self._store_into(outcome)
            return outcome
        raise AssertionError("unreachable")  # pragma: no cover

    async def _store_into(self, pending: _PendingInto) -> IntoResult:
        meta = await self._store.put_blob(
            self._job_id, pending.name,
            source=pending.path,
            produced_by=self._produced_by,
            schema={"format": "csv", "columns": list(pending.columns),
                    "rows": pending.rows},
        )
        return IntoResult(meta, pending.rows, pending.columns, pending.row_limited)

    async def inspect(self, artifact: str) -> QueryResult | QueryFailure:
        """Columns, types, row count and a few rows -- before writing any SQL."""
        async with AsyncExitStack() as stack:
            prepared = await self._prepare([artifact], stack)
            if isinstance(prepared, QueryFailure):
                return prepared
            table = prepared.bindings[0].table
            bindings = describe(prepared.bindings)
            try:
                async with _INGEST_SLOTS:
                    return await asyncio.to_thread(
                        self._describe, prepared, table, bindings
                    )
            except SandboxError as exc:
                return QueryFailure("ingest_failed", str(exc), bindings)
        raise AssertionError("unreachable")  # pragma: no cover

    # ---- preparation: every refusal happens here, before any byte is read ----

    async def _prepare(
        self, artifacts: Sequence[str], stack: AsyncExitStack
    ) -> _Prepared | QueryFailure:
        parsed: list[ArtifactId] = []
        for raw in artifacts:
            try:
                parsed.append(ArtifactId.parse(str(raw).strip()))
            except ValueError as exc:
                return QueryFailure("bad_artifact_id", str(exc))

        bindings = bind_all(parsed)
        prepared = _Prepared(bindings)
        for binding in bindings:
            try:
                meta = await self._store.stat(binding.artifact, grants=self._grants)
            except ArtifactError as exc:
                return QueryFailure(
                    exc.to_dict().get("code", "not_granted"), str(exc)
                )
            if meta.kind != "blob":
                return QueryFailure(
                    "not_a_blob",
                    f"{binding.artifact} is a note. Use read_note for notes; "
                    "duckdb_query is for raw data blobs.",
                )
            too_big = check_size(meta)
            if too_big is not None:
                return QueryFailure(too_big.code, too_big.message)
            try:
                path = await stack.enter_async_context(
                    self._store.localize(binding.artifact, grants=self._grants)
                )
            except ArtifactError as exc:
                return QueryFailure(
                    exc.to_dict().get("code", "not_granted"), str(exc)
                )
            reader = choose_reader(path, meta)
            if isinstance(reader, IngestRefusal):
                return QueryFailure(reader.code, reader.message)
            prepared.ingests.append(Ingest(binding.table, path, reader))
        return prepared

    # ---- execution: inside a thread, behind the sandbox ---------------------

    def _execute(
        self, prepared: _Prepared, sql: str, bindings: str, into: str, scratch: Path
    ) -> QueryResult | _PendingInto | QueryFailure:
        with sandboxed(prepared.ingests) as conn:
            if into:
                return self._run_into(conn, sql, bindings, into, scratch)
            started = time.monotonic()
            try:
                columns, rows = run_guarded(
                    conn, sql,
                    timeout_s=self._timeout_s,
                    fetch=self._max_rows + 1,
                )
            except duckdb.Error as exc:
                return self._sql_failure(exc, bindings)
            elapsed = time.monotonic() - started
            rendered = render_rows(
                columns, rows,
                max_rows=self._max_rows,
                max_chars=self._max_chars,
                more_available=len(rows) > self._max_rows,
            )
            return QueryResult(rendered, bindings, elapsed)

    def _describe(
        self, prepared: _Prepared, table: str, bindings: str
    ) -> QueryResult | QueryFailure:
        with sandboxed(prepared.ingests) as conn:
            try:
                schema_cols, schema_rows = run_guarded(
                    conn, f"DESCRIBE {table}", timeout_s=self._timeout_s, fetch=500
                )
                with interruptible(conn, self._timeout_s):
                    (count,), = conn.execute(f"SELECT count(*) FROM {table}").fetchall()
                sample_cols, sample_rows = run_guarded(
                    conn, f"SELECT * FROM {table} LIMIT {INSPECT_SAMPLE_ROWS}",
                    timeout_s=self._timeout_s, fetch=INSPECT_SAMPLE_ROWS,
                )
            except duckdb.Error as exc:
                return self._sql_failure(exc, bindings)

        schema = render_rows(
            schema_cols, schema_rows, max_rows=500, max_chars=self._max_chars
        )
        sample = render_rows(
            sample_cols, sample_rows,
            max_rows=INSPECT_SAMPLE_ROWS, max_chars=self._max_chars,
        )
        text = (
            f"Tables: {bindings}\n{count:,} rows\n\n"
            f"Columns:\n{schema.text}\n\nFirst {INSPECT_SAMPLE_ROWS} rows:\n{sample.text}"
        )
        return QueryResult(Rendered(text, len(sample_rows), False, False), bindings, 0.0)

    def _run_into(
        self, conn: duckdb.DuckDBPyConnection, sql: str, bindings: str,
        into: str, scratch: Path,
    ) -> _PendingInto | QueryFailure:
        """Stream the full result to a CSV the worker never reads.

        DuckDB cannot COPY it out -- external access is off, which is the point
        -- so the harness writes it, in batches, so a large result never sits in
        memory whole.
        """
        try:
            name = _derived_name(self._job_id, self._derived_namespace, into)
        except ValueError as exc:
            return QueryFailure("bad_into_name", str(exc), bindings)

        target = scratch / "result.csv"
        written = 0
        limited = False
        try:
            # The guard has to span the fetch loop, not just the execute: this
            # path is almost entirely fetching, and DuckDB streams.
            with interruptible(conn, self._timeout_s):
                cursor = conn.execute(sql)
                columns = tuple(d[0] for d in (cursor.description or ()))
                with target.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(columns)
                    while True:
                        batch = cursor.fetchmany(_INTO_BATCH)
                        if not batch:
                            break
                        if written + len(batch) > MAX_INTO_ROWS:
                            batch = batch[: MAX_INTO_ROWS - written]
                            limited = True
                        writer.writerows(batch)
                        written += len(batch)
                        if limited:
                            break
        except duckdb.Error as exc:
            # No artifact: a half-written CSV published as a result is worse
            # than no result, because nothing downstream can tell.
            return self._sql_failure(exc, bindings)
        return _PendingInto(target, name, written, columns, limited)

    def _sql_failure(self, exc: duckdb.Error, bindings: str) -> QueryFailure:
        message = str(exc).splitlines()[0]
        if isinstance(exc, duckdb.InterruptException):
            return QueryFailure(
                "query_timeout",
                f"the query was still running after {self._timeout_s:.0f}s and was "
                "stopped. Narrow it: aggregate instead of listing, or add a WHERE.",
                bindings,
            )
        return QueryFailure("sql_error", message, bindings)


@dataclass(frozen=True, slots=True)
class _PendingInto:
    """A written CSV that still has to become an artifact, outside the thread."""

    path: Path
    name: str
    rows: int
    columns: tuple[str, ...]
    row_limited: bool


def _derived_name(job_id: str, namespace: str, into: str) -> str:
    """Derived data lands in the lane's own namespace.

    That is what keeps it inside the existing grant model instead of inventing a
    third source of access -- ``GrantSet.allows`` already passes anything under
    a lane's own namespace (design.md D4, DESIGN.md D2).
    """
    leaf = into.strip()
    if not leaf:
        raise ValueError("into must be a name")
    name = f"{namespace}/derived/{leaf}"
    ArtifactId(job_id=job_id, kind="blob", name=name)  # raises on an illegal name
    return name


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "INSPECT_SAMPLE_ROWS",
    "MAX_INTO_ROWS",
    "IntoResult",
    "QueryFailure",
    "QueryResult",
    "QueryRunner",
]
