"""Choosing a reader, and refusing a blob that is too large to sandbox.

The size cap is not a performance knob. Views are lazy, so querying a file
without materialising it would mean the read happens *after* the sandbox
closes -- which means the sandbox cannot close. spike #10 tried the alternative
(``allowed_paths`` as a per-file allowlist) and it fenced nothing: it read
``/etc/hosts`` and ``COPY`` overwrote the granted blob. Ingest-then-lock is the
only sound shape and this cap is its price (design.md D3).

The refusal says so, because otherwise someone with a big machine will raise it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from myharness.artifacts.types import ArtifactMeta

#: 256 MiB of CSV is roughly 2-4x that in memory once typed. Sized to fit
#: comfortably beside a worker on an ordinary machine, not to the data.
MAX_INGEST_BYTES = 256 * 1024 * 1024

_READERS: dict[str, str] = {
    ".csv": "read_csv_auto",
    ".tsv": "read_csv_auto",
    ".txt": "read_csv_auto",
    ".parquet": "read_parquet",
    ".pq": "read_parquet",
    ".json": "read_json_auto",
    ".jsonl": "read_json_auto",
    ".ndjson": "read_json_auto",
}

SUPPORTED = tuple(sorted(_READERS))


@dataclass(frozen=True, slots=True)
class IngestRefusal:
    code: str
    message: str


def choose_reader(path: Path, meta: ArtifactMeta) -> str | IngestRefusal:
    """Reader for one blob, from its declared format or its suffix.

    The stored ``schema`` wins over the filename: a producer that recorded what
    it wrote knows better than an extension does.
    """
    declared = (meta.schema or {}).get("format")
    if isinstance(declared, str):
        reader = _READERS.get(f".{declared.lower().lstrip('.')}")
        if reader:
            return reader
    reader = _READERS.get(path.suffix.lower())
    if reader:
        return reader
    return IngestRefusal(
        "unsupported_format",
        f"cannot load {path.name}: no reader for {path.suffix or 'a file with no suffix'}. "
        f"Supported: {', '.join(SUPPORTED)}. "
        "Use localize_blob if you need the raw file for something else.",
    )


def check_size(meta: ArtifactMeta, *, limit: int | None = None) -> IngestRefusal | None:
    """Decided from the index alone -- a refused blob is never opened.

    ``limit`` is resolved here rather than as a default argument so the module
    constant stays authoritative: a default is bound once at import and cannot
    be changed afterwards, which makes the constant a decoration.
    """
    limit = MAX_INGEST_BYTES if limit is None else limit
    if meta.bytes <= limit:
        return None
    return IngestRefusal(
        "blob_too_large",
        f"{meta.id} is {meta.bytes:,} bytes, over the {limit:,} byte query limit. "
        "This limit is what makes the query sandbox possible -- the data has to "
        "be loaded before external file access is shut off -- so it is not a "
        "tuning knob. Narrow the job: ask for a pre-aggregated extract, or use "
        "localize_blob and process the file in chunks.",
    )


__all__ = [
    "IngestRefusal",
    "MAX_INGEST_BYTES",
    "SUPPORTED",
    "check_size",
    "choose_reader",
]
