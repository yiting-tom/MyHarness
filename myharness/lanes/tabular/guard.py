"""What the worker is allowed to submit, decided before DuckDB sees it.

DuckDB will not do this for us. ``execute()`` happily runs every statement in
a semicolon-separated string -- spike #10 confirmed both halves of
``CREATE TEMP TABLE z AS SELECT 1; SELECT * FROM z`` executed -- so a
multi-statement guard has to live on this side.

Restricting to SELECT costs nothing real: a CTE is a SELECT, and under a
one-statement rule a ``CREATE TEMP TABLE`` has no following statement that
could use it.
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb

#: ``PRAGMA`` and ``DESCRIBE`` parse as SELECT, which is fine -- they read.
ALLOWED_STATEMENT = "SELECT"


@dataclass(frozen=True, slots=True)
class Rejection:
    """A refusal the worker can act on. Never an exception (design.md D7)."""

    code: str
    message: str


def guard_sql(sql: str) -> Rejection | None:
    """``None`` when ``sql`` is exactly one read-only statement."""
    text = sql.strip()
    if not text:
        return Rejection("empty_sql", "sql must not be empty")
    try:
        statements = duckdb.extract_statements(text)
    except duckdb.Error as exc:
        return Rejection("unparseable_sql", str(exc).splitlines()[0])
    if not statements:
        return Rejection("empty_sql", "sql contained no statement")
    if len(statements) > 1:
        return Rejection(
            "multiple_statements",
            f"sql must be exactly one statement, got {len(statements)}. "
            "Split the work across separate calls, or combine it into one "
            "SELECT with CTEs (WITH a AS (...), b AS (...) SELECT ...).",
        )
    kind = str(statements[0].type).rsplit(".", 1)[-1]
    if kind != ALLOWED_STATEMENT:
        return Rejection(
            "not_a_select",
            f"only SELECT is allowed, got {kind}. The data is read-only here; "
            "to keep a derived result, pass `into` and it becomes a new artifact.",
        )
    return None


__all__ = ["ALLOWED_STATEMENT", "Rejection", "guard_sql"]
