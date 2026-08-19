"""The data tools end to end, against a real store and a real DuckDB.

Reaching data is the point of these tools, so almost every test here is really
a question about authorisation: can the worker get at something it was not
given? The answer has to stay no through SQL, through `into`, and through a
derived artifact used as the input to a later query.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from myharness.artifacts.local import LocalArtifactStore
from myharness.artifacts.types import GrantSet
from myharness.lanes.tools import WorkerToolbox
from myharness.lanes.types import LaneRegistry, LaneType

JOB = "j9"

ROWS = "\n".join(
    ["ts,account,amount,channel"]
    + [f"2024-01-{d:02d},A{d % 3},{d * 100},{'atm' if d % 2 else 'wire'}"
       for d in range(1, 21)]
)


def text_of(result: dict) -> str:
    return result["content"][0]["text"]


@pytest.fixture
async def bench(tmp_path: Path):
    store = LocalArtifactStore(tmp_path)
    await store.init_job(JOB)
    charter = tmp_path / "c.md"
    charter.write_text("charter", encoding="utf-8")
    registry = LaneRegistry(
        LaneType(
            name="ta", charter_path=charter, state_max_tokens=200,
            tools=("read_note", "write_finding", "update_state",
                   "localize_blob", "inspect_blob", "duckdb_query"),
        )
    )
    lane = registry.create("txn-2024", "ta")
    txns = await store.put_blob(JOB, "raw/txns.csv", data=ROWS.encode(),
                                produced_by="user")
    secret = await store.put_blob(JOB, "raw/payroll.csv",
                                  data=b"who,salary\nceo,999\n", produced_by="user")
    # Under the lane's own namespace, so it is granted -- otherwise the
    # authorisation check fires first and the kind check is never reached.
    note = await store.put_note(JOB, f"{lane.namespace}/findings/1", "text",
                                produced_by=f"lane:{lane.id}")
    toolbox = WorkerToolbox(
        store=store, job_id=JOB, lane=lane,
        grants=GrantSet.for_lane(JOB, lane.namespace, [txns.id]),
        read_budget=3000,
    )
    toolbox.build_server()
    try:
        yield toolbox, {"txns": txns, "secret": secret, "note": note}, store
    finally:
        await toolbox.aclose()


async def call(toolbox, name: str, **args) -> str:
    return text_of(await toolbox.handlers[name](args))


class TestInspect:
    async def test_reports_columns_types_and_count(self, bench):
        toolbox, ids, _ = bench
        out = await call(toolbox, "inspect_blob", artifact=str(ids["txns"].id))
        assert "account" in out and "amount" in out
        assert "20 rows" in out

    async def test_reports_the_table_name_so_sql_can_be_written(self, bench):
        toolbox, ids, _ = bench
        out = await call(toolbox, "inspect_blob", artifact=str(ids["txns"].id))
        assert "txns_csv" in out

    async def test_shows_a_sample(self, bench):
        toolbox, ids, _ = bench
        out = await call(toolbox, "inspect_blob", artifact=str(ids["txns"].id))
        assert "2024-01-01" in out

    async def test_ungranted_blob_is_refused(self, bench):
        toolbox, ids, _ = bench
        out = await call(toolbox, "inspect_blob", artifact=str(ids["secret"].id))
        assert out.startswith("ERROR") and "999" not in out


class TestQuery:
    async def test_aggregate_over_granted_data(self, bench):
        toolbox, ids, _ = bench
        out = await call(
            toolbox, "duckdb_query", artifacts=[str(ids["txns"].id)],
            sql="SELECT channel, count(*) AS n FROM txns_csv GROUP BY 1 ORDER BY 1",
        )
        assert "atm" in out and "wire" in out and "10" in out

    async def test_response_always_names_the_bound_tables(self, bench):
        toolbox, ids, _ = bench
        out = await call(toolbox, "duckdb_query", artifacts=[str(ids["txns"].id)],
                         sql="SELECT count(*) FROM txns_csv")
        assert "txns_csv = " in out

    async def test_wrong_table_name_refusal_carries_the_right_one(self, bench):
        """A worker has one context; "error" alone costs it a whole turn."""
        toolbox, ids, _ = bench
        out = await call(toolbox, "duckdb_query", artifacts=[str(ids["txns"].id)],
                         sql="SELECT * FROM transactions")
        assert out.startswith("ERROR") and "txns_csv" in out

    async def test_two_blobs_can_be_joined(self, bench):
        toolbox, ids, store = bench
        other = await store.put_blob(
            JOB, "lanes/txn-2024/lookup.csv",
            data=b"account,tier\nA0,gold\nA1,silver\nA2,bronze\n", produced_by="lane:txn-2024",
        )
        out = await call(
            toolbox, "duckdb_query",
            artifacts=[str(ids["txns"].id), str(other.id)],
            sql="SELECT l.tier, count(*) AS n FROM txns_csv t "
                "JOIN lookup_csv l USING (account) GROUP BY 1 ORDER BY 1",
        )
        assert "gold" in out and "silver" in out


class TestAuthorisation:
    async def test_ungranted_artifact_named_directly(self, bench):
        toolbox, ids, _ = bench
        out = await call(toolbox, "duckdb_query", artifacts=[str(ids["secret"].id)],
                         sql="SELECT * FROM payroll_csv")
        assert out.startswith("ERROR") and "999" not in out

    async def test_ungranted_artifact_alongside_a_granted_one(self, bench):
        """Partial authorisation is no authorisation."""
        toolbox, ids, _ = bench
        out = await call(
            toolbox, "duckdb_query",
            artifacts=[str(ids["txns"].id), str(ids["secret"].id)],
            sql="SELECT * FROM txns_csv",
        )
        assert out.startswith("ERROR") and "999" not in out

    async def test_sql_cannot_reach_a_file_by_path(self, bench, tmp_path: Path):
        toolbox, ids, _ = bench
        planted = tmp_path / "planted.csv"
        planted.write_text("secret\n42\n", encoding="utf-8")
        out = await call(
            toolbox, "duckdb_query", artifacts=[str(ids["txns"].id)],
            sql=f"SELECT * FROM read_csv_auto('{planted}')",
        )
        assert out.startswith("ERROR") and "42" not in out

    async def test_sql_cannot_reach_the_job_index(self, bench, tmp_path: Path):
        toolbox, ids, _ = bench
        out = await call(
            toolbox, "duckdb_query", artifacts=[str(ids["txns"].id)],
            sql=f"SELECT * FROM read_csv_auto('{tmp_path}/**/*.csv')",
        )
        assert out.startswith("ERROR")

    async def test_granted_note_is_refused_with_a_pointer_to_read_note(self, bench):
        """Authorisation is checked first; this is the kind check behind it."""
        toolbox, ids, _ = bench
        out = await call(toolbox, "duckdb_query", artifacts=[str(ids["note"].id)],
                         sql="SELECT 1")
        assert "read_note" in out

    async def test_naming_nothing_is_refused(self, bench):
        toolbox, _, _ = bench
        out = await call(toolbox, "duckdb_query", artifacts=[], sql="SELECT 1")
        assert out.startswith("ERROR") and "authorisation" in out

    async def test_object_shaped_input_is_refused_not_mangled(self, bench):
        """The fourth golden run lost two lanes to str() on a dict."""
        toolbox, ids, _ = bench
        out = await call(
            toolbox, "duckdb_query",
            artifacts=[{"unexpected": "shape"}], sql="SELECT 1",
        )
        assert out.startswith("ERROR") and "bad_artifacts" in out

    async def test_object_wrapping_a_real_id_is_understood(self, bench):
        toolbox, ids, _ = bench
        out = await call(
            toolbox, "duckdb_query",
            artifacts=[{"blob_path": str(ids["txns"].id)}],
            sql="SELECT count(*) AS n FROM txns_csv",
        )
        assert "20" in out


class TestGuards:
    async def test_second_statement_is_refused(self, bench):
        toolbox, ids, _ = bench
        out = await call(toolbox, "duckdb_query", artifacts=[str(ids["txns"].id)],
                         sql="SELECT 1; DROP TABLE txns_csv")
        assert "multiple_statements" in out

    async def test_write_is_refused_and_points_at_into(self, bench):
        toolbox, ids, _ = bench
        out = await call(toolbox, "duckdb_query", artifacts=[str(ids["txns"].id)],
                         sql="CREATE TABLE x AS SELECT 1")
        assert "not_a_select" in out and "into" in out

    async def test_results_are_row_limited(self, bench):
        toolbox, ids, _ = bench
        toolbox.lane.type  # noqa: B018 - readability
        out = await call(toolbox, "duckdb_query", artifacts=[str(ids["txns"].id)],
                         sql="SELECT * FROM range(10000) t(n)")
        assert "more rows exist" in out
        assert len(out) < 20_000


class TestInto:
    async def test_result_becomes_an_artifact_not_context(self, bench):
        toolbox, ids, _ = bench
        out = await call(
            toolbox, "duckdb_query", artifacts=[str(ids["txns"].id)],
            sql="SELECT account, sum(amount) AS total FROM txns_csv GROUP BY 1",
            into="totals",
        )
        assert "derived/totals" in out
        assert "3" in out  # three accounts
        assert "\n2024-01-01" not in out, "into must not return the rows"

    async def test_derived_artifact_is_recorded_on_the_toolbox(self, bench):
        toolbox, ids, _ = bench
        await call(toolbox, "duckdb_query", artifacts=[str(ids["txns"].id)],
                   sql="SELECT 1 AS n", into="tiny")
        assert toolbox.derived and "derived/tiny" in toolbox.derived[0]

    async def test_derived_artifact_is_queryable_by_the_same_lane(self, bench):
        """Authorisation comes from the lane's own namespace, not a new source."""
        toolbox, ids, _ = bench
        first = await call(
            toolbox, "duckdb_query", artifacts=[str(ids["txns"].id)],
            sql="SELECT account, sum(amount) AS total FROM txns_csv GROUP BY 1",
            into="totals",
        )
        derived = next(w for w in first.split() if "derived/totals" in w)
        out = await call(toolbox, "duckdb_query", artifacts=[derived],
                         sql="SELECT max(total) AS m FROM totals")
        assert out and not out.startswith("ERROR"), out

    async def test_derived_artifact_lands_in_the_lane_namespace(self, bench):
        toolbox, ids, store = bench
        await call(toolbox, "duckdb_query", artifacts=[str(ids["txns"].id)],
                   sql="SELECT 1 AS n", into="tiny")
        listed = await store.list(JOB, kind="blob")
        names = [a.id.name for a in listed]
        assert f"{toolbox.lane.namespace}/derived/tiny" in names

    async def test_into_is_attributed_to_the_lane(self, bench):
        toolbox, ids, store = bench
        await call(toolbox, "duckdb_query", artifacts=[str(ids["txns"].id)],
                   sql="SELECT 1 AS n", into="tiny")
        listed = await store.list(JOB, kind="blob")
        derived = next(a for a in listed if a.id.name.endswith("derived/tiny"))
        assert derived.produced_by == f"lane:{toolbox.lane.id}"

    async def test_illegal_into_name_is_refused(self, bench):
        toolbox, ids, _ = bench
        out = await call(toolbox, "duckdb_query", artifacts=[str(ids["txns"].id)],
                         sql="SELECT 1 AS n", into="../escape")
        assert out.startswith("ERROR") and "bad_into_name" in out

    async def test_into_records_the_schema_for_the_next_reader(self, bench):
        toolbox, ids, store = bench
        await call(toolbox, "duckdb_query", artifacts=[str(ids["txns"].id)],
                   sql="SELECT account, sum(amount) AS total FROM txns_csv GROUP BY 1",
                   into="totals")
        listed = await store.list(JOB, kind="blob")
        derived = next(a for a in listed if a.id.name.endswith("derived/totals"))
        assert derived.schema and derived.schema["columns"] == ["account", "total"]


class TestLocalizeLifetime:
    async def test_path_is_still_readable_after_the_call_returns(self, bench):
        """design.md D8: the old code returned from inside the context manager."""
        toolbox, ids, _ = bench
        out = await call(toolbox, "localize_blob", artifact=str(ids["txns"].id))
        path = Path(json.loads(out)["path"])
        assert path.read_text(encoding="utf-8").startswith("ts,account")

    async def test_toolbox_close_releases_the_localisation(self, bench):
        toolbox, ids, _ = bench
        await call(toolbox, "localize_blob", artifact=str(ids["txns"].id))
        await toolbox.aclose()
        await toolbox.aclose()  # idempotent


class TestLocalizeAgainstADeletingBackend:
    """The bug design.md D8 describes is invisible on the local backend.

    ``LocalArtifactStore.localize`` yields the permanent file and copies
    nothing, so returning a path from inside its ``async with`` happened to
    work. ``ArtifactStore.localize`` promises something else: object-store
    backends "download to scratch and clean up on exit". Under that contract
    the old code handed the worker a path to a file it had just deleted.

    This backend keeps that promise, so the test fails against the old code
    and passes against the new one -- which is the only way to have a
    regression test for a bug that has not shipped yet.
    """

    @pytest.fixture
    async def deleting(self, tmp_path: Path):
        from contextlib import asynccontextmanager

        store = LocalArtifactStore(tmp_path / "store")
        await store.init_job(JOB)
        blob = await store.put_blob(JOB, "raw/a.csv", data=b"x\n1\n",
                                    produced_by="user")
        real = store.localize
        scratch = tmp_path / "scratch"
        scratch.mkdir()

        @asynccontextmanager
        async def downloading(artifact_id, *, grants):
            async with real(artifact_id, grants=grants) as source:
                copy = scratch / f"{artifact_id.name.replace('/', '_')}"
                copy.write_bytes(source.read_bytes())
                try:
                    yield copy
                finally:
                    copy.unlink()          # the object-store contract

        store.localize = downloading  # type: ignore[method-assign]

        charter = tmp_path / "c.md"
        charter.write_text("charter", encoding="utf-8")
        lane = LaneRegistry(
            LaneType(name="ta", charter_path=charter, state_max_tokens=100,
                     tools=("localize_blob", "duckdb_query"))
        ).create("txn-2024", "ta")
        toolbox = WorkerToolbox(
            store=store, job_id=JOB, lane=lane,
            grants=GrantSet.for_lane(JOB, lane.namespace, [blob.id]),
            read_budget=1000,
        )
        toolbox.build_server()
        try:
            yield toolbox, blob
        finally:
            await toolbox.aclose()

    async def test_path_survives_the_call_that_produced_it(self, deleting):
        toolbox, blob = deleting
        out = await call(toolbox, "localize_blob", artifact=str(blob.id))
        path = Path(json.loads(out)["path"])
        assert path.exists(), "the worker was handed a deleted path"
        assert path.read_bytes() == b"x\n1\n"

    async def test_closing_the_toolbox_cleans_up(self, deleting):
        toolbox, blob = deleting
        out = await call(toolbox, "localize_blob", artifact=str(blob.id))
        path = Path(json.loads(out)["path"])
        await toolbox.aclose()
        assert not path.exists(), "scratch outlived the worker"

    async def test_query_still_works_when_localisation_is_a_download(self, deleting):
        toolbox, blob = deleting
        out = await call(toolbox, "duckdb_query", artifacts=[str(blob.id)],
                         sql="SELECT sum(x) AS s FROM a_csv")
        assert "1" in out and not out.startswith("ERROR"), out


class TestToolSchemas:
    """The SDK's shorthand marks every property required and types no items.

    Both defaults are wrong here, and neither fails loudly: a required `into`
    just makes the model invent a name on every read-only query, and untyped
    array items are how {"blob_path": ...} reached dispatch in the fourth
    golden run. Pinned because the shorthand is the tempting thing to write.
    """

    def test_into_is_optional(self):
        from myharness.lanes.tools import _QUERY_SCHEMA

        assert "into" in _QUERY_SCHEMA["properties"]
        assert "into" not in _QUERY_SCHEMA["required"]

    def test_artifacts_and_sql_are_required(self):
        from myharness.lanes.tools import _QUERY_SCHEMA

        assert set(_QUERY_SCHEMA["required"]) == {"artifacts", "sql"}

    def test_artifacts_items_are_typed_as_strings(self):
        from myharness.lanes.tools import _QUERY_SCHEMA

        assert _QUERY_SCHEMA["properties"]["artifacts"]["items"] == {"type": "string"}

    def test_read_note_section_is_optional(self):
        """It always was in the code; the schema said otherwise."""
        from myharness.lanes.tools import _READ_NOTE_SCHEMA

        assert _READ_NOTE_SCHEMA["required"] == ["artifact"]

    def test_the_sdk_passes_a_full_schema_through_untouched(self):
        """If it ever stops doing that, these schemas silently stop applying."""
        from claude_agent_sdk import create_sdk_mcp_server, tool

        from myharness.lanes.tools import _QUERY_SCHEMA

        @tool("probe", "d", _QUERY_SCHEMA)
        async def probe(args):  # pragma: no cover - never called
            return {"content": []}

        assert probe.input_schema is _QUERY_SCHEMA
        create_sdk_mcp_server(name="probe", version="1", tools=[probe])
