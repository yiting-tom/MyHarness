"""Spike #24: what does the A2A envelope add to a price list?

Spike #12 measured a classification at 8,991 input tokens of which 8,372 -- 93%
-- were the CLI's own base system prompt, and that number is why
`myharness/proxy/direct.py` exists. Change `expose-over-a2a` asks the same
question of the new boundary, and it matters more here than anywhere else: the
whole point of `analysis_result` is that it returns a summary and a per-section
price instead of the report, and a wrapper that costs more than the thing it
wraps would undo that in one step.

Measured against a real job rather than a mock: jobs-scratch holds finished
golden runs, and `AnalysisService.result` reads only the event log and the
store, so it answers for a job this process never ran (design.md D4). The A2A
side is built from the canonical proto's own field names -- Task, TaskStatus,
Artifact with `extensions` (spike #13's price-list mark) -- inside a JSON-RPC
envelope.

The comparison is against the MCP boundary's answer for the same job, because
that is what A2A has to be no worse than.

(Numbered 24, not 16: the change's tasks.md was written when 16 was free.)

Run: python spikes/spike24_a2a_overhead.py
     python spikes/spike24_a2a_overhead.py --root jobs-scratch/golden18 --job golden18
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from myharness.artifacts.tokens import estimate_tokens
from myharness.lanes.types import LaneRegistry, LaneType
from myharness.mcp.service import AnalysisService

PRICE_LIST_EXTENSION = "https://myharness.dev/a2a/ext/section-price-list/v1"


def newest_job() -> tuple[Path, str]:
    logs = sorted(Path("jobs-scratch").glob("*/jobs/*/events.jsonl"),
                  key=lambda p: p.stat().st_mtime)
    if not logs:
        raise SystemExit("no finished jobs in jobs-scratch; run a golden job first")
    job_dir = logs[-1].parent
    return job_dir.parent.parent, job_dir.name


async def mcp_answer(root: Path, job_id: str) -> dict:
    lanes = LaneRegistry(LaneType(name="unused", charter_path=Path("charters/critic.md")))
    service = AnalysisService(root, lanes=lanes, backend="self-hosted")
    return await service.result(job_id)


def as_a2a_task(job_id: str, answer: dict) -> dict:
    """The same price list, in the shapes a2a.proto actually declares.

    Field names from the proto, not from memory: Task{id, context_id, status,
    artifacts}, Artifact{artifact_id, name, description, parts, extensions}.
    The extension URI is the mark spike #13 established -- declared on the agent
    card with required=true, repeated here so a client sees it on the artifact.
    """
    sections = answer.get("sections") or []
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "id": f"task-{job_id}",
            "contextId": f"ctx-{job_id}",
            "status": {
                "state": "TASK_STATE_COMPLETED",
                "timestamp": "2026-09-11T00:00:00Z",
            },
            "artifacts": [{
                "artifactId": f"{job_id}-price-list",
                "name": "section price list",
                "description": (
                    "章節價目表，不是章節內容。每一節的 est_tokens 是讀它要花的"
                    "token；用 analysis_drill / 全文 skill 取需要的那幾節。"
                ),
                "extensions": [PRICE_LIST_EXTENSION],
                # The whole body, unabridged. Carrying less than MCP does would
                # make the envelope look cheap by measuring a smaller answer.
                "parts": [{"data": {k: v for k, v in answer.items() if k != "ok"}}],
            }],
            "metadata": {"revision": 0},
        },
    }


def measure(label: str, payload: dict) -> tuple[str, int, int]:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return label, len(text), estimate_tokens(text)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root")
    ap.add_argument("--job")
    args = ap.parse_args()

    if args.root and args.job:
        root, job_id = Path(args.root), args.job
    else:
        root, job_id = newest_job()
    print(f"job {job_id} under {root}\n")

    answer = await mcp_answer(root, job_id)
    if not answer.get("ok"):
        raise SystemExit(f"cannot read that job: {answer}")

    sections = answer.get("sections") or []
    body = {k: answer[k] for k in answer if k != "ok"}
    a2a = as_a2a_task(job_id, answer)
    assert json.dumps(a2a["result"]["artifacts"][0]["parts"][0]["data"],
                      sort_keys=True) == json.dumps(body, sort_keys=True), \
        "both boundaries must be carrying the same answer for this to mean anything"

    rows = [
        measure("the price list itself", body),
        measure("MCP: what analysis_result returns", answer),
        measure("A2A: the same, as a JSON-RPC Task", a2a),
    ]
    print(f"{'':38} {'chars':>8} {'est tokens':>11}")
    for label, chars, tokens in rows:
        print(f"{label:38} {chars:8,} {tokens:11,}")

    payload_tokens = rows[0][2]
    mcp_tokens = rows[1][2]
    a2a_tokens = rows[2][2]
    print()
    print(f"sections priced: {len(sections)}; "
          f"reading every one of them would cost "
          f"{sum(s.get('est_tokens', 0) for s in sections):,} tokens")
    print()
    print(f"MCP envelope: {mcp_tokens - payload_tokens:+,} tokens "
          f"({(mcp_tokens - payload_tokens) / mcp_tokens:.0%} of its response)")
    print(f"A2A envelope: {a2a_tokens - payload_tokens:+,} tokens "
          f"({(a2a_tokens - payload_tokens) / a2a_tokens:.0%} of its response)")
    print(f"A2A over MCP: {a2a_tokens - mcp_tokens:+,} tokens "
          f"({a2a_tokens / mcp_tokens:.2f}x)")
    print()
    print("spike #12, for scale: 8,372 of 8,991 input tokens were the CLI's own")
    print("system prompt -- 93% overhead, and the reason proxy/direct.py exists.")
    if (a2a_tokens - payload_tokens) / a2a_tokens > 0.5:
        print("\nA2A repeats that shape: the wrapper costs more than the answer.")
    else:
        print("\nNothing like it here. The A2A envelope is a fixed, small addition")
        print("to a response whose whole purpose is to stay small, and it does not")
        print("grow with the report -- the report is still not in it.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
