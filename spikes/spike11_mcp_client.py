"""Spike #11: drive the real server from a real MCP client, over a real pipe.

Everything else is tested in-process. This asks the only question those tests
cannot: does `myharness-mcp` work when a client spawns it as a subprocess and
talks to it over stdio, the way Claude Code will?

Small on purpose -- a 40-row CSV and one countable question, so the bill is
cents and the answer is checkable.

Run: set -a && . ./.env && set +a && python spikes/spike11_mcp_client.py
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# The shell's key is invalid and the SDK retries it ten times before failing.
os.environ.pop("ANTHROPIC_API_KEY", None)

ROWS = "\n".join(
    ["txn_id,ts,account,amount,channel"]
    + [f"T{i:03d},2024-01-{i % 28 + 1:02d}T10:00:00,A{i % 7},{100 * (i % 5 + 1)},"
       f"{'app' if i % 3 else 'atm'}"
       for i in range(40)]
)
EXPECTED_ACCOUNTS = 7


def body(result) -> dict:
    return json.loads(result.content[0].text)


async def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="mh-mcp-"))
    backend = os.environ.get("HARNESS_LIVE_BACKEND", "openrouter")
    params = StdioServerParameters(
        command=shutil.which("myharness-mcp") or "myharness-mcp",
        args=["--root", str(root), "--backend", backend],
        env=dict(os.environ),
    )
    started = time.monotonic()
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            listed = await session.list_tools()
            print(f"tools: {sorted(t.name for t in listed.tools)}\n")

            job = body(await session.call_tool("analysis_start", {
                "task": "分析提供的交易資料。報告中必須明確給出不重複帳戶的總數。",
            }))
            print("start:", job)
            job_id = job["job_id"]

            provided = body(await session.call_tool("analysis_provide", {
                "job_id": job_id, "payload": ROWS, "name": "txn.csv",
            }))
            print("provide:", provided, "\n")

            revision, last = None, None
            for _ in range(40):
                progress = body(await session.call_tool("analysis_poll", {
                    "job_id": job_id, "wait": 30, **({"since": revision} if revision else {}),
                }))
                if not progress.get("ok"):
                    print("poll refused:", progress)
                    break
                revision = progress["revision"]
                line = (f"[{time.monotonic()-started:6.1f}s] {progress['state']:9s} "
                        f"rev={revision:<3} dispatches={progress['dispatches']} "
                        f"${progress['spent_usd']:.4f} "
                        f"{progress['recent'][-1] if progress['recent'] else ''}")
                if line[20:] != (last or "")[20:]:
                    print(line[:150])
                    last = line
                for q in progress["pending_questions"]:
                    print(f"    answering {q['id']}: {q['text'][:80]}")
                    await session.call_tool("analysis_answer", {
                        "job_id": job_id, "question_id": q["id"],
                        "text": "使用提供的 txn.csv，不需要其他資料。",
                    })
                if progress["state"] != "running":
                    break

            print()
            result = body(await session.call_tool("analysis_result", {"job_id": job_id}))
            if not result.get("ok"):
                print("no result:", result)
                return 1
            encoded = json.dumps(result, ensure_ascii=False)
            print(f"result: {len(encoded)} chars, "
                  f"{len(result['sections'])} sections, "
                  f"{result['total_section_tokens']} section tokens")
            print("  summary:", result["executive_summary"][:200])
            for s in result["sections"]:
                print(f"    {s['id']}  {s['est_tokens']} tokens")

            drilled = None
            if result["sections"]:
                drilled = body(await session.call_tool("analysis_drill", {
                    "job_id": job_id, "section_id": result["sections"][0]["id"],
                }))
                print(f"\ndrill '{result['sections'][0]['id']}': "
                      f"{len(drilled.get('text',''))} chars, "
                      f"truncated={drilled.get('truncated')}")

    print(f"\n--- checks ---  ({time.monotonic()-started:.0f}s)")
    checks = [
        ("the report contains the computed account count",
         str(EXPECTED_ACCOUNTS) in encoded),
        ("the result payload stayed small", len(encoded) < 4000),
        ("the raw rows never came back", "T039" not in encoded),
        ("sections are priced", all(s["est_tokens"] > 0 for s in result["sections"])),
        ("drill returned a body", bool(drilled and drilled.get("text"))),
    ]
    for label, good in checks:
        print(f"  {'PASS' if good else 'FAIL'}  {label}")
    return 0 if all(g for _, g in checks) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
