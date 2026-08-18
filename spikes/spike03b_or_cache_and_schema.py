"""Spike #3b — OpenRouter 上兩件決定性能力：
  A) prompt caching 命中（ephemeral worker 成本模型的前提）
  B) 結構化輸出強制（handle 契約「強制 vs 祈禱」的前提）
對照組同時跑 Anthropic 直連。
"""
import os, sys, json, time
os.environ.pop("ANTHROPIC_API_KEY", None)

import anyio
from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage, AssistantMessage, TextBlock

BUILTINS = ["Agent","Bash","CronCreate","CronDelete","CronList","Edit","EnterWorktree",
            "ExitWorktree","Glob","Grep","NotebookEdit","Read","ReportFindings","ScheduleWakeup",
            "SendMessage","Skill","TaskCreate","TaskGet","TaskList","TaskOutput","TaskStop",
            "TaskUpdate","WebFetch","WebSearch","Workflow","Write"]

# 模擬 lane charter：夠長才會觸發 caching（Sonnet 最低 1024 tokens）
CHARTER = ("You are a tabular data analyst lane worker in an agent harness.\n"
           + "\n".join(f"Rule {i}: Always report findings with explicit sample sizes, "
                       f"state confidence, and never speculate beyond the data provided. "
                       f"Prefer exact counts over percentages when n is small."
                       for i in range(60)))

HANDLE_SCHEMA = {
    "type": "object",
    "properties": {
        "artifact": {"type": "string"},
        "headline": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "metrics": {"type": "object", "additionalProperties": {"type": "number"}},
    },
    "required": ["artifact", "headline", "confidence"],
    "additionalProperties": False,
}

OR = {"ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
      "ANTHROPIC_AUTH_TOKEN": os.environ["OR_KEY"],
      "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}


def opts(model, env, **kw):
    return ClaudeAgentOptions(model=model, max_turns=3, setting_sources=[],
                              disallowed_tools=BUILTINS, permission_mode="bypassPermissions",
                              system_prompt=CHARTER, env=env or {}, **kw)


async def one(model, env, prompt, **kw):
    res, texts = None, []
    try:
        with anyio.fail_after(180):
            async for m in query(prompt=prompt, options=opts(model, env, **kw)):
                if isinstance(m, AssistantMessage):
                    texts += [b.text for b in m.content if isinstance(b, TextBlock)]
                elif isinstance(m, ResultMessage):
                    res = m
    except Exception as e:
        return {"err": f"{type(e).__name__}: {str(e)[:200]}"}
    u = res.usage or {}
    return {"in": u.get("input_tokens"), "cc": u.get("cache_creation_input_tokens"),
            "cr": u.get("cache_read_input_tokens"), "is_error": res.is_error,
            "structured": getattr(res, "structured_output", None), "text": " ".join(texts)[:200]}


async def suite(name, model, env):
    print(f"\n{'='*62}\n{name}   model={model}\n{'='*62}")

    print("A) prompt caching — 相同 charter 前綴連跑兩次")
    a1 = await one(model, env, "Reply with exactly: RUN1")
    a2 = await one(model, env, "Reply with exactly: RUN2")
    for i, r in enumerate((a1, a2), 1):
        if "err" in r: print(f"   run{i}: ERR {r['err']}"); continue
        print(f"   run{i}: in={r['in']} cache_create={r['cc']} cache_read={r['cr']}")
    hit = (a2.get("cr") or 0) > 0
    print(f"   → prompt caching {'命中 ✅' if hit else '未命中 ❌'}")

    print("B) 結構化輸出強制 (--json-schema)")
    b = await one(model, env,
                  "You finished analysing 30412 transactions and wrote findings to "
                  "lanes/txn/findings/003. Report your handle.",
                  output_format={"type": "json_schema", "schema": HANDLE_SCHEMA})
    if "err" in b:
        print(f"   ERR {b['err']}")
        ok = False
    else:
        so = b["structured"]
        ok = isinstance(so, dict) and all(k in so for k in ("artifact", "headline", "confidence"))
        print(f"   structured_output = {json.dumps(so, ensure_ascii=False)[:220]}")
        print(f"   text = {b['text'][:120]}")
    print(f"   → 結構化輸出 {'成立 ✅' if ok else '不成立 ❌'}")
    return {"name": name, "cache_hit": hit, "schema_ok": ok}


async def main():
    out = [await suite("OpenRouter", "anthropic/claude-sonnet-4.5", OR)]
    if "--with-direct" in sys.argv:
        out.append(await suite("Anthropic 直連（對照組）", "sonnet", None))
    print(f"\n{'='*62}\n=== SUMMARY ===")
    for r in out:
        print(f"  {r['name']:<26} caching={'✅' if r['cache_hit'] else '❌'}  "
              f"structured_output={'✅' if r['schema_ok'] else '❌'}")

anyio.run(main)
