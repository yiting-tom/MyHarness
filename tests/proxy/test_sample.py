"""What the classifier is allowed to see.

The sample is the one place a blob's own bytes get near a prompt, so the gates
here are the same two as everywhere else, and each is tested against the case
the other cannot catch.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from myharness.artifacts.ids import ArtifactId
from myharness.artifacts.types import ArtifactMeta
from myharness.proxy.sample import (
    MAX_LINE_CHARS,
    MAX_SAMPLE_CHARS,
    MAX_SAMPLE_LINES,
    describe_meta,
    read_sample,
)


def meta(name="raw/a.csv", *, size=100, schema=None) -> ArtifactMeta:
    return ArtifactMeta(id=ArtifactId("j", "blob", name), kind="blob", bytes=size,
                        produced_by="client", created_at=datetime.now(UTC), schema=schema)


class TestDescribeMeta:
    def test_names_id_and_size(self):
        out = describe_meta(meta(size=1472))
        assert "j/blob/raw/a.csv" in out and "1,472" in out

    def test_includes_declared_format_and_columns(self):
        out = describe_meta(meta(schema={"format": "csv", "columns": ["ts", "amount"]}))
        assert "csv" in out and "ts, amount" in out

    def test_many_columns_are_bounded(self):
        out = describe_meta(meta(schema={"columns": [f"c{i}" for i in range(200)]}))
        assert "+180" in out and len(out) < 400

    def test_absent_schema_is_not_a_crash(self):
        assert "bytes" in describe_meta(meta(schema=None))


class TestLineGate:
    def test_only_the_first_lines_are_kept(self, tmp_path: Path):
        f = tmp_path / "a.csv"
        f.write_text("\n".join(f"row{i}" for i in range(500)), encoding="utf-8")
        s = read_sample(f)
        assert s.lines == MAX_SAMPLE_LINES and s.line_limited

    def test_a_short_file_is_not_flagged(self, tmp_path: Path):
        f = tmp_path / "a.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")
        s = read_sample(f)
        assert not s.truncated and s.lines == 2

    def test_the_header_survives(self, tmp_path: Path):
        """It is the single most useful line for classifying a table."""
        f = tmp_path / "a.csv"
        f.write_text("txn_id,ts,account\n" + "\n".join("x,y,z" for _ in range(999)),
                     encoding="utf-8")
        assert read_sample(f).text.startswith("txn_id,ts,account")


class TestCharGate:
    def test_few_long_lines_are_still_bounded(self, tmp_path: Path):
        """The case the line gate cannot catch."""
        f = tmp_path / "a.csv"
        f.write_text("\n".join("x" * 290 for _ in range(10)), encoding="utf-8")
        s = read_sample(f)
        assert s.char_limited and len(s.text) <= MAX_SAMPLE_CHARS

    def test_a_single_enormous_line_yields_a_prefix_not_nothing(self, tmp_path: Path):
        f = tmp_path / "a.json"
        f.write_text("{" + "\"k\":1," * 50_000 + "}", encoding="utf-8")
        s = read_sample(f)
        assert s.text and len(s.text) <= MAX_SAMPLE_CHARS

    def test_one_long_line_is_clipped_before_the_budget_is_spent(self, tmp_path: Path):
        f = tmp_path / "a.csv"
        f.write_text("h1,h2\n" + "y" * 5000 + "\nafter\n", encoding="utf-8")
        s = read_sample(f)
        assert "…" in s.text
        assert max(len(line) for line in s.text.splitlines()) <= MAX_LINE_CHARS + 1

    def test_both_gates_can_fire(self, tmp_path: Path):
        f = tmp_path / "a.csv"
        f.write_text("\n".join("z" * 250 for _ in range(400)), encoding="utf-8")
        s = read_sample(f)
        assert s.line_limited and s.char_limited


class TestBinary:
    def test_binary_is_described_not_decoded(self, tmp_path: Path):
        f = tmp_path / "a.parquet"
        f.write_bytes(b"PAR1\x00\x01\x02\xff\xfe" * 100)
        s = read_sample(f)
        assert s.binary and "binary" in s.text
        assert "\x00" not in s.text

    def test_a_textual_suffix_is_trusted(self, tmp_path: Path):
        """A .csv with an odd byte is still a csv worth sampling."""
        f = tmp_path / "a.csv"
        f.write_bytes("名稱,值\n甲,1\n".encode())
        s = read_sample(f)
        assert not s.binary and "名稱" in s.text

    def test_invalid_utf8_in_an_unknown_suffix_is_binary(self, tmp_path: Path):
        f = tmp_path / "a.dat"
        f.write_bytes(b"\xff\xfe\xfd\xfc" * 100)
        assert read_sample(f).binary


class TestReadFailures:
    def test_a_missing_file_is_a_value_not_an_exception(self, tmp_path: Path):
        s = read_sample(tmp_path / "nope.csv")
        assert "unreadable" in s.text and not s.binary

    def test_an_empty_file(self, tmp_path: Path):
        f = tmp_path / "a.csv"
        f.write_text("", encoding="utf-8")
        s = read_sample(f)
        assert s.lines == 0 and not s.truncated


def test_the_limits_are_classifier_sized_not_page_sized():
    """If these grow, the proxy stops being cheap and starts being a reader."""
    assert MAX_SAMPLE_LINES <= 30
    assert MAX_SAMPLE_CHARS <= 4_000
