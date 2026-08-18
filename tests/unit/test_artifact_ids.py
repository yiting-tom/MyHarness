"""ArtifactId parsing, validation, and namespace derivation."""

from __future__ import annotations

import pytest

from myharness.artifacts.ids import (
    KIND_BLOB,
    KIND_NOTE,
    ArtifactId,
    InvalidArtifactId,
    lane_namespace,
)


def test_roundtrip_through_string():
    aid = ArtifactId.parse("j7/blob/raw/txns-2024")
    assert (aid.job_id, aid.kind, aid.name) == ("j7", KIND_BLOB, "raw/txns-2024")
    assert str(aid) == "j7/blob/raw/txns-2024"
    assert ArtifactId.parse(str(aid)) == aid


@pytest.mark.parametrize(
    ("name", "namespace"),
    [
        ("plan", ""),
        ("raw/txns", "raw"),
        ("lanes/txn-2024/state", "lanes/txn-2024"),
        ("lanes/txn-2024/findings/003", "lanes/txn-2024/findings"),
    ],
)
def test_namespace_is_the_immediate_parent(name: str, namespace: str):
    assert ArtifactId("j7", KIND_NOTE, name).namespace == namespace


def test_lane_namespace_matches_by_prefix_not_equality():
    """A lane owns everything *under* its namespace, however deeply nested.

    ``namespace`` is the immediate parent, so grants and listings must compare
    by prefix -- otherwise findings/003 would fall outside its own lane.
    """
    lane = lane_namespace("txn-2024")
    deep = ArtifactId("j7", KIND_NOTE, "lanes/txn-2024/findings/003")
    assert deep.namespace != lane
    assert deep.name.startswith(lane + "/")


@pytest.mark.parametrize(
    "raw",
    [
        "j7/blob",                  # too few segments
        "j7",                       # not an id at all
        "j7/xxx/name",              # unknown kind
        "j7/note/",                 # empty name
        "j7/note/a//b",             # empty segment
        "j7/note/../etc/passwd",    # traversal
        "j7/note/./x",              # traversal
        "j7/note//leading",         # leading slash in name
        "j7/note/trailing/",        # trailing slash
        "-bad/note/x",              # job id may not start with a separator char
        "j7/note/-leading-dash",    # name segment may not start with a separator char
        "j7/note/has space",        # whitespace
    ],
)
def test_illegal_ids_are_rejected(raw: str):
    with pytest.raises(InvalidArtifactId):
        ArtifactId.parse(raw)


def test_illegal_kind_rejected_on_construction():
    with pytest.raises(InvalidArtifactId):
        ArtifactId("j7", "blobs", "x")


def test_kind_predicates():
    assert ArtifactId("j7", KIND_BLOB, "x").is_blob
    assert ArtifactId("j7", KIND_NOTE, "x").is_note


def test_ids_are_hashable_and_ordered():
    a = ArtifactId.parse("j7/note/a")
    b = ArtifactId.parse("j7/note/b")
    assert len({a, b, ArtifactId.parse("j7/note/a")}) == 2
    assert sorted([b, a]) == [a, b]


def test_lane_namespace_validates_the_lane_id():
    assert lane_namespace("txn-2024") == "lanes/txn-2024"
    with pytest.raises(InvalidArtifactId):
        lane_namespace("../escape")
