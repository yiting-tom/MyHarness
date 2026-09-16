"""Spike #29: can the existing records carry a turn-level view?

`myharness inspect` answers the dispatch-level question -- what was this report
based on. Every problem diagnosed in the last four golden runs lived one level
below it:

    #21/#22  re-prompt returned the JSON Schema instead of an instance
    #23      re-prompt returned a bare artifact id
    #23 d1   thirteen queries, then artifact: null
    #24 d1   warned at 57% "five requests left", wrote the finding next turn

Every one of those answers is in `blobs/traces/dN`, and that file has no reader.
It is written on every dispatch and read by a throwaway script each time
something goes wrong.

This spike asks whether those two existing records -- the event stream and the
transcript -- are enough on their own, by building the view out of them and
looking at what it cannot say. It writes nothing into the job.

    python3 spikes/spike29_flow_viewer.py --job golden24 -o /tmp/flow.html

`--fragment` drops the document wrapper (for embedding); `--fonts web` links
Google Fonts instead of the system stack, which the shipped version must not do
-- the spec requires the output open with no network at all.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from myharness.dataflow import (  # noqa: E402
    EdgeKind,
    NodeKind,
    build_dataflow,
    detect,
)
from myharness.events.query import summarize  # noqa: E402
from myharness.local_layout import find_jobs  # noqa: E402
from myharness.monitor.cli import load  # noqa: E402

#: The harness appends its own instructions to the end of a tool result. They
#: are the only per-turn budget measurement that exists anywhere -- the
#: transcript carries no token counts of its own.
HARNESS_NOTE = re.compile(r"\[harness\](.*)$", re.S)
BUDGET_PCT = re.compile(r"預算已用\s*(\d+)%")
EXCERPTED = re.compile(r"…\[略過 (\d+) 字元\]…")

KIND_GLYPH = {
    NodeKind.BLOB: "▣", NodeKind.FINDING: "▪", NodeKind.REPORT: "★",
    NodeKind.STATE: "◇", NodeKind.PLAN: "◆", NodeKind.LANE: "▸",
}


# --- extraction -----------------------------------------------------------


def parse_trace(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """One transcript into an ordered step list.

    Calls and results are paired by order, not by id: `_block_to_dict` keeps
    `tool_use_id` on the result and drops it on the call. That works here only
    because no assistant message in any golden run issues two calls at once --
    which is a property of these runs, not of the format.
    """
    steps: list[dict[str, Any]] = []
    turn = 0
    attempt = 0
    open_calls: list[int] = []
    unpaired = 0

    for row in rows:
        role = row.get("role")
        blocks = row.get("content") if isinstance(row.get("content"), list) else []

        if role == "system":
            if row.get("subtype") == "init":
                attempt += 1
                steps.append({"kind": "attempt", "n": attempt})
            else:
                steps.append({"kind": "system", "subtype": row.get("subtype", "")})

        elif role == "assistant":
            turn += 1
            for block in blocks:
                t = block.get("type")
                if t == "thinking":
                    steps.append({"kind": "think", "turn": turn,
                                  "chars": int(block.get("chars") or 0)})
                elif t == "text":
                    steps.append({"kind": "text", "turn": turn,
                                  "text": block.get("text", "")})
                elif t == "tool_use":
                    open_calls.append(len(steps))
                    steps.append({"kind": "call", "turn": turn,
                                  "name": block.get("name", ""),
                                  "input": block.get("input") or {}})
                else:
                    steps.append({"kind": "other", "turn": turn, "type": str(t)})

        elif role == "user":
            for block in blocks:
                if block.get("type") != "tool_result":
                    continue
                body = block.get("content") or ""
                note = HARNESS_NOTE.search(body)
                harness = note.group(0).strip() if note else ""
                tool_said = body[: note.start()].rstrip() if note else body
                pct = BUDGET_PCT.search(harness)
                skipped = EXCERPTED.search(body)
                step = {
                    "kind": "result",
                    "error": bool(block.get("is_error")),
                    "body": tool_said,
                    "harness": harness,
                    "pct": int(pct.group(1)) if pct else None,
                    "skipped": int(skipped.group(1)) if skipped else 0,
                    "for": None,
                }
                if open_calls:
                    step["for"] = open_calls.pop(0)
                else:
                    unpaired += 1
                steps.append(step)

        elif role == "result":
            steps.append({"kind": "end", "subtype": row.get("subtype", ""),
                          "error": bool(row.get("is_error")),
                          "turns": row.get("turns")})

    return {"steps": steps, "turns": turn, "attempts": attempt,
            "unpaired_results": unpaired}


def dispatch_rows(events: list[Any]) -> dict[str, dict[str, Any]]:
    """Per-dispatch figures, straight off dispatch.start / end / ctx."""
    out: dict[str, dict[str, Any]] = {}
    last = ""
    for event in events:
        if event.t == "dispatch.start":
            out.setdefault(str(event.get("id")), {}).update(
                task=event.get("task", ""), model=event.get("model", ""),
                backend=event.get("backend", ""),
                contract=event.get("contract_path", ""),
                started=event.ts.isoformat(),
            )
        elif event.t == "dispatch.end":
            last = str(event.get("id"))
            out.setdefault(last, {}).update(
                tokens=event.get("tokens") or {},
                estimate=event.get("estimate") or {},
                turns=event.get("turns"), usd=event.get("usd"),
                ended=event.ts.isoformat(),
            )
        elif event.t == "ctx" and str(event.get("who", "")).startswith("lane:"):
            # The lane's budget is in no event. Its spend and its share of the
            # budget both are, and the one divides into the other. This ctx
            # follows its own dispatch.end immediately, which is the only thing
            # tying the two together.
            spent, pct = event.get("spent"), event.get("pct")
            if last and spent and pct:
                out[last].update(spent=spent, pct=pct, budget=round(spent / pct))
    return out


def collect(root: Path, job_id: str) -> dict[str, Any]:
    events, artifacts = asyncio.run(load(root, job_id))
    flow = build_dataflow(events, artifacts, job_id=job_id)
    summary = summarize(events)
    anomalies = detect(flow)
    extra = dispatch_rows(list(events))

    layout = next((j for j in find_jobs(root) if j.job_id == job_id), None)
    traces: dict[str, Any] = {}
    if layout is not None:
        for dispatch_id in flow.dispatches:
            path = layout.blob_path(f"traces/{dispatch_id}")
            if not path.exists():
                continue
            rows = [json.loads(line) for line in
                    path.read_text(encoding="utf-8").splitlines() if line.strip()]
            traces[dispatch_id] = parse_trace(rows) | {"bytes": path.stat().st_size}

    goal = next((e.get("goal", "") for e in events if e.t == "job.start"), "")

    dispatches = []
    for dispatch in flow.dispatches.values():
        read = sorted({e.dst for e in flow.edges_from(dispatch.id, EdgeKind.READ)})
        dispatches.append({
            "id": dispatch.id, "lane": dispatch.lane, "status": dispatch.status,
            "granted": list(dispatch.granted), "produced": list(dispatch.produced),
            "read": read, "usd": dispatch.usd,
            "tokens_in": dispatch.tokens_in, "tokens_out": dispatch.tokens_out,
            "estimated": dispatch.tokens_estimated,
            **extra.get(dispatch.id, {}),
        })

    return {
        "job_id": job_id,
        "goal": goal,
        "finished": flow.finished,
        "finish_reason": flow.finish_reason,
        "report": flow.report_artifact,
        "read_edges_available": flow.read_edges_available,
        "usd": summary.total_usd,
        "context_peak": summary.context_peak,
        "dispatch_count": summary.dispatches,
        "failures": summary.failures,
        "nodes": [{"id": n.id, "kind": str(n.kind),
                   "glyph": KIND_GLYPH.get(n.kind, "·"), "label": n.label,
                   "est_tokens": n.est_tokens, "bytes": n.bytes}
                  for n in flow.nodes.values()],
        "anomalies": [a.to_dict() for a in anomalies],
        "dispatches": dispatches,
        "traces": traces,
        "caveats": [{"kind": c.kind, "detail": c.detail} for c in summary.caveats],
    }


# --- rendering ------------------------------------------------------------


FONT_LINK = (
    '<link rel="stylesheet" '
    'href="https://fonts.googleapis.com/css2?'
    "family=JetBrains+Mono:wght@400;500;700&"
    'family=IBM+Plex+Sans:wght@400;500;600&display=swap">'
)

TEMPLATE = (Path(__file__).parent / "spike29_viewer.html")


def render(data: dict[str, Any], *, fonts: str, fragment: bool) -> str:
    body = TEMPLATE.read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    body = body.replace("/*__DATA__*/null", payload)
    head = FONT_LINK if fonts == "web" else ""
    if fragment:
        return head + "\n" + body
    return (
        "<!doctype html>\n<html lang=\"zh-Hant\">\n<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"{head}\n</head>\n<body>\n{body}\n</body>\n</html>\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=Path("jobs-scratch"))
    ap.add_argument("--job", default="golden24")
    ap.add_argument("-o", "--out", type=Path)
    ap.add_argument("--fonts", choices=("system", "web"), default="system")
    ap.add_argument("--fragment", action="store_true")
    args = ap.parse_args()

    root = args.root
    job = next((j for j in find_jobs(root) if j.job_id == args.job), None)
    if job is None:
        print(f"找不到 job {args.job}（root={root}）", file=sys.stderr)
        return 1

    data = collect(job.root, args.job)
    html = render(data, fonts=args.fonts, fragment=args.fragment)

    if args.out:
        args.out.write_text(html, encoding="utf-8")
    else:
        sys.stdout.write(html)

    traces = data["traces"]
    calls = sum(1 for t in traces.values() for s in t["steps"] if s["kind"] == "call")
    thinks = [s for t in traces.values() for s in t["steps"] if s["kind"] == "think"]
    marks = [s for t in traces.values() for s in t["steps"] if s.get("pct")]
    unpaired = sum(t["unpaired_results"] for t in traces.values())
    print(
        f"job={args.job}  派工 {len(data['dispatches'])}"
        f"  軌跡 {len(traces)} 份（{sum(t['bytes'] for t in traces.values()):,} B）\n"
        f"工具呼叫 {calls}  推理標記 {len(thinks)}"
        f"（其中 chars=0 的 {sum(1 for s in thinks if not s['chars'])}）\n"
        f"預算量測點 {len(marks)} 個 —— 這是 transcript 裡唯一的逐輪 token 訊號\n"
        f"配不到呼叫的結果 {unpaired} 個"
        f"  HTML {len(html):,} B",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
