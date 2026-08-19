"""The handle contract: shape by schema, size by code.

These tests are the offline half of the change's central claim. The live half
(tests/lanes/test_live_lane_worker.py) proves a real model cannot get past them.
"""

from __future__ import annotations

import json

import pytest

from myharness.lanes.contract import (
    MAX_SCHEMA_RETRIES,
    extract_json_object,
    failure_handle,
    reprompt_text,
    validate_payload,
)
from myharness.lanes.handle import (
    HANDLE_SCHEMA,
    MAX_HANDLE_CHARS,
    MAX_HEADLINE_CHARS,
    MAX_METRICS,
    MAX_FOLLOWUPS,
    DEGRADED_STATUSES,
    HandleStatus,
    LaneHandle,
    clamp_handle,
)

GOOD = {
    "artifact": "lanes/txn-2024/findings/003",
    "headline": "3 類異常交易，最大宗為深夜小額高頻",
    "confidence": "high",
    "metrics": {"anomaly_rate": 0.023, "n": 30412},
    "followups": ["需要 KYC 資料交叉比對"],
}


# --- schema: shape --------------------------------------------------------


def test_valid_payload_becomes_a_handle():
    outcome = validate_payload(GOOD)
    assert outcome.ok
    assert outcome.handle.artifact == GOOD["artifact"]
    assert outcome.handle.status is HandleStatus.OK


@pytest.mark.parametrize(
    ("mutation", "expected_fragment"),
    [
        ({"artifact": None}, "artifact"),
        ({"confidence": "sky-high"}, "confidence"),
        ({"metrics": {"n": "lots"}}, "metrics"),
        ({"followups": "not a list"}, "followups"),
    ],
)
def test_malformed_payloads_are_rejected(mutation: dict, expected_fragment: str):
    payload = {**GOOD, **mutation}
    outcome = validate_payload(payload)
    assert not outcome.ok
    assert any(expected_fragment in p for p in outcome.problems)


def test_missing_required_field_is_rejected():
    payload = {k: v for k, v in GOOD.items() if k != "headline"}
    outcome = validate_payload(payload)
    assert not outcome.ok
    assert any("headline" in p for p in outcome.problems)


def test_extra_fields_are_rejected():
    """A worker must not smuggle a report out through an unexpected key."""
    outcome = validate_payload({**GOOD, "full_report": "…" * 5000})
    assert not outcome.ok
    assert any("Additional properties" in p for p in outcome.problems)


def test_schema_forbids_free_form_metric_values():
    assert HANDLE_SCHEMA["properties"]["metrics"]["additionalProperties"] == {"type": "number"}
    assert HANDLE_SCHEMA["additionalProperties"] is False


# --- clamping: size -------------------------------------------------------


def test_normal_handle_is_left_alone():
    handle = clamp_handle(validate_payload(GOOD).handle)
    assert not handle.truncated
    assert handle.headline == GOOD["headline"]


def test_overlong_headline_is_truncated_not_passed_through():
    handle = clamp_handle(
        LaneHandle(artifact="a", headline="報" * 3000, confidence="high")
    )
    assert handle.truncated
    assert len(handle.headline) <= MAX_HEADLINE_CHARS


def test_hostile_handle_is_forced_under_the_ceiling():
    """Schema-valid but enormous: many long metric keys and followups.

    A schema cannot stop this; only the whole-object ceiling can.
    """
    hostile = LaneHandle(
        artifact="lanes/a/findings/1",
        headline="報告" * 2000,
        confidence="high",
        metrics={f"key{i}" * 40: float(i) for i in range(60)},
        followups=tuple("後續建議" * 150 for _ in range(30)),
    )
    handle = clamp_handle(hostile)
    assert handle.truncated
    assert len(handle.to_json()) <= MAX_HANDLE_CHARS
    assert len(handle.metrics) <= MAX_METRICS
    assert len(handle.followups) <= MAX_FOLLOWUPS


def test_required_fields_survive_the_ceiling():
    handle = clamp_handle(
        LaneHandle(artifact="lanes/a/f/1", headline="x" * 5000, confidence="low")
    )
    assert handle.artifact == "lanes/a/f/1"
    assert handle.confidence == "low"
    assert handle.headline
    assert len(handle.to_json()) <= MAX_HANDLE_CHARS


def test_clamping_is_idempotent():
    once = clamp_handle(LaneHandle(artifact="a", headline="報" * 900, confidence="high"))
    assert clamp_handle(once) == once


def test_non_numeric_metric_is_dropped_and_marked():
    handle = clamp_handle(
        LaneHandle(artifact="a", headline="h", confidence="high",
                   metrics={"n": "not a number"})  # type: ignore[dict-item]
    )
    assert handle.truncated
    assert "n" not in handle.metrics


# --- JSON recovery for the degraded path ---------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 2}\n```', {"a": 2}),
        ('Sure! {"a": 3} hope that helps', {"a": 3}),
        ("no object at all", None),
        ("", None),
        ("[1,2,3]", None),
    ],
)
def test_json_extraction_from_free_form_output(text: str, expected):
    assert extract_json_object(text) == expected


def test_reprompt_names_the_problems_and_the_schema():
    problems = validate_payload({"headline": "x"}).problems
    text = reprompt_text(problems)
    assert "artifact" in text
    assert json.dumps(HANDLE_SCHEMA, ensure_ascii=False) in text
    assert MAX_SCHEMA_RETRIES >= 1


# --- failure handles ------------------------------------------------------


def test_failure_handle_carries_partial_and_suggestion():
    handle = failure_handle(
        HandleStatus.BUDGET_EXCEEDED,
        headline="完成 2024 Q1–Q3 掃描，Q4 未處理",
        lane="txn-2024",
        partial="lanes/txn-2024/findings/003-partial",
        suggest="縮小範圍重派，或接受部分結果",
    )
    assert not handle.ok
    assert handle.status is HandleStatus.BUDGET_EXCEEDED
    assert handle.partial and handle.suggest
    assert len(handle.to_json()) <= MAX_HANDLE_CHARS


def test_every_non_ok_status_counts_as_degraded():
    assert HandleStatus.OK not in DEGRADED_STATUSES
    for status in HandleStatus:
        if status is not HandleStatus.OK:
            assert status in DEGRADED_STATUSES


def test_failure_handle_is_serialisable_without_an_artifact():
    handle = failure_handle(HandleStatus.BACKEND_UNAVAILABLE, headline="後端不可用")
    assert json.loads(handle.to_json())["status"] == "backend_unavailable"
