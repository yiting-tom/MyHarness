"""Spike #2 — 指向自架 endpoint（LiteLLM）可行嗎？CLI 實際送出什麼？

架一個會錄音的假 /v1/messages，把 ANTHROPIC_BASE_URL 指過去，
記錄 CLI 打了哪些路徑、送了哪些 header 與 body 欄位。
這決定 LiteLLM 必須原樣轉發哪些東西才不會弄壞 harness 依賴的強制機制。
"""
import os
for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
    os.environ.pop(k, None)

import anyio, json, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

CAPTURED = []

SSE = [
    ("message_start", {"type": "message_start", "message": {
        "id": "msg_fake", "type": "message", "role": "assistant", "model": "fake",
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 1}}}),
    ("content_block_start", {"type": "content_block_start", "index": 0,
                             "content_block": {"type": "text", "text": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "text_delta", "text": "OK"}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("message_delta", {"type": "message_delta",
                       "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                       "usage": {"output_tokens": 1}}),
    ("message_stop", {"type": "message_stop"}),
]


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _capture(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw)
        except Exception:
            body = {"_raw": raw[:400].decode("utf8", "replace")}
        CAPTURED.append({"path": self.path, "method": self.command,
                         "headers": dict(self.headers), "body": body})
        return body

    def do_POST(self):
        body = self._capture()
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for ev, data in SSE:
                chunk = f"event: {ev}\ndata: {json.dumps(data)}\n\n".encode()
                self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        else:
            payload = json.dumps({
                "id": "msg_fake", "type": "message", "role": "assistant", "model": "fake",
                "content": [{"type": "text", "text": "OK"}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 1}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def do_GET(self):
        CAPTURED.append({"path": self.path, "method": "GET", "headers": dict(self.headers)})
        payload = b'{"data":[]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


async def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    print(f"fake endpoint: {base}")

    opts = ClaudeAgentOptions(
        model="sonnet", max_turns=1,
        setting_sources=[], permission_mode="bypassPermissions",
        env={  # ← 關鍵：per-query 覆寫後端
            "ANTHROPIC_BASE_URL": base,
            "ANTHROPIC_AUTH_TOKEN": "sk-litellm-dummy",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "my-litellm-alias",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        },
        system_prompt="test",
    )
    err = None
    try:
        with anyio.fail_after(90):
            async for m in query(prompt="Say OK.", options=opts):
                if isinstance(m, ResultMessage):
                    print("result:", m.subtype, "is_error =", m.is_error)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    srv.shutdown()

    print(f"\nexception: {err}")
    print(f"requests received: {len(CAPTURED)}")
    for c in CAPTURED:
        print(f"\n--- {c['method']} {c['path']} ---")
        hdr = {k: (v[:24] + "…" if k.lower() in ("authorization", "x-api-key") and len(v) > 24 else v)
               for k, v in c["headers"].items()
               if k.lower() not in ("host", "connection", "content-length", "accept-encoding")}
        print("headers:", json.dumps(hdr, indent=2, ensure_ascii=False))
        b = c.get("body")
        if isinstance(b, dict):
            print("body keys:", sorted(b.keys()))
            for k in ("model", "stream", "max_tokens", "temperature", "tool_choice",
                      "output_format", "response_format", "thinking", "metadata",
                      "task_budget", "service_tier", "top_p", "stop_sequences"):
                if k in b:
                    print(f"  {k} = {json.dumps(b[k], ensure_ascii=False)[:200]}")
            if "tools" in b:
                print(f"  tools: {len(b['tools'])} 個")
            if "system" in b:
                sysb = b["system"]
                if isinstance(sysb, list):
                    print(f"  system: {len(sysb)} blocks, cache_control="
                          f"{[blk.get('cache_control') for blk in sysb]}")
            if "messages" in b:
                cc = []
                for msg in b["messages"]:
                    ct = msg.get("content")
                    if isinstance(ct, list):
                        cc += [blk.get("cache_control") for blk in ct if isinstance(blk, dict) and blk.get("cache_control")]
                print(f"  messages: {len(b['messages'])}, cache_control blocks: {len(cc)}")

    with open("spikes/spike02_captured.json", "w") as f:
        json.dump(CAPTURED, f, indent=2, ensure_ascii=False, default=str)

anyio.run(main)
