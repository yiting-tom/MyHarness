from __future__ import annotations

import pytest

from myharness.lanes.tabular.guard import guard_sql


def test_single_select_passes():
    assert guard_sql("SELECT * FROM t") is None


def test_cte_is_a_select():
    assert guard_sql("WITH a AS (SELECT 1 AS n) SELECT n FROM a") is None


@pytest.mark.parametrize("sql", ["", "   ", "\n\t "])
def test_empty_is_rejected(sql: str):
    r = guard_sql(sql)
    assert r is not None and r.code == "empty_sql"


def test_multiple_statements_are_rejected():
    """duckdb would run both; spike #10 confirmed it."""
    r = guard_sql("SELECT 1; SELECT 2")
    assert r is not None and r.code == "multiple_statements"
    assert "CTE" in r.message or "WITH" in r.message, "refusal must offer a way out"


def test_trailing_semicolon_is_still_one_statement():
    assert guard_sql("SELECT * FROM t;") is None


@pytest.mark.parametrize(
    "sql",
    ["CREATE TABLE q AS SELECT 1",
     "CREATE TEMP TABLE q AS SELECT 1",
     "DROP TABLE t",
     "UPDATE t SET x = 9",
     "DELETE FROM t",
     "INSERT INTO t VALUES (1, 2)",
     "ATTACH 'x.db'",
     "SET enable_external_access=true"],
)
def test_non_select_is_rejected(sql: str):
    r = guard_sql(sql)
    assert r is not None and r.code == "not_a_select"


def test_not_a_select_refusal_points_at_into():
    r = guard_sql("CREATE TABLE q AS SELECT 1")
    assert r is not None and "into" in r.message


def test_unparseable_sql_is_a_rejection_not_a_crash():
    r = guard_sql("SELECT FROM WHERE )(")
    assert r is not None and r.code == "unparseable_sql"


def test_smuggling_a_second_statement_past_a_select():
    r = guard_sql("SELECT 1 ; DROP TABLE t")
    assert r is not None and r.code == "multiple_statements"
