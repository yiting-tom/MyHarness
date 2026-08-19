"""The routing table: the proxy's entire view of the job.

Most of these are about refusing rather than dropping. A silently discarded
entry is a lane that never receives anything, with no message saying why --
the same shape of failure as the fourth golden run's mangled inputs, which
cost two lanes before anyone understood it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from myharness.artifacts.local import LocalArtifactStore
from myharness.orchestrator.routing import (
    MAX_ENTRIES,
    LaneStatus,
    RoutingEntry,
    RoutingError,
    RoutingTable,
    read_routing,
    write_routing,
)

JOB = "jr"


class TestParsing:
    def test_a_plain_table(self):
        table = RoutingTable.from_raw([
            {"lane": "txn-2024", "accepts": "2024 交易明細"},
            {"lane": "kyc", "accepts": "身分文件", "status": "closed"},
        ])
        assert [e.lane for e in table.entries] == ["txn-2024", "kyc"]
        assert table.entries[0].status is LaneStatus.OPEN
        assert table.entries[1].status is LaneStatus.CLOSED

    def test_status_defaults_to_open(self):
        table = RoutingTable.from_raw([{"lane": "a", "accepts": "x"}])
        assert table.entries[0].open

    def test_none_is_an_empty_table_not_an_error(self):
        assert RoutingTable.from_raw(None).entries == ()

    def test_a_json_string_is_accepted(self):
        """Models pass JSON as a string often enough to be worth handling."""
        raw = json.dumps([{"lane": "a", "accepts": "x"}])
        assert RoutingTable.from_raw(raw).entries[0].lane == "a"

    def test_invalid_json_string_is_refused(self):
        with pytest.raises(RoutingError, match="not valid JSON"):
            RoutingTable.from_raw("{not json")

    def test_a_non_list_is_refused(self):
        with pytest.raises(RoutingError, match="must be a list"):
            RoutingTable.from_raw({"lane": "a"})

    def test_a_non_object_entry_is_refused(self):
        with pytest.raises(RoutingError, match="must be objects"):
            RoutingTable.from_raw(["txn-2024"])

    def test_an_entry_without_a_lane_is_refused(self):
        with pytest.raises(RoutingError, match="no lane"):
            RoutingTable.from_raw([{"accepts": "x"}])

    def test_an_entry_without_accepts_is_refused_with_the_reason(self):
        """An entry with no description can never match anything."""
        with pytest.raises(RoutingError, match="nothing can be routed"):
            RoutingTable.from_raw([{"lane": "a"}])

    def test_an_unknown_status_is_refused_and_names_the_options(self):
        with pytest.raises(RoutingError, match="open or closed"):
            RoutingTable.from_raw([{"lane": "a", "accepts": "x", "status": "paused"}])

    def test_a_duplicate_lane_is_refused(self):
        with pytest.raises(RoutingError, match="appears twice"):
            RoutingTable.from_raw([
                {"lane": "a", "accepts": "x"}, {"lane": "a", "accepts": "y"},
            ])

    def test_an_over_long_table_is_refused(self):
        raw = [{"lane": f"l{i}", "accepts": "x"} for i in range(MAX_ENTRIES + 1)]
        with pytest.raises(RoutingError, match="not a routing table"):
            RoutingTable.from_raw(raw)

    def test_accepts_is_clipped_rather_than_refused(self):
        """Over-long prose is a nuisance, not an error -- it just costs the
        proxy context, so bound it and move on."""
        table = RoutingTable.from_raw([{"lane": "a", "accepts": "長" * 5000}])
        assert len(table.entries[0].accepts) <= 200

    def test_status_is_case_insensitive(self):
        table = RoutingTable.from_raw([{"lane": "a", "accepts": "x", "status": "CLOSED"}])
        assert not table.entries[0].open


class TestQuerying:
    def _table(self):
        return RoutingTable.from_raw([
            {"lane": "open-one", "accepts": "x"},
            {"lane": "shut", "accepts": "y", "status": "closed"},
        ])

    def test_only_open_lanes_accept(self):
        table = self._table()
        assert table.accepts_from("open-one")
        assert not table.accepts_from("shut")

    def test_an_unknown_lane_does_not_accept(self):
        assert not self._table().accepts_from("never-declared")

    def test_describe_shows_only_open_lanes(self):
        described = self._table().describe()
        assert "open-one" in described and "shut" not in described

    def test_describe_is_honest_when_nothing_is_open(self):
        table = RoutingTable.from_raw([{"lane": "a", "accepts": "x", "status": "closed"}])
        assert "no open lanes" in table.describe()

    def test_an_empty_table_is_falsey(self):
        assert not RoutingTable()
        assert RoutingTable.from_raw([{"lane": "a", "accepts": "x"}])


class TestStorage:
    async def test_round_trip(self, tmp_path: Path):
        store = LocalArtifactStore(tmp_path)
        await store.init_job(JOB)
        table = RoutingTable.from_raw([
            {"lane": "txn-2024", "accepts": "2024 交易明細"},
            {"lane": "kyc", "accepts": "身分文件", "status": "closed"},
        ])
        await write_routing(store, JOB, table)
        loaded = await read_routing(store, JOB)
        assert loaded.entries == table.entries

    async def test_absent_table_reads_as_empty(self, tmp_path: Path):
        store = LocalArtifactStore(tmp_path)
        await store.init_job(JOB)
        assert not await read_routing(store, JOB)

    async def test_a_corrupt_table_degrades_to_empty(self, tmp_path: Path):
        """It must not stop ingress: data lands whatever the table looks like."""
        store = LocalArtifactStore(tmp_path)
        await store.init_job(JOB)
        await store.put_note(JOB, "routing", "this is not json", produced_by="test")
        assert not await read_routing(store, JOB)

    async def test_a_structurally_wrong_table_degrades_to_empty(self, tmp_path: Path):
        store = LocalArtifactStore(tmp_path)
        await store.init_job(JOB)
        await store.put_note(JOB, "routing", '[{"lane": "a"}]', produced_by="test")
        assert not await read_routing(store, JOB)

    async def test_writing_again_replaces(self, tmp_path: Path):
        store = LocalArtifactStore(tmp_path)
        await store.init_job(JOB)
        await write_routing(store, JOB, RoutingTable.from_raw(
            [{"lane": "first", "accepts": "x"}]))
        await write_routing(store, JOB, RoutingTable.from_raw(
            [{"lane": "second", "accepts": "y"}]))
        loaded = await read_routing(store, JOB)
        assert [e.lane for e in loaded.entries] == ["second"]

    async def test_it_is_a_note_not_a_new_storage_path(self, tmp_path: Path):
        store = LocalArtifactStore(tmp_path)
        await store.init_job(JOB)
        await write_routing(store, JOB, RoutingTable.from_entries(
            [RoutingEntry("a", "x")]))
        notes = await store.list(JOB, kind="note")
        assert any(n.id.name == "routing" for n in notes)
