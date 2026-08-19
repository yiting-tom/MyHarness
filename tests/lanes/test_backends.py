"""Backend profiles: routing, credentials, aliases, and tool trimming."""

from __future__ import annotations

from dataclasses import replace

import pytest

from myharness.backends.profile import (
    ANTHROPIC_DIRECT,
    BUILTIN_TOOLS,
    OPENROUTER,
    SELF_HOSTED,
    BackendCapability,
    BackendError,
    BackendProfile,
    MissingCredential,
    ModelTier,
    UnknownModelAlias,
    registry,
)
from myharness.lanes.handle import HandleStatus
from myharness.lanes.transport import ScriptedTransport
from myharness.lanes.worker import WorkerRequest, run_lane_worker

from .conftest import JOB, GOOD_HANDLE, assistant, result, with_backend


# --- Requirement: Per-lane 的後端設定 ------------------------------------


def test_two_lanes_target_different_endpoints(monkeypatch):
    """Scenario: 兩條 lane 使用不同後端"""
    monkeypatch.setenv("OPENROUTER_KEY", "sk-or-test")
    direct = ANTHROPIC_DIRECT.to_sdk_env()
    via_or = OPENROUTER.to_sdk_env()
    assert "ANTHROPIC_BASE_URL" not in direct
    assert via_or["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"
    assert via_or["ANTHROPIC_AUTH_TOKEN"] == "sk-or-test"


def test_profiles_reference_keys_by_env_var_not_value():
    """Scenario: 金鑰不寫在設定中"""
    for profile in (ANTHROPIC_DIRECT, OPENROUTER, SELF_HOSTED):
        for value in (profile.auth_token_env, *profile.extra_env.values()):
            assert not (value or "").startswith("sk-"), f"{profile.name} embeds a key"


def test_missing_credential_fails_before_any_request(monkeypatch):
    """Scenario: 缺少金鑰時明確失敗"""
    monkeypatch.delenv("OPENROUTER_KEY", raising=False)
    with pytest.raises(MissingCredential) as exc:
        OPENROUTER.to_sdk_env()
    assert exc.value.env_var == "OPENROUTER_KEY"
    assert "OPENROUTER_KEY" in str(exc.value)


def test_unknown_backend_lists_the_registered_ones():
    with pytest.raises(BackendError) as exc:
        registry.get("nope")
    assert "openrouter" in str(exc.value)


# --- Requirement: 模型別名映射 --------------------------------------------


def test_same_tier_resolves_differently_per_backend():
    """Scenario: 同一別名在不同後端解析為不同模型"""
    assert ANTHROPIC_DIRECT.resolve_model(ModelTier.STRONG) == "opus"
    assert OPENROUTER.resolve_model(ModelTier.STRONG).startswith("nvidia/")


def test_unmapped_alias_lists_what_is_available():
    """Scenario: 未映射的別名明確失敗"""
    with pytest.raises(UnknownModelAlias) as exc:
        OPENROUTER.resolve_model("gigantic")
    assert "cheap" in str(exc.value) and "strong" in str(exc.value)


# --- Requirement: 內建工具的裁切 ------------------------------------------


def test_undeclared_builtins_are_stripped():
    """Scenario: 未宣告的工具不出現在請求中"""
    disallowed = BackendProfile.disallowed_for(["Glob"])
    assert "Glob" not in disallowed
    assert "Bash" in disallowed and "Read" in disallowed
    assert len(disallowed) == len(BUILTIN_TOOLS) - 1


def test_trimming_removes_nearly_all_builtins():
    """Scenario: 裁切後的固定成本顯著低於預設

    Measured cost of the untrimmed set is ~18.9k tokens per request
    (spikes/RESULTS.md §Spike #2b); every ephemeral worker pays it.
    """
    assert len(BackendProfile.disallowed_for(())) == len(BUILTIN_TOOLS)


async def test_worker_options_strip_the_builtins(bench):
    transport = ScriptedTransport([result(structured=GOOD_HANDLE)])
    await run_lane_worker(
        WorkerRequest(job_id=JOB, lane=bench.lane, task="t", dispatch_id="d1"),
        store=bench.store, event_log=bench.events, transport=transport,
    )
    options = transport.calls[0][1]
    assert set(BUILTIN_TOOLS) <= set(options.disallowed_tools)
    assert options.allowed_tools == [
        "mcp__lane__read_note", "mcp__lane__write_finding", "mcp__lane__update_state",
    ]


# --- Requirement: Backend capability 的宣告與降級 ------------------------


def test_self_hosted_declares_nothing_until_proven():
    assert SELF_HOSTED.capabilities == frozenset()
    for capability in BackendCapability:
        assert not SELF_HOSTED.supports(capability)


async def test_api_budget_is_only_sent_when_supported(bench):
    enforcing = ScriptedTransport([result(structured=GOOD_HANDLE)])
    await run_lane_worker(
        WorkerRequest(job_id=JOB, lane=bench.lane, task="t", dispatch_id="d1"),
        store=bench.store, event_log=bench.events, transport=enforcing,
    )
    assert enforcing.calls[0][1].task_budget == {"total": bench.lane.type.token_budget}

    lane = with_backend(bench.lane, "test-degraded")
    degraded = ScriptedTransport([assistant('{"artifact":"a","headline":"h","confidence":"low"}'), result()])
    await run_lane_worker(
        WorkerRequest(job_id=JOB, lane=lane, task="t", dispatch_id="d2"),
        store=bench.store, event_log=bench.events, transport=degraded,
    )
    assert degraded.calls[0][1].task_budget is None


async def test_local_token_ceiling_stops_a_backend_without_api_budget(bench):
    """Scenario: 不支援 API 端預算時以本地計數硬斷"""
    lane = with_backend(bench.lane, "test-degraded")
    lane = replace(lane, type=replace(lane.type, token_budget=500))
    over = {"input_tokens": 900, "output_tokens": 400}
    transport = ScriptedTransport([assistant("…", usage=over), assistant("…", usage=over), result()])

    handle = await run_lane_worker(
        WorkerRequest(job_id=JOB, lane=lane, task="t", dispatch_id="d1"),
        store=bench.store, event_log=bench.events, transport=transport,
    )
    assert handle.status is HandleStatus.BUDGET_EXCEEDED
    assert transport.call_count == 1, "a local ceiling is a semantic failure, not a retry"
