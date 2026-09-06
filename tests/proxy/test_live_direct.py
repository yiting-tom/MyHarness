"""The direct path against a real endpoint.

Spike #12 measured the SDK path at 8,991 input tokens for a classification
whose own prompt is ~600 -- 93% of the request written by the Claude Code CLI,
which a single-shot tool-free classifier uses none of. This is the same
classification with the framework removed, run for real.

Two claims, and the second is the one that could regress silently:

  1. the input token count collapses to roughly our own prompt, and
  2. it still routes to the same lane.

A cheaper request that classifies worse is not an improvement, and the offline
suite cannot tell -- it feeds the classifier a scripted answer.

    HARNESS_DIRECT_BASE_URL=http://<host>:<port> \
    HARNESS_DIRECT_MODEL=<model> \
    pytest -m live tests/proxy/test_live_direct.py -s
"""

from __future__ import annotations

import os

import pytest

from myharness.backends.profile import ModelTier, direct_openai_from_env
from myharness.orchestrator.routing import RoutingTable
from myharness.proxy.classify import SYSTEM_PROMPT, build_prompt, classify
from myharness.proxy.sample import Sample

pytestmark = pytest.mark.live

#: Measured on the SDK path, spikes/RESULTS.md §Spike #12.
SDK_INPUT_TOKENS = 8_991
#: The classifier's own worst-case prompt is ~620 tokens. Anything much above
#: this means a framework preamble crept back in.
MAX_DIRECT_INPUT_TOKENS = 1_500

TXN_ROWS = ["txn_id,ts,account,amount,channel"] + [
    f"T{i:03d},2024-0{i % 9 + 1}-1{i % 9},ACCT{i % 7:04d},{300 * (i % 7 + 1)}.{i % 100:02d},"
    f"{'atm' if i % 3 == 0 else 'app' if i % 3 == 1 else 'web'}"
    for i in range(11)
]
KYC_ROWS = ["holder_id,full_name,doc_type,doc_no,issued,verified"] + [
    f"H{i:03d},Holder {i},passport,P{i:07d},2021-0{i % 9 + 1}-01,{'yes' if i % 2 else 'no'}"
    for i in range(11)
]


@pytest.fixture(scope="module")
def profile():
    built = direct_openai_from_env()
    if built is None:
        pytest.skip("set HARNESS_DIRECT_BASE_URL and HARNESS_DIRECT_MODEL")
    return built


def table() -> RoutingTable:
    return RoutingTable.from_raw([
        {"lane": "txn-2024", "accepts": "2024 年交易明細、金流紀錄"},
        {"lane": "kyc-docs", "accepts": "身分與 KYC 文件"},
        {"lane": "legacy", "accepts": "2023 前系統 log", "status": "closed"},
    ])


def sample(rows) -> Sample:
    return Sample("\n".join(rows), len(rows), True, False)


async def test_two_payloads_reach_two_lanes(profile, capsys):
    cases = [("txn.csv", TXN_ROWS, "txn-2024"), ("kyc.csv", KYC_ROWS, "kyc-docs")]
    results = []
    for name, rows, expected in cases:
        routing = await classify(
            table(), f"{name} · text/csv", sample(rows), profile=profile
        )
        results.append((name, routing, expected))

    with capsys.disabled():
        own = len(SYSTEM_PROMPT) + len(build_prompt(table(), "x", sample(TXN_ROWS)))
        print(f"\n  endpoint : {profile.base_url}")
        print(f"  model    : {profile.resolve_model(ModelTier.CHEAP)}")
        print(f"  own prompt: {own} chars")
        for name, r, expected in results:
            print(f"  {name:9} -> {str(r.lane):9} {r.confidence:7} "
                  f"in={r.tokens_in:<6} out={r.tokens_out:<4} {r.reason[:48]}")
        worst = max(r.tokens_in for _, r, _ in results)
        print(f"  SDK path : {SDK_INPUT_TOKENS:,} input tokens (spike #12)")
        print(f"  direct   : {worst:,} input tokens "
              f"({100 * (SDK_INPUT_TOKENS - worst) / SDK_INPUT_TOKENS:.1f}% less)")

    for name, routing, expected in results:
        assert routing.lane == expected, f"{name}: {routing.reason}"
        assert routing.tokens_in > 0, "the endpoint reported no usage"
        assert routing.tokens_in < MAX_DIRECT_INPUT_TOKENS, (
            f"{name}: {routing.tokens_in} input tokens -- a framework preamble "
            "is back, or the sample bound is not holding"
        )
        assert routing.model == profile.resolve_model(ModelTier.CHEAP)


async def test_a_payload_matching_nothing_is_no_match_not_a_guess(profile):
    """The cheap model must be willing to say it cannot tell."""
    noise = ["=== kernel ring buffer ==="] + [
        f"[{i:>6}.000000] usb {i}-1: new high-speed USB device" for i in range(10)
    ]
    routing = await classify(
        RoutingTable.from_raw([{"lane": "kyc-docs", "accepts": "身分與 KYC 文件"}]),
        "dmesg.log · text/plain", sample(noise), profile=profile,
    )
    assert routing.lane is None, f"guessed {routing.lane!r}: {routing.reason}"
