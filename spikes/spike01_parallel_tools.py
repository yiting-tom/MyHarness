"""Spike #1 — 同一 turn 內多個 custom MCP tool call 是併發還是循序？

決策 #15 (dispatch 阻塞、平行度靠 LLM 在同一 turn 發多個 tool call) 完全建立在
「併發」這個假設上。若為循序，lane 會排隊跑，必須改回非阻塞 + check_tasks(wait)。

測法：sleep_tool(label, seconds) 記錄每次呼叫的 start/end（單調時鐘），
要求模型在單一 response 內發 N 個 call，然後看區間有沒有重疊。

同時測 readOnlyHint=True / False —— dispatch 不是唯讀，若只有唯讀工具會被
併發化，那決策 #15 一樣翻盤。
"""
import os
os.environ.pop("ANTHROPIC_API_KEY", None)  # env 裡那把是無效的，讓子行程走 claude.ai 登入

import anyio, json, sys, time
from mcp.types import ToolAnnotations
from claude_agent_sdk import (
    query, tool, create_sdk_mcp_server, ClaudeAgentOptions,
    AssistantMessage, ToolUseBlock, TextBlock, ResultMessage,
)

N_CALLS = 3
SLEEP_S = 5.0

calls: list[dict] = []
T0 = None


def make_tool(read_only: bool):
    ann = ToolAnnotations(readOnlyHint=read_only, destructiveHint=False,
                          idempotentHint=True, openWorldHint=False)

    @tool("sleep_tool", "Sleep for the given number of seconds, then return the label.",
          {"label": str, "seconds": float}, annotations=ann)
    async def sleep_tool(args):
        global T0
        now = time.monotonic()
        if T0 is None:
            T0 = now
        rec = {"label": args["label"], "start": now - T0}
        await anyio.sleep(float(args["seconds"]))
        rec["end"] = time.monotonic() - T0
        calls.append(rec)
        return {"content": [{"type": "text", "text": f"slept {args['seconds']}s for {args['label']}"}]}

    return sleep_tool


async def run(read_only: bool):
    global calls, T0
    calls, T0 = [], None

    server = create_sdk_mcp_server(name="spike", version="1.0.0", tools=[make_tool(read_only)])
    opts = ClaudeAgentOptions(
        model="sonnet",
        mcp_servers={"spike": server},
        allowed_tools=["mcp__spike__sleep_tool"],
        disallowed_tools=["Task", "Bash", "Read", "Edit", "Write", "Glob", "Grep",
                          "WebFetch", "WebSearch", "TodoWrite", "NotebookEdit"],
        strict_mcp_config=True,
        setting_sources=[],
        permission_mode="bypassPermissions",
        max_turns=4,
        system_prompt=("You are a test harness. Follow the instruction exactly. "
                       "Do not explain, do not think out loud, just make the tool calls."),
    )

    prompt = (
        f"Call mcp__spike__sleep_tool exactly {N_CALLS} times with seconds={SLEEP_S}, "
        f"using labels A, B, C.\n"
        f"IMPORTANT: issue all {N_CALLS} tool calls together in a SINGLE response, "
        f"not one at a time. They are independent and must run at once."
    )

    turns: list[int] = []   # tool_use blocks per assistant message
    wall0 = time.monotonic()
    async for msg in query(prompt=prompt, options=opts):
        if isinstance(msg, AssistantMessage):
            n = sum(1 for b in msg.content if isinstance(b, ToolUseBlock))
            if n:
                turns.append(n)
        elif isinstance(msg, ResultMessage):
            pass
    wall = time.monotonic() - wall0

    return {"read_only": read_only, "wall_s": round(wall, 2),
            "tool_use_blocks_per_turn": turns,
            "calls": sorted(calls, key=lambda c: c["start"])}


def verdict(r):
    cs = r["calls"]
    if not cs:
        return "NO CALLS — 測試無效"
    if not any(n >= 2 for n in r["tool_use_blocks_per_turn"]):
        return f"模型未在單一 turn 發多個 call（每 turn: {r['tool_use_blocks_per_turn']}）— 測試無效，需改 prompt"
    span = max(c["end"] for c in cs) - min(c["start"] for c in cs)
    busy = sum(c["end"] - c["start"] for c in cs)
    overlap = busy / span if span else 0
    if overlap > 1.5:
        return f"併發 ✅  (span {span:.1f}s, 累計 busy {busy:.1f}s, 併發度 {overlap:.2f}x)"
    return f"循序 ❌  (span {span:.1f}s, 累計 busy {busy:.1f}s, 併發度 {overlap:.2f}x)"


async def main():
    out = []
    for ro in (True, False):
        print(f"\n=== readOnlyHint={ro} ===", flush=True)
        r = await run(ro)
        r["verdict"] = verdict(r)
        for c in r["calls"]:
            print(f"  {c['label']}: {c['start']:6.2f}s → {c['end']:6.2f}s")
        print(f"  turns(tool_use per assistant msg): {r['tool_use_blocks_per_turn']}")
        print(f"  wall: {r['wall_s']}s   理論循序={N_CALLS*SLEEP_S}s  理論併發≈{SLEEP_S}s")
        print(f"  → {r['verdict']}")
        out.append(r)
    print("\n" + json.dumps(out, indent=2))


if __name__ == "__main__":
    anyio.run(main)
