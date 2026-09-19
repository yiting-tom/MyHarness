"""The live view, in a browser. Read-only, loopback-only, stdlib-only.

`add-flow-viewer/design.md` ruled this out with a reason that was not true:

    不做即時模式。瀏覽器裡的即時模式需要一個 server，那就違反了「零新增相依」。

`http.server` is not a dependency; it ships with Python, like `json`. The
terminal live view stays -- over ssh it is still the only thing that works --
but it cannot be left on a second screen, cannot be scrolled back, and cannot
open a finished dispatch to show what it actually did.

Polling rather than SSE or inotify, for `live.py`'s own reason: the stream is a
few kilobytes of append-only JSONL, so re-reading costs nothing a person can
perceive, while SSE on `http.server` costs a thread per client and makes
shutdown awkward.

**This is the only surface in the harness that listens on a port.** MCP is
stdio and the terminal monitor listens to nothing, so the guards here are not
ceremony: loopback only, GET only, and every identifier on the path treated as
untrusted input.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import threading
from collections.abc import Sequence
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urlparse

from myharness.dataflow import EdgeKind, build_dataflow, detect
from myharness.events.query import summarize
from myharness.events.types import LANE_STEP
from myharness.local_layout import find_jobs
from myharness.loopback import require_loopback
from myharness.monitor.content import artifact_view, writes_summary
from myharness.monitor.html import SLOT, script_literal, template
from myharness.monitor.live import current_activity
from myharness.monitor.trace import parse_trace

_TEMPLATE: Final = "live.html"

#: An id that came in on a URL. Anything outside this is refused before it can
#: be joined to anything -- the harness's ids are ascii words, dashes and dots,
#: and a path separator has no business in one.
_ID_MAX: Final = 128


def safe_id(value: str) -> str | None:
    """An identifier from an untrusted path, or None."""
    if not value or len(value) > _ID_MAX:
        return None
    if any(c in value for c in ("/", "\\", "\0")) or value.startswith("."):
        return None
    if not all(c.isalnum() or c in "-_." for c in value):
        return None
    return value


def build_state(root: Path, job_id: str) -> dict[str, Any]:
    """The whole current picture, re-derived from the stream on every call."""
    jobs = find_jobs(root)
    layout = next((j for j in jobs if j.job_id == job_id), None)
    if layout is None:
        # Two stores under one root can hold jobs with the same id; listing it
        # twice reads as a bug in the list, not as two stores.
        return missing_job(root, job_id, list(dict.fromkeys(j.job_id for j in jobs)))

    events = _read_events(layout.events_path)
    flow = build_dataflow(events, job_id=job_id)
    summary = summarize(events)
    activity = current_activity(events, flow)
    steps = _steps_by_dispatch(events)
    recorded = bool(steps)

    return {
        "job_id": job_id,
        "seq": events[-1].seq if events else -1,
        "finished": flow.finished,
        "finish_reason": flow.finish_reason,
        # The same Activity the terminal renders, so the two surfaces cannot
        # drift into saying different things about the same stream.
        "activity": {"state": activity.state, "detail": activity.detail},
        "running": [d.id for d in flow.running()],
        "report": flow.report_artifact,
        # Granted and actually-opened only differ in meaning when the stream
        # records openings at all. Without artifact.read events, drawing every
        # grant as "not opened" would be an unrecorded fact impersonating a
        # recorded one.
        "read_edges_available": flow.read_edges_available,
        # The same rule for steps: a stream written before lane.step existed has
        # none, and an agent drawn without steps must not read as an idle one.
        "steps_recorded": recorded,
        # Writes the transcripts recorded, including findings a lane wrote but
        # did not name in its handle -- dispatch.end carries one artifact, and
        # golden #26's d3 wrote two.
        "writes": writes_summary(layout, flow),
        "flow_empty_why": explain_empty_flow(events, flow),
        "usd": summary.total_usd,
        "context_peak": summary.context_peak,
        "throttle_seconds": summary.throttle_seconds,
        "dispatches": [
            {
                "id": d.id, "lane": d.lane, "task": d.task, "status": d.status,
                "running": d.running, "ok": d.ok,
                "granted": list(d.granted), "produced": list(d.produced),
                # artifact.read is written *during* a run, so this is the one
                # per-dispatch signal that moves while the dispatch is alive.
                "opened": sorted({e.dst for e in flow.edges_from(d.id, EdgeKind.READ)}),
                "tokens_in": d.tokens_in, "tokens_out": d.tokens_out,
                "usd": d.usd, "turns": d.turns,
                "has_trace": bool(d.transcript),
                "nothing_granted_why": ("" if d.granted else
                                        "沒有被授權任何輸入 —— 它讀不到 job 裡的任何資料"),
                "nothing_written_why": "" if d.produced else explain_no_output(d.status),
                "steps": steps.get(d.id, [])[-_STEPS_SHOWN:],
                "step_count": len(steps.get(d.id, [])),
                "steps_why": explain_no_steps(d, recorded, bool(steps.get(d.id))),
                "started": _stamp(events, "dispatch.start", d.id),
                "ended": _stamp(events, "dispatch.end", d.id),
            }
            for d in flow.dispatches.values()
        ],
        "nodes": [{"id": n.id, "kind": str(n.kind), "label": n.label,
                   "bytes": n.bytes, "est_tokens": n.est_tokens}
                  for n in flow.nodes.values()],
        "anomalies": [a.to_dict() for a in detect(flow)],
        "events": [
            {"seq": e.seq, "t": e.t, "ts": e.ts.isoformat(),
             "line": _one_line(e)}
            # Steps are drawn on the agents they belong to; in this column they
            # would be forty lines of one lane's queries burying everything else.
            for e in [e for e in events if e.t != LANE_STEP][-120:]
        ],
    }


def build_trace(root: Path, job_id: str, dispatch_id: str) -> dict[str, Any]:
    """One dispatch's turns, or an honest account of why there are none.

    A transcript is written when the dispatch *ends*. For one still running
    there is nothing to load -- not yet loaded, not missing: not written. The
    page has to say which, because "還沒有" and "不會有" are different answers
    and neither of them is a spinner.
    """
    layout = next((j for j in find_jobs(root) if j.job_id == job_id), None)
    if layout is None:
        return {"error": "no_such_job", "state": "absent",
                "why": f"找不到 job {job_id}，所以也沒有它的派工紀錄。"}

    events = _read_events(layout.events_path)
    flow = build_dataflow(events, job_id=job_id)
    dispatch = flow.dispatches.get(dispatch_id)
    if dispatch is None:
        return {"error": "no_such_dispatch", "state": "absent",
                "why": f"事件流裡沒有派工 {dispatch_id}。"}
    if dispatch.running:
        return {"state": "running", "why": "這段還在跑。逐輪紀錄是在派工結束時才寫下的，"
                                           "所以現在不是還沒載入 —— 是還不存在。"}
    if not dispatch.transcript:
        return {"state": "absent", "why": "這段派工沒有留下逐輪紀錄。"}

    name = _blob_name(dispatch.transcript)
    if name is None:
        return {"state": "absent", "why": "逐輪紀錄的位置無法解析。"}
    path = layout.blob_path(name)
    if not path.exists():
        return {"state": "absent", "why": "逐輪紀錄的檔案不在儲存區裡。"}

    rows = [json.loads(line) for line
            in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    trace = parse_trace(rows)
    if not trace.steps:
        return {"state": "absent",
                "why": "逐輪紀錄存在，但裡面一輪都沒有 —— 這段派工在送出第一個請求之前就結束了。"}
    return {
        "state": "ready",
        "turns": trace.turns,
        "attempts": trace.attempts,
        "unpaired_results": trace.unpaired_results,
        "steps": [
            {"kind": str(s.kind), "turn": s.turn, "chars": s.chars,
             "empty_reasoning": s.empty_reasoning, "name": s.name,
             "args": dict(s.args), "error": s.error, "body": s.body,
             "harness": s.harness, "budget_pct": s.budget_pct,
             "skipped": s.skipped, "answers": s.answers, "n": s.n,
             "subtype": s.subtype, "detail": s.detail}
            for s in trace.steps
        ],
    }


def build_artifact(root: Path, job_id: str, raw_id: str) -> dict[str, Any]:
    """One artifact's content and recorded versions (see ``content.py``).

    The id arrives on a query string and is untrusted: ``ArtifactId.parse``
    refuses traversal and illegal segments, and an id from another job is
    refused before the store is asked anything.
    """
    layout = next((j for j in find_jobs(root) if j.job_id == job_id), None)
    if layout is None:
        return {"error": "no_such_job", "why": f"找不到 job {job_id}。"}
    if not raw_id or len(raw_id) > 512:
        return {"error": "bad_id", "why": "沒有給 artifact id，或它長得不合理。"}
    events = _read_events(layout.events_path)
    return artifact_view(layout, build_dataflow(events, job_id=job_id), raw_id)


# --- why something is empty -----------------------------------------------
#
# An empty region on a monitor has at least four causes that call for opposite
# responses: the job has not started, it has started but not dispatched yet, it
# finished having dispatched nothing, or the monitor is pointed at the wrong
# place. A blank panel says none of them, and the reader is left to guess which
# -- which is exactly the question a monitor exists to answer.


def missing_job(root: Path, job_id: str, known: Sequence[str]) -> dict[str, Any]:
    """No stream for this id -- say where we looked and what is there instead.

    Not an error the page gives up on: a monitor started before its job is a
    normal thing to do, so the page keeps polling and fills in when the first
    event lands.
    """
    return {
        "error": "no_such_job",
        "job_id": job_id,
        "root": str(root),
        "known": list(known),
        "why": (f"在 {root} 底下找不到 job {job_id} 的事件流。"
                "如果它還沒開始，這頁會在它寫下第一個事件時自己長出來；"
                "如果它早就該在跑了，多半是 job 名稱或 --root 指錯了。"),
    }


def explain_empty_flow(events: Sequence[Any], flow: Any) -> str:
    """Why the flow has nothing in it, or "" when it has something."""
    if not events:
        return "事件流是空的：這個 job 還沒寫下任何事件。它一開始，這裡就會長出東西。"
    if flow.dispatches:
        return ""
    if flow.finished:
        reason = f"（{flow.finish_reason}）" if flow.finish_reason else ""
        return (f"這個 job 已經結束{reason}，但一次派工都沒有發出 —— "
                "所以沒有任何資料被讀、也沒有任何分析被寫。")
    planned = any(e.t == "plan.update" for e in events)
    return ("orchestrator 還沒派出任何工作。"
            + ("計畫已經排好了，" if planned else "它還在讀目標、排計畫，")
            + "第一個派工出現時會長在這裡。")


def explain_no_steps(dispatch: Any, recorded: bool, has_steps: bool) -> str:
    """Why an agent shows no steps, or "" when it has some.

    Four causes, and a reader should do different things about each: wait, look
    at the transcript instead, suspect the stream, or accept that the run never
    got as far as a first answer.
    """
    if has_steps:
        return ""
    if not dispatch.running and not dispatch.turns:
        return "它在收到第一輪回應之前就結束了，所以沒有任何一步可記。"
    if not recorded:
        if dispatch.running:
            return ("還沒有步驟紀錄。可能它還在等第一輪回應；"
                    "也可能這份事件流是舊版 harness 寫的，那一版不記錄步驟。")
        return ("這份事件流沒有記錄步驟 —— 它是在 harness 開始寫 lane.step 之前跑的。"
                "這不代表它閒著：完整逐輪紀錄仍然可以打開。")
    if dispatch.running:
        return "剛開始，還在等第一輪回應。"
    return "這次派工結束了，但事件流裡沒有它的步驟 —— 這一段紀錄不完整。"


_NO_OUTPUT: Final[dict[str, str]] = {
    "running": "還在跑，還沒寫出東西",
    "ok": "回報完成，但沒有寫出任何 artifact",
    "budget_exceeded": "預算在寫出任何東西之前就用完了",
    "max_turns": "來回次數在寫出任何東西之前就用完了",
    "tool_failure": "工具出錯，在寫出任何東西之前就中止了",
    "duplicate": "跟前一次派工重複，沒有重跑，所以沒有產出",
}


def explain_no_output(status: str) -> str:
    return _NO_OUTPUT.get(status, f"以 {status} 結束，沒有寫出任何東西")


# --- helpers --------------------------------------------------------------


def _read_events(path: Path) -> list[Any]:
    from myharness.events.types import Event
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            out.append(Event.from_json(line))
        except Exception:  # noqa: BLE001 - a half-written tail is normal while
            continue       # a job is running; the next poll will pick it up
    return out


#: Per agent, how many recent steps ride along on every poll. The node shows
#: the last one; the side panel shows these; the transcript has all of them.
_STEPS_SHOWN: Final = 60


def _steps_by_dispatch(events: Sequence[Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        if e.t != LANE_STEP:
            continue
        out.setdefault(str(e.get("dispatch") or ""), []).append({
            "seq": e.seq, "ts": e.ts.isoformat(), "phase": e.get("phase"),
            "attempt": e.get("attempt"), "turn": e.get("turn"),
            "calls": e.get("calls") or [], "results": e.get("results") or [],
            "thinking_chars": e.get("thinking_chars"), "text_chars": e.get("text_chars"),
            "thinking_blocks": e.get("thinking_blocks"),
            "spent": e.get("spent"), "pct": e.get("pct"),
        })
    return out


def _stamp(events: Sequence[Any], kind: str, dispatch_id: str) -> str | None:
    for event in events:
        if event.t == kind and str(event.get("id") or "") == dispatch_id:
            return str(event.ts.isoformat())
    return None


def _blob_name(artifact_id: str) -> str | None:
    """`job/blob/traces/d1` -> `traces/d1`, refusing anything else."""
    parts = artifact_id.split("/")
    if len(parts) < 3 or parts[1] != "blob":
        return None
    tail = parts[2:]
    if any(p in ("", ".", "..") for p in tail):
        return None
    return "/".join(tail)


_EVENT_SAYS: Final[dict[str, str]] = {
    "job.start": "job 開始", "job.finish": "job 結束",
    "plan.update": "計畫更新", "ingress": "資料進入",
    "proxy.route": "分流", "dispatch.start": "派工開始",
    "dispatch.end": "派工結束", "artifact.read": "讀取",
    "ctx": "context", "peek": "窺看", "ask.user": "提問",
    "ask.answer": "回答", "throttle.cooldown": "限流冷卻",
    "throttle.wait": "限流等待", "throttle.gave_up": "限流放棄",
    "limit.reached": "觸及上限", "no_progress": "無進展",
    "handoff.restart": "交接重啟", "lane.step": "步驟",
}


def _one_line(event: Any) -> str:
    """One event as a line someone can read while it scrolls past."""
    head = _EVENT_SAYS.get(event.t, event.t)
    bits = [str(event.get(k)) for k in ("id", "lane", "artifact", "payload",
                                        "who", "backend", "limit")
            if event.get(k)]
    return f"{head} {' · '.join(dict.fromkeys(bits))}".strip()


# --- server ---------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    """GET only. There is no write path, by construction rather than by check."""

    server_version = "myharness-monitor"
    root: Path
    job_id: str

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._page()
        elif path == "/state":
            self._json(build_state(self.root, self.job_id))
        elif path == "/artifact":
            raw = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
            self._json(build_artifact(self.root, self.job_id, raw))
        elif path.startswith("/trace/"):
            dispatch_id = safe_id(path[len("/trace/"):])
            if dispatch_id is None:
                self._json({"error": "bad_id"}, HTTPStatus.BAD_REQUEST)
            else:
                self._json(build_trace(self.root, self.job_id, dispatch_id))
        else:
            self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def _page(self) -> None:
        body = template(_TEMPLATE).replace(
            SLOT, script_literal(build_state(self.root, self.job_id)))
        self._send(
            '<!doctype html>\n<html lang="zh-Hant">\n<head>\n'
            '<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f"</head>\n<body>\n{body}\n</body>\n</html>\n",
            "text/html; charset=utf-8",
        )

    def _json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(json.dumps(payload, ensure_ascii=False),
                   "application/json; charset=utf-8", status)

    def _send(self, text: str, content_type: str,
              status: HTTPStatus = HTTPStatus.OK) -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        # Every response is derived fresh from the stream; a cached one would
        # be a monitor that shows the past.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        """Silence. The terminal running this is showing the URL, not a log."""


def make_server(root: Path, job_id: str, *, host: str = "127.0.0.1",
                port: int = 0) -> ThreadingHTTPServer:
    """A read-only server for one job. Refuses anything but loopback."""
    host = require_loopback(
        host, "the web monitor has no authentication, so it serves loopback only")
    # A subclass per server rather than attributes on the shared class: two
    # monitors in one process would otherwise show each other's job. The address
    # family goes on the subclass too -- ::1 is a loopback address the rule
    # accepts, and refusing to bind it would make the guard and the server
    # disagree about what "local" means.
    bound = type("_BoundHandler", (_Handler,), {"root": root, "job_id": job_id})
    server = type("_Server", (ThreadingHTTPServer,),
                  {"address_family": _family(host)})
    return server((host, port), bound)  # type: ignore[no-any-return]


def _family(host: str) -> int:
    if host == "localhost":
        return socket.AF_INET
    try:
        return (socket.AF_INET6 if ipaddress.ip_address(host).version == 6
                else socket.AF_INET)
    except ValueError:
        return socket.AF_INET


def serve(root: Path, job_id: str, *, host: str = "127.0.0.1",
          port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    """Start the server in a background thread and return it with its URL."""
    httpd = make_server(root, job_id, host=host, port=port)
    bound_host, bound_port = httpd.server_address[0], httpd.server_address[1]
    shown = bound_host.decode() if isinstance(bound_host, bytes) else str(bound_host)
    if ":" in shown:  # an IPv6 literal has to be bracketed in a URL
        shown = f"[{shown}]"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, f"http://{shown}:{bound_port}/"


__all__ = ["build_artifact", "build_state", "build_trace", "explain_empty_flow",
           "explain_no_output", "explain_no_steps",
           "make_server", "missing_job", "safe_id", "serve"]
