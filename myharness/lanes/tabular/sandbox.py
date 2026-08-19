"""A DuckDB connection a lane worker's SQL cannot escape from.

Giving a worker SQL is giving it a file reader, and the grant model in
design.md D2 holds only for as long as the worker has no way around it. The
configuration below is not a collection of hardening tips: spike #10 walked
twelve escape routes and each one closes on a specific line here.

``enable_external_access=false`` is the fence, and in DuckDB 1.5.5 it defends
itself -- it cannot be re-enabled on a running database, and it freezes the
``allowed_paths``/``allowed_directories`` settings that would widen it. The
other four pragmas are not redundant but they are not the fence either:
``lock_configuration`` pins ``autoload``/``autoinstall``, which stay mutable
without it, and it keeps the fence's self-defence from being the only thing
standing between a worker and a future DuckDB that relaxes it.

Order matters. Views are lazy and tables are not, so every granted blob has to
be materialised *before* the door closes. That is the whole reason a byte cap
exists (design.md D3) -- it is the sandbox's price, not a performance knob.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

#: Applied after ingest, in this order. ``lock_configuration`` must be last:
#: it freezes everything above it, including itself.
#: Only the first line is the fence -- see the module docstring.
SANDBOX_PRAGMAS: tuple[str, ...] = (
    "SET enable_external_access=false",
    "SET allow_community_extensions=false",
    "SET autoinstall_known_extensions=false",
    "SET autoload_known_extensions=false",
    "SET lock_configuration=true",
)


class SandboxError(RuntimeError):
    """The sandbox could not be established. Never raised at the worker."""


@dataclass(frozen=True, slots=True)
class Ingest:
    """One granted blob, already checked, ready to become a table."""

    table: str
    path: Path
    reader: str

    def sql(self) -> str:
        # The path comes from the store, never from the worker (design.md D2).
        escaped = str(self.path).replace("'", "''")
        return f"CREATE TABLE {self.table} AS SELECT * FROM {self.reader}('{escaped}')"


@contextmanager
def sandboxed(ingests: Sequence[Ingest]) -> Iterator[duckdb.DuckDBPyConnection]:
    """Ingest, then close the door. The yielded connection is worker-safe."""
    conn = duckdb.connect(":memory:")
    try:
        for ingest in ingests:
            try:
                conn.execute(ingest.sql())
            except duckdb.Error as exc:
                raise SandboxError(f"could not load {ingest.table}: {_first_line(exc)}") from exc
        for pragma in SANDBOX_PRAGMAS:
            conn.execute(pragma)
        yield conn
    finally:
        conn.close()


@contextmanager
def interruptible(conn: duckdb.DuckDBPyConnection, timeout_s: float) -> Iterator[None]:
    """Cut anything inside this block off after ``timeout_s``.

    Covers the whole block, not just ``execute``: DuckDB streams, so a query
    that returns instantly can still spend minutes inside ``fetchmany``. The
    ``into`` path is entirely fetching, and an earlier version of it wrapped
    only the execute -- which is to say it had no timeout at all.
    """
    timer = threading.Timer(timeout_s, conn.interrupt)
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
        # cancel() only signals; join so no timer can fire into the next query
        # on this connection.
        timer.join()


def run_guarded(
    conn: duckdb.DuckDBPyConnection, sql: str, *, timeout_s: float, fetch: int
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Run one statement under a wall-clock cap, fetching at most ``fetch`` rows.

    A runaway join does not stop on its own and no amount of prompting makes a
    worker write only terminating SQL, so the cap is enforced from outside by a
    timer thread (design.md D5). ``fetch`` is honoured here rather than by
    appending a LIMIT: rewriting the worker's SQL would change its meaning.
    """
    with interruptible(conn, timeout_s):
        cursor = conn.execute(sql)
        columns = [d[0] for d in (cursor.description or ())]
        rows = cursor.fetchmany(fetch)
        return columns, [tuple(r) for r in rows]


def _first_line(exc: BaseException) -> str:
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


__all__ = [
    "Ingest", "SANDBOX_PRAGMAS", "SandboxError",
    "interruptible", "run_guarded", "sandboxed",
]
