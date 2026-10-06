"""`myharness compare` -- the golden question, asked of one agent and of the harness.

    myharness compare --backend self-hosted

"Low context" is a claim; this is the measurement. Two baselines: the naive
way to ask (the whole CSV in one prompt, no tools), and the real competitor --
one agent with the same SQL tools a lane gets and the whole task to itself.
All sides are scored the same way, by the two numbers the golden report must
contain.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from statistics import median

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock

from myharness.artifacts.ids import ArtifactId
from myharness.artifacts.local import LocalArtifactStore
from myharness.artifacts.types import GrantSet
from myharness.backends.profile import BackendProfile, ModelTier
from myharness.backends.profile import registry as backends
from myharness.events.log import LocalEventLog
from myharness.events.query import footprint
from myharness.goldens import (
    TASKS,
    GoldenTask,
    available,
    lane_types,
    put_task_blobs,
    run_golden,
)
from myharness.lanes.stream import RequestMeter
from myharness.lanes.transport import SdkTransport
from myharness.lanes.types import LaneInstance
from myharness.lanes.worker import WorkerRequest, run_lane_worker
from myharness.local_layout import DEFAULT_ROOT

BASELINE_TIMEOUT_S = 1800.0
BASELINE_MAX_OUTPUT = 8_192
#: The tool-using agent's limits: generous, so that it is the single context
#: that has to cope, not a lane's per-dispatch ceilings.
SOLO_MAX_TURNS = 60
SOLO_TOKEN_BUDGET = 1_000_000


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
    run: int = 0

    @property
    def correct(self) -> bool:
        return not self.missing and not self.note


async def run_baseline(backend: str, task: GoldenTask) -> Row:
    profile = backends.get(backend)
    goal = task.goal.replace("資料已上傳為 blob，", "資料附在下面，")
    prompt = goal + "".join(
        f"\n\n{b.name}:\n```csv\n{b.path.read_text(encoding='utf-8')}```" for b in task.blobs)
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
    row.missing = task.check("\n".join(texts))
    return row


async def run_tool_baseline(root: Path, backend: str, job_id: str, task: GoldenTask) -> Row:
    """One lane worker given the whole task: same model, tools and charter as
    the harness's analyst, no orchestrator, no other lanes."""
    store, events = LocalArtifactStore(root), LocalEventLog(root)
    await store.init_job(job_id)
    ids = await put_task_blobs(store, job_id, task)
    analyst = lane_types(backend=backend).get_type("tabular-analyst")
    lane = LaneInstance(id="solo", type=replace(
        analyst, max_turns=SOLO_MAX_TURNS, token_budget=SOLO_TOKEN_BUDGET))
    started = time.monotonic()
    handle = await run_lane_worker(
        WorkerRequest(job_id=job_id, lane=lane, task=f"{task.goal}\n\n{available(ids)}",
                      dispatch_id="solo", inputs=tuple(ids)),
        store=store, event_log=events,
    )
    report = handle.headline or ""
    if handle.artifact:
        report += "\n" + await store.read_note(
            ArtifactId.parse(handle.artifact), grants=GrantSet.unrestricted(job_id),
            max_tokens=100_000,
        )
    fp = footprint(await events.read(job_id))
    return Row(
        "單一 agent（SQL 工具）", fp["in"], fp["out"], fp["peak"], fp["peak_estimated"],
        time.monotonic() - started, task.check(report),
        "" if handle.ok else f"{handle.status}: {handle.headline}"[:200],
    )


async def run_harness(root: Path, backend: str, job_id: str, task: GoldenTask) -> Row:
    started = time.monotonic()
    result = await run_golden(root, job_id=job_id, backend=backend, task=task)
    fp = footprint(await LocalEventLog(root).read(job_id))
    return Row(
        "MyHarness", fp["in"], fp["out"], fp["peak"], fp["peak_estimated"],
        time.monotonic() - started,
        task.check(result.report_text),
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
        name = f"{r.name} #{r.run}" if r.run else r.name
        lines.append(f"| {name} | {verdict} | {peak} | {r.tokens_in:,} | "
                     f"{r.tokens_out:,} | {r.seconds:,.0f} |")
    return "\n".join(lines)


def summary(rows: list[Row]) -> str:
    """Median and range per side. One run each was all the earlier figures
    had, and a lane more or less moved a total by 10% (compare-live-5 vs 6)."""
    lines = []
    for name in dict.fromkeys(r.name for r in rows):
        runs = [r for r in rows if r.name == name]
        def spread(values: list[float]) -> str:
            return f"{median(values):,.0f}（{min(values):,.0f}–{max(values):,.0f}）"
        lines.append(
            f"{name}：{len(runs)} 次，答對 {sum(r.correct for r in runs)}；"
            f"峰值 {spread([r.peak for r in runs])}；"
            f"總輸入 {spread([r.tokens_in for r in runs])}；"
            f"總輸出 {spread([r.tokens_out for r in runs])}；"
            f"秒 {spread([r.seconds for r in runs])}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, prog: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__.splitlines()[0])
    parser.add_argument("--backend", default="openrouter")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT / "compare")
    parser.add_argument("--job-id", default=f"compare-{int(time.time())}")
    parser.add_argument("--only", nargs="+", choices=("paste", "tools", "harness"),
                        default=["paste", "tools", "harness"])
    parser.add_argument("--task", choices=sorted(TASKS), default="txn")
    parser.add_argument("--runs", type=int, default=1,
                        help="each side this many times; prints median and range")
    args = parser.parse_args(argv)

    task = TASKS[args.task]()
    rows = []
    for i in range(1, args.runs + 1):
        run = i if args.runs > 1 else 0
        job_id = f"{args.job_id}-{i}" if run else args.job_id
        if "paste" in args.only:
            rows.append(asyncio.run(run_baseline(args.backend, task)))
            rows[-1].run = run
        if "tools" in args.only:
            rows.append(asyncio.run(
                run_tool_baseline(args.root, args.backend, f"{job_id}-solo", task)))
            rows[-1].run = run
        if "harness" in args.only:
            rows.append(asyncio.run(run_harness(args.root, args.backend, job_id, task)))
            rows[-1].run = run
        print(f"run {i}/{args.runs} done", flush=True)
    print(table(rows))
    if args.runs > 1:
        print("\n" + summary(rows))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
