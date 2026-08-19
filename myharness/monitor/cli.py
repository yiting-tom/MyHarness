"""`myharness monitor|inspect|jobs` — read-only views of a job.

Read-only in the strict sense (spec: Monitor 不影響被觀察的 job): starting,
stopping or crashing one of these must not perturb a job in flight, and none of
them writes anything anywhere.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from myharness.artifacts.local import LocalArtifactStore
from myharness.artifacts.types import ArtifactMeta
from myharness.dataflow import build_dataflow, detect
from myharness.events.log import LocalEventLog
from myharness.events.types import Event
from myharness.local_layout import JobLayout, find_jobs
from myharness.monitor.inspect import render_inspect
from myharness.monitor.live import LiveView
from myharness.monitor.render import colour_enabled, human_duration, pad, style

DEFAULT_ROOT = Path("jobs-scratch")
POLL_INTERVAL_S = 1.0


@dataclass(frozen=True, slots=True)
class JobRef:
    job_id: str
    events: int
    finished: bool
    last_activity: float
    root: Path


def discover(root: Path) -> list[JobRef]:
    """Every job at or below a root, newest activity first.

    Path composition lives in local_layout (design.md D6); this only reads.
    """
    found: list[JobRef] = []
    for layout in find_jobs(root):
        path = layout.events_path
        lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        finished = any('"job.finish"' in l for l in lines[-5:])
        found.append(JobRef(layout.job_id, len(lines), finished,
                            path.stat().st_mtime, layout.root))
    return sorted(found, key=lambda j: -j.last_activity)


def resolve_root(root: Path, job_id: str) -> Path:
    """Where this job actually lives, given a root the user guessed at."""
    for job in discover(root):
        if job.job_id == job_id:
            return job.root
    return root


async def load(root: Path, job_id: str) -> tuple[list[Event], Sequence[ArtifactMeta]]:
    events = list(await LocalEventLog(root).read(job_id))
    try:
        artifacts = await LocalArtifactStore(root).list(job_id)
    except Exception:
        artifacts = ()
    return events, artifacts


def term_width(default: int = 78) -> int:
    return min(shutil.get_terminal_size((default, 24)).columns, 100)


# --- commands -------------------------------------------------------------


def cmd_jobs(args) -> int:
    jobs = discover(args.root)
    if not jobs:
        print(f"{args.root} 下沒有 job")
        return 1
    nested = {j.root for j in jobs} != {args.root}
    colour = colour_enabled()
    print(style(f"  {pad('job', 22)}{pad('狀態', 10)}{pad('事件', 8, 'right')}"
                f"  最後活動", "dim", enabled=colour))
    now = time.time()
    for job in jobs:
        state = "完成" if job.finished else "執行中"
        line = (f"  {pad(job.job_id, 22)}{pad(state, 10)}"
                f"{pad(str(job.events), 8, 'right')}"
                f"  {human_duration(now - job.last_activity)} 前")
        if nested:
            line += style(f"   {job.root}", "dim", enabled=colour)
        print(line)
    return 0


def cmd_inspect(args) -> int:
    root = resolve_root(args.root, args.job)
    events, artifacts = asyncio.run(load(root, args.job))
    if not events:
        known = ", ".join(j.job_id for j in discover(args.root)) or "（無）"
        print(f"{args.root} 下找不到 job {args.job}；已知的 job：{known}")
        return 1
    flow = build_dataflow(events, artifacts, job_id=args.job)

    if args.json:
        print(json.dumps(
            {**flow.to_dict(), "anomalies": [a.to_dict() for a in detect(flow)]},
            ensure_ascii=False, indent=2))
        return 0

    print(render_inspect(flow, events, colour=colour_enabled(),
                         width=term_width()))
    return 0 if not any(a.severity == "critical" for a in detect(flow)) else 2


def cmd_monitor(args) -> int:
    """Redraw until the job finishes, then leave the final frame on screen."""
    root = resolve_root(args.root, args.job)
    view = LiveView(args.job)
    colour = colour_enabled()
    width = term_width()
    interactive = sys.stdout.isatty()
    previous_lines = 0

    try:
        while True:
            events, artifacts = asyncio.run(load(root, args.job))
            frame = view.render(events, artifacts, colour=colour, width=width)

            if interactive and previous_lines:
                sys.stdout.write(f"\033[{previous_lines}A\033[J")
            sys.stdout.write(frame + "\n")
            sys.stdout.flush()
            previous_lines = frame.count("\n") + 1

            if any(e.t == "job.finish" for e in events):
                return 0
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()
        return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="myharness", description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT,
                        help="job 儲存根目錄")
    sub = parser.add_subparsers(dest="command", required=True)

    jobs = sub.add_parser("jobs", help="列出可觀察的 job")
    jobs.set_defaults(func=cmd_jobs)

    inspect = sub.add_parser("inspect", help="展開一個 job 的資料流")
    inspect.add_argument("job")
    inspect.add_argument("--json", action="store_true", help="結構化輸出")
    inspect.set_defaults(func=cmd_inspect)

    monitor = sub.add_parser("monitor", help="即時跟蹤一個執行中的 job")
    monitor.add_argument("job")
    monitor.add_argument("--interval", type=float, default=POLL_INTERVAL_S)
    monitor.add_argument("--once", action="store_true", help="只畫一次就結束")
    monitor.set_defaults(func=cmd_monitor)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
