"""`myharness compare` -- the golden question, asked of one agent and of the harness.

    myharness compare --backend self-hosted

"Low context" is a claim; this is the measurement. The baseline is the naive
way to ask: the whole CSV in one prompt, one request, no tools. Both sides are
scored the same way -- the two numbers the golden report must contain -- and
measured by what the backend reported per request, so neither side's figure
is an estimate the other's is not.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock

from myharness.backends.profile import BackendProfile, ModelTier
from myharness.backends.profile import registry as backends
from myharness.events.log import LocalEventLog
from myharness.events.query import footprint
from myharness.goldens import GOAL, GOLDEN_CSV, ground_truth, run_golden
from myharness.lanes.stream import RequestMeter
from myharness.lanes.transport import SdkTransport
from myharness.local_layout import DEFAULT_ROOT

BASELINE_TIMEOUT_S = 1800.0
BASELINE_MAX_OUTPUT = 8_192


@dataclass
class Row:
    name: str
    tokens_in: int = 0
    tokens_out: int = 0
    peak: int = 0
    peak_estimated: bool = False
    seconds: float = 0.0
    missing: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def correct(self) -> bool:
        return not self.missing and not self.note


async def run_baseline(backend: str, csv_path: Path = GOLDEN_CSV) -> Row:
    profile = backends.get(backend)
    goal = GOAL.replace("資料已上傳為 blob，", "資料附在下面，")
    prompt = f"{goal}\n\n```csv\n{csv_path.read_text(encoding='utf-8')}```"
    options = ClaudeAgentOptions(
        model=profile.resolve_model(ModelTier.STRONG), max_turns=1,
        allowed_tools=[], disallowed_tools=BackendProfile.disallowed_for(()),
        # Claude Code reserves 32,000 output tokens by default, which alone
        # pushed the golden CSV one token past a 64k window. The baseline is
        # meant to be the naive approach given a fair chance, not a strawman.
        env={**profile.to_sdk_env(), "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(BASELINE_MAX_OUTPUT)},
    )
    row, meter, texts = Row("單一 agent（CSV 全文進 prompt）"), RequestMeter(), []
    started = time.monotonic()
    try:
        async with asyncio.timeout(BASELINE_TIMEOUT_S):
            async for message in SdkTransport().stream(prompt, options):
                if isinstance(message, AssistantMessage):
                    meter.add(message)
                    texts += [b.text for b in message.content if isinstance(b, TextBlock)]
                elif isinstance(message, ResultMessage) and message.is_error:
                    row.note = f"error: {message.subtype}"
    except Exception as exc:  # noqa: BLE001 - a baseline that cannot answer is a result
        text = str(exc)
        if over := re.search(r"prompt contains at least (\d+) input tokens", text):
            row.note = f"放不進 context window：prompt 至少 {int(over[1]):,} token"
        else:
            row.note = f"{type(exc).__name__}: {text.splitlines()[0][:200] if text else ''}"
    row.seconds = time.monotonic() - started
    row.tokens_in, row.tokens_out, row.peak = meter.tokens_in, meter.tokens_out, meter.peak
    row.missing = ground_truth(csv_path).missing_from("\n".join(texts))
    return row


async def run_harness(root: Path, backend: str, job_id: str) -> Row:
    started = time.monotonic()
    result = await run_golden(root, job_id=job_id, backend=backend)
    fp = footprint(await LocalEventLog(root).read(job_id))
    return Row(
        "MyHarness", fp["in"], fp["out"], fp["peak"], fp["peak_estimated"],
        time.monotonic() - started,
        ground_truth().missing_from(result.report_text),
        "" if result.delivery.report_artifact and not result.outcome.salvaged
        else f"phase={result.outcome.phase} salvaged={result.outcome.salvaged}",
    )


def table(rows: list[Row]) -> str:
    lines = ["| | 答對 | 單一 context 峰值 | 總輸入 token | 總輸出 token | 秒 |",
             "|---|---|---:|---:|---:|---:|"]
    for r in rows:
        why = [r.note] if r.note else r.missing
        verdict = "是" if r.correct else f"否（{'；'.join(why)}）"
        peak = f"{'≈' if r.peak_estimated else ''}{r.peak:,}"
        lines.append(f"| {r.name} | {verdict} | {peak} | {r.tokens_in:,} | "
                     f"{r.tokens_out:,} | {r.seconds:,.0f} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, prog: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__.splitlines()[0])
    parser.add_argument("--backend", default="openrouter")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT / "compare")
    parser.add_argument("--job-id", default=f"compare-{int(time.time())}")
    parser.add_argument("--only", choices=("baseline", "harness"))
    args = parser.parse_args(argv)

    rows = []
    if args.only != "harness":
        rows.append(asyncio.run(run_baseline(args.backend)))
    if args.only != "baseline":
        rows.append(asyncio.run(run_harness(args.root, args.backend, args.job_id)))
    print(table(rows))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
