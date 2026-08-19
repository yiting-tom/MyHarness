"""The golden job: a fixed input, a real run, and assertable bounds.

An orchestrator's planning quality cannot be asserted offline, and arguably not
at all. Its *discipline* can: how much context it used, whether it repeated
itself, what it spent, and whether a delivery came out. This module runs one
small job end to end so those numbers exist to assert against (design.md D7).

    python -m myharness.goldens --backend openrouter
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from myharness.artifacts.local import LocalArtifactStore
from myharness.artifacts.types import GrantSet
from myharness.events.log import LocalEventLog
from myharness.events.query import summarize
from myharness.jobs.runner import JobRunner
from myharness.jobs.spec import JobSpec
from myharness.lanes.types import LaneRegistry, LaneType
from myharness.dataflow import Anomaly, DataFlow, build_dataflow, critical, detect
from myharness.orchestrator.delivery import Delivery, build_delivery
from myharness.orchestrator.loop import LoopOutcome, OrchestratorLoop

GOLDEN_CSV = Path("goldens/txn-2024.csv")
GOAL = (
    "分析 2024 年的交易資料，找出異常樣態並說明其特徵。"
    "資料已上傳為 blob，欄位為 txn_id / ts / account / amount / channel。"
)

ANALYST_TOOLS = ("read_note", "write_finding", "update_state", "localize_blob")


def lane_types(
    charters: Path = Path("charters"), backend: str = "openrouter"
) -> LaneRegistry:
    """Lane types for the golden job, all on one backend.

    The first run put the lanes on the default backend while the orchestrator
    ran on OpenRouter; every dispatch then failed 401 against a stale key in the
    shell. Backend belongs to the job, not to a per-type default.
    """
    return LaneRegistry(
        LaneType(
            name="tabular-analyst",
            charter_path=charters / "tabular-analyst.md",
            tools=ANALYST_TOOLS, model_tier="strong", backend=backend,
            token_budget=60_000, max_turns=12, state_max_tokens=2_000,
            description="表格與交易資料的統計分析；可直接處理大型 CSV",
        ),
        LaneType(
            name="synthesizer",
            charter_path=charters / "synthesizer.md",
            tools=("read_note", "write_finding"), model_tier="strong", backend=backend,
            token_budget=40_000, max_turns=8, state_max_tokens=1_000,
            description="讀取多份 finding 並收斂成一份給人閱讀的報告",
        ),
    )


@dataclass
class GoldenResult:
    outcome: LoopOutcome
    delivery: Delivery
    summary: Any
    blob_id: str
    flow: DataFlow
    anomalies: list[Anomaly]

    @property
    def critical(self) -> list[Anomaly]:
        return critical(self.anomalies)

    def report_line(self) -> str:
        s, o = self.summary, self.outcome
        anomaly_note = (
            "\nanomalies=" + ", ".join(f"{a.kind}({a.severity})" for a in self.anomalies)
            if self.anomalies else "\nanomalies=none"
        )
        return anomaly_note.lstrip("\n") + "\n" + (
            f"phase={o.phase} salvaged={o.salvaged} turns={o.turns} "
            f"handoffs={o.handoffs}\n"
            f"context_peak={o.context_peak:,} dispatches={s.dispatches} "
            f"duplicates={s.duplicates} failures={s.failures}\n"
            f"cost=${s.total_usd:.4f} peek={s.peek_tokens} "
            f"throttle={s.throttle_seconds}s cache_hit={s.cache_hit_ratio}\n"
            f"caveats={[c.kind for c in s.caveats]}"
        )


async def run_golden(
    root: Path,
    *,
    job_id: str = "golden",
    backend: str = "openrouter",
    csv_path: Path = GOLDEN_CSV,
    charters: Path = Path("charters"),
    spec_overrides: dict[str, Any] | None = None,
) -> GoldenResult:
    store = LocalArtifactStore(root)
    await store.init_job(job_id)
    events = LocalEventLog(root)

    blob = await store.put_blob(
        job_id, "raw/txn-2024", source=csv_path, produced_by="user",
        schema={"columns": ["txn_id", "ts", "account", "amount", "channel"],
                "format": "csv"},
    )

    spec = JobSpec(
        job_id=job_id, goal=f"{GOAL}\n\n可用資料：{blob.id}",
        max_dispatches=12, max_budget_usd=1.0, max_wall_clock_s=1800.0,
        peek_budget_tokens=8_000, question_quota=2,
        **(spec_overrides or {}),
    )
    runner = JobRunner(spec, store=store, event_log=events)
    loop = OrchestratorLoop(
        runner=runner, lanes=lane_types(charters, backend=backend), backend=backend
    )

    outcome = await loop.run()
    stream = await events.read(job_id)
    delivery = await build_delivery(
        store=store, events=stream, job_id=job_id, status=str(outcome.phase),
        report_artifact=outcome.report_artifact,
    )
    flow = build_dataflow(stream, await store.list(job_id), job_id=job_id)
    return GoldenResult(outcome, delivery, summarize(stream), str(blob.id),
                        flow, detect(flow))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("jobs-scratch/golden"))
    parser.add_argument("--backend", default="openrouter")
    parser.add_argument("--job-id", default="golden")
    args = parser.parse_args(argv)

    result = asyncio.run(run_golden(args.root, job_id=args.job_id, backend=args.backend))
    print("\n--- golden job ---")
    print(result.report_line())
    if result.anomalies:
        print("\n--- 資料流異常 ---")
        for anomaly in result.anomalies:
            print(f"  [{anomaly.severity.upper()}] {anomaly.detail}")
    print("\n--- delivery ---")
    print(json.dumps(result.delivery.to_dict(), ensure_ascii=False, indent=2)[:2500])
    return 0 if result.delivery.report_artifact else 1


if __name__ == "__main__":
    sys.exit(main())
