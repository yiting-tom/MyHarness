"""Offline scaffolding for the worker loop: no network, no keys, no money."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock

from myharness.artifacts.local import LocalArtifactStore
from myharness.backends.profile import BackendCapability, BackendProfile, registry
from myharness.events.log import LocalEventLog
from myharness.lanes.types import LaneInstance, LaneRegistry, LaneType

JOB = "j7"


# --- message builders -----------------------------------------------------


def assistant(*texts: str, usage: dict[str, int] | None = None) -> AssistantMessage:
    return AssistantMessage(
        content=[TextBlock(text=t) for t in texts],
        model="test-model",
        usage=usage or {"input_tokens": 100, "output_tokens": 20},
    )


def result(
    *,
    structured: Any = None,
    subtype: str = "success",
    is_error: bool = False,
    turns: int = 1,
    usd: float = 0.01,
    usage: dict[str, int] | None = None,
) -> ResultMessage:
    return ResultMessage(
        subtype=subtype, duration_ms=10, duration_api_ms=8, is_error=is_error,
        num_turns=turns, session_id="s", total_cost_usd=usd,
        usage=usage or {"input_tokens": 1200, "output_tokens": 80},
        structured_output=structured,
    )


def api_retry(status: int = 429) -> SystemMessage:
    return SystemMessage(subtype="api_retry", data={"error_status": status, "error": "rate_limit"})


GOOD_HANDLE = {
    "artifact": "j7/note/lanes/txn-2024/findings/001",
    "headline": "3 類異常交易，最大宗為深夜小額高頻",
    "confidence": "high",
    "metrics": {"n": 30412},
}


def handle_text(payload: dict[str, Any] | None = None) -> str:
    return json.dumps(payload or GOOD_HANDLE, ensure_ascii=False)


# --- backends -------------------------------------------------------------

ENFORCING = BackendProfile(
    name="test-enforcing",
    models={"mid": "test-model"},
    capabilities=frozenset(BackendCapability),
)

DEGRADED = BackendProfile(
    name="test-degraded",
    models={"mid": "test-model"},
    capabilities=frozenset(),
)


@pytest.fixture(autouse=True)
def _register_test_backends():
    registry.register(ENFORCING)
    registry.register(DEGRADED)


# --- bench ----------------------------------------------------------------


@dataclass
class Bench:
    store: LocalArtifactStore
    events: LocalEventLog
    lane: LaneInstance
    root: Path

    async def events_for(self, t: str) -> list:
        return [e for e in await self.events.read(JOB) if e.t == t]


@pytest.fixture
async def bench(tmp_path: Path) -> Bench:
    store = LocalArtifactStore(tmp_path)
    await store.init_job(JOB)
    charter = tmp_path / "charter.md"
    charter.write_text("你是一個測試用的 lane worker。\n", encoding="utf-8")
    lanes = LaneRegistry(
        LaneType(
            name="ta", charter_path=charter, backend="test-enforcing", model_tier="mid",
            tools=("read_note", "write_finding", "update_state"),
            token_budget=5000, max_turns=4, state_max_tokens=60,
        )
    )
    return Bench(store, LocalEventLog(tmp_path), lanes.create("txn-2024", "ta"), tmp_path)


def with_backend(lane: LaneInstance, backend: str) -> LaneInstance:
    from dataclasses import replace
    return replace(lane, type=replace(lane.type, backend=backend))
