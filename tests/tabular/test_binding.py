from __future__ import annotations

import pytest

from myharness.artifacts.ids import ArtifactId
from myharness.lanes.tabular.binding import bind_all, bind_name, describe


def aid(name: str) -> ArtifactId:
    return ArtifactId("job1", "blob", name)


@pytest.mark.parametrize(
    "name,expected",
    [("txn", "txn"),
     ("txn-2024", "txn_2024"),
     ("txn.2024", "txn_2024"),
     ("lanes/txn-2024/raw", "raw"),
     ("2024-txn", "t_2024_txn")],
)
def test_leaf_segment_becomes_a_legal_identifier(name: str, expected: str):
    assert bind_name(aid(name)) == expected


@pytest.mark.parametrize("bad", ["_leading", "交易", "has space", ""])
def test_ids_that_would_need_defending_against_cannot_exist(bad: str):
    """The invariant bind_name relies on is enforced one layer down."""
    with pytest.raises(Exception):
        ArtifactId("job1", "blob", bad)


def test_every_legal_leaf_yields_a_legal_identifier():
    """Exhaustive over the character classes ArtifactId actually permits."""
    import re
    for leaf in ("a", "Z9", "a.b", "a-b", "9x", "9.9-9", "A" * 60):
        table = bind_name(aid(leaf))
        assert re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", table), (leaf, table)


def test_collision_gets_a_suffix():
    """Two artifacts can share a leaf under different namespaces."""
    bindings = bind_all([aid("lanes/a/raw"), aid("lanes/b/raw")])
    assert [b.table for b in bindings] == ["raw", "raw_2"]


def test_binding_order_is_the_call_order():
    bindings = bind_all([aid("b"), aid("a")])
    assert [b.table for b in bindings] == ["b", "a"]


def test_long_names_are_bounded():
    long = "x" * 200
    assert len(bind_name(aid(long))) <= 48


def test_describe_names_both_sides():
    line = describe(bind_all([aid("txn-2024")]))
    assert "txn_2024" in line and "job1/blob/txn-2024" in line


def test_describe_is_honest_when_empty():
    assert "no tables" in describe(())
