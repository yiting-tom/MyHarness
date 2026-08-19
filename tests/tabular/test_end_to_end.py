"""The whole workflow the charter now describes, against the real fixture.

Offline: no model, just the tools a worker would call in the order it would
call them. The point is that the chain closes -- inspect tells you the column
names, a query over those names works, `into` makes an artifact, and that
artifact is itself queryable. A break anywhere in that chain leaves a worker
holding a path and nothing to do with it, which is exactly where this started.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from myharness.artifacts.local import LocalArtifactStore
from myharness.artifacts.types import GrantSet
from myharness.goldens import GOLDEN_CSV, ground_truth
from myharness.lanes.tools import WorkerToolbox
from myharness.lanes.types import LaneRegistry, LaneType

pytestmark = pytest.mark.skipif(
    not GOLDEN_CSV.exists(), reason=f"{GOLDEN_CSV} missing"
)

JOB = "e2e"


def text_of(result: dict) -> str:
    return result["content"][0]["text"]


@pytest.fixture
async def worker(tmp_path: Path):
    store = LocalArtifactStore(tmp_path)
    await store.init_job(JOB)
    blob = await store.put_blob(
        JOB, "raw/txn-2024", source=GOLDEN_CSV, produced_by="user",
        # No .csv suffix on the stored name -- the declared format is what
        # picks the reader, which is why it beats the filename.
        schema={"columns": ["txn_id", "ts", "account", "amount", "channel"],
                "format": "csv"},
    )
    charter = Path("charters/tabular-analyst.md")
    lane = LaneRegistry(
        LaneType(name="tabular-analyst", charter_path=charter, state_max_tokens=2000,
                 tools=("read_note", "write_finding", "update_state",
                        "localize_blob", "inspect_blob", "duckdb_query"))
    ).create("txn-2024", "tabular-analyst")
    toolbox = WorkerToolbox(
        store=store, job_id=JOB, lane=lane,
        grants=GrantSet.for_lane(JOB, lane.namespace, [blob.id]),
        read_budget=8000,
    )
    toolbox.build_server()
    try:
        yield toolbox, blob, store
    finally:
        await toolbox.aclose()


async def call(toolbox, tool: str, /, **args) -> str:
    return text_of(await toolbox.handlers[tool](args))


def data_rows(rendered: str) -> list[str]:
    """The rows of the result table, past the bindings line and the header."""
    lines = rendered.splitlines()
    separator = next(i for i, line in enumerate(lines) if set(line) <= {"-", " "} and "-" in line)
    return [line for line in lines[separator + 1:] if line and not line.startswith("...")]


async def test_the_full_chain_a_charter_describes(worker):
    toolbox, blob, store = worker
    truth = ground_truth(GOLDEN_CSV)

    # 1. inspect first -- this is where the worker learns the column names.
    schema = await call(toolbox, "inspect_blob", artifact=str(blob.id))
    assert f"{truth.rows:,} rows" in schema
    assert "account" in schema and "channel" in schema
    table = "txn_2024"
    assert table in schema, schema[:300]

    # 2. a query over what inspect reported.
    counted = await call(
        toolbox, "duckdb_query", artifacts=[str(blob.id)],
        sql=f"SELECT count(DISTINCT account) AS accounts FROM {table}",
    )
    assert str(truth.accounts) in counted

    # 3. the answer to the golden job's second question.
    channels = await call(
        toolbox, "duckdb_query", artifacts=[str(blob.id)],
        sql=f"SELECT channel, round(avg(amount), 2) AS avg_amt FROM {table} "
            "GROUP BY 1 ORDER BY 2",
    )
    assert data_rows(channels)[0].startswith(truth.cheapest_channel), channels

    # 4. a large intermediate result goes to disk, not into context.
    derived = await call(
        toolbox, "duckdb_query", artifacts=[str(blob.id)],
        sql=f"SELECT account, count(*) AS n, sum(amount) AS total FROM {table} "
            "GROUP BY 1",
        into="per-account",
    )
    assert f"{truth.accounts:,} rows" in derived
    assert "A0791" not in derived, "into must not return the rows"

    # 5. and that artifact is queryable by the same lane, on its own namespace.
    derived_id = next(w for w in derived.split() if "derived/per-account" in w)
    top = await call(
        toolbox, "duckdb_query", artifacts=[derived_id],
        sql="SELECT account, total FROM per_account ORDER BY total DESC LIMIT 3",
    )
    assert "A0791" in top, top

    # 6. the finding is what enters the job; the data never did.
    wrote = await call(
        toolbox, "write_finding", name="001",
        text=f"## 結論\n共 {truth.accounts} 個帳戶，{truth.cheapest_channel} 平均最低。",
    )
    assert "wrote" in wrote


async def test_the_raw_blob_is_never_read_into_context(worker):
    """A query over 2,940 rows returns a bounded answer, not the rows."""
    toolbox, blob, _ = worker
    out = await call(toolbox, "duckdb_query", artifacts=[str(blob.id)],
                     sql="SELECT * FROM txn_2024")
    assert "more rows exist" in out
    assert len(out) < 6000, f"{len(out)} chars reached the worker"
