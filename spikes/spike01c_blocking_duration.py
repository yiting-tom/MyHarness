"""Spike #1c — SDK in-process MCP tool 能阻塞多久？

CLI 內有 MCP_TOOL_TIMEOUT（預設 1e8 ms ≈ 27.8h，實質無限）
與 CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT（「無回應或進度通知」即中止，0 可停用）。
問題是後者對 SDK in-process server 適不適用 —— 決定 dispatch/await_tasks
能不能阻塞好幾分鐘等 lane worker。
"""
import os
os.environ.pop("ANTHROPIC_API_KEY", None)

import anyio, sys, time
from mcp.types import ToolAnnotations
from claude_agent_sdk import query, tool, create_sdk_mcp_server, ClaudeAgentOptions, ResultMessage

BLOCK_S = float(sys.argv[1]) if len(sys.argv) > 1 else 180.0
result = {}

@tool("long_block", "Block silently for a long time, then return.",
      {"seconds": float}, annotations=ToolAnnotations(readOnlyHint=True))
async def long_block(args):
    t0 = time.monotonic()
    await anyio.sleep(float(args["seconds"]))
    result["blocked_s"] = round(time.monotonic() - t0, 1)
    return {"content": [{"type": "text", "text": "DONE_BLOCKING"}]}

async def main():
    server = create_sdk_mcp_server(name="spike", version="1.0.0", tools=[long_block])
    opts = ClaudeAgentOptions(
        model="sonnet", mcp_servers={"spike": server},
        allowed_tools=["mcp__spike__long_block"],
        disallowed_tools=["Task","Bash","Read","Edit","Write","Glob","Grep","WebFetch","WebSearch","TodoWrite"],
        strict_mcp_config=True, setting_sources=[],
        permission_mode="bypassPermissions", max_turns=3,
        system_prompt="Test harness. Follow instructions exactly. No commentary.",
    )
    t0 = time.monotonic()
    err = None
    try:
        async for m in query(prompt=f"Call mcp__spike__long_block with seconds={BLOCK_S}. "
                                    f"Then reply with exactly the tool's output text.",
                             options=opts):
            if isinstance(m, ResultMessage):
                result["is_error"] = m.is_error
                result["subtype"] = m.subtype
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    result["wall_s"] = round(time.monotonic() - t0, 1)
    result["exception"] = err
    result["requested_s"] = BLOCK_S
    print(result)
    ok = result.get("blocked_s", 0) >= BLOCK_S - 1 and not err and not result.get("is_error")
    print("→", f"阻塞 {BLOCK_S}s 可行 ✅" if ok else f"阻塞 {BLOCK_S}s 失敗 ❌")

anyio.run(main)
