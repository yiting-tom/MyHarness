"""Spike #8 — 不用 claude-agent-sdk：自己寫 agentic loop。

同一份 charter、同一組工具（連授權檢查都是既有的 WorkerToolbox）、同一份
handle 契約，只換掉「誰在跑那個 loop」。跑同一組 live 斷言後對照兩邊的
token 數、延遲、成本與可觀測性。

用 httpx 直打 /v1/messages，不引入新相依 —— 順便證明這個 loop 到底幾行。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import httpx

from myharness.artifacts.store import ArtifactStore
from myharness.artifacts.types import GrantSet
from myharness.events.log import EventLog
from myharness.events.types import DISPATCH_END, DISPATCH_START
from myharness.lanes.contract import validate_payload
from myharness.lanes.handle import (
    HANDLE_SCHEMA,
    HandleStatus,
    LaneHandle,
    clamp_handle,
)
from myharness.lanes.tools import WorkerToolbox
from myharness.lanes.types import LaneInstance
from myharness.lanes.worker import WorkerRequest, build_prompt

TRANSIENT = {408, 409, 425, 429, 500, 502, 503, 504, 529}

#: The handle contract as a forced tool call. Every model that supports tool use
#: supports this, which is more than can be said for a structured-output flag --
#: and it lives inside the loop rather than bolted onto its output.
SUBMIT_HANDLE = {
    "name": "submit_handle",
    "description": "Report your result and end the task. Call this exactly once, last.",
    "input_schema": HANDLE_SCHEMA,
}

TOOL_SCHEMAS = {
    "read_note": {
        "name": "read_note",
        "description": "Read an analysis note you are allowed to see. Blobs are not readable this way.",
        "input_schema": {
            "type": "object",
            "properties": {"artifact": {"type": "string"}, "section": {"type": "string"}},
            "required": ["artifact"],
        },
    },
    "write_finding": {
        "name": "write_finding",
        "description": "Write your full analysis. Do NOT put the analysis in your final reply.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "text": {"type": "string"}},
            "required": ["name", "text"],
        },
    },
    "update_state": {
        "name": "update_state",
        "description": "Replace this lane's carried-over knowledge (conclusions and open questions only).",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    "localize_blob": {
        "name": "localize_blob",
        "description": "Get a local file path for a raw data blob so tools can read it.",
        "input_schema": {
            "type": "object",
            "properties": {"artifact": {"type": "string"}},
            "required": ["artifact"],
        },
    },
}


@dataclass
class DirectStats:
    turns: int = 0
    tool_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    prefix_tokens: int = 0
    wall_s: float = 0.0
    transient_retries: int = 0
    status: HandleStatus = HandleStatus.OK
    handle_chars: int = 0
    detail: str = ""


@dataclass
class DirectLaneWorker:
    """The whole agentic loop, in one place, with every tool call in hand."""

    store: ArtifactStore
    event_log: EventLog
    base_url: str
    api_key: str
    model: str
    max_turns: int = 12
    token_budget: int = 60_000
    stats: DirectStats = field(default_factory=DirectStats)

    async def run(self, request: WorkerRequest) -> LaneHandle:
        lane: LaneInstance = request.lane
        grants = GrantSet.for_lane(request.job_id, lane.namespace, request.inputs)
        toolbox = WorkerToolbox(
            store=self.store, job_id=request.job_id, lane=lane, grants=grants,
            read_budget=lane.type.input_token_budget,
        )
        toolbox.build_server()  # reuse the authorised handlers verbatim

        charter = lane.type.charter()
        tools = [TOOL_SCHEMAS[t] for t in lane.type.tools if t in TOOL_SCHEMAS]
        tools.append(SUBMIT_HANDLE)

        state, toolbox.state_revision = await self._load_state(request, grants)
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": build_prompt(request, state)}
        ]

        await self.event_log.append(
            request.job_id, DISPATCH_START, id=request.dispatch_id, lane=lane.id,
            task=request.task, model=self.model, contract_path="forced_tool",
        )

        started = time.monotonic()
        handle = await self._loop(messages, charter, tools, toolbox, request)
        self.stats.wall_s = round(time.monotonic() - started, 2)
        self.stats.status = handle.status
        self.stats.handle_chars = len(handle.to_json())

        await self.event_log.append(
            request.job_id, DISPATCH_END, id=request.dispatch_id, lane=lane.id,
            status=str(handle.status), artifact=handle.artifact or None,
            tokens={"in": self.stats.tokens_in, "out": self.stats.tokens_out,
                    "cache_read": self.stats.cache_read},
            turns=self.stats.turns, contract_path="forced_tool",
        )
        return handle

    async def _loop(self, messages, charter, tools, toolbox, request) -> LaneHandle:
        async with httpx.AsyncClient(timeout=300.0) as http:
            for _ in range(self.max_turns):
                self.stats.turns += 1
                try:
                    body = await self._call(http, messages, charter, tools)
                except _Transient as exc:
                    self.stats.transient_retries += 1
                    if self.stats.transient_retries > 3:
                        return self._fail(HandleStatus.BACKEND_UNAVAILABLE, str(exc))
                    await anyio.sleep(2.0 * self.stats.transient_retries)
                    continue
                except _Fatal as exc:
                    return self._fail(HandleStatus.TOOL_FAILURE, str(exc))

                self._account(body)
                if self.stats.tokens_in + self.stats.tokens_out > self.token_budget:
                    return self._fail(
                        HandleStatus.BUDGET_EXCEEDED, "local token ceiling reached",
                        partial=toolbox.last_finding,
                    )

                content = body.get("content", [])
                calls = [b for b in content if b.get("type") == "tool_use"]
                if not calls:
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content": (
                        "你必須呼叫工具。完成分析後呼叫 submit_handle 回報。")})
                    continue

                # Every tool call is in hand before anything runs.
                results = []
                for call in calls:
                    self.stats.tool_calls += 1
                    if call["name"] == "submit_handle":
                        outcome = validate_payload(call["input"])
                        if outcome.ok:
                            return clamp_handle(outcome.handle)
                        results.append(self._tool_result(
                            call["id"], "ERROR " + json.dumps(
                                {"problems": list(outcome.problems)}, ensure_ascii=False)))
                        continue
                    handler = toolbox.handlers.get(call["name"])
                    if handler is None:
                        results.append(self._tool_result(call["id"], "ERROR unknown tool"))
                        continue
                    result = await handler(call["input"])
                    results.append(self._tool_result(
                        call["id"], result["content"][0]["text"]))

                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": results})

        return self._fail(HandleStatus.MAX_TURNS, f"{self.max_turns} turns exhausted",
                          partial=toolbox.last_finding)

    async def _call(self, http, messages, charter, tools) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "max_tokens": 4096,
            # Cache the charter: it is the stable prefix across every worker.
            "system": [{"type": "text", "text": charter,
                        "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
            "tools": tools,
        }
        if self.stats.turns == 1:
            self.stats.prefix_tokens = (
                len(json.dumps(payload["system"], ensure_ascii=False))
                + len(json.dumps(tools, ensure_ascii=False))
            ) // 4

        response = await http.post(
            f"{self.base_url}/v1/messages",
            headers={"authorization": f"Bearer {self.api_key}",
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json=payload,
        )
        if response.status_code in TRANSIENT:
            raise _Transient(f"HTTP {response.status_code}")
        if response.status_code >= 400:
            raise _Fatal(f"HTTP {response.status_code}: {response.text[:200]}")
        return response.json()

    def _account(self, body: dict[str, Any]) -> None:
        usage = body.get("usage") or {}
        self.stats.tokens_in += int(usage.get("input_tokens") or 0)
        self.stats.cache_read += int(usage.get("cache_read_input_tokens") or 0)
        self.stats.tokens_out += int(usage.get("output_tokens") or 0)

    @staticmethod
    def _tool_result(call_id: str, text: str) -> dict[str, Any]:
        return {"type": "tool_result", "tool_use_id": call_id, "content": text}

    def _fail(self, status: HandleStatus, detail: str, partial: str | None = None) -> LaneHandle:
        self.stats.detail = detail
        return clamp_handle(LaneHandle(
            artifact=partial or "", headline=f"執行未完成：{status}", confidence="low",
            status=status, partial=partial, detail=detail[:300],
        ))

    async def _load_state(self, request: WorkerRequest, grants: GrantSet):
        from myharness.artifacts.ids import ArtifactId

        aid = ArtifactId(request.job_id, "note", request.lane.state_name)
        try:
            meta = await self.store.stat(aid, grants=grants)
            text = await self.store.read_note(
                aid, grants=grants, max_tokens=request.lane.type.state_max_tokens)
            return text, meta.revision
        except Exception:
            return None, 0


class _Transient(Exception):
    pass


class _Fatal(Exception):
    pass
