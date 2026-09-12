"""Spike #21: read what the CLI actually sends, instead of inferring it.

Spike #20 killed the per-message envelope: the same 8,000 characters cost 7 to 9
tokens more for each extra message they are split across, not the 283-306 that
golden #18's residual needs. Thinking is the surviving candidate -- the blocks
arrive at the client with empty text (#17), but the CLI holds the original and
may well send it back.

That has been inferred from token counts for six golden runs. It does not have
to be. A recording proxy in front of the real backend sees every request the CLI
makes, verbatim, and the question becomes: does request N carry the thinking
from turn N-1, or does it not.

Everything else the estimate cannot explain is readable from the same capture,
so this is worth having whether or not thinking turns out to be the answer.

Run: set -a && . ./.env && set +a && python spikes/spike21_capture_requests.py
"""

import asyncio
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

os.environ.pop("ANTHROPIC_API_KEY", None)

CAPTURED: list[dict] = []
UPSTREAM = ""
FORWARD_HEADERS = {"content-type", "authorization", "x-api-key",
                   "anthropic-version", "anthropic-beta", "accept"}


class Recorder(BaseHTTPRequestHandler):
    """Forwards everything, keeps a copy of every request body."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _headers(self) -> dict:
        return {k: v for k, v in self.headers.items() if k.lower() in FORWARD_HEADERS}

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            CAPTURED.append({"path": self.path, "body": json.loads(raw)})
        except ValueError:
            CAPTURED.append({"path": self.path, "unparsed": len(raw)})
        self._relay("POST", raw)

    def do_GET(self):
        self._relay("GET", None)

    def _relay(self, method: str, content: bytes | None):
        url = UPSTREAM.rstrip("/") + self.path
        try:
            with httpx.Client(timeout=600.0) as client:
                with client.stream(method, url, content=content,
                                   headers=self._headers()) as upstream:
                    self.send_response(upstream.status_code)
                    for key, value in upstream.headers.items():
                        if key.lower() in ("content-type", "cache-control"):
                            self.send_header(key, value)
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for chunk in upstream.iter_raw():
                        if not chunk:
                            continue
                        self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
        except Exception as exc:  # a dead recorder must not look like a model error
            body = json.dumps({"error": {"message": f"recorder: {exc}"}}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def _blocks(message: dict) -> list:
    content = message.get("content")
    return content if isinstance(content, list) else []


def _chars(message: dict) -> int:
    content = message.get("content")
    if isinstance(content, str):
        return len(content)
    return len(json.dumps(content, ensure_ascii=False)) if content else 0


def _reasoning(message: dict) -> tuple[int, int]:
    """(thinking blocks, characters of thinking they actually carry).

    Counted apart, because the two answer different questions: whether the
    conversation carries thinking back at all, and whether what it carries has
    anything in it. A wrapper with an empty string inside is not a re-send.
    """
    blocks = chars = 0
    for key in ("reasoning", "reasoning_content", "thinking"):
        value = message.get(key)
        if isinstance(value, str) and value:
            blocks += 1
            chars += len(value)
    for block in _blocks(message):
        if not isinstance(block, dict):
            continue
        if block.get("type") in ("thinking", "redacted_thinking", "reasoning"):
            blocks += 1
            body = block.get("thinking") or block.get("reasoning") or block.get("data") or ""
            chars += len(body) if isinstance(body, str) else 0
    return blocks, chars


async def run_a_lane() -> dict:
    from myharness.artifacts.local import LocalArtifactStore
    from myharness.backends.profile import registry, self_hosted_from_env
    from myharness.events.log import LocalEventLog
    from myharness.lanes.types import LaneInstance, LaneType
    from myharness.lanes.worker import WorkerRequest, run_lane_worker

    profile = self_hosted_from_env()
    registry.register(profile)
    root = Path(tempfile.mkdtemp(prefix="mh-capture-"))
    store = LocalArtifactStore(root)
    await store.init_job("p")
    note = await store.put_note("p", "lanes/src/findings/data",
                                "帳戶 A 有 12 筆交易，帳戶 B 有 7 筆。金額分布右偏。",
                                produced_by="src")
    lane_type = LaneType(
        name="probe", charter_path=Path("charters/critic.md"),
        tools=("read_note", "write_finding"), model_tier="strong",
        backend=profile.name, token_budget=40_000, max_turns=6,
    )
    events = LocalEventLog(root)
    await run_lane_worker(
        WorkerRequest(job_id="p", lane=LaneInstance(id="probe", type=lane_type),
                      task="讀那份 finding，指出一個證據不足的地方，"
                           "然後用 write_finding 寫下來。",
                      dispatch_id="d1", inputs=(str(note.id),)),
        store=store, event_log=events,
    )
    return next(e.data for e in await events.read("p") if e.t == "dispatch.end")


async def main() -> int:
    global UPSTREAM
    UPSTREAM = os.environ.get("HARNESS_PROXY_BASE_URL", "")
    if not UPSTREAM:
        print("HARNESS_PROXY_BASE_URL unset; nothing to record")
        return 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    os.environ["HARNESS_PROXY_BASE_URL"] = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        end = await run_a_lane()
    finally:
        server.shutdown()

    posts = [c for c in CAPTURED if "body" in c and c["body"].get("messages")]
    print(f"{len(CAPTURED)} requests recorded, {len(posts)} of them completions\n")
    print(f"{'#':>2} {'msgs':>5} {'content chars':>13} {'think blk':>8} "
          f"{'think chars':>14} {'roles'}")
    for i, capture in enumerate(posts, 1):
        messages = capture["body"]["messages"]
        chars = sum(_chars(m) for m in messages)
        counted = [_reasoning(m) for m in messages]
        blocks = sum(b for b, _ in counted)
        thinking = sum(c for _, c in counted)
        roles = "".join(str(m.get("role", "?"))[0] for m in messages)
        print(f"{i:2} {len(messages):5} {chars:13,} {blocks:8} {thinking:14,} {roles[:36]}")

    breakdown = end.get("estimate") or {}
    tokens = end.get("tokens") or {}
    last = posts[-1]["body"]["messages"] if posts else []
    print()
    print(f"the accountant saw   {breakdown.get('requests', 0)} requests")
    print(f"the wire carried     {len(posts)} requests")
    print(f"reported tokens in   {tokens.get('in', 0):,}"
          f"{'  (estimated, no usage arrived)' if tokens.get('estimated') else ''}")
    print(f"estimated tokens in  {breakdown.get('tokens_in', 0):,}")
    all_chars = sum(sum(_chars(m) for m in c["body"]["messages"]) for c in posts)
    last_chars = sum(_chars(m) for m in last)
    print(f"characters on the wire, whole dispatch {all_chars:,}; "
          f"final request {last_chars:,}")
    if breakdown.get("requests", 0) < len(posts):
        print()
        print("The estimate counts one attempt. A schema re-prompt starts a fresh")
        print("Accumulated and the previous attempt's tokens go with it -- so does")
        print("the local ceiling's idea of what this dispatch has spent.")

    if posts:
        out = Path("spikes/spike21_captured.json")
        out.write_text(json.dumps(posts, ensure_ascii=False, indent=1)[:2_000_000])
        print(f"\nbodies written to {out}")
        totals = [_reasoning(m) for c in posts for m in c["body"]["messages"]]
        blocks = sum(b for b, _ in totals)
        chars = sum(c for _, c in totals)
        print()
        print(f"thinking: {blocks:,} blocks re-sent, carrying {chars:,} characters")
        if blocks and not chars:
            print("The blocks go back empty -- a wrapper and a signature, nothing in")
            print("them. Thinking is not what the conversation is paying for either.")
        elif chars:
            print("Thinking is re-sent with content in it, and the estimator counts")
            print("none of it: the ThinkingBlock the SDK hands us is empty, but what")
            print("goes back on the wire is not.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
