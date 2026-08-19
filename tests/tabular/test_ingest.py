from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from myharness.artifacts.ids import ArtifactId
from myharness.artifacts.types import ArtifactMeta
from myharness.lanes.tabular.ingest import (
    MAX_INGEST_BYTES,
    IngestRefusal,
    check_size,
    choose_reader,
)


def meta(name: str = "raw", *, size: int = 100, schema=None) -> ArtifactMeta:
    return ArtifactMeta(
        id=ArtifactId("job1", "blob", name), kind="blob", bytes=size,
        produced_by="test", created_at=datetime.now(UTC), schema=schema,
    )


@pytest.mark.parametrize(
    "filename,reader",
    [("a.csv", "read_csv_auto"), ("a.TSV", "read_csv_auto"),
     ("a.parquet", "read_parquet"), ("a.jsonl", "read_json_auto")],
)
def test_reader_from_suffix(filename: str, reader: str):
    assert choose_reader(Path(filename), meta()) == reader


def test_declared_format_beats_the_suffix():
    """A producer that recorded what it wrote knows better than a filename."""
    got = choose_reader(Path("data.bin"), meta(schema={"format": "parquet"}))
    assert got == "read_parquet"


def test_declared_format_tolerates_a_leading_dot():
    assert choose_reader(Path("x"), meta(schema={"format": ".CSV"})) == "read_csv_auto"


def test_unknown_declared_format_falls_back_to_suffix():
    assert choose_reader(Path("a.csv"), meta(schema={"format": "avro"})) == "read_csv_auto"


@pytest.mark.parametrize("filename", ["a.xlsx", "a.pdf", "noSuffix"])
def test_unsupported_format_is_a_refusal_naming_the_alternatives(filename: str):
    got = choose_reader(Path(filename), meta())
    assert isinstance(got, IngestRefusal) and got.code == "unsupported_format"
    assert ".csv" in got.message and "localize_blob" in got.message


def test_size_under_the_cap_passes():
    assert check_size(meta(size=MAX_INGEST_BYTES)) is None


def test_oversized_blob_is_refused_from_the_index_alone():
    got = check_size(meta(size=MAX_INGEST_BYTES + 1))
    assert got is not None and got.code == "blob_too_large"


def test_refusal_explains_the_cap_is_the_sandbox_not_a_knob():
    """Otherwise the next person with a big machine just raises it."""
    got = check_size(meta(size=MAX_INGEST_BYTES + 1))
    assert got is not None
    assert "sandbox" in got.message and "not a tuning knob" in got.message
    assert "localize_blob" in got.message, "a refusal should offer a way forward"
