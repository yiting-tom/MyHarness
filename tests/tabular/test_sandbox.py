"""The escape tests. These are the grant model, not a hardening checklist.

Every case here corresponds to a route walked in spike #10. If one of them
starts passing after a DuckDB upgrade, a worker can read data it was never
granted -- so these assert on behaviour, not on the pragma strings.
"""

from __future__ import annotations

import threading
from pathlib import Path

import duckdb
import pytest

from myharness.lanes.tabular.sandbox import (
    SANDBOX_PRAGMAS,
    Ingest,
    SandboxError,
    run_guarded,
    sandboxed,
)


@pytest.fixture
def granted(tmp_path: Path) -> Path:
    p = tmp_path / "granted.csv"
    p.write_text("x,y\n1,2\n3,4\n", encoding="utf-8")
    return p


@pytest.fixture
def ungranted(tmp_path: Path) -> Path:
    p = tmp_path / "secret.csv"
    p.write_text("s\n99\n", encoding="utf-8")
    return p


@pytest.fixture
def conn(granted: Path):
    with sandboxed([Ingest("t", granted, "read_csv_auto")]) as c:
        yield c


def test_granted_data_is_queryable(conn):
    """The sandbox must not also close the legitimate path."""
    assert conn.execute("SELECT sum(y) FROM t").fetchall() == [(6,)]


@pytest.mark.parametrize(
    "label",
    ["read_file", "glob", "attach_duckdb", "attach_sqlite", "copy_out",
     "read_text", "http", "install", "load"],
)
def test_filesystem_and_network_are_closed(conn, ungranted: Path, label: str):
    d = ungranted.parent
    escapes = {
        "read_file": f"SELECT * FROM read_csv_auto('{ungranted}')",
        "glob": f"SELECT * FROM read_csv_auto('{d}/*.csv')",
        "attach_duckdb": f"ATTACH '{d}/other.db' AS other",
        "attach_sqlite": f"ATTACH '{d}/other.db' (TYPE sqlite)",
        "copy_out": f"COPY t TO '{d}/leak.csv'",
        "read_text": f"SELECT * FROM read_text('{ungranted}')",
        "http": "SELECT * FROM read_csv_auto('https://example.com/a.csv')",
        "install": "INSTALL httpfs",
        "load": "LOAD httpfs",
    }
    with pytest.raises(duckdb.Error):
        conn.execute(escapes[label]).fetchall()
    assert ungranted.read_text(encoding="utf-8") == "s\n99\n", "blob was modified"


@pytest.mark.parametrize(
    "sql",
    ["SET enable_external_access=true",
     "SET lock_configuration=false",
     "SET allow_community_extensions=true",
     "SET allowed_paths=['/etc']",
     "SET allowed_directories=['/etc']"],
)
def test_configuration_cannot_be_reopened(conn, sql: str):
    with pytest.raises(duckdb.Error):
        conn.execute(sql)


def test_external_access_is_the_fence(granted: Path, ungranted: Path):
    """Drop that one pragma and every escape above reopens.

    Dropping it is a silent regression: the queries a worker actually writes
    keep working and only the escape route comes back. Pinned here so a future
    "simplification" fails a test rather than a grant check.
    """
    conn = duckdb.connect(":memory:")
    conn.execute(Ingest("t", granted, "read_csv_auto").sql())
    for pragma in SANDBOX_PRAGMAS:
        if "enable_external_access" not in pragma:
            conn.execute(pragma)
    try:
        assert conn.execute(
            f"SELECT * FROM read_csv_auto('{ungranted}')"
        ).fetchall(), "the rest of the pragmas fenced nothing on their own"
    finally:
        conn.close()


def test_lock_configuration_pins_what_the_fence_does_not(granted: Path):
    """The fence defends itself; the lock covers what it leaves mutable.

    In DuckDB 1.5.5 ``enable_external_access=false`` cannot be reversed and it
    freezes ``allowed_paths``/``allowed_directories`` too, so the lock is not a
    second fence and this file must not claim it is. What it does add is real:
    extension autoloading stays settable without it.
    """
    unlocked = duckdb.connect(":memory:")
    unlocked.execute(Ingest("t", granted, "read_csv_auto").sql())
    for pragma in SANDBOX_PRAGMAS:
        if "lock_configuration" not in pragma:
            unlocked.execute(pragma)
    unlocked.execute("SET autoload_known_extensions=true")  # allowed, unlocked
    unlocked.close()

    with sandboxed([Ingest("t", granted, "read_csv_auto")]) as locked:
        with pytest.raises(duckdb.Error):
            locked.execute("SET autoload_known_extensions=true")


def test_the_fence_cannot_be_reopened_even_unlocked(granted: Path):
    """Documents why the lock is defence in depth rather than the fence."""
    conn = duckdb.connect(":memory:")
    conn.execute(Ingest("t", granted, "read_csv_auto").sql())
    conn.execute("SET enable_external_access=false")
    try:
        with pytest.raises(duckdb.Error):
            conn.execute("SET enable_external_access=true")
    finally:
        conn.close()


def test_lock_configuration_is_applied_last():
    """It freezes everything above it, itself included."""
    assert SANDBOX_PRAGMAS[-1] == "SET lock_configuration=true"
    assert "SET enable_external_access=false" in SANDBOX_PRAGMAS[:-1]


def test_ingest_happens_before_the_door_closes(granted: Path):
    """Views are lazy, tables are not. A view would read after lock-down."""
    with sandboxed([Ingest("t", granted, "read_csv_auto")]) as c:
        assert c.execute("SELECT count(*) FROM t").fetchall() == [(2,)]


def test_unreadable_source_raises_sandbox_error(tmp_path: Path):
    with pytest.raises(SandboxError, match="could not load"):
        with sandboxed([Ingest("t", tmp_path / "nope.csv", "read_csv_auto")]):
            pass


def test_connection_is_closed_even_on_error(granted: Path):
    held = None
    with pytest.raises(ValueError):
        with sandboxed([Ingest("t", granted, "read_csv_auto")]) as c:
            held = c
            raise ValueError("boom")
    with pytest.raises(duckdb.Error):
        held.execute("SELECT 1")


def test_paths_with_quotes_are_escaped(tmp_path: Path):
    """The path comes from the store, but a name can still contain a quote."""
    odd = tmp_path / "it's data.csv"
    odd.write_text("x\n7\n", encoding="utf-8")
    with sandboxed([Ingest("t", odd, "read_csv_auto")]) as c:
        assert c.execute("SELECT sum(x) FROM t").fetchall() == [(7,)]


class TestRunGuarded:
    def test_returns_columns_and_rows(self, conn):
        columns, rows = run_guarded(conn, "SELECT x, y FROM t ORDER BY x",
                                    timeout_s=5.0, fetch=10)
        assert columns == ["x", "y"]
        assert rows == [(1, 2), (3, 4)]

    def test_fetch_bounds_the_rows_pulled(self, conn):
        _, rows = run_guarded(conn, "SELECT * FROM range(1000)",
                              timeout_s=5.0, fetch=3)
        assert len(rows) == 3

    def test_runaway_query_is_interrupted(self, conn):
        """No prompt makes a worker write only terminating SQL (design.md D5)."""
        with pytest.raises(duckdb.Error):
            run_guarded(
                conn,
                "SELECT count(*) FROM range(100000000000) a, range(100000) b",
                timeout_s=0.5, fetch=1,
            )

    def test_no_timer_outlives_the_call(self, conn):
        """A timer left running would interrupt the *next* query instead."""
        before = threading.active_count()
        run_guarded(conn, "SELECT 1", timeout_s=30.0, fetch=1)
        assert threading.active_count() == before
        assert conn.execute("SELECT 2").fetchall() == [(2,)]
