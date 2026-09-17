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

from myharness.artifacts.ids import ArtifactId
from myharness.artifacts.local import LocalArtifactStore
from myharness.artifacts.types import ArtifactMeta, GrantSet
from myharness.dataflow import DataFlow, build_dataflow, detect
from myharness.events.log import LocalEventLog
from myharness.events.types import Event
from myharness.local_layout import find_jobs
from myharness.monitor.inspect import render_inspect
from myharness.monitor.live import LiveView
from myharness.monitor.render import colour_enabled, human_duration, pad, style
from myharness.monitor.report import render_report
from myharness.monitor.serve import serve
from myharness.monitor.trace import Trace, parse_trace
from myharness.monitor.viewer import render_html
from myharness.orchestrator.delivery import build_delivery, drill

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
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        finished = any('"job.finish"' in ln for ln in lines[-5:])
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
    except Exception:  # noqa: BLE001 - a job with no artifacts still has events
        artifacts = ()
    return events, artifacts


async def load_traces(root: Path, job_id: str, flow: DataFlow) -> dict[str, Trace]:
    """Each dispatch's stored transcript, by dispatch id.

    The id comes off ``dispatch.end``; the bytes come through the store, not off
    a composed path (design.md D6). A transcript is a blob, so the grant used is
    the harness's own -- reporting is exactly what ``unrestricted`` is for. A
    dispatch whose transcript is missing is skipped rather than fatal: a run
    killed mid-stream never got to write one.
    """
    store = LocalArtifactStore(root)
    grants = GrantSet.unrestricted(job_id)
    traces: dict[str, Trace] = {}
    for dispatch in flow.dispatches.values():
        if not dispatch.transcript:
            continue
        try:
            async with store.localize(ArtifactId.parse(dispatch.transcript),
                                      grants=grants) as path:
                text = path.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001 - a missing transcript is not a reason to fail
            continue
        traces[dispatch.id] = parse_trace(
            [json.loads(line) for line in text.splitlines() if line.strip()]
        )
    return traces


def term_width(default: int = 78) -> int:
    return min(shutil.get_terminal_size((default, 24)).columns, 100)


# --- commands -------------------------------------------------------------


def cmd_jobs(args: argparse.Namespace) -> int:
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


def cmd_inspect(args: argparse.Namespace) -> int:
    root = resolve_root(args.root, args.job)
    events, artifacts = asyncio.run(load(root, args.job))
    if not events:
        known = ", ".join(j.job_id for j in discover(args.root)) or "（無）"
        print(f"{args.root} 下找不到 job {args.job}；已知的 job：{known}")
        return 1
    flow = build_dataflow(events, artifacts, job_id=args.job)

    critical = any(a.severity == "critical" for a in detect(flow))

    if args.json:
        print(json.dumps(
            {**flow.to_dict(), "anomalies": [a.to_dict() for a in detect(flow)]},
            ensure_ascii=False, indent=2))
        return 0

    if args.html:
        html = render_html(flow, events,
                           asyncio.run(load_traces(root, args.job, flow)))
        # Never into the job store by default: the page embeds excerpts of
        # transcripts, and a transcript is a blob whose whole contract is that
        # it does not leak (spec: 輸出位置須被指定).
        if args.out:
            args.out.write_text(html, encoding="utf-8")
        else:
            sys.stdout.write(html)
        return 2 if critical else 0

    print(render_inspect(flow, events, colour=colour_enabled(),
                         width=term_width()))
    return 2 if critical else 0


async def _provenance(root: Path, job_id: str, flow: DataFlow,
                      events: Sequence[Event]) -> str:
    """The report for whoever handed over the data.

    `build_delivery` is reused rather than reimplemented: it is already the
    contract this harness offers the outside world, and a second summary
    written for the page would be a second truth that drifts.
    """
    store = LocalArtifactStore(root)
    delivery = (await build_delivery(
        store=store, events=events, job_id=job_id,
        status="complete" if flow.finished else "running",
        report_artifact=flow.report_artifact,
    )).to_dict()

    sections = []
    for section in delivery.get("sections") or ():
        text = ""
        if flow.report_artifact:
            try:
                text = await drill(store, job_id, flow.report_artifact,
                                   str(section["id"]), max_tokens=1_000_000)
            except Exception:  # noqa: BLE001 - a section that will not open is
                text = ""      # shown as empty, not as a crashed report
        sections.append({**section, "text": text})

    return render_report(flow, events, delivery, sections=sections)


def cmd_report(args: argparse.Namespace) -> int:
    root = resolve_root(args.root, args.job)
    events, artifacts = asyncio.run(load(root, args.job))
    if not events:
        known = ", ".join(j.job_id for j in discover(args.root)) or "（無）"
        print(f"{args.root} 下找不到 job {args.job}；已知的 job：{known}")
        return 1
    flow = build_dataflow(events, artifacts, job_id=args.job)
    html = asyncio.run(_provenance(root, args.job, flow, events))
    if args.out:
        args.out.write_text(html, encoding="utf-8")
    else:
        sys.stdout.write(html)
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    """Redraw until the job finishes, then leave the final frame on screen."""
    root = resolve_root(args.root, args.job)
    if args.web:
        return _serve_web(root, args)
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


def _serve_web(root: Path, args: argparse.Namespace) -> int:
    """Hold the terminal open while the browser watches.

    The server is the only thing in this package that listens on a port, so the
    process that started it is the one that has to be killed to stop it -- no
    daemon, no pidfile, nothing left listening after Ctrl-C.
    """
    known = [j.job_id for j in discover(args.root)]
    if args.job not in known and not args.wait:
        # A page that can only ever say "not found" is a worse way to learn the
        # name was wrong than a line here. --wait is for starting the monitor
        # before the job, which is legitimate.
        print(f"{args.root} 下找不到 job {args.job}；已知的 job："
              f"{', '.join(known) or '（無）'}")
        print("如果這個 job 還沒開始，加上 --wait 先把監看頁開起來。")
        return 1
    httpd, url = serve(root, args.job, host=args.host, port=args.port)
    print(f"monitor: {url}")
    print("Ctrl-C 停止")
    try:
        while True:
            time.sleep(POLL_INTERVAL_S)
    except KeyboardInterrupt:
        print()
        return 130
    finally:
        httpd.shutdown()
        httpd.server_close()


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
    inspect.add_argument("--html", action="store_true",
                         help="逐輪軌跡視圖（單一自包檔案，預設寫到 stdout）")
    inspect.add_argument("-o", "--out", type=Path,
                         help="--html 的輸出檔案；不給就寫到 stdout")
    inspect.set_defaults(func=cmd_inspect)

    report = sub.add_parser("report", help="給資料提供者的來源報告（HTML）")
    report.add_argument("job")
    report.add_argument("-o", "--out", type=Path,
                        help="輸出檔案；不給就寫到 stdout")
    report.set_defaults(func=cmd_report)

    monitor = sub.add_parser("monitor", help="即時跟蹤一個執行中的 job")
    monitor.add_argument("job")
    monitor.add_argument("--interval", type=float, default=POLL_INTERVAL_S)
    monitor.add_argument("--once", action="store_true", help="只畫一次就結束")
    monitor.add_argument("--web", action="store_true",
                         help="改在瀏覽器裡看（起一個只綁 loopback 的唯讀 server）")
    monitor.add_argument("--wait", action="store_true",
                         help="--web：job 還不存在也先開頁面，等它出現")
    monitor.add_argument("--host", default="127.0.0.1",
                         help="--web 的繫結位址；非 loopback 會被拒絕")
    monitor.add_argument("--port", type=int, default=0,
                         help="--web 的 port；0 表示讓系統挑一個")
    monitor.set_defaults(func=cmd_monitor)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    exit_code: int = args.func(args)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
