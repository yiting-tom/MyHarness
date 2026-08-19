"""The golden job: one real run, with the bounds asserted.

Planning quality is not assertable — model output varies and "was this a good
plan" has no test. Discipline is: how much context was used, whether the
orchestrator repeated itself, what it spent, and whether anything came out.
Those are the claims this whole architecture makes, so those are what get
checked (design.md D7).

    pytest -m live tests/golden
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from myharness.goldens import GOLDEN_CSV, GoldenResult, run_golden

pytestmark = pytest.mark.live

BACKEND = os.environ.get("HARNESS_LIVE_BACKEND", "openrouter")

#: Bounds, not targets. Generous enough that model variance does not flap them,
#: tight enough that a regression in context discipline trips one.
MAX_CONTEXT_PEAK = 120_000
MAX_COST_USD = 1.0
MAX_DELIVERY_CHARS = 4_000


@pytest.fixture(scope="module")
def golden(tmp_path_factory) -> GoldenResult:
    """One live run shared by every assertion — it costs real money."""
    import asyncio

    from myharness.backends.profile import registry

    profile = registry.get(BACKEND)
    if profile.auth_token_env and not os.environ.get(profile.auth_token_env):
        pytest.skip(f"{profile.auth_token_env} not set")
    if not GOLDEN_CSV.exists():
        pytest.skip(f"{GOLDEN_CSV} missing")

    root = tmp_path_factory.mktemp("golden")
    return asyncio.run(run_golden(root, backend=BACKEND))


def test_the_job_delivers_something(golden: GoldenResult):
    """Scenario: 善終後仍有交付 -- the guarantee that matters most."""
    assert golden.delivery.report_artifact
    assert golden.delivery.executive_summary.strip()
    assert golden.outcome.phase


def test_context_peak_stays_under_the_bound(golden: GoldenResult):
    """The claim this layer exists to make."""
    assert 0 < golden.outcome.context_peak < MAX_CONTEXT_PEAK, golden.report_line()


def test_peek_stayed_within_its_budget(golden: GoldenResult):
    assert golden.summary.peek_tokens <= 8_000, golden.report_line()


def test_no_duplicate_dispatches(golden: GoldenResult):
    assert golden.summary.duplicates == 0, golden.report_line()


def test_cost_stays_under_the_bound(golden: GoldenResult):
    assert golden.summary.total_usd < MAX_COST_USD, golden.report_line()


def test_delivery_is_small_enough_to_hand_to_an_agent(golden: GoldenResult):
    encoded = json.dumps(golden.delivery.to_dict(), ensure_ascii=False)
    assert len(encoded) < MAX_DELIVERY_CHARS, f"{len(encoded)} chars"


def test_the_report_was_written_by_a_lane_not_the_orchestrator(golden: GoldenResult):
    """Scenario: 報告由 synthesis lane 產出

    A salvaged report is written by the harness and is a legitimate outcome, but
    it must never be attributed to the orchestrator's own reading.
    """
    produced_by = golden.delivery.metadata.get("produced_by", "")
    assert produced_by, golden.report_line()
    assert produced_by.startswith(("lane:", "harness:salvage")), produced_by
    assert "orchestrator" not in produced_by


def test_every_shortfall_is_declared(golden: GoldenResult):
    """Whatever went wrong must reach the caller, not stay in the log."""
    stream_kinds = {c.kind for c in golden.summary.caveats}
    delivery_kinds = {c.kind for c in golden.delivery.caveats}
    assert stream_kinds == delivery_kinds


def test_the_raw_blob_never_became_a_note(golden: GoldenResult):
    """The invariant, checked against a real run rather than a fixture."""
    assert golden.blob_id.split("/")[1] == "blob"
    assert not golden.delivery.report_artifact.startswith(golden.blob_id)


# --- data-flow health (added after the fifth run shipped a hollow report) ---


def test_no_output_was_produced_without_a_grant(golden: GoldenResult):
    """The fifth run passed every other assertion here and still delivered a
    report from a dispatch that had been granted nothing."""
    from myharness.dataflow import AnomalyKind

    offenders = [a for a in golden.anomalies
                 if a.kind is AnomalyKind.UNGRANTED_PRODUCTION]
    assert not offenders, "\n".join(a.detail for a in offenders)


def test_the_report_was_not_overwritten(golden: GoldenResult):
    """Two dispatches writing the same report means the delivery may not be the
    one that had the findings."""
    from myharness.dataflow import AnomalyKind

    clobbered = [a for a in golden.anomalies
                 if a.kind is AnomalyKind.OVERWRITTEN_OUTPUT
                 and a.context.get("artifact") == golden.delivery.report_artifact]
    assert not clobbered, "\n".join(a.detail for a in clobbered)


def test_the_report_traces_back_to_the_raw_data(golden: GoldenResult):
    """A report with no provenance was not based on the job's input."""
    chain = golden.flow.provenance(golden.delivery.report_artifact)
    assert chain, "the report has no producing dispatch"
    granted = {a for d in chain for a in d.granted}
    assert golden.blob_id in granted, "nothing in the chain ever saw the raw data"


def test_no_critical_data_flow_anomalies(golden: GoldenResult):
    assert not golden.critical, golden.report_line()


# --- did an analysis actually happen? (added after the fifth run) ------------


def test_the_report_contains_a_number_only_computation_could_produce(
    golden: GoldenResult,
):
    """Every assertion above passed on the fifth run, which delivered a report
    saying it could not read the data. Discipline was all they checked.

    765 distinct accounts is not a number a model guesses, and it is wrong for
    any other reading of the file -- so its presence means a query ran, and its
    absence means the harness is back where it was.
    """
    from myharness.goldens import ground_truth

    truth = ground_truth(GOLDEN_CSV)
    missing = truth.missing_from(golden.report_text)
    assert not missing, (
        f"report is missing {missing}; the analysis did not happen\n"
        f"{golden.report_line()}\n---\n{golden.report_text[:1500]}"
    )


def test_the_report_does_not_plead_lack_of_tooling(golden: GoldenResult):
    """The exact sentence the fifth run shipped."""
    excuses = ["未能讀取", "無法讀取", "權限限制", "沒有工具", "無法存取資料"]
    found = [e for e in excuses if e in golden.report_text]
    assert not found, f"{found} in the report:\n{golden.report_text[:1500]}"
