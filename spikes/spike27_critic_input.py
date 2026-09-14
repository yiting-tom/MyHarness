"""Spike #27: why the critic lane's input estimate runs 14-21% low.

Four golden runs say the same thing and say it only about one lane:

    golden21 d3 critic  input -14.0%     golden21 d5 synth  +3.0%
    golden22 d2 critic  input -19.3%     golden22 d3 synth  -6.0%
    golden23 d3 critic  input -14.3%     golden23 d5 synth  -2.0%
    golden24 d2 critic  input -21.1%     golden24 d3 synth  -7.5%

Three explanations are already dead. Thinking is not re-sent as input -- spike
#26's captures carry it back as `{"thinking": "", "signature": ""}`, 53 bytes
with nothing in them. The tool declarations are not mis-sized: two of them
measure 196 tokens on the wire against the flat model's 2x98 = 196, exactly.
And it is not the Chinese rate as such: synth lanes carry the same 54-58% CJK
share as critics and estimate within 6%.

What is left is the text itself. Spike #26 measured this counter against the
wire at +-2% -- on its own probe strings, which are what the coefficients were
fitted to. A real critic reads something else: golden #24's analyst finding is
1,133 tokens of dense analytical Chinese, numbers and markdown tables, and the
critic's gap is 4,098 tokens over four requests, near 1,025 a request. That is
the size of the note.

So this replays a critic dispatch on a real finding and compares, per request,
what our counter says the wire carried against what the backend charged for it.
A per-request residual that tracks the note says the rates are wrong for this
kind of text. One that appears all at once says something else is riding along.

Run: set -a && . ./.env && set +a && .venv/bin/python spikes/spike27_critic_input.py
"""

import asyncio
import json
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, "spikes")

os.environ.pop("ANTHROPIC_API_KEY", None)

import spike26_solve_rates as s26  # noqa: E402

from myharness.lanes.budget import count  # noqa: E402

#: The finding a real critic actually read, rather than a probe string.
SOURCE = ("jobs-scratch/golden24/jobs/golden24/notes/lanes/"
          "analyst1/findings/txn-2024-analysis.md")

TASK = ("讀那份 finding，逐條指出證據不足的地方，每指出一條就用 write_finding "
        "寫下來，至少三條。全部用中文寫，每條至少 200 字。")

TIMED: list[float] = []


class TimedRecorder(s26.Recorder):
    """s26's proxy, plus how long each completion took.

    Latency is the only handle on the other open question: the self-hosted
    backend reports no cache fields at all, so whether it honours the
    cache_control breakpoints the CLI sends can only be read off the clock.
    A prefix that is genuinely cached does not make later requests slower in
    proportion to how much of it there is.
    """

    def _relay(self, method, content, body):
        started = time.monotonic()
        try:
            super()._relay(method, content, body)
        finally:
            if body is not None:
                TIMED.append(time.monotonic() - started)


def wire_tokens(request: dict) -> dict[str, int]:
    """What our counter says each part of one request carried."""
    parts = {
        "system": sum(count(json.dumps(b, ensure_ascii=False)).tokens
                      for b in (request.get("system") or [])),
        "tools": sum(count(json.dumps(t, ensure_ascii=False)).tokens
                     for t in (request.get("tools") or [])),
        "messages": sum(count(json.dumps(m, ensure_ascii=False)).tokens
                        for m in (request.get("messages") or [])),
    }
    parts["total"] = sum(parts.values())
    return parts


async def main() -> int:
    upstream = os.environ.get("HARNESS_PROXY_BASE_URL", "")
    if not upstream:
        print("HARNESS_PROXY_BASE_URL unset; nothing to record")
        return 1
    note_text = Path(SOURCE).read_text()
    print(f"note: {len(note_text):,} chars, {count(note_text).tokens:,} tokens "
          f"({count(note_text).cjk:,} cjk)\n")

    s26.UPSTREAM = upstream
    server = ThreadingHTTPServer(("127.0.0.1", 0), TimedRecorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    os.environ["HARNESS_PROXY_BASE_URL"] = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        end = await s26.run_probe(note_text, TASK)
    finally:
        server.shutdown()
        os.environ["HARNESS_PROXY_BASE_URL"] = upstream

    rows = s26.EXCHANGES
    print(f"{'req':>3} {'system':>7} {'tools':>6} {'messages':>9} {'wire':>8} "
          f"{'reported':>9} {'residual':>9} {'err':>7} {'secs':>6}")
    cumulative = 0
    for i, (ex, secs) in enumerate(zip(rows, TIMED, strict=False)):
        parts = wire_tokens(ex["request"])
        reported = ex["usage"]["input_tokens"]
        residual = reported - parts["total"]
        cumulative += residual
        print(f"{i:3} {parts['system']:7,} {parts['tools']:6,} {parts['messages']:9,} "
              f"{parts['total']:8,} {reported:9,} {residual:9,} "
              f"{residual / reported:+6.1%} {secs:6.1f}")

    total_wire = sum(wire_tokens(e["request"])["total"] for e in rows)
    total_rep = sum(e["usage"]["input_tokens"] for e in rows)
    print(f"\n{len(rows)} requests   wire {total_wire:,}   reported {total_rep:,}   "
          f"residual {total_rep - total_wire:,} ({(total_rep - total_wire) / total_rep:+.1%})")
    print(f"per request: {(total_rep - total_wire) / max(len(rows), 1):,.0f}")
    print(f"the note it read: {count(note_text).tokens:,} tokens")

    est = end.get("estimate") or {}
    print(f"\nestimator said {est.get('tokens_in', 0):,} for the run; "
          f"the wire carried {total_wire:,} and the backend charged {total_rep:,}")

    out = Path("spikes/spike27_exchanges.json")
    out.write_text(json.dumps(
        [{"request": e["request"], "usage": e["usage"], "secs": s}
         for e, s in zip(rows, TIMED, strict=False)], ensure_ascii=False))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
