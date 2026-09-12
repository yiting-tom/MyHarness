"""Spike #19: what does a request cost before any conversation?

Golden #18 could finally solve for the rates from a recorded run, and the
solution was a negative number of tokens per Chinese character -- no
per-character pair fits both lanes. Dividing the residual by requests instead
gave 1,488 and 1,661, which is a per-request constant the estimate is missing.

FRAMEWORK_TOKENS_PER_REQUEST is 432, from spike #15. That spike divided a
probe's total input by its request count, and total input is a sum over
requests of a growing conversation -- so the division only holds when the probe
made exactly one request, and its output never showed whether it did.

This measures the same thing without that assumption. charged_ascii and
charged_cjk now record the conversation exactly as the estimate charged it, so

    fixed_per_request = (reported_in - estimate(charged)) / requests

holds however many requests a probe took. One probe per real lane
configuration, because the charter and the tool declarations are what a request
re-sends and both differ per lane.

Run: set -a && . ./.env && set +a && python spikes/spike19_fixed_cost.py
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.pop("ANTHROPIC_API_KEY", None)

from myharness.artifacts.local import LocalArtifactStore
from myharness.backends.profile import self_hosted_from_env
from myharness.events.log import LocalEventLog
from myharness.lanes.budget import estimate
from myharness.lanes.types import LaneInstance, LaneType
from myharness.lanes.worker import (
    FRAMEWORK_TOKENS_PER_REQUEST,
    WorkerRequest,
    run_lane_worker,
)

ANALYST_TOOLS = ("read_note", "write_finding", "update_state",
                 "localize_blob", "inspect_blob", "duckdb_query")
PAIR = ("read_note", "write_finding")

#: The three real configurations from the golden job. Tool count is the thing
#: spike #15's docstring warned about and never tested.
LANES = (
    ("tabular-analyst", Path("charters/tabular-analyst.md"), ANALYST_TOOLS),
    ("critic", Path("charters/critic.md"), PAIR),
    ("synthesizer", Path("charters/synthesizer.md"), PAIR),
)

TASK = "直接回傳一個 handle 結束，不要呼叫任何工具，不要分析任何東西。"


async def probe(name: str, charter: Path, tools: tuple[str, ...]) -> dict:
    profile = self_hosted_from_env()
    root = Path(tempfile.mkdtemp(prefix=f"mh-fx-{name}-"))
    store = LocalArtifactStore(root)
    await store.init_job("p")
    lane_type = LaneType(
        name=name, charter_path=charter, tools=tools, model_tier="strong",
        backend=profile.name, token_budget=60_000, max_turns=1,
    )
    await run_lane_worker(
        WorkerRequest(job_id="p", lane=LaneInstance(id=name, type=lane_type),
                      task=TASK, dispatch_id="d1"),
        store=store, event_log=LocalEventLog(root),
    )
    events = [json.loads(line) for line in
              (root / "jobs" / "p" / "events.jsonl").read_text().splitlines()]
    end = next(e for e in events if e["t"] == "dispatch.end")
    b, reported = end["estimate"], end["tokens"]["in"]
    if end["tokens"].get("estimated"):
        return {"name": name, "tools": len(tools), "error": "no reported usage"}
    charged = estimate(b["charged_ascii"], b["charged_cjk"])
    return {
        "name": name, "tools": len(tools), "requests": b["requests"],
        "reported": reported, "charged": charged,
        "measured": (reported - charged) / b["requests"],
        "assumed": b["fixed_per_request"],
        "charter_priced": b["fixed_per_request"] - FRAMEWORK_TOKENS_PER_REQUEST,
    }


async def main() -> int:
    if self_hosted_from_env() is None:
        print("HARNESS_PROXY_BASE_URL / _MODEL unset; nothing to measure")
        return 1

    rows = [await probe(*lane) for lane in LANES]
    print(f"{'lane':18} {'tools':>5} {'reqs':>4} {'reported':>9} {'charged':>8} "
          f"{'measured F':>10} {'assumed F':>9} {'short by':>9}")
    for r in rows:
        if "error" in r:
            print(f"{r['name']:18} {r['tools']:5} {'':>4} {r['error']}")
            continue
        print(f"{r['name']:18} {r['tools']:5} {r['requests']:4} {r['reported']:9,} "
              f"{r['charged']:8,} {r['measured']:10,.0f} {r['assumed']:9,} "
              f"{r['measured'] - r['assumed']:9,.0f}")

    solved = [r for r in rows if "error" not in r]
    if len(solved) >= 2:
        print()
        print("framework constant implied by each lane "
              f"(measured F - charter as this repo prices it, currently "
              f"{FRAMEWORK_TOKENS_PER_REQUEST}):")
        for r in solved:
            print(f"  {r['name']:18} {r['measured'] - r['charter_priced']:8,.0f}")
        print()
        print("If those agree, the constant is simply too low. If they scale with")
        print("the tool count, the tool declarations are the term nobody priced.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
