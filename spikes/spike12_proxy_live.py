"""Spike #12: does a real classifier sort real payloads?

The offline test uses a keyword classifier, which proves the wiring and nothing
about whether a cheap model can actually tell a transaction extract from a KYC
document given only a routing table and twelve lines.

Two payloads, two lanes, one job, through the real MCP server over stdio.

Run: set -a && . ./.env && set +a && python spikes/spike12_proxy_live.py
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

os.environ.pop("ANTHROPIC_API_KEY", None)

TXNS = "\n".join(
    ["txn_id,ts,account,amount,channel"]
    + [f"T{i:03d},2024-0{i % 9 + 1}-1{i % 9},A{i % 5},{300 * (i % 7 + 1)},"
       f"{'atm' if i % 2 else 'app'}" for i in range(30)]
)
DOCS = "\n".join(
    ["doc_id,holder,id_number,issued,verified_at"]
    + [f"D{i:03d},持有人{i},A1{i:06d},2019-0{i % 9 + 1}-01,2024-02-1{i % 9}"
       for i in range(30)]
)

#: What the orchestrator would declare. The classifier sees only this.
ROUTING = [
    {"lane": "txn", "accepts": "交易明細、金流紀錄、消費紀錄"},
    {"lane": "kyc", "accepts": "身分證明、KYC 文件、開戶資料"},
]


def body(result) -> dict:
    return json.loads(result.content[0].text)


async def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="mh-proxy-"))
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
            job = body(await session.call_tool("analysis_start", {
                "task": "分析交易資料與 KYC 文件。先宣告 routing table，"
                        "lane txn 收交易明細，lane kyc 收身分文件。",
            }))
            job_id = job["job_id"]
            print(f"job {job_id}\n")

            # Wait for the orchestrator to publish a routing table. Without one
            # the proxy short-circuits, and the spike would prove nothing.
            print("waiting for a routing table…")
            revision, table_seen = None, False
            for _ in range(20):
                progress = body(await session.call_tool("analysis_poll", {
                    "job_id": job_id, "wait": 30,
                    **({"since": revision} if revision else {}),
                }))
                if not progress.get("ok"):
                    print("poll refused:", progress)
                    return 1
                revision = progress["revision"]
                for q in progress["pending_questions"]:
                    print(f"  answering {q['id']}: {q['text'][:70]}")
                    await session.call_tool("analysis_answer", {
                        "job_id": job_id, "question_id": q["id"],
                        "text": "請先呼叫 plan_update 宣告 routing table："
                                + json.dumps(ROUTING, ensure_ascii=False),
                    })
                if any("plan.update" in line for line in progress["recent"]):
                    table_seen = True
                    break
                if progress["state"] != "running":
                    break
            print(f"  [{time.monotonic()-started:5.1f}s] "
                  f"routing table published: {table_seen}\n")

            print("providing two payloads…")
            first = body(await session.call_tool("analysis_provide", {
                "job_id": job_id, "payload": TXNS, "name": "txn.csv"}))
            second = body(await session.call_tool("analysis_provide", {
                "job_id": job_id, "payload": DOCS, "name": "kyc.csv"}))
            for label, out in (("txn.csv", first), ("kyc.csv", second)):
                print(f"  {label:9s} routed={str(out['routed']):5s} "
                      f"-> {out['routed_to']}  "
                      f"({out.get('unrouted_because') or out['routing_reason'][:60]})")

    print(f"\n--- checks ---  ({time.monotonic()-started:.0f}s)")
    checks = [
        ("a routing table was published", table_seen),
        ("txn.csv routed to txn", first.get("routed_to") == "txn"),
        ("kyc.csv routed to kyc", second.get("routed_to") == "kyc"),
        ("both were announced", first["announced"] and second["announced"]),
        ("neither response carried the data",
         "T029" not in json.dumps(first) and "A1000029" not in json.dumps(second)),
    ]
    for label, good in checks:
        print(f"  {'PASS' if good else 'FAIL'}  {label}")
    if not table_seen:
        print("\n  (without a routing table the classifier short-circuits;"
              " routing checks are inconclusive rather than failed)")
    return 0 if all(g for _, g in checks) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
