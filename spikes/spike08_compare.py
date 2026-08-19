"""Run the same live assertions against both loops and print the comparison."""
from __future__ import annotations

import os, sys, time
os.environ.pop("ANTHROPIC_API_KEY", None)
sys.path.insert(0, "spikes")

import anyio
from pathlib import Path

from spike08_direct_loop import DirectLaneWorker

from myharness.artifacts.local import LocalArtifactStore
from myharness.backends.profile import OPENROUTER
from myharness.events.log import LocalEventLog
from myharness.events.query import summarize
from myharness.lanes.handle import MAX_HANDLE_CHARS, HandleStatus
from myharness.lanes.types import LaneRegistry, LaneType
from myharness.lanes.worker import WorkerRequest, run_lane_worker

CHARTER = Path("charters/tabular-analyst.md")
TOOLS = ("read_note", "write_finding", "update_state", "localize_blob")
LONG_TASK = ("請針對『表格資料分析的方法論』寫一份至少 3000 字的詳盡報告，"
             "涵蓋抽樣、異常偵測與信賴區間。寫得越詳細越好。")


def lane(root: Path, backend: str, budget: int, name: str):
    reg = LaneRegistry(LaneType(
        name="ta", charter_path=CHARTER, tools=TOOLS, model_tier="strong",
        backend=backend, token_budget=budget, max_turns=10, state_max_tokens=1500))
    return reg.create(name, "ta")


async def via_sdk(root: Path, job: str, task: str, budget: int):
    store = LocalArtifactStore(root); await store.init_job(job)
    events = LocalEventLog(root)
    t0 = time.monotonic()
    handle = await run_lane_worker(
        WorkerRequest(job_id=job, lane=lane(root, "openrouter", budget, "sdk"),
                      task=task, dispatch_id="d1"),
        store=store, event_log=events)
    wall = time.monotonic() - t0
    s = summarize(await events.read(job))
    end = [e for e in await events.read(job) if e.t == "dispatch.end"][-1]
    tok = end.get("tokens") or {}
    return {
        "handle": handle, "wall": round(wall, 1), "usd": s.total_usd,
        "in": tok.get("in", 0), "out": tok.get("out", 0),
        "cache_read": tok.get("cache_read", 0), "turns": end.get("turns", 0),
    }


async def via_direct(root: Path, job: str, task: str, budget: int):
    store = LocalArtifactStore(root); await store.init_job(job)
    events = LocalEventLog(root)
    worker = DirectLaneWorker(
        store=store, event_log=events, base_url=OPENROUTER.base_url,
        api_key=os.environ["OPENROUTER_KEY"],
        model=OPENROUTER.resolve_model("strong"), token_budget=budget, max_turns=10)
    handle = await worker.run(
        WorkerRequest(job_id=job, lane=lane(root, "openrouter", budget, "direct"),
                      task=task, dispatch_id="d1"))
    st = worker.stats
    return {
        "handle": handle, "wall": st.wall_s, "usd": 0.0,
        "in": st.tokens_in + st.cache_read, "out": st.tokens_out,
        "cache_read": st.cache_read, "turns": st.turns,
        "prefix": st.prefix_tokens, "tool_calls": st.tool_calls,
    }


async def scenario(name: str, task: str, budget: int, expect_ok: bool):
    print(f"\n{'='*66}\n{name}\n{'='*66}")
    rows = {}
    for label, fn, sub in (("SDK", via_sdk, "sdk"), ("Direct", via_direct, "direct")):
        root = Path(f"/tmp/cmp/{sub}-{budget}")
        try:
            with anyio.fail_after(420):
                rows[label] = await fn(root, "cmp", task, budget)
        except Exception as exc:
            print(f"  {label}: EXC {type(exc).__name__}: {str(exc)[:120]}")
            rows[label] = None
    for label, r in rows.items():
        if r is None:
            continue
        h = r["handle"]
        bounded = len(h.to_json()) <= MAX_HANDLE_CHARS
        ok = (h.status is HandleStatus.OK) if expect_ok else (h.status is not HandleStatus.OK)
        print(f"  {label:<7} status={str(h.status):<20} handle={len(h.to_json()):>4}ch "
              f"bounded={'✅' if bounded else '❌'} expected={'✅' if ok else '⚠️'}")
        print(f"  {'':<7} turns={r['turns']:<3} in={r['in']:>6,} out={r['out']:>5,} "
              f"cache_read={r['cache_read']:>6,} wall={r['wall']}s"
              + (f" prefix≈{r.get('prefix'):,}" if r.get("prefix") else ""))
    return rows


async def main():
    print(f"model = {OPENROUTER.resolve_model('strong')}")
    await scenario("A) 要求寫 3000 字報告 —— handle 必須守住上界", LONG_TASK, 60_000, True)
    await scenario("B) 預算不足 —— 必須回值而非例外", LONG_TASK, 900, False)

anyio.run(main)
