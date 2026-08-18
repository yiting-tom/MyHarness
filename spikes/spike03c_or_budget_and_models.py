"""Spike #3c — OpenRouter 上的 task_budget 硬預算 + 非 Anthropic 模型可行性。"""
import os, sys, json
os.environ.pop("ANTHROPIC_API_KEY", None)
import anyio
from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage, AssistantMessage, TextBlock

BUILTINS = ["Agent","Bash","CronCreate","CronDelete","CronList","Edit","EnterWorktree",
            "ExitWorktree","Glob","Grep","NotebookEdit","Read","ReportFindings","ScheduleWakeup",
            "SendMessage","Skill","TaskCreate","TaskGet","TaskList","TaskOutput","TaskStop",
            "TaskUpdate","WebFetch","WebSearch","Workflow","Write"]
OR = {"ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
      "ANTHROPIC_AUTH_TOKEN": os.environ["OR_KEY"],
      "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}

async def run(model, prompt, **kw):
    res, texts = None, []
    try:
        with anyio.fail_after(150):
            async for m in query(prompt=prompt, options=ClaudeAgentOptions(
                    model=model, max_turns=3, setting_sources=[], disallowed_tools=BUILTINS,
                    permission_mode="bypassPermissions", env=OR,
                    system_prompt="Terse test harness.", **kw)):
                if isinstance(m, AssistantMessage):
                    texts += [b.text for b in m.content if isinstance(b, TextBlock)]
                elif isinstance(m, ResultMessage): res = m
    except Exception as e:
        return {"err": f"{type(e).__name__}: {str(e)[:220]}"}
    u = res.usage or {}
    return {"is_error": res.is_error, "subtype": res.subtype, "in": u.get("input_tokens"),
            "out": u.get("output_tokens"), "text": " ".join(texts)[:150],
            "structured": getattr(res, "structured_output", None)}

SCHEMA = {"type":"object","properties":{"answer":{"type":"string"}},
          "required":["answer"],"additionalProperties":False}

async def main():
    print("A) task_budget 硬預算（total=200，故意設到不夠用）")
    r = await run("anthropic/claude-sonnet-4.5",
                  "Write a detailed 500-word essay about tabular data analysis.",
                  task_budget={"total": 200})
    print(f"   {json.dumps(r, ensure_ascii=False)[:400]}")
    print(f"   → task_budget {'有作用（被截斷/報錯）✅' if (r.get('err') or r.get('is_error') or (r.get('out') or 0) < 400) else '無作用 ❌'}")

    print("\nB) 非 Anthropic 模型走 /v1/messages")
    for m in ["openai/gpt-4o-mini", "google/gemini-2.5-flash"]:
        r = await run(m, "Reply with exactly: PONG")
        ok = not r.get("err") and not r.get("is_error")
        print(f"   {m:<28} {'✅' if ok else '❌'}  {r.get('text') or r.get('err','')[:160]}")
        if ok:
            r2 = await run(m, "What is 2+2? Answer briefly.",
                           output_format={"type":"json_schema","schema":SCHEMA})
            so = r2.get("structured")
            print(f"   {'':<28} structured_output: {'✅ ' + json.dumps(so, ensure_ascii=False)[:80] if isinstance(so,dict) else '❌ ' + str(so or r2.get('err',''))[:120]}")

anyio.run(main)
