"""Spike #6 — 預算耗盡與回合用盡在真實後端上長什麼樣？

Live 測試顯示 worker 把兩者都歸類成 TOOL_FAILURE。_classify 的假設是
「預算耗盡時 ResultMessage 不會來」（spike #3c 的觀察），但實際上似乎會來。
這裡直接印出訊息序列，讓分類邏輯依事實而非假設。
"""
import os, sys, json
os.environ.pop("ANTHROPIC_API_KEY", None)          # 無效，會蓋掉 AUTH_TOKEN

import anyio
from claude_agent_sdk import (ClaudeAgentOptions, ResultMessage, SystemMessage,
                              AssistantMessage, query)
from myharness.backends.profile import OPENROUTER, BUILTIN_TOOLS

MODEL = OPENROUTER.resolve_model("strong")


async def probe(label: str, **over):
    kw = dict(model=MODEL, setting_sources=[], permission_mode="bypassPermissions",
              disallowed_tools=list(BUILTIN_TOOLS), env=OPENROUTER.to_sdk_env(),
              system_prompt="Terse test harness.", max_turns=6)
    prompt = over.pop("prompt", None) or "請寫一篇 2000 字的長文說明離群值偵測。"
    kw.update(over)
    seen, exc = [], None
    try:
        with anyio.fail_after(180):
            async for m in query(prompt=prompt, options=ClaudeAgentOptions(**kw)):
                if isinstance(m, ResultMessage):
                    seen.append(("result", {
                        "subtype": m.subtype, "is_error": m.is_error,
                        "num_turns": m.num_turns, "stop_reason": m.stop_reason,
                        "terminal_reason": getattr(m, "terminal_reason", None),
                        "api_error_status": getattr(m, "api_error_status", None),
                        "errors": str(getattr(m, "errors", None))[:120],
                        "usage_out": (m.usage or {}).get("output_tokens"),
                    }))
                elif isinstance(m, SystemMessage):
                    seen.append(("system", m.subtype))
                elif isinstance(m, AssistantMessage):
                    seen.append(("assistant", f"{len(m.content)} blocks"))
    except BaseException as e:
        exc = f"{type(e).__name__}: {str(e)[:120]}"
    print(f"\n=== {label} ===")
    for kind, payload in seen:
        print(f"  {kind}: {json.dumps(payload, ensure_ascii=False) if isinstance(payload, dict) else payload}")
    print(f"  exception: {exc}")
    print(f"  result_arrived: {any(k=='result' for k,_ in seen)}")


async def main():
    print(f"model={MODEL}")
    await probe("A) task_budget=600（故意不足）", task_budget={"total": 600})
    await probe("A2) task_budget=40000（充足）— 測 OpenRouter 是否接受此欄位",
                task_budget={"total": 40000}, prompt="用一句話說明離群值。")
    await probe("A3) 完全不帶 task_budget", prompt="用一句話說明離群值。")
    await probe("B) max_turns=1", max_turns=1,
                prompt="請呼叫五次不同的工具，然後才回答。")
    await probe("C) 正常", task_budget={"total": 40000},
                prompt="用一句話說明離群值。")

anyio.run(main)
