"""Spike #3 — 把 claude-agent-sdk 指向 OpenRouter 的 Anthropic-compatible endpoint。

驗證三件事：
  1. CLI 送的 7 個 anthropic-beta + output_config + context_management 會不會被拒
  2. 工具呼叫（含 SDK in-process MCP tool）能不能用
  3. cache_control 有沒有真的生效（看 cache_read_input_tokens）
"""
import os, sys
os.environ.pop("ANTHROPIC_API_KEY", None)

import anyio, json, time
from mcp.types import ToolAnnotations
from claude_agent_sdk import (
    query, tool, create_sdk_mcp_server, ClaudeAgentOptions,
    AssistantMessage, TextBlock, ResultMessage, SystemMessage,
)

OR_KEY = os.environ["OR_KEY"]
MODEL = sys.argv[1] if len(sys.argv) > 1 else "anthropic/claude-sonnet-4.5"

BUILTINS = ["Agent","Bash","CronCreate","CronDelete","CronList","Edit","EnterWorktree",
            "ExitWorktree","Glob","Grep","NotebookEdit","Read","ReportFindings","ScheduleWakeup",
            "SendMessage","Skill","TaskCreate","TaskGet","TaskList","TaskOutput","TaskStop",
            "TaskUpdate","WebFetch","WebSearch","Workflow","Write"]

ENV = {
    "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
    "ANTHROPIC_AUTH_TOKEN": OR_KEY,
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}

calls = []

@tool("add", "Add two numbers", {"a": float, "b": float},
      annotations=ToolAnnotations(readOnlyHint=True))
async def add(args):
    calls.append(args)
    return {"content": [{"type": "text", "text": str(args["a"] + args["b"])}]}


async def run(label, prompt, use_tool=False):
    kw = {}
    if use_tool:
        kw = {"mcp_servers": {"m": create_sdk_mcp_server(name="m", version="1.0.0", tools=[add])},
              "allowed_tools": ["mcp__m__add"], "strict_mcp_config": True}
    opts = ClaudeAgentOptions(
        model=MODEL, max_turns=4, setting_sources=[], disallowed_tools=BUILTINS,
        permission_mode="bypassPermissions", env=ENV,
        system_prompt="You are a terse test harness. Answer with the minimum text.",
        **kw)
    t0 = time.monotonic(); texts = []; res = None; err = None; errs = []
    try:
        with anyio.fail_after(180):
            async for m in query(prompt=prompt, options=opts):
                if isinstance(m, AssistantMessage):
                    texts += [b.text for b in m.content if isinstance(b, TextBlock)]
                    if getattr(m, "error", None): errs.append(m.error)
                elif isinstance(m, SystemMessage) and m.subtype in ("api_retry","api_error"):
                    errs.append(str(m.data)[:200])
                elif isinstance(m, ResultMessage):
                    res = m
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:300]}"
    dt = round(time.monotonic() - t0, 1)
    u = (res.usage if res else {}) or {}
    print(f"\n── {label}  ({dt}s)")
    print(f"   text     : {' | '.join(t.strip()[:90] for t in texts) or '(none)'}")
    print(f"   is_error : {getattr(res,'is_error',None)}   exception: {err}")
    if errs: print(f"   errors   : {errs[:2]}")
    print(f"   usage    : in={u.get('input_tokens')} out={u.get('output_tokens')} "
          f"cache_create={u.get('cache_creation_input_tokens')} cache_read={u.get('cache_read_input_tokens')}")
    if use_tool: print(f"   tool_calls: {calls}")
    return {"label": label, "ok": bool(res and not res.is_error and not err),
            "usage": u, "err": err or errs[:1]}


async def main():
    print(f"model = {MODEL}   base = {ENV['ANTHROPIC_BASE_URL']}")
    out = []
    out.append(await run("1) 純文字", "Reply with exactly: PONG"))
    out.append(await run("2) 工具呼叫", "Use the add tool to compute 17 + 25, then state the number.", use_tool=True))
    out.append(await run("3) 重跑（測 cache_read）", "Reply with exactly: PONG2"))
    print("\n=== summary ===")
    for r in out:
        print(f"  {r['label']:<24} {'✅' if r['ok'] else '❌'}  {r['err'] if not r['ok'] else ''}")

anyio.run(main)
