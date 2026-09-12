"""Spike #14: what does one SDK lane turn cost before any conversation?

Golden #13 estimated high and #14 estimated low with the same expression. Two
errors pulling opposite ways: the estimate adds a turn's cost per streamed
AssistantMessage (there were 27 for 13 actual API calls), and it counts only
conversation text, ignoring everything re-sent on every request -- the CLI's
own system prompt, the charter, and the tool definitions.

The second one is a constant, and a constant can be measured once and
declared. This measures it: one dispatch, one turn, a task small enough that
the reported input is almost entirely overhead.

Run: set -a && . ./.env && set +a && python spikes/spike14_turn_overhead.py
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
from myharness.lanes.types import LaneInstance, LaneType
from myharness.lanes.worker import WorkerRequest, run_lane_worker

#: Small enough that the reported input is overhead plus a rounding error.
TASK = "回答 handle 即可，不要使用任何工具。"


async def main() -> int:
    profile = self_hosted_from_env()
    if profile is None:
        print("HARNESS_PROXY_BASE_URL / _MODEL unset; nothing to measure")
        return 1

    root = Path(tempfile.mkdtemp(prefix="mh-overhead-"))
    store = LocalArtifactStore(root)
    job = "overhead"
    await store.init_job(job)
    log = LocalEventLog(root)

    charter_path = Path("charters/tabular-analyst.md")
    lane_type = LaneType(
        name="probe", charter_path=charter_path,
        tools=("read_note", "write_finding"), model_tier="strong",
        backend=profile.name, token_budget=60_000, max_turns=1,
    )
    lane = LaneInstance(id="probe", type=lane_type)

    handle = await run_lane_worker(
        WorkerRequest(job_id=job, lane=lane, task=TASK, dispatch_id="d1"),
        store=store, event_log=log,
    )

    events = [json.loads(l) for l in (root / "jobs" / job / "events.jsonl").read_text().splitlines()]
    end = next(e for e in events if e["t"] == "dispatch.end")
    tokens = end["tokens"]

    # One API request goes out for the opening prompt and one more each time a
    # tool result comes back, so the user messages count the round trips.
    trace = (root / "jobs" / job / "blobs" / "traces" / "d1").read_text().splitlines()
    messages = [json.loads(l) for l in trace if l.strip()]
    requests = 1 + sum(1 for m in messages if m.get("role") == "user")
    convo = sum(
        len(b.get("text") or "") + len(json.dumps(b.get("input") or {}, ensure_ascii=False))
        for m in messages for b in (m.get("content") or [])
    )
    charter = len(charter_path.read_text())

    print(f"status         {handle.status}")
    print(f"turns          {end['turns']}")
    print(f"reported in    {tokens['in']:,}")
    print(f"reported out   {tokens['out']:,}")
    print(f"estimated?     {tokens.get('estimated', False)}")
    print()
    print(f"charter        {charter:,} chars  ~{charter // 4:,} tokens")
    print(f"task           {len(TASK):,} chars  ~{len(TASK) // 4:,} tokens")
    print()
    print(f"API requests   {requests}")
    print(f"conversation   {convo:,} chars  ~{convo // 4:,} tokens (all of it, every request)")
    print()
    overhead = (tokens["in"] - requests * (convo // 4)) / requests
    print(f"→ fixed per-request overhead ≈ {overhead:,.0f} tokens")
    print("  = (reported in - requests x conversation) / requests")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
