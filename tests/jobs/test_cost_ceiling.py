"""A dollar ceiling is only a gate where dollars are real.

Golden run #7 tripped max_budget_usd at $1.0014 on a model that bills nothing.
The figure was not missing -- it was invented by the SDK -- so the ceiling fired
on a number with no referent and put the job into wrap-up at 120s. These tests
pin the two halves of the fix: the ceiling can be absent, and its absence is
visible.
"""

from __future__ import annotations

from myharness.backends.profile import (
    ANTHROPIC_DIRECT,
    OPENROUTER,
    SELF_HOSTED,
    BackendCapability,
    BackendProfile,
    ModelTier,
)
from myharness.events.query import derive_caveats
from myharness.events.types import JOB_START, Event, now
from myharness.jobs.spec import JobSpec, LimitKind


def start_event(**data) -> Event:
    return Event(t=JOB_START, seq=0, ts=now(), job_id="j", data=data)


# ---- who reports money ---------------------------------------------------


def test_backends_that_bill_declare_cost_reporting():
    assert ANTHROPIC_DIRECT.supports(BackendCapability.COST_REPORTING)
    # High is still reported; spike #7's inflated figures are an accuracy
    # problem, not an absence of one.
    assert OPENROUTER.supports(BackendCapability.COST_REPORTING)


def test_self_hosted_does_not_declare_it():
    """An unknown proxy has proved nothing, including what it charges."""
    assert not SELF_HOSTED.supports(BackendCapability.COST_REPORTING)


def test_the_direct_profile_does_not_declare_it(monkeypatch):
    from myharness.backends import profile as mod

    monkeypatch.setenv(mod.DIRECT_BASE_URL_ENV, "http://192.0.2.1:8000")
    monkeypatch.setenv(mod.DIRECT_MODEL_ENV, "m")
    built = mod.direct_openai_from_env()
    assert built is not None
    assert not built.supports(BackendCapability.COST_REPORTING)


# ---- the ceiling can be absent -------------------------------------------


def test_no_ceiling_means_money_never_breaches(runner_factory):
    runner = runner_factory(max_budget_usd=None, max_dispatches=99)
    runner.state.spent_usd = 10_000.0
    assert runner._limit_breached() is None


def test_a_ceiling_still_breaches(runner_factory):
    runner = runner_factory(max_budget_usd=1.0, max_dispatches=99)
    runner.state.spent_usd = 1.5
    assert runner._limit_breached() is LimitKind.BUDGET_USD


def test_dispatch_and_clock_still_bound_a_run_with_no_ceiling(runner_factory):
    """Spec: 沒有金額上限時仍然有界."""
    runner = runner_factory(max_budget_usd=None, max_dispatches=2)
    runner.state.spent_usd = 10_000.0
    runner.state.dispatches = 2
    assert runner._limit_breached() is LimitKind.DISPATCHES


# ---- who decides ---------------------------------------------------------


def loop_for(profile: BackendProfile, spec: JobSpec, runner_factory):
    from myharness.backends.profile import registry
    from myharness.lanes.types import LaneRegistry
    from myharness.orchestrator.loop import OrchestratorLoop

    registry.register(profile)
    runner = runner_factory(spec=spec)
    return OrchestratorLoop(runner=runner, lanes=LaneRegistry(), backend=profile.name)


def profile_with(*caps, name="probe") -> BackendProfile:
    return BackendProfile(
        name=name, models=dict.fromkeys(ModelTier, "m"), capabilities=frozenset(caps)
    )


def test_a_backend_without_cost_reporting_loses_the_ceiling(runner_factory):
    loop = loop_for(profile_with(name="silent"),
                    JobSpec(job_id="j", goal="g", max_budget_usd=1.0), runner_factory)
    assert loop.runner.spec.max_budget_usd is None


def test_a_backend_with_cost_reporting_keeps_it(runner_factory):
    """Spec: 宣告成本回報時金額上限照常生效."""
    loop = loop_for(profile_with(BackendCapability.COST_REPORTING, name="billing"),
                    JobSpec(job_id="j", goal="g", max_budget_usd=1.0), runner_factory)
    assert loop.runner.spec.max_budget_usd == 1.0


def test_an_explicit_ceiling_is_not_revived(runner_factory):
    """Spec: 明確指定也不生效.

    The number an explicit ceiling would compare against is the same fabricated
    one. Honouring the caller's intent here would reproduce golden #7 exactly.
    """
    loop = loop_for(profile_with(name="silent2"),
                    JobSpec(job_id="j", goal="g", max_budget_usd=0.01), runner_factory)
    assert loop.runner.spec.max_budget_usd is None


# ---- and it says so ------------------------------------------------------


def test_a_run_with_no_ceiling_carries_a_caveat():
    """Spec: 沒有金額上限時交付會說."""
    caveats = derive_caveats([start_event(budget_usd=None, max_dispatches=12)])
    kinds = [c.kind for c in caveats]
    assert kinds == ["no_cost_ceiling"]
    detail = caveats[0].detail
    assert "沒有金額上限" in detail
    # The point is not just the absence but what is holding the run instead.
    assert "12" in detail


def test_a_run_with_a_ceiling_carries_none():
    """Spec: 有金額上限而未觸頂時不加註."""
    assert not derive_caveats([start_event(budget_usd=1.0, max_dispatches=12)])


def test_a_stream_that_does_not_say_is_not_treated_as_absent():
    """Silence is not a declaration.

    An older log, or a hand-written stream, has no budget_usd at all. Reading
    that as "no ceiling" would put the caveat on runs that had one.
    """
    assert not derive_caveats([start_event(goal="g")])
