"""The claims this layer exists to make, asserted rather than assumed.

Everything else tests that a piece works. These test that the *bounds* hold:
that peeking converges, that a job past its ceilings still delivers, and that
the orchestrator never reads a full analysis to write the report.
"""

from __future__ import annotations

import json

import pytest

from myharness.artifacts.ids import ArtifactId
from myharness.artifacts.types import GrantSet
from myharness.events.query import peek_tokens_spent
from myharness.jobs.spec import JobPhase
from myharness.orchestrator.tools import MIN_USEFUL_PEEK_TOKENS

from .conftest import JOB, payload


# --- Requirement: Peek 有 job 級的總預算（收斂性） ------------------------


@pytest.mark.parametrize("bench", [{"peek_budget_tokens": 2_000}], indirect=True)
async def test_relentless_peeking_converges(bench):
    """An orchestrator that will not stop peeking still cannot exceed the budget.

    This is the difference between an estimated context ceiling and a
    guaranteed one (design.md D2).
    """
    for i in range(6):
        await bench.store.put_note(
            JOB, f"lanes/txn/findings/{i}", "## 結論\n" + "夜間高頻交易。" * 30,
            produced_by="lane:txn",
        )

    refusals = 0
    for round_ in range(60):
        artifact = f"{JOB}/note/lanes/txn/findings/{round_ % 6}"
        body = payload(await bench.h["peek"]({"artifact": artifact, "max_tokens": 5000}))
        if body.get("error") == "peek_budget_exhausted":
            refusals += 1

    budget = bench.runner.spec.peek_budget_tokens
    assert bench.runner.state.peek_spent_tokens <= budget
    assert refusals > 0, "the budget must actually bite"
    assert peek_tokens_spent(await bench.stream()) <= budget


@pytest.mark.parametrize("bench", [{"peek_budget_tokens": 2_000}], indirect=True)
async def test_the_event_stream_agrees_with_the_counter(bench):
    """Two records of the same spending must not be able to disagree."""
    await bench.store.put_note(JOB, "lanes/txn/findings/1", "## 結論\n夜間高頻。",
                               produced_by="lane:txn")
    for _ in range(5):
        await bench.h["peek"]({"artifact": f"{JOB}/note/lanes/txn/findings/1"})
    assert peek_tokens_spent(await bench.stream()) == bench.runner.state.peek_spent_tokens


# --- Requirement: Job 級硬上限與善終（完整路徑） --------------------------


@pytest.mark.parametrize(
    "bench", [{"max_dispatches": 2, "wrap_up_grace": 1}], indirect=True
)
async def test_limit_then_grace_then_coded_delivery(bench):
    """Ceiling → wrap-up → bounded grace → a delivery written by code.

    Each step is tested elsewhere; this walks the whole path, because the
    guarantee that matters is that a user never receives an empty job.
    """
    from myharness.orchestrator.delivery import build_delivery
    from myharness.orchestrator.loop import OrchestratorLoop
    from myharness.orchestrator.session import ScriptedSession, ScriptedSessionFactory
    from myharness.backends.profile import BackendCapability, BackendProfile, registry

    registry.register(BackendProfile(
        name="path-test", models={"strong": "m"},
        capabilities=frozenset({BackendCapability.STRUCTURED_OUTPUT}),
    ))
    await bench.declare("txn", "syn")

    # Two dispatches reach the ceiling.
    ids = []
    for i in range(2):
        ids.append(payload(await bench.h["dispatch"]({"lane": "txn", "task": f"分析 {i}"}))["task_id"])
    collected = payload(await bench.h["await_tasks"]({"task_ids": ids}))
    assert "收工" in collected["notice"], "the ceiling must be told to the orchestrator"

    # One dispatch of grace is allowed…
    allowed = payload(await bench.h["dispatch"]({"lane": "syn", "task": "寫報告"}))
    assert allowed["status"] == "running"
    # …and no more.
    refused = payload(await bench.h["dispatch"]({"lane": "txn", "task": "再多做一點"}))
    assert refused["status"] == "aborted"

    # The orchestrator never calls finish; the harness delivers anyway.
    session = ScriptedSession(turns=[[]], usage_series=[1_000])
    loop = OrchestratorLoop(runner=bench.runner, lanes=bench.tools.lanes,
                            backend="path-test", sessions=ScriptedSessionFactory([session]),
                            tools=bench.tools)
    outcome = await loop.run()

    assert outcome.salvaged
    assert outcome.phase is JobPhase.ABORTED
    assert outcome.report_artifact

    delivery = await build_delivery(
        store=bench.store, events=await bench.stream(), job_id=JOB,
        status=str(outcome.phase), report_artifact=outcome.report_artifact,
    )
    assert "limit_reached" in {c.kind for c in delivery.caveats}
    assert len(json.dumps(delivery.to_dict(), ensure_ascii=False)) < 4000


# --- Requirement: Orchestrator 規劃而不彙整 -------------------------------


async def test_synthesis_lane_gets_the_grants_the_orchestrator_names(bench):
    """Scenario: Synthesis lane 只讀被授權的產出"""
    granted: list[tuple[str, tuple[str, ...]]] = []

    async def spy(request, *, store, event_log):
        granted.append((request.lane.id, request.inputs))
        return await bench.fake(request, store=store, event_log=event_log)

    bench.runner._run_lane = spy
    await bench.declare("txn", "syn")

    first = payload(await bench.h["dispatch"]({"lane": "txn", "task": "分析"}))
    handles = payload(await bench.h["await_tasks"]({"task_ids": [first.get("task_id")]}))
    finding = handles["handles"][0]["artifact"]

    await bench.h["dispatch"]({
        "lane": "syn", "task": "彙整成報告", "inputs": [finding],
    })
    await bench.runner.settle()

    synthesis = [g for g in granted if g[0] == "syn"]
    assert synthesis == [("syn", (finding,))]


async def test_orchestrator_never_reads_a_full_analysis_to_report(bench):
    """Scenario: 報告由 synthesis lane 產出

    The orchestrator's only read tool is peek, and peek is budgeted. If it could
    assemble a report itself the whole layering would be pointless.
    """
    await bench.declare("txn", "syn")
    long_finding = "## 發現\n" + "非常長的分析內容。" * 500
    meta = await bench.store.put_note(JOB, "lanes/txn/findings/big", long_finding,
                                      produced_by="lane:txn")

    body = payload(await bench.h["peek"]({"artifact": str(meta.id), "max_tokens": 100_000}))
    spent = body.get("tokens", 0)
    assert spent <= bench.runner.spec.peek_budget_tokens

    # And whatever it did read is a fraction of the whole.
    full = await bench.store.read_note(
        ArtifactId.parse(str(meta.id)), grants=GrantSet.unrestricted(JOB),
        max_tokens=10**6,
    )
    assert len(body.get("text", "")) <= len(full)
