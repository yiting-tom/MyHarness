"""Building the flow: nodes, edges, provenance, and what must not be inferred."""

from __future__ import annotations

import pytest

from myharness.dataflow import (
    EdgeKind,
    NodeKind,
    build_dataflow,
    classify,
    short_label,
)

from .conftest import JOB, Stream, meta

BLOB = f"{JOB}/blob/raw/txns"
F1 = f"{JOB}/note/lanes/a/findings/1"
F2 = f"{JOB}/note/lanes/a/findings/2"
REPORT = f"{JOB}/note/report"


# --- Requirement: 資料流由事件流推導，不另行記錄 --------------------------


def test_events_alone_are_enough():
    """Scenario: 僅需事件流即可建構"""
    stream = (Stream().start().ingress(BLOB)
              .dispatch("d1", "a", [BLOB]).done("d1", "a", F1)
              .finish(F1))
    flow = build_dataflow(stream.events)
    assert flow.job_id == JOB
    assert flow.dispatches["d1"].produced == (F1,)


def test_building_writes_nothing():
    """Scenario: 推導過程不寫入"""
    stream = Stream().start().dispatch("d1", "a").done("d1", "a", F1)
    before = list(stream.events)
    build_dataflow(stream.events)
    assert stream.events == before


# --- Requirement: 節點與邊涵蓋完整的流向 ----------------------------------


def test_one_dispatch_has_three_kinds_of_edge():
    """Scenario: 一次派工的三種邊"""
    stream = (Stream().ingress(BLOB).ingress(F2)
              .dispatch("d1", "a", [BLOB, F2])
              .read("d1", BLOB)
              .done("d1", "a", F1))
    flow = build_dataflow(stream.events)

    assert len(flow.edges_to("d1", EdgeKind.GRANTED)) == 2
    assert len(flow.edges_from("d1", EdgeKind.READ)) == 1
    assert len(flow.edges_from("d1", EdgeKind.PRODUCED)) == 1
    assert flow.read_edges_available


def test_granted_is_never_taken_for_read():
    """design.md D1: authorisation must not stand in for reading.

    Without artifact.read events the read view is unavailable, and saying so is
    the point — the two carry information only when they disagree.
    """
    stream = Stream().dispatch("d1", "a", [BLOB]).done("d1", "a", F1)
    flow = build_dataflow(stream.events)
    assert not flow.read_edges_available
    assert flow.edges_from("d1", EdgeKind.READ) == []
    assert len(flow.edges_to("d1", EdgeKind.GRANTED)) == 1


def test_provenance_walks_back_to_the_raw_data():
    """Scenario: 來源鏈可回溯"""
    stream = (Stream().ingress(BLOB)
              .dispatch("d1", "a", [BLOB]).done("d1", "a", F1)
              .dispatch("d2", "syn", [F1]).done("d2", "syn", REPORT)
              .finish(REPORT))
    flow = build_dataflow(stream.events)
    assert [d.id for d in flow.provenance(REPORT)] == ["d1", "d2"]


def test_ungranted_data_has_a_node_but_no_grant_edge():
    """Scenario: 未被授權的資料不出現授權邊"""
    stream = Stream().ingress(BLOB).dispatch("d1", "a").done("d1", "a", F1)
    flow = build_dataflow(stream.events)
    assert BLOB in flow.nodes
    assert flow.edges_from(BLOB, EdgeKind.GRANTED) == []


def test_lane_nodes_link_their_dispatches():
    stream = (Stream().dispatch("d1", "a").done("d1", "a", F1)
              .dispatch("d2", "a").done("d2", "a", F2))
    flow = build_dataflow(stream.events)
    assert len(flow.edges_to("lane:a", EdgeKind.RAN_ON)) == 2


# --- Requirement: 成本與 token 可歸屬到資料流 -----------------------------


def test_provenance_cost_sums_the_chain():
    """Scenario: 報告的累計成本"""
    stream = (Stream().ingress(BLOB)
              .dispatch("d1", "a", [BLOB]).done("d1", "a", F1, usd=0.30)
              .dispatch("d2", "syn", [F1]).done("d2", "syn", REPORT, usd=0.12)
              .finish(REPORT))
    cost = build_dataflow(stream.events).provenance_cost(REPORT)
    assert cost["usd"] == pytest.approx(0.42)
    assert cost["dispatches"] == ["d1", "d2"]


def test_unrelated_work_is_not_charged_to_the_report():
    """Scenario: 未貢獻的執行不計入"""
    stream = (Stream().ingress(BLOB)
              .dispatch("d1", "a", [BLOB]).done("d1", "a", F1, usd=0.30)
              .dispatch("d2", "b", [BLOB]).done("d2", "b", F2, usd=0.99)
              .dispatch("d3", "syn", [F1]).done("d3", "syn", REPORT, usd=0.10)
              .finish(REPORT))
    cost = build_dataflow(stream.events).provenance_cost(REPORT)
    assert "d2" not in cost["dispatches"]
    assert cost["usd"] == pytest.approx(0.40)


def test_cost_and_tokens_group_by_lane():
    stream = (Stream().dispatch("d1", "a").done("d1", "a", F1, usd=0.3)
              .dispatch("d2", "a").done("d2", "a", F2, usd=0.2)
              .dispatch("d3", "syn").done("d3", "syn", REPORT, usd=0.1))
    flow = build_dataflow(stream.events)
    assert flow.cost_by_lane() == {"a": pytest.approx(0.5), "syn": pytest.approx(0.1)}
    assert flow.tokens_by_lane()["a"]["in"] == 2000


# --- Requirement: 部分或損壞的事件流仍可推導 ------------------------------


def test_a_running_job_builds_fine():
    """Scenario: 執行中的 job"""
    stream = Stream().start().dispatch("d1", "a", [BLOB]).dispatch("d2", "b")
    flow = build_dataflow(stream.events)
    assert not flow.finished
    assert {d.id for d in flow.running()} == {"d1", "d2"}


def test_unknown_event_types_are_ignored():
    """Scenario: 未知事件型別"""
    stream = Stream().start().unknown().dispatch("d1", "a").unknown().done("d1", "a", F1)
    flow = build_dataflow(stream.events)
    assert flow.dispatches["d1"].ok


def test_an_end_without_a_start_is_still_shown():
    flow = build_dataflow(Stream().done("d9", "a", F1).events)
    assert flow.dispatches["d9"].lane == "a"


def test_an_empty_stream_is_an_empty_flow():
    flow = build_dataflow([])
    assert flow.nodes == {} and flow.dispatches == {}


# --- classification -------------------------------------------------------


@pytest.mark.parametrize(
    ("artifact_id", "kind"),
    [
        (f"{JOB}/blob/raw/txns", NodeKind.BLOB),
        (f"{JOB}/note/lanes/a/findings/1", NodeKind.FINDING),
        (f"{JOB}/note/lanes/a/state", NodeKind.STATE),
        (f"{JOB}/note/plan", NodeKind.PLAN),
        (f"{JOB}/note/report", NodeKind.REPORT),
        (f"{JOB}/note/lanes/syn/findings/report", NodeKind.REPORT),
    ],
)
def test_artifact_classification(artifact_id: str, kind: NodeKind):
    assert classify(artifact_id) == kind


def test_labels_keep_the_lane_visible():
    assert short_label(f"{JOB}/note/lanes/txn-2024/findings/003") == "txn-2024:003"
    assert short_label(f"{JOB}/blob/raw/txns") == "raw/txns"


def test_index_metadata_enriches_the_nodes():
    stream = Stream().dispatch("d1", "a").done("d1", "a", F1)
    flow = build_dataflow(stream.events, [meta(F1, est_tokens=453, revision=2)])
    assert flow.nodes[F1].est_tokens == 453
    assert flow.nodes[F1].revision == 2
