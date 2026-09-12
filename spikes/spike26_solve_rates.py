"""Spike #26: stop inferring the estimator's coefficients and measure them.

Six golden runs have said the same thing: the budget estimate runs low, by 29%
to 34%, and the two candidate explanations are both dead. Spike #20 measured the
per-message envelope at 7-9 tokens against the 283 the residual needs. Spike #21
read the wire and found thinking re-sent as `{"thinking": "", "signature": ""}`
stubs -- a wrapper with nothing in it. Neither is the missing mass.

What has never been done is the direct measurement. Every hypothesis so far was
fitted to one number per dispatch -- the total input tokens of a whole run --
which is one equation for three unknowns, so any pair of them can absorb the
third's error. A recording proxy that keeps the *response* as well as the
request turns one lane into fifteen equations:

    input_tokens(i) == ascii(i) / A + cjk(i) * C + K

where ascii(i) and cjk(i) are counted over exactly what request i carried --
system prompt, tool declarations and the whole conversation, the same bytes the
backend tokenized -- and K is whatever is charged that is not in the text.
Fifteen points, three unknowns, and the residual says whether the model is even
the right shape.

Run: set -a && . ./.env && set +a && python spikes/spike26_solve_rates.py
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

sys.path.insert(0, ".")

#: One entry per completion: what went up, and what the backend said it cost.
EXCHANGES: list[dict] = []
UPSTREAM = ""
FORWARD_HEADERS = {"content-type", "authorization", "x-api-key",
                   "anthropic-version", "anthropic-beta", "accept"}


def _usage_from(raw: bytes) -> dict:
    """Input and output tokens, from an SSE stream or a plain JSON body.

    Streamed responses report input once in `message_start` and output in the
    final `message_delta`; a non-streamed one carries both in `usage`. Taking
    the max rather than the first: some backends repeat a running total.
    """
    usage = {"input_tokens": 0, "output_tokens": 0}
    text = raw.decode("utf-8", "replace")
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        for holder in (event, event.get("message") or {}):
            found = holder.get("usage") if isinstance(holder, dict) else None
            if not isinstance(found, dict):
                continue
            for key in ("input_tokens", "output_tokens"):
                value = found.get(key)
                if isinstance(value, int):
                    usage[key] = max(usage[key], value)
            for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
                value = found.get(key)
                if isinstance(value, int) and value:
                    usage[key] = max(usage.get(key, 0), value)
    return usage


class Recorder(BaseHTTPRequestHandler):
    """Forwards everything, and keeps both halves of every completion."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _headers(self) -> dict:
        return {k: v for k, v in self.headers.items() if k.lower() in FORWARD_HEADERS}

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        self._relay("POST", raw, body)

    def do_GET(self):
        self._relay("GET", None, None)

    def _relay(self, method: str, content: bytes | None, body: dict | None):
        url = UPSTREAM.rstrip("/") + self.path
        echo = bytearray()
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
                        echo += chunk
                        self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
        except Exception as exc:  # a dead recorder must not look like a model error
            body_out = json.dumps({"error": {"message": f"recorder: {exc}"}}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_out)))
            self.end_headers()
            self.wfile.write(body_out)
            return
        if body and body.get("messages"):
            EXCHANGES.append({"request": body, "usage": _usage_from(bytes(echo))})


# --- counting exactly what the request carried -------------------------------

def _split(text: str) -> tuple[int, int]:
    """(ascii, non-ascii), the same split the estimator uses."""
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    return len(text) - non_ascii, non_ascii


def _text_of(value) -> str:
    """Everything a request field contributes, as one string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _message_text(message: dict) -> str:
    """A message's text, counted the way myharness.lanes.worker counts it.

    Not a json.dumps of the whole message: the estimator reads text blocks,
    serialises tool_use inputs and takes tool_result content, and a fit whose
    characters are counted differently from the estimator's cannot be carried
    back into it.
    """
    content = message.get("content")
    if isinstance(content, str):
        return content
    parts = []
    for block in content or ():
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text") or "")
        elif kind == "tool_use":
            parts.append(json.dumps(block.get("input"), ensure_ascii=False))
        elif kind == "tool_result":
            parts.append(_text_of(block.get("content")))
        elif kind in ("thinking", "redacted_thinking", "reasoning"):
            parts.append(block.get("thinking") or block.get("reasoning") or "")
    return "".join(parts)


def _counted(request: dict) -> dict[str, tuple[int, int]]:
    """The ascii/cjk split of each part of one request, kept apart."""
    return {
        "system": _split(_text_of(request.get("system"))),
        "tools": _split(_text_of(request.get("tools"))),
        "messages": _split("".join(
            _message_text(m) for m in request.get("messages") or ())),
    }


# --- least squares, written out so the spike has no numpy dependency ---------

def _solve(rows: list[list[float]], targets: list[float]) -> list[float]:
    """Normal equations with Gaussian elimination. Small and square is enough."""
    n = len(rows[0])
    ata = [[sum(r[i] * r[j] for r in rows) for j in range(n)] for i in range(n)]
    atb = [sum(r[i] * t for r, t in zip(rows, targets, strict=True)) for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(ata[r][col]))
        if abs(ata[pivot][col]) < 1e-12:
            raise ValueError("singular: the columns are not independent")
        ata[col], ata[pivot] = ata[pivot], ata[col]
        atb[col], atb[pivot] = atb[pivot], atb[col]
        for row in range(n):
            if row == col:
                continue
            factor = ata[row][col] / ata[col][col]
            for k in range(col, n):
                ata[row][k] -= factor * ata[col][k]
            atb[row] -= factor * atb[col]
    return [atb[i] / ata[i][i] for i in range(n)]


async def run_probe(note_text: str, task: str) -> dict:
    """One lane against the real backend, recorded request by request."""
    from myharness.artifacts.local import LocalArtifactStore
    from myharness.backends.profile import registry, self_hosted_from_env
    from myharness.events.log import LocalEventLog
    from myharness.lanes.types import LaneInstance, LaneType
    from myharness.lanes.worker import WorkerRequest, run_lane_worker

    profile = self_hosted_from_env()
    registry.register(profile)
    root = Path(tempfile.mkdtemp(prefix="mh-rates-"))
    store = LocalArtifactStore(root)
    await store.init_job("p")
    note = await store.put_note("p", "lanes/src/findings/data", note_text,
                                produced_by="src")
    lane_type = LaneType(
        name="probe", charter_path=Path("charters/critic.md"),
        tools=("read_note", "write_finding"), model_tier="strong",
        backend=profile.name, token_budget=40_000, max_turns=8,
    )
    events = LocalEventLog(root)
    await run_lane_worker(
        WorkerRequest(job_id="p", lane=LaneInstance(id="probe", type=lane_type),
                      task=task, dispatch_id="d1", inputs=(str(note.id),)),
        store=store, event_log=events,
    )
    return next(e.data for e in await events.read("p") if e.t == "dispatch.end")


#: Two probes, deliberately of different language mixes. One alone leaves the
#: non-ascii count nearly constant across its requests, which makes that column
#: collinear with the constant and the system unsolvable -- the first attempt at
#: this spike died exactly there, with eight clean data points and no answer.
PROBES = [
    ("帳戶 A 有 12 筆交易，帳戶 B 有 7 筆。金額分布右偏，而且週末的筆數明顯少於"
     "平日。樣本只涵蓋 2024 上半年，且未排除內部轉帳與測試交易。結論宣稱"
     "「週末交易量下降代表使用者行為改變」。",
     "讀那份 finding，逐條指出證據不足的地方，每指出一條就用 write_finding "
     "寫下來，至少三條。全部用中文寫，每條至少 200 字。"),
    ("Account A has 12 transactions and account B has 7. Amounts are "
     "right-skewed and weekend counts are visibly lower than weekdays. The "
     "sample covers only H1 2024 and does not exclude internal transfers or "
     "test transactions. The stated conclusion is that lower weekend volume "
     "means user behaviour changed.",
     "Read that finding and name, one at a time, each place the evidence does "
     "not support the claim. Call write_finding for each one, at least three. "
     "Write in English, at least 200 words each."),
]


async def main() -> int:
    global UPSTREAM
    UPSTREAM = os.environ.get("HARNESS_PROXY_BASE_URL", "")
    if not UPSTREAM:
        print("HARNESS_PROXY_BASE_URL unset; nothing to record")
        return 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    real = UPSTREAM
    os.environ["HARNESS_PROXY_BASE_URL"] = f"http://127.0.0.1:{server.server_address[1]}"
    ends = []
    try:
        for note_text, task in PROBES:
            ends.append(await run_probe(note_text, task))
    finally:
        server.shutdown()
        os.environ["HARNESS_PROXY_BASE_URL"] = real

    priced = [e for e in EXCHANGES if e["usage"].get("input_tokens")]
    print(f"{len(EXCHANGES)} completions, {len(priced)} of them with usage\n")
    if len(priced) < 4:
        print("not enough priced requests to solve for three unknowns")
        return 1

    Path("spikes/spike26_exchanges.json").write_text(
        json.dumps(EXCHANGES, ensure_ascii=False), encoding="utf-8")

    print(f"{'#':>2} {'msgs':>4} {'ascii':>8} {'非ascii':>8} {'in':>7} {'out':>6}")
    rows, targets = [], []
    for i, exchange in enumerate(priced, 1):
        counts = _counted(exchange["request"])
        ascii_chars = sum(a for a, _ in counts.values())
        cjk_chars = sum(c for _, c in counts.values())
        rows.append([float(ascii_chars), float(cjk_chars), 1.0])
        targets.append(float(exchange["usage"]["input_tokens"]))
        print(f"{i:2} {len(exchange['request']['messages']):4} {ascii_chars:8} "
              f"{cjk_chars:8} {exchange['usage']['input_tokens']:7} "
              f"{exchange['usage'].get('output_tokens', 0):6}")

    a_rate, c_rate, k = _solve(rows, targets)
    print()
    print("solved over what the wire actually carried:")
    print(f"  ascii    {a_rate:.4f} tokens/char  ({1 / a_rate:.2f} chars/token)")
    print(f"  非 ascii {c_rate:.4f} tokens/char")
    print(f"  constant {k:>8.0f} tokens/request")

    worst = 0.0
    for row, target in zip(rows, targets, strict=True):
        predicted = row[0] * a_rate + row[1] * c_rate + k
        worst = max(worst, abs(predicted - target) / target)
    print(f"  worst residual {worst * 100:.1f}%")

    from myharness.lanes.budget import ASCII_CHARS_PER_TOKEN, CJK_TOKENS_PER_CHAR
    print()
    print("against what the estimator currently believes:")
    print(f"  ascii    {1 / ASCII_CHARS_PER_TOKEN:.4f} tokens/char "
          f"({ASCII_CHARS_PER_TOKEN:.2f} chars/token)")
    print(f"  非 ascii {CJK_TOKENS_PER_CHAR:.4f} tokens/char")
    print()
    for i, end in enumerate(ends, 1):
        print(f"probe {i} as the harness recorded it:")
        print(" ", json.dumps(end.get("estimate") or {}, ensure_ascii=False))
        print(" ", json.dumps(end.get("tokens") or {}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
