"""Anomaly detection, including against the run that motivated it."""

from __future__ import annotations

from myharness.dataflow import (
    AnomalyKind,
    Severity,
    build_dataflow,
    counts,
    critical,
    detect,
)

from .conftest import JOB, Stream, meta

BLOB = f"{JOB}/blob/raw/txns"
F1 = f"{JOB}/note/lanes/a/findings/1"
F2 = f"{JOB}/note/lanes/a/findings/2"
STATE = f"{JOB}/note/lanes/a/state"
REPORT = f"{JOB}/note/report"


def kinds(flow) -> set[str]:
    return {str(a.kind) for a in detect(flow)}


# --- Requirement: 偵測無授權卻有產出 --------------------------------------


def test_a_dispatch_granted_nothing_that_still_produced_is_flagged():
    """Scenario: 空授權的產出被標記

    It cannot have based that output on anything in the job.
    """
    stream = (Stream().ingress(BLOB)
              .dispatch("d1", "syn", []).done("d1", "syn", REPORT)
              .finish(REPORT))
    found = detect(build_dataflow(stream.events))
    (anomaly,) = [a for a in found if a.kind is AnomalyKind.UNGRANTED_PRODUCTION]
    assert anomaly.severity is Severity.CRITICAL
    assert anomaly.context["dispatch"] == "d1"


def test_a_granted_dispatch_is_not_flagged():
    """Scenario: 有授權的產出不被標記"""
    stream = (Stream().ingress(BLOB)
              .dispatch("d1", "a", [BLOB]).done("d1", "a", F1).finish(F1))
    assert "ungranted_production" not in kinds(build_dataflow(stream.events))


def test_writing_only_lane_state_without_grants_is_not_an_anomaly():
    """A lane recording its own memory needs no input."""
    stream = Stream().dispatch("d1", "a", []).done("d1", "a", STATE)
    flow = build_dataflow(stream.events, [meta(STATE)])
    assert "ungranted_production" not in kinds(flow)


def test_a_dispatch_that_produced_nothing_is_not_flagged():
    stream = Stream().dispatch("d1", "a", []).done("d1", "a", None, status="tool_failure")
    assert "ungranted_production" not in kinds(build_dataflow(stream.events))


# --- Requirement: 偵測產出被覆蓋 ------------------------------------------


def test_overwriting_names_the_version_that_survived():
    """Scenario: 覆蓋被標記且指出勝出者"""
    stream = (Stream().ingress(BLOB)
              .dispatch("d1", "syn", [BLOB]).done("d1", "syn", REPORT)
              .dispatch("d2", "syn", [BLOB]).done("d2", "syn", REPORT)
              .finish(REPORT))
    found = detect(build_dataflow(stream.events))
    (anomaly,) = [a for a in found if a.kind is AnomalyKind.OVERWRITTEN_OUTPUT]
    assert anomaly.context["winner"] == "d2"
    assert anomaly.context["writers"] == ["d1", "d2"]
    assert anomaly.severity is Severity.CRITICAL, "the delivery itself was replaced"


def test_distinct_artifacts_are_not_an_overwrite():
    """Scenario: 各自寫入不同 artifact 不算覆蓋"""
    stream = (Stream().ingress(BLOB)
              .dispatch("d1", "a", [BLOB]).done("d1", "a", F1)
              .dispatch("d2", "a", [BLOB]).done("d2", "a", F2))
    assert "overwritten_output" not in kinds(build_dataflow(stream.events))


def test_overwriting_a_mere_finding_is_only_a_warning():
    stream = (Stream().ingress(BLOB)
              .dispatch("d1", "a", [BLOB]).done("d1", "a", F1)
              .dispatch("d2", "a", [BLOB]).done("d2", "a", F1)
              .dispatch("d3", "syn", [F1]).done("d3", "syn", REPORT).finish(REPORT))
    found = detect(build_dataflow(stream.events))
    (anomaly,) = [a for a in found if a.kind is AnomalyKind.OVERWRITTEN_OUTPUT]
    assert anomaly.severity is Severity.WARNING


# --- Requirement: 偵測未被使用的資料與產出 --------------------------------


def test_raw_data_nobody_was_granted_is_flagged():
    """Scenario: 未被授權的原始資料"""
    stream = (Stream().ingress(BLOB)
              .dispatch("d1", "a", []).done("d1", "a", F1).finish(F1))
    found = detect(build_dataflow(stream.events))
    (anomaly,) = [a for a in found if a.kind is AnomalyKind.UNUSED_INPUT]
    assert anomaly.context["artifact"] == BLOB
    assert anomaly.severity is Severity.WARNING


def test_an_analysis_nobody_read_is_an_orphan():
    """Scenario: 無人讀取的分析產出"""
    stream = (Stream().ingress(BLOB)
              .dispatch("d1", "a", [BLOB]).done("d1", "a", F1)
              .dispatch("d2", "a", [BLOB]).done("d2", "a", F2)
              .dispatch("d3", "syn", [F1]).done("d3", "syn", REPORT)
              .finish(REPORT))
    found = detect(build_dataflow(stream.events))
    orphans = [a for a in found if a.kind is AnomalyKind.ORPHAN_OUTPUT]
    assert [a.context["artifact"] for a in orphans] == [F2]


def test_the_report_is_never_an_orphan():
    """Scenario: 最終報告不算孤兒"""
    stream = (Stream().ingress(BLOB)
              .dispatch("d1", "a", [BLOB]).done("d1", "a", REPORT).finish(REPORT))
    assert "orphan_output" not in kinds(build_dataflow(stream.events))


def test_lane_state_is_never_an_orphan():
    """It exists for the lane's own next run; having no reader is normal."""
    stream = (Stream().ingress(BLOB)
              .dispatch("d1", "a", [BLOB]).done("d1", "a", STATE)
              .dispatch("d2", "syn", [BLOB]).done("d2", "syn", REPORT).finish(REPORT))
    flow = build_dataflow(stream.events, [meta(STATE)])
    assert "orphan_output" not in kinds(flow)


def test_transcripts_are_not_unused_inputs():
    """Transcripts are blobs, but they are bookkeeping rather than job input."""
    trace = f"{JOB}/blob/traces/d1"
    stream = (Stream().ingress(BLOB)
              .dispatch("d1", "a", [BLOB]).done("d1", "a", F1).finish(F1))
    flow = build_dataflow(stream.events, [meta(BLOB), meta(trace)])
    unused = [a.context["artifact"] for a in detect(flow)
              if a.kind is AnomalyKind.UNUSED_INPUT]
    assert trace not in unused


# --- ordering and a clean run ---------------------------------------------


def test_critical_anomalies_come_first():
    stream = (Stream().ingress(BLOB)
              .dispatch("d1", "syn", []).done("d1", "syn", REPORT).finish(REPORT))
    found = detect(build_dataflow(stream.events))
    assert found[0].severity is Severity.CRITICAL
    assert critical(found)


def test_a_healthy_job_reports_nothing():
    stream = (Stream().start().ingress(BLOB)
              .dispatch("d1", "a", [BLOB]).done("d1", "a", F1)
              .dispatch("d2", "syn", [F1]).done("d2", "syn", REPORT)
              .finish(REPORT))
    assert detect(build_dataflow(stream.events)) == []


# --- regression against the run that motivated all of this ----------------


def test_golden5_is_caught(golden5):
    """The fifth golden run passed every discipline check and shipped a report
    written by a dispatch that had been granted nothing, overwriting the one
    that had the findings. A human found that by luck; this is the defence."""
    events, artifacts = golden5
    flow = build_dataflow(events, artifacts)
    found = detect(flow)

    tally = counts(found)
    assert tally.get("ungranted_production") == 1
    assert tally.get("overwritten_output") == 1
    assert len(critical(found)) == 2

    ungranted = next(a for a in found if a.kind is AnomalyKind.UNGRANTED_PRODUCTION)
    assert ungranted.context["dispatch"] == "d5"
    assert ungranted.context["lane"] == "syn1"

    overwrite = next(a for a in found if a.kind is AnomalyKind.OVERWRITTEN_OUTPUT)
    assert overwrite.context["writers"] == ["d4", "d5"]
    assert overwrite.context["winner"] == "d5", "the delivered report came from d5"


def test_golden5_provenance_is_reconstructible(golden5):
    events, artifacts = golden5
    flow = build_dataflow(events, artifacts)
    assert flow.report_artifact
    chain = [d.id for d in flow.provenance(flow.report_artifact)]
    assert {"d1", "d2", "d3", "d4", "d5"} == set(chain)
    assert flow.provenance_cost(flow.report_artifact)["usd"] > 0
