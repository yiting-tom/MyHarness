"""Spike #15: what does a character actually cost on this backend?

Golden #15's estimate ran 31% low for a lane reading ASCII query output and
54% low for one reading Chinese findings -- the more Chinese, the worse. The
coefficients in myharness/artifacts/tokens.py are wrong in both directions for
this tokenizer, so myharness/lanes/budget.py carries its own, and this
measures them.

Two rules, both learned the hard way:

- Use real text. A repeated filler string measures the tokenizer's merges, not
  the text: the first attempt at this reported 17 ASCII characters per token.
- Subtract a baseline taken the same way. What one request costs before any
  content is the framework's own prompt plus the charter, and it is large
  enough to swamp a small probe.

Run: set -a && . ./.env && set +a && python spikes/spike15_token_rates.py
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
from myharness.lanes.budget import (
    ASCII_CHARS_PER_TOKEN,
    CJK_TOKENS_PER_CHAR,
    split_chars,
)
from myharness.lanes.types import LaneInstance, LaneType
from myharness.lanes.worker import WorkerRequest, run_lane_worker

CHARTER = Path("charters/synthesizer.md")
PROBE_CHARS = 2_000


def pools() -> tuple[str, str]:
    """Real Chinese prose and real tabular data, from this repository."""
    prose = Path("docs/introduction.md").read_text()
    cjk = "".join(ch for ch in prose if ord(ch) > 127)[:PROBE_CHARS]
    csv = Path("tests/golden/fixtures/txn-2024.csv")
    if not csv.exists():  # fall back to whatever golden data is around
        csv = next(Path("jobs-scratch").rglob("blobs/raw/txn-2024"))
    return csv.read_text()[:PROBE_CHARS], cjk


async def probe(tag: str, pad: str) -> tuple[int, int]:
    profile = self_hosted_from_env()
    root = Path(tempfile.mkdtemp(prefix=f"mh-{tag}-"))
    store = LocalArtifactStore(root)
    await store.init_job("p")
    lane_type = LaneType(
        name="probe", charter_path=CHARTER, tools=("read_note", "write_finding"),
        model_tier="strong", backend=profile.name, token_budget=60_000, max_turns=1,
    )
    await run_lane_worker(
        WorkerRequest(job_id="p", lane=LaneInstance(id="probe", type=lane_type),
                      task=f"忽略以下內容，直接回 handle。{pad}", dispatch_id="d1"),
        store=store, event_log=LocalEventLog(root),
    )
    events = [json.loads(l) for l in
              (root / "jobs" / "p" / "events.jsonl").read_text().splitlines()]
    end = next(e for e in events if e["t"] == "dispatch.end")
    return end["tokens"]["in"], end["estimate"]["requests"]


async def main() -> int:
    if self_hosted_from_env() is None:
        print("HARNESS_PROXY_BASE_URL / _MODEL unset; nothing to measure")
        return 1

    ascii_pad, cjk_pad = pools()
    base_in, base_reqs = await probe("base", "")
    ascii_in, ascii_reqs = await probe("ascii", ascii_pad)
    cjk_in, cjk_reqs = await probe("cjk", cjk_pad)

    per_base = base_in / base_reqs
    ascii_rate = (ascii_in / ascii_reqs - per_base) / len(ascii_pad)
    cjk_rate = (cjk_in / cjk_reqs - per_base) / len(cjk_pad)

    print(f"baseline    {base_in:6,d} in over {base_reqs} requests "
          f"= {per_base:7,.0f}/request (framework + charter)")
    print(f"+{len(ascii_pad):,} ascii  {ascii_in:6,d} in over {ascii_reqs} requests")
    print(f"+{len(cjk_pad):,} cjk    {cjk_in:6,d} in over {cjk_reqs} requests")
    print()
    print(f"ascii   {1 / ascii_rate:5.2f} chars per token   "
          f"(budget.py says {ASCII_CHARS_PER_TOKEN})")
    print(f"cjk     {cjk_rate:5.2f} tokens per char   "
          f"(budget.py says {CJK_TOKENS_PER_CHAR})")

    charter_ascii, charter_cjk = split_chars(CHARTER.read_text())
    print()
    print(f"charter {CHARTER.name}: {charter_ascii:,} ascii + {charter_cjk:,} cjk")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
