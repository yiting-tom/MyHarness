"""Every way a query can fail, and what the worker is told about it.

These are not edge cases to the worker -- they are its normal experience. It
writes SQL against a schema it saw once, in one turn, with no chance to
iterate interactively. A refusal that only says "error" costs it a whole turn,
so each of these asserts on the content of the message, not just on failing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from myharness.artifacts.local import LocalArtifactStore
from myharness.artifacts.types import GrantSet
from myharness.lanes.tabular.query import QueryFailure, QueryRunner
from myharness.lanes.tabular import ingest as ingest_mod

JOB = "jf"


@pytest.fixture
async def runner(tmp_path: Path):
    store = LocalArtifactStore(tmp_path)
    await store.init_job(JOB)
    blob = await store.put_blob(JOB, "raw/a.csv", data=b"x,y\n1,2\n3,4\n",
                                produced_by="user")
    grants = GrantSet.for_lane(JOB, "lanes/l1", [blob.id])
    return (
        QueryRunner(store, job_id=JOB, grants=grants, produced_by="lane:l1",
                    derived_namespace="lanes/l1"),
        blob, store, grants,
    )


async def test_unparseable_artifact_id(runner):
    r, *_ = runner
    got = await r.query(["not an id"], "SELECT 1")
    assert isinstance(got, QueryFailure) and got.code == "bad_artifact_id"


async def test_missing_artifact(runner):
    r, *_ = runner
    got = await r.query([f"{JOB}/blob/raw/absent"], "SELECT 1")
    assert isinstance(got, QueryFailure)


async def test_oversized_blob_is_refused_before_anything_is_read(runner, monkeypatch):
    """Also pins that the module constant is read at call time.

    It was a default argument at first, bound once at import -- which made the
    constant look authoritative while being impossible to change.
    """
    r, blob, *_ = runner
    monkeypatch.setattr(ingest_mod, "MAX_INGEST_BYTES", 4)
    got = await r.query([str(blob.id)], "SELECT * FROM a_csv")
    assert isinstance(got, QueryFailure) and got.code == "blob_too_large"
    assert "sandbox" in got.message


async def test_unsupported_format_names_what_is_supported(tmp_path: Path):
    store = LocalArtifactStore(tmp_path)
    await store.init_job(JOB)
    blob = await store.put_blob(JOB, "raw/report.xlsx", data=b"PK\x03\x04",
                                produced_by="user")
    r = QueryRunner(store, job_id=JOB,
                    grants=GrantSet.for_lane(JOB, "lanes/l1", [blob.id]),
                    produced_by="lane:l1", derived_namespace="lanes/l1")
    got = await r.query([str(blob.id)], "SELECT 1")
    assert isinstance(got, QueryFailure) and got.code == "unsupported_format"
    assert ".parquet" in got.message


async def test_a_file_that_is_not_what_it_claims(tmp_path: Path):
    """A .csv full of binary is an ingest failure, not a crash."""
    store = LocalArtifactStore(tmp_path)
    await store.init_job(JOB)
    blob = await store.put_blob(JOB, "raw/broken.parquet",
                                data=b"definitely not parquet", produced_by="user")
    r = QueryRunner(store, job_id=JOB,
                    grants=GrantSet.for_lane(JOB, "lanes/l1", [blob.id]),
                    produced_by="lane:l1", derived_namespace="lanes/l1")
    got = await r.query([str(blob.id)], "SELECT 1")
    assert isinstance(got, QueryFailure) and got.code == "ingest_failed"
    assert "broken_parquet" in got.message


async def test_sql_error_carries_the_table_names(runner):
    r, blob, *_ = runner
    got = await r.query([str(blob.id)], "SELECT nope FROM a_csv")
    assert isinstance(got, QueryFailure) and got.code == "sql_error"
    assert "a_csv" in got.bindings


async def test_timeout_says_what_to_do_instead(tmp_path: Path):
    store = LocalArtifactStore(tmp_path)
    await store.init_job(JOB)
    blob = await store.put_blob(JOB, "raw/a.csv", data=b"x\n1\n", produced_by="user")
    r = QueryRunner(store, job_id=JOB,
                    grants=GrantSet.for_lane(JOB, "lanes/l1", [blob.id]),
                    produced_by="lane:l1", derived_namespace="lanes/l1",
                    timeout_s=0.5)
    got = await r.query(
        [str(blob.id)],
        "SELECT count(*) FROM range(100000000000) a, range(100000) b",
    )
    assert isinstance(got, QueryFailure) and got.code == "query_timeout"
    assert "WHERE" in got.message or "aggregate" in got.message


async def test_timeout_during_into_is_still_a_failure_not_a_partial_artifact(
    tmp_path: Path,
):
    store = LocalArtifactStore(tmp_path)
    await store.init_job(JOB)
    blob = await store.put_blob(JOB, "raw/a.csv", data=b"x\n1\n", produced_by="user")
    r = QueryRunner(store, job_id=JOB,
                    grants=GrantSet.for_lane(JOB, "lanes/l1", [blob.id]),
                    produced_by="lane:l1", derived_namespace="lanes/l1",
                    timeout_s=0.5)
    got = await r.query(
        [str(blob.id)],
        "SELECT count(*) FROM range(100000000000) a, range(100000) b",
        into="never",
    )
    assert isinstance(got, QueryFailure) and got.code == "query_timeout"
    listed = await store.list(JOB, kind="blob")
    assert not any("derived/never" in a.id.name for a in listed), (
        "a failed query left an artifact behind"
    )


@pytest.mark.parametrize("bad", ["../escape", "..", "a//b", "with space", "-lead"])
async def test_illegal_into_names(runner, bad: str):
    r, blob, *_ = runner
    got = await r.query([str(blob.id)], "SELECT 1 AS n", into=bad)
    assert isinstance(got, QueryFailure) and got.code == "bad_into_name"


async def test_a_nested_into_name_is_allowed(runner):
    """`q1/summary` is legal and still confined to the lane's namespace."""
    r, blob, *_ = runner
    got = await r.query([str(blob.id)], "SELECT 1 AS n", into="q1/summary")
    assert not isinstance(got, QueryFailure), got
    assert got.artifact.id.name == "lanes/l1/derived/q1/summary"


async def test_empty_into_means_no_into(runner):
    """The tool strips it, so "" is "return the rows", not a bad name."""
    r, blob, *_ = runner
    got = await r.query([str(blob.id)], "SELECT 1 AS n", into="")
    assert not isinstance(got, QueryFailure) and hasattr(got, "rendered")


async def test_into_row_cap_is_reported_not_silent(runner, monkeypatch):
    from myharness.lanes.tabular import query as query_mod

    r, blob, *_ = runner
    monkeypatch.setattr(query_mod, "MAX_INTO_ROWS", 5)
    monkeypatch.setattr(query_mod, "_INTO_BATCH", 2)
    got = await r.query([str(blob.id)], "SELECT * FROM range(100) t(n)", into="capped")
    assert not isinstance(got, QueryFailure), got
    assert got.rows == 5 and got.row_limited
    assert "row limit reached" in got.text()
