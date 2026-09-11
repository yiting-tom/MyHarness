"""Spike #25: what does the price list cost in round trips, and save in tokens?

Change `expose-over-a2a` task 8.3. The gate's whole claim is that a caller sees
the prices before it spends, so the thing to measure is the trade it actually
makes: one extra round trip per section read, against the sections not read.

The crossover is the number worth knowing. A caller that ends up reading every
section pays for the price list on top of the report and makes more requests to
do it -- the gate is a loss for that caller, and saying so is more useful than
claiming it always wins.

Measured against a real finished job, like spike #24: `AnalysisService.result`
and `drill_section` read only the event log and the store, so this needs no
model and no network.

Run: python spikes/spike25_roundtrips.py
     python spikes/spike25_roundtrips.py --root jobs-scratch/golden18 --job golden18
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from myharness.artifacts.tokens import estimate_tokens
from myharness.lanes.types import LaneRegistry, LaneType
from myharness.mcp.service import AnalysisService


def newest_job() -> tuple[Path, str]:
    logs = sorted(Path("jobs-scratch").glob("*/jobs/*/events.jsonl"),
                  key=lambda p: p.stat().st_mtime)
    if not logs:
        raise SystemExit("no finished jobs in jobs-scratch; run a golden job first")
    job_dir = logs[-1].parent
    return job_dir.parent.parent, job_dir.name


def tokens_of(payload) -> int:
    return estimate_tokens(json.dumps(payload, ensure_ascii=False))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root")
    ap.add_argument("--job")
    args = ap.parse_args()
    root, job_id = ((Path(args.root), args.job) if args.root and args.job
                    else newest_job())

    lanes = LaneRegistry(LaneType(name="unused",
                                  charter_path=Path("charters/critic.md")))
    service = AnalysisService(root, lanes=lanes, backend="self-hosted")

    listing = await service.result(job_id)
    if not listing.get("ok"):
        raise SystemExit(f"cannot read {job_id}: {listing}")
    sections = listing.get("sections") or []
    price_list_tokens = tokens_of({k: v for k, v in listing.items() if k != "ok"})

    bodies = []
    for section in sections:
        answer = await service.drill_section(job_id, str(section["id"]))
        bodies.append(tokens_of(answer) if answer.get("ok") else 0)

    print(f"job {job_id}: {len(sections)} sections\n")
    print(f"price list           1 round trip   {price_list_tokens:6,} tokens")
    print(f"every section        {len(sections)} round trips  "
          f"{sum(bodies):6,} tokens")
    print()
    print(f"{'sections read':>14} {'price-list mode':>26} {'full-text mode':>22}")
    print(f"{'':14} {'trips':>8} {'tokens':>9} {'':>7} {'trips':>8} {'tokens':>12}")
    ordered = sorted(bodies, reverse=True)  # worst case: the dearest ones first
    for k in range(len(sections) + 1):
        gate = price_list_tokens + sum(ordered[:k])
        full = sum(bodies)
        print(f"{k:>14} {1 + k:>8} {gate:>9,} {'':>7} {1:>8} {full:>12,}")

    breakeven = next(
        (k for k in range(len(sections) + 1)
         if price_list_tokens + sum(ordered[:k]) >= sum(bodies)),
        None,
    )
    print()
    print(f"the gate costs one extra round trip per section read: "
          f"{len(sections) + 1} against 1 to read everything")
    if breakeven is None:
        print("and it never costs more tokens than the full text, however much "
              "of it the caller ends up reading")
    else:
        print(f"and it stops saving tokens at {breakeven} of {len(sections)} "
              f"sections read. A caller that wants the whole report should ask "
              f"for the whole report -- the gate is for the caller that does not.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
