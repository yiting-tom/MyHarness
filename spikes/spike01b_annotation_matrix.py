"""Spike #1b — 哪個 annotation 決定併發？併發上限是多少？

spike01 顯示 readOnlyHint 疑似是開關。這裡做矩陣隔離，並測併發度上限。
判定不看 turn 框架（SDK 會把同 turn 的多個 tool_use 拆成多個 AssistantMessage），
只看時間軸重疊。
"""
import os
os.environ.pop("ANTHROPIC_API_KEY", None)

import anyio, json, time
from mcp.types import ToolAnnotations
from claude_agent_sdk import (
    query, tool, create_sdk_mcp_server, ClaudeAgentOptions, ResultMessage,
)

SLEEP_S = 4.0
calls: list[dict] = []
T0 = None


def build(ann: ToolAnnotations):
    @tool("sleep_tool", "Sleep for the given number of seconds, then return the label.",
          {"label": str, "seconds": float}, annotations=ann)
    async def sleep_tool(args):
        global T0
        now = time.monotonic()
        if T0 is None:
            T0 = now
        rec = {"label": args["label"], "start": round(now - T0, 3)}
        await anyio.sleep(float(args["seconds"]))
        rec["end"] = round(time.monotonic() - T0, 3)
        calls.append(rec)
        return {"content": [{"type": "text", "text": f"slept for {args['label']}"}]}
    return sleep_tool


async def run(name: str, ann: ToolAnnotations, n: int):
    global calls, T0
    calls, T0 = [], None
    labels = "ABCDEFGH"[:n]
    server = create_sdk_mcp_server(name="spike", version="1.0.0", tools=[build(ann)])
    opts = ClaudeAgentOptions(
        model="sonnet",
        mcp_servers={"spike": server},
        allowed_tools=["mcp__spike__sleep_tool"],
        disallowed_tools=["Task", "Bash", "Read", "Edit", "Write", "Glob", "Grep",
                          "WebFetch", "WebSearch", "TodoWrite", "NotebookEdit"],
        strict_mcp_config=True, setting_sources=[],
        permission_mode="bypassPermissions", max_turns=n + 2,
        system_prompt="You are a test harness. Follow instructions exactly. No commentary.",
    )
    prompt = (f"Call mcp__spike__sleep_tool exactly {n} times with seconds={SLEEP_S}, "
              f"labels {', '.join(labels)}.\n"
              f"Issue ALL {n} tool calls together in a SINGLE response. They are independent.")

    t0 = time.monotonic()
    async for m in query(prompt=prompt, options=opts):
        if isinstance(m, ResultMessage):
            pass
    wall = time.monotonic() - t0

    cs = sorted(calls, key=lambda c: c["start"])
    span = max(c["end"] for c in cs) - min(c["start"] for c in cs) if cs else 0
    busy = sum(c["end"] - c["start"] for c in cs)
    conc = round(busy / span, 2) if span else 0
    # 最大同時在跑的數量
    events = sorted([(c["start"], 1) for c in cs] + [(c["end"], -1) for c in cs])
    cur = peak = 0
    for _, d in events:
        cur += d
        peak = max(peak, cur)
    return {"case": name, "n": n, "wall": round(wall, 1), "span": round(span, 1),
            "busy": round(busy, 1), "avg_concurrency": conc, "peak_concurrency": peak,
            "calls": cs}


CASES = [
    ("readOnly=T", ToolAnnotations(readOnlyHint=True,  destructiveHint=False, idempotentHint=True,  openWorldHint=False), 3),
    ("readOnly=T + destructive=T + idempotent=F",
                   ToolAnnotations(readOnlyHint=True,  destructiveHint=True,  idempotentHint=False, openWorldHint=True),  3),
    ("readOnly=F + idempotent=T",
                   ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True,  openWorldHint=False), 3),
    ("annotations=None", None, 3),
    ("readOnly=T, n=6 (找上限)",
                   ToolAnnotations(readOnlyHint=True,  destructiveHint=False, idempotentHint=True,  openWorldHint=False), 6),
]


async def main():
    out = []
    for name, ann, n in CASES:
        r = await run(name, ann, n)
        out.append(r)
        tl = "  ".join(f"{c['label']}[{c['start']:.1f}→{c['end']:.1f}]" for c in r["calls"])
        print(f"\n{name}  (n={n})")
        print(f"  {tl}")
        print(f"  span={r['span']}s busy={r['busy']}s  avg_conc={r['avg_concurrency']}  peak_conc={r['peak_concurrency']}")
        print(f"  → {'併發' if r['peak_concurrency'] > 1 else '循序'}")
    with open("spikes/spike01b_result.json", "w") as f:
        json.dump(out, f, indent=2)
    print("\n--- summary ---")
    for r in out:
        print(f"{r['case']:<42} peak_conc={r['peak_concurrency']}  span={r['span']}s")

anyio.run(main)
