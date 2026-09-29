"""`myharness run` -- one analysis from a terminal, through the same service as MCP.

    myharness run "找出 2024 年交易的異常樣態" goldens/txn-2024.csv

It is an MCP client with a keyboard: start, provide each file, poll until the
job ends, answer whatever the orchestrator asks, then print the summary. Going
through ``AnalysisService`` rather than wiring a runner by hand (as goldens.py
does, to pin its bounds) means what you see here is what an MCP client gets.

The job runs in this process: Ctrl-C stops it. Its events stay on disk, so
``myharness inspect <job_id>`` still works afterwards.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from myharness.local_layout import DEFAULT_ROOT
from myharness.mcp.service import AnalysisService

#: Said on the user's behalf when nobody is at the keyboard. An unanswered
#: question holds the job until the question times out, so silence is not free.
NO_INPUT_ANSWER = "沒有額外資訊，請依你的最佳判斷繼續。"

Ask = Callable[[dict[str, Any]], Awaitable[str]]


async def ask_terminal(question: dict[str, Any]) -> str:
    # In a thread: the job runs on this event loop, and a blocking input()
    # would freeze the very analysis that is waiting for the answer.
    text = await asyncio.to_thread(input, f"\n? {question['text']}\n> ")
    return text.strip() or NO_INPUT_ANSWER


async def ask_nobody(question: dict[str, Any]) -> str:
    print(f"\n? {question['text']}\n> {NO_INPUT_ANSWER}（非互動模式，自動回答）")
    return NO_INPUT_ANSWER


async def drive(
    service: AnalysisService,
    task: str,
    files: list[Path],
    *,
    ask: Ask,
    job_id: str | None = None,
    out: Path | None = None,
    wait: float = 20.0,
) -> int:
    started = await service.start(task, job_id=job_id)
    if not started.get("ok"):
        print(f"無法開始：{started.get('message')}", file=sys.stderr)
        return 1
    job = started["job_id"]
    print(f"job {job}（另開終端機：myharness monitor {job}）")

    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # provide() carries text, as analysis_provide does over MCP.
            print(f"{path} 不是 UTF-8 文字檔；目前只收文字（例如 CSV、JSON）",
                  file=sys.stderr)
            return 1
        provided = await service.provide(job, text, name=path.stem)
        if not provided.get("ok"):
            print(f"{path} 上傳失敗：{provided.get('message')}", file=sys.stderr)
            return 1
        print(f"已提供 {path}（{provided['bytes']:,} bytes）：{provided['note']}")

    seen: set[str] = set()
    answered: set[str] = set()
    revision: int | None = None
    while True:
        progress = await service.poll(job, wait=wait, since=revision)
        if not progress.get("ok"):
            break  # not_running: the job ended between two polls
        revision = progress["revision"]
        for line in progress["recent"]:
            if line not in seen:
                seen.add(line)
                print(f"  {line}")
        for question in progress["pending_questions"]:
            if question["id"] not in answered:
                answered.add(question["id"])
                await service.answer(job, question["id"], await ask(question))
        if progress["state"] != "running":
            break

    result = await service.result(job)
    if not result.get("ok"):
        print(f"\n沒有報告：{result.get('message')}", file=sys.stderr)
        return 1
    print(f"\n=== {result.get('status', '')} ===\n{result.get('executive_summary', '')}")
    for finding in result.get("key_findings") or ():
        print(f"- {finding}")
    for caveat in result.get("caveats") or ():
        print(f"! {caveat.get('detail', caveat.get('kind'))}")
    cost = result.get("cost") or {}
    print(f"（{cost.get('dispatches', 0)} 次派工，${cost.get('usd', 0.0):.4f}）")

    sections = result.get("sections") or []
    if out is not None:
        parts = []
        for section in sections:
            drilled = await service.drill_section(job, str(section["id"]))
            if drilled.get("ok"):
                parts.append(drilled["text"])
        out.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
        print(f"\n完整報告（{len(parts)} 節）寫到 {out}")
    elif sections:
        print(f"\n報告共 {len(sections)} 節；加 -o report.md 取得全文，"
              f"或 myharness report {job} -o report.html")
    # A salvaged report is written by code, not by an analysis. Exiting 0 on it
    # told scripts a job had succeeded when nothing was analysed.
    salvaged = any(c.get("kind") == "salvaged" for c in result.get("caveats") or ())
    return 0 if result.get("report_artifact") and not salvaged else 1


def main(argv: list[str] | None = None, *, prog: str | None = None) -> int:
    from myharness.mcp.server import DEFAULT_CHARTERS, default_lanes

    parser = argparse.ArgumentParser(prog=prog, description="從終端機跑一次分析。")
    parser.add_argument("task", help="要分析什麼，用一句話說")
    parser.add_argument("files", nargs="*", type=Path, help="要提供的資料檔（文字，例如 CSV）")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--charters", type=Path, default=DEFAULT_CHARTERS)
    parser.add_argument("--backend", default="openrouter")
    parser.add_argument("--job-id", default=None)
    parser.add_argument("-o", "--out", type=Path, help="把完整報告寫到這個檔案")
    parser.add_argument("--no-input", action="store_true",
                        help="不問人：orchestrator 的提問一律自動回答"
                             "（stdin 不是終端機時預設如此）")
    args = parser.parse_args(argv)

    missing = [str(p) for p in args.files if not p.is_file()]
    if missing:
        parser.error(f"找不到檔案：{', '.join(missing)}")

    ask = ask_nobody if args.no_input or not sys.stdin.isatty() else ask_terminal
    service = AnalysisService(
        args.root, lanes=default_lanes(args.charters, args.backend), backend=args.backend
    )

    async def run() -> int:
        try:
            return await drive(service, args.task, args.files, ask=ask,
                               job_id=args.job_id, out=args.out)
        finally:
            await service.aclose()

    with contextlib.suppress(KeyboardInterrupt):  # operator action, not a failure
        return asyncio.run(run())
    return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
