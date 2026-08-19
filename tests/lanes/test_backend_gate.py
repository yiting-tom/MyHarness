"""The shared backend gate.

A provider's rate limit belongs to the backend, not to whichever worker met it
first. With a fan-out coming, per-worker retry would mean N workers each
rediscovering the same 429 -- N times the load on someone already refusing us.
"""

from __future__ import annotations

import asyncio
import random

import pytest

from myharness.backends.gate import (
    BASE_BACKOFF_S,
    MAX_BACKOFF_S,
    BackendGate,
    GateRegistry,
    ThrottleReport,
    backoff_delay,
)
from myharness.events.query import throttle_seconds, throttled_backends
from myharness.events.types import THROTTLE_GAVE_UP, THROTTLE_WAIT
from myharness.lanes.handle import HandleStatus
from myharness.lanes.transport import ScriptedTransport

from .conftest import FakeClock, api_retry, result, GOOD_HANDLE


def make_gate(clock: FakeClock, **kw) -> BackendGate:
    return BackendGate("b", clock=clock, sleep=clock.sleep,
                       rng=random.Random(0), **kw)


# --- Requirement: 每個後端共享的節流閘 -----------------------------------


async def test_cooldown_triggered_by_one_worker_blocks_the_others():
    """Scenario: 一個 worker 觸發的冷卻對所有 worker 生效"""
    clock = FakeClock()
    gate = make_gate(clock)

    # Worker A is refused and parks the whole backend.
    parked = gate.trigger_cooldown(30.0)
    assert parked == pytest.approx(30.0)
    assert gate.cooling_down

    # Worker B arrives while the cooldown stands: it waits rather than sending a
    # request of its own to find out what A already knows.
    second = ThrottleReport()
    await gate.wait_for_clearance(second)
    assert second.waited_s == pytest.approx(30.0)
    assert not gate.cooling_down


async def test_backing_off_parks_the_backend_for_everyone():
    clock = FakeClock()
    gate = make_gate(clock)
    report = ThrottleReport()
    await gate.back_off(0, report)
    assert report.cooldowns_triggered == 1
    assert report.waited_s > 0


async def test_cooldowns_are_per_backend():
    """Scenario: 不同後端的冷卻互不影響"""
    clock = FakeClock()
    registry = GateRegistry(clock=clock, sleep=clock.sleep, rng=random.Random(0))
    a, b = registry.for_backend("a"), registry.for_backend("b")

    a.trigger_cooldown(30.0)
    assert a.cooling_down
    assert not b.cooling_down

    report = ThrottleReport()
    await b.wait_for_clearance(report)
    assert report.waited_s == 0


async def test_concurrency_is_capped():
    """Scenario: 並行請求數受限"""
    gate = BackendGate("b", max_concurrency=2)
    inside, peak = 0, 0

    async def worker():
        nonlocal inside, peak
        async with gate.acquire():
            inside += 1
            peak = max(peak, inside)
            await asyncio.sleep(0.01)
            inside -= 1

    await asyncio.gather(*(worker() for _ in range(6)))
    assert peak <= 2


def test_cooldown_extends_but_never_shortens():
    clock = FakeClock()
    gate = make_gate(clock)
    gate.trigger_cooldown(60.0)
    gate.trigger_cooldown(5.0)
    assert gate.remaining_cooldown() == pytest.approx(60.0)


# --- Requirement: 重試以時間預算為界，且帶隨機抖動 ------------------------


async def test_retry_stops_at_the_time_budget_not_a_count():
    """Scenario: 超過時間預算後成為失敗值"""
    clock = FakeClock()
    gate = make_gate(clock, retry_budget_s=45.0)
    report = ThrottleReport()

    attempts = 0
    while await gate.back_off(attempts, report):
        attempts += 1
        assert attempts < 100, "must terminate"

    assert report.gave_up
    assert report.waited_s == pytest.approx(45.0)


def test_backoff_grows_and_is_bounded():
    rng = random.Random(3)
    assert max(backoff_delay(0, rng=rng) for _ in range(50)) <= BASE_BACKOFF_S
    assert max(backoff_delay(20, rng=rng) for _ in range(50)) <= MAX_BACKOFF_S


def test_backoff_has_jitter():
    """Scenario: 退避帶抖動

    Without jitter, every worker refused at the same instant wakes together and
    reproduces the burst that got them limited.
    """
    rng = random.Random(11)
    samples = {round(backoff_delay(4, rng=rng), 6) for _ in range(40)}
    assert len(samples) > 30, "same attempt number must not give the same delay"


# --- Requirement: 節流事件寫入事件流 --------------------------------------


async def test_throttle_events_quantify_the_wait(bench):
    """Scenario: 冷卻被記錄 / 等待時間可被加總"""
    from myharness.lanes.worker import WorkerRequest, run_lane_worker

    from .conftest import JOB

    transport = ScriptedTransport(*[[api_retry(429), RuntimeError("429")] for _ in range(20)])
    handle = await run_lane_worker(
        WorkerRequest(job_id=JOB, lane=bench.lane, task="t", dispatch_id="d1"),
        store=bench.store, event_log=bench.events, transport=transport,
    )
    assert handle.status is HandleStatus.BACKEND_UNAVAILABLE

    stream = await bench.events.read(JOB)
    waits = [e for e in stream if e.t == THROTTLE_WAIT]
    assert waits and waits[0].get("backend") == "test-enforcing"
    assert throttle_seconds(stream) > 0
    assert throttled_backends(stream)["test-enforcing"] > 0
    assert any(e.t == THROTTLE_GAVE_UP for e in stream)


async def test_a_clean_run_records_no_throttling(bench):
    from myharness.lanes.worker import WorkerRequest, run_lane_worker

    from .conftest import JOB

    await run_lane_worker(
        WorkerRequest(job_id=JOB, lane=bench.lane, task="t", dispatch_id="d1"),
        store=bench.store, event_log=bench.events,
        transport=ScriptedTransport([result(structured=GOOD_HANDLE)]),
    )
    assert throttle_seconds(await bench.events.read(JOB)) == 0


async def test_rate_limit_becomes_a_report_caveat(bench):
    """Giving up to a rate limit is a shortfall the final report must own."""
    from myharness.events.query import derive_caveats
    from myharness.lanes.worker import WorkerRequest, run_lane_worker

    from .conftest import JOB

    await run_lane_worker(
        WorkerRequest(job_id=JOB, lane=bench.lane, task="t", dispatch_id="d1"),
        store=bench.store, event_log=bench.events,
        transport=ScriptedTransport(*[[api_retry(429), RuntimeError("429")] for _ in range(20)]),
    )
    kinds = {c.kind for c in derive_caveats(await bench.events.read(JOB))}
    assert "rate_limited" in kinds
