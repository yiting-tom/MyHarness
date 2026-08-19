"""A pure-code driver for one lane task.

Standing in for the orchestrator that does not exist yet: register a type,
create an instance, hand it a task, print the handle. That is enough to verify
the contract end to end against a real model, which is the whole reason this
layer is built before the orchestrator (proposal: Why).

    python -m myharness.lanes.driver "分析這批交易的異常樣態"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from myharness.artifacts.local import LocalArtifactStore
from myharness.events.log import LocalEventLog
from myharness.events.query import summarize
from myharness.lanes.types import LaneRegistry, LaneType
from myharness.lanes.worker import WorkerRequest, run_lane_worker

DEFAULT_CHARTER = Path("charters/tabular-analyst.md")


async def run_one(
    task: str,
    *,
    root: Path,
    job_id: str = "driver",
    backend: str = "openrouter",
    tier: str = "strong",
    charter: Path = DEFAULT_CHARTER,
    dispatch_id: str = "d1",
    inputs: tuple[str, ...] = (),
    scope: str = "",
):
    store = LocalArtifactStore(root)
    await store.init_job(job_id)
    events = LocalEventLog(root)

    lanes = LaneRegistry(
        LaneType(
            name="tabular-analyst",
            charter_path=charter,
            tools=("read_note", "write_finding", "update_state", "localize_blob"),
            model_tier=tier,
            backend=backend,
            description="表格/交易資料分析",
        )
    )
    lane = lanes.create("lane-1", "tabular-analyst", scope=scope)

    handle = await run_lane_worker(
        WorkerRequest(job_id=job_id, lane=lane, task=task,
                      dispatch_id=dispatch_id, inputs=inputs),
        store=store, event_log=events,
    )
    return handle, summarize(await events.read(job_id))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task")
    parser.add_argument("--root", type=Path, default=Path("jobs-scratch"))
    parser.add_argument("--backend", default=os.environ.get("HARNESS_BACKEND", "openrouter"))
    parser.add_argument("--tier", default="strong")
    parser.add_argument("--charter", type=Path, default=DEFAULT_CHARTER)
    parser.add_argument("--scope", default="")
    args = parser.parse_args(argv)

    handle, summary = asyncio.run(
        run_one(args.task, root=args.root, backend=args.backend,
                tier=args.tier, charter=args.charter, scope=args.scope)
    )
    print("\n--- handle ---")
    print(json.dumps(handle.to_dict(), ensure_ascii=False, indent=2))
    print(f"\nhandle size: {len(handle.to_json())} chars")
    print(f"cost: ${summary.total_usd:.4f}   caveats: {[c.kind for c in summary.caveats]}")
    return 0 if handle.ok else 1


if __name__ == "__main__":
    sys.exit(main())
