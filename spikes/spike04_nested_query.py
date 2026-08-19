"""Spike #2 — 巢狀 query() 的穩定性與資源回收。

決策 #6 的 dispatch 會在一個 SDK in-process @tool handler 裡啟動另一個 query()。
每個 query() 會 spawn 一個 claude CLI 子行程；若不回收，一個 job 跑 40 次 dispatch
就會留下 40 個殭屍行程與大量 fd。

Phase A: 序列跑 N 次裸 query()，量測行程與 fd 增長。
Phase B: 在 @tool handler 內跑巢狀 query()，確認語意正確且同樣回收。
"""

import os
import sys

os.environ.pop("ANTHROPIC_API_KEY", None)

import anyio
import psutil
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

N_A = int(sys.argv[1]) if len(sys.argv) > 1 else 20
N_B = 3
MODEL = os.environ.get("SPIKE_MODEL", "haiku")
USE_OR = os.environ.get("OR_KEY") and MODEL.startswith(("nvidia/", "openai/", "google/", "anthropic/"))
ENV = (
    {"ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
     "ANTHROPIC_AUTH_TOKEN": os.environ["OR_KEY"],
     "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    if USE_OR else {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
)

BUILTINS = ["Agent","Bash","CronCreate","CronDelete","CronList","Edit","EnterWorktree",
            "ExitWorktree","Glob","Grep","NotebookEdit","Read","ReportFindings","ScheduleWakeup",
            "SendMessage","Skill","TaskCreate","TaskGet","TaskList","TaskOutput","TaskStop",
            "TaskUpdate","WebFetch","WebSearch","Workflow","Write"]

ME = psutil.Process()


def snapshot() -> tuple[int, int]:
    """(descendant processes, open file descriptors)"""
    try:
        kids = len(ME.children(recursive=True))
    except psutil.Error:
        kids = -1
    try:
        fds = ME.num_fds()
    except psutil.Error:
        fds = -1
    return kids, fds


def opts(**kw) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=MODEL, max_turns=kw.pop("max_turns", 2), setting_sources=[],
        disallowed_tools=BUILTINS, permission_mode="bypassPermissions",
        env=ENV, system_prompt="Terse test harness. No commentary.", **kw
    )


async def run_once(prompt: str, **kw) -> str:
    out = []
    async for m in query(prompt=prompt, options=opts(**kw)):
        if isinstance(m, AssistantMessage):
            out += [b.text for b in m.content if isinstance(b, TextBlock)]
        elif isinstance(m, ResultMessage):
            pass
    return " ".join(out).strip()


async def phase_a() -> dict:
    base_kids, base_fds = snapshot()
    print(f"Phase A: {N_A} 次序列 query()   baseline kids={base_kids} fds={base_fds}")
    ok = 0
    marks = []
    for i in range(N_A):
        try:
            with anyio.fail_after(120):
                text = await run_once(f"Reply with exactly: R{i}")
            ok += 1 if f"R{i}" in text else 0
        except Exception as e:
            print(f"  [{i}] EXC {type(e).__name__}: {str(e)[:90]}")
        if (i + 1) % 5 == 0:
            k, f = snapshot()
            marks.append((i + 1, k, f))
            print(f"  after {i+1:>3}: kids={k} fds={f}")
    end_kids, end_fds = snapshot()
    return {"phase": "A", "n": N_A, "ok": ok, "base": (base_kids, base_fds),
            "end": (end_kids, end_fds), "marks": marks}


async def phase_b() -> dict:
    calls = []

    @tool("sub_analyze", "Delegate a sub-analysis and return its one-line result.",
          {"topic": str})
    async def sub_analyze(args):
        # This is the shape dispatch() will have: a nested query inside a tool.
        text = await run_once(f"Reply with exactly: SUB[{args['topic']}]")
        calls.append(args["topic"])
        return {"content": [{"type": "text", "text": text}]}

    server = create_sdk_mcp_server(name="d", version="1.0.0", tools=[sub_analyze])
    base_kids, base_fds = snapshot()
    print(f"\nPhase B: {N_B} 次巢狀 query()      baseline kids={base_kids} fds={base_fds}")
    texts = []
    for i in range(N_B):
        try:
            with anyio.fail_after(180):
                t = await run_once(
                    f"Call sub_analyze with topic='t{i}', then reply with exactly its output.",
                    mcp_servers={"d": server}, max_turns=4,
                )
            texts.append(t)
            print(f"  [{i}] parent said: {t[:60]!r}")
        except Exception as e:
            print(f"  [{i}] EXC {type(e).__name__}: {str(e)[:90]}")
        k, f = snapshot()
        print(f"       kids={k} fds={f}")
    end_kids, end_fds = snapshot()
    return {"phase": "B", "n": N_B, "nested_calls": calls, "texts": texts,
            "base": (base_kids, base_fds), "end": (end_kids, end_fds)}


async def main():
    print(f"model={MODEL}  via={'OpenRouter' if USE_OR else 'Anthropic direct'}\n")
    a = await phase_a()
    b = await phase_b()
    print("\n=== VERDICT ===")
    for r in (a, b):
        dk = r["end"][0] - r["base"][0]
        df = r["end"][1] - r["base"][1]
        leak = dk > 0 or df > 4
        print(f"  Phase {r['phase']}: n={r['n']}  Δkids={dk:+d} Δfds={df:+d}  "
              f"→ {'洩漏 ❌' if leak else '回收正常 ✅'}")
    print(f"  Phase A 成功 {a['ok']}/{a['n']}")
    print(f"  Phase B 巢狀呼叫實際發生 {len(b['nested_calls'])}/{b['n']} 次: {b['nested_calls']}")

anyio.run(main)
