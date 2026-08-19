"""Spike #7 — 量測 lane worker 的實際固定成本（task 8.7）。

Part A（離線、零成本）：用錄音端點攔下真正的請求，比較裁切前後的 prefix token 數。
Part B（live）：同一條 lane 連跑兩次，量 prompt cache 命中率與單次成本。
"""
import os, sys, json, threading
os.environ.pop("ANTHROPIC_API_KEY", None)

import anyio
from pathlib import Path
from http.server import ThreadingHTTPServer

sys.path.insert(0, "spikes")
from spike02_litellm_passthrough import H, CAPTURED  # noqa: E402

from myharness.artifacts.local import LocalArtifactStore
from myharness.backends.profile import (BUILTIN_TOOLS, BackendCapability,
                                        BackendProfile, registry)
from myharness.events.log import LocalEventLog
from myharness.events.query import summarize
from myharness.lanes.types import LaneRegistry, LaneType
from myharness.lanes.worker import WorkerRequest, run_lane_worker

CHARTER = Path("charters/tabular-analyst.md")
TOOLS = ("read_note", "write_finding", "update_state", "localize_blob")


async def capture_request(trim: bool, tmp: Path) -> dict:
    """Run one worker against a recording endpoint and return the request body."""
    CAPTURED.clear()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    name = f"probe-{'trim' if trim else 'full'}"
    registry.register(BackendProfile(
        name=name, models={"mid": "probe-model"},
        capabilities=frozenset({BackendCapability.STRUCTURED_OUTPUT}),
        base_url=f"http://127.0.0.1:{port}", auth_token_env=None,
    ))
    store = LocalArtifactStore(tmp / name)
    await store.init_job("m")
    lanes = LaneRegistry(LaneType(name="ta", charter_path=CHARTER, backend=name,
                                  model_tier="mid", tools=TOOLS, max_turns=2))
    lane = lanes.create("l1", "ta")

    import myharness.lanes.worker as W
    original = W.BackendProfile.disallowed_for
    if not trim:
        W.BackendProfile.disallowed_for = staticmethod(lambda _: [])
    try:
        await run_lane_worker(
            WorkerRequest(job_id="m", lane=lane, task="測試", dispatch_id="d1"),
            store=store, event_log=LocalEventLog(tmp / name),
        )
    finally:
        W.BackendProfile.disallowed_for = original
        srv.shutdown()

    body = next((c["body"] for c in CAPTURED if c.get("path", "").startswith("/v1/messages")), None)
    return body or {}


def tokens(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False)) // 4


async def part_a(tmp: Path):
    print("=== Part A：固定 prefix 成本（離線，零成本）===")
    rows = []
    for trim in (False, True):
        body = await capture_request(trim, tmp)
        if not body:
            print(f"  {'裁切' if trim else '未裁切'}: 沒抓到請求")
            continue
        t_tools = tokens(body.get("tools", []))
        t_sys = tokens(body.get("system", []))
        rows.append((trim, len(body.get("tools", [])), t_tools, t_sys, t_tools + t_sys))
        print(f"  {'裁切後' if trim else '未裁切'}: tools={len(body.get('tools', [])):>2} "
              f"tool_tokens≈{t_tools:>6,}  system≈{t_sys:>5,}  prefix≈{t_tools + t_sys:>6,}")
    if len(rows) == 2:
        saved = rows[0][4] - rows[1][4]
        print(f"  → 每個 ephemeral worker 省下 ≈{saved:,} tokens（196k 的 {saved/196000:.1%}）")


async def part_b(tmp: Path):
    print("\n=== Part B：prompt cache 命中與單次成本（live）===")
    store = LocalArtifactStore(tmp / "live")
    await store.init_job("m")
    events = LocalEventLog(tmp / "live")
    lanes = LaneRegistry(LaneType(name="ta", charter_path=CHARTER, backend="openrouter",
                                  model_tier="strong", tools=TOOLS,
                                  token_budget=20_000, max_turns=6))
    lane = lanes.create("l1", "ta")
    for i in (1, 2):
        handle = await run_lane_worker(
            WorkerRequest(job_id="m", lane=lane,
                          task="用一句話說明什麼是離群值，寫進 finding 後回報 handle。",
                          dispatch_id=f"d{i}"),
            store=store, event_log=events,
        )
        print(f"  run{i}: status={handle.status} handle={len(handle.to_json())} chars")
    stream = await events.read("m")
    s = summarize(stream)
    for e in stream:
        if e.t == "dispatch.end":
            print(f"  {e.get('id')}: tokens={e.get('tokens')} usd={e.get('usd')} turns={e.get('turns')}")
    print(f"  總成本 ${s.total_usd:.5f} | 節流等待 {s.throttle_seconds}s | caveats {[c.kind for c in s.caveats]}")


async def main():
    tmp = Path("/tmp/measure"); tmp.mkdir(exist_ok=True)
    await part_a(tmp)
    if "--live" in sys.argv:
        await part_b(tmp)

anyio.run(main)
