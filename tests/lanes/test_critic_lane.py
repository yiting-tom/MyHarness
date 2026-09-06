"""The critic lane: it examines conclusions, it does not recompute them.

The chain was analyst -> synthesizer -> report, with nothing questioning the
analysis itself. Golden run #7 delivered "未發現典型詐騙異常" and no step asked
whether that meant "we looked and found nothing" or "we did not look".

The tests here pin the two properties that make this lane worth having rather
than a fourth voice saying the same thing: it cannot touch data, and its
charter forbids inventing objections.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from myharness.goldens import lane_types
from myharness.mcp.server import default_lanes

CHARTER = Path("charters/critic.md")

#: Everything that would let a lane reach a blob. A critic holding any of these
#: would re-run the analysis instead of examining it, and its output would
#: compete with the analyst's rather than check it.
DATA_TOOLS = frozenset({"duckdb_query", "inspect_blob", "localize_blob"})


@pytest.fixture(params=["mcp", "goldens"])
def critic(request):
    """Both shipped registries, because they are declared separately.

    goldens.lane_types and mcp.server.default_lanes are two lists that must
    agree; a lane added to one and forgotten in the other is exactly the kind
    of drift nothing else here would catch.
    """
    registry = default_lanes() if request.param == "mcp" else lane_types()
    # get_type, not get: get() looks up a runtime *instance*, and a job has
    # none until the orchestrator creates one.
    return registry.get_type("critic")


def test_the_critic_ships_in_both_registries(critic):
    assert critic.name == "critic"


def test_it_cannot_reach_data(critic):
    assert not DATA_TOOLS & set(critic.tools)
    assert set(critic.tools) == {"read_note", "write_finding"}


def test_it_cannot_carry_state_between_tasks(critic):
    """No update_state: a critique is about the findings in front of it.

    Accumulated opinion across dispatches would be an argument the orchestrator
    cannot see the origin of.
    """
    assert "update_state" not in critic.tools


def test_the_orchestrator_is_told_when_to_use_it(critic):
    """description is the only thing the orchestrator reads when choosing."""
    assert "不查資料" in critic.description
    assert "收斂成報告之前" in critic.description, "when to dispatch it, not just what it is"


def test_the_charter_is_a_file_that_exists(critic):
    assert critic.charter_path == CHARTER
    assert CHARTER.is_file()


# ---- what the charter commits to ----------------------------------------
#
# A charter is prose, so these assert the commitments that would otherwise be
# edited away without anyone noticing -- the same reason test_classify pins
# "no plan, no goal".


def test_the_charter_forbids_manufacturing_objections():
    """The failure mode of an LLM critic is always finding something."""
    text = CHARTER.read_text(encoding="utf-8")
    assert "沒有問題就說沒有問題" in text
    assert "無異議" in text


def test_the_charter_separates_unsupported_from_wrong():
    """It has no way to check a number, so it must not claim one is wrong."""
    text = CHARTER.read_text(encoding="utf-8")
    assert "沒有被支撐" in text
    assert "你查不了" in text


def test_the_charter_requires_a_citation_for_every_objection():
    text = CHARTER.read_text(encoding="utf-8")
    assert "指名出處" in text


def test_the_charter_makes_missing_grants_the_first_thing_reported():
    """A critique of half the findings misleads more than none at all."""
    text = CHARTER.read_text(encoding="utf-8")
    assert "沒有授權給你" in text
