"""One lane task: a fresh agent, a durable state file, and a bounded handle.

The loop accumulates as it streams rather than waiting for ``ResultMessage``,
because an exhausted ``task_budget`` raises and no result message ever arrives
(spikes/RESULTS.md §Spike #3c). Waiting for the result would mean losing exactly
the partial work the caller most needs.

Semantic failures never escape as exceptions: they come back as handles with a
status, so the orchestrator -- the only party with a global view -- decides what
to do about them (DESIGN.md decision #12).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)

from myharness.artifacts.ids import ArtifactId
from myharness.artifacts.store import ArtifactStore
from myharness.artifacts.types import GrantSet
from myharness.backends.profile import BackendCapability, BackendProfile
from myharness.events.log import EventLog
from myharness.events.types import CTX, DISPATCH_END, DISPATCH_START
from myharness.lanes.contract import (
    MAX_SCHEMA_RETRIES,
    ContractPath,
    extract_json_object,
    failure_handle,
    reprompt_text,
    validate_payload,
)
from myharness.lanes.handle import HANDLE_SCHEMA, HandleStatus, LaneHandle, clamp_handle
from myharness.lanes.tools import WorkerToolbox
from myharness.lanes.transport import SdkTransport, WorkerTransport
from myharness.lanes.types import LaneInstance

#: How many times to retry when the backend itself is unavailable. The SDK
#: already retries 429/5xx internally; this catches the case where it gave up.
MAX_TRANSIENT_RETRIES = 2
TRANSIENT_BACKOFF_S = (2.0, 8.0)

#: HTTP statuses the SDK reports through ``api_retry`` system messages.
TRANSIENT_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True, slots=True)
class WorkerRequest:
    job_id: str
    lane: LaneInstance
    task: str
    dispatch_id: str
    inputs: tuple[str, ...] = ()


@dataclass
class Accumulated:
    """What we know so far, usable even if the run dies mid-stream."""

    texts: list[str] = field(default_factory=list)
    transcript: list[dict[str, Any]] = field(default_factory=list)
    structured: Any = None
    result: ResultMessage | None = None
    turns: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    usd: float = 0.0
    retry_statuses: list[int] = field(default_factory=list)
    max_turns_hit: bool = False
    api_error_status: int | None = None
    terminal_reason: str | None = None
    thinking_events: int = 0

    @property
    def text(self) -> str:
        return "\n".join(self.texts).strip()

    @property
    def tokens_in(self) -> int:
        # Usage dicts carry the keys with null values when a provider does not
        # report them, so `.get(k, 0)` yields None rather than the default.
        return sum(
            _as_int(self.usage.get(key))
            for key in ("input_tokens", "cache_read_input_tokens",
                        "cache_creation_input_tokens")
        )

    @property
    def tokens_out(self) -> int:
        return _as_int(self.usage.get("output_tokens"))

    @property
    def saw_transient(self) -> bool:
        return any(s in TRANSIENT_STATUSES for s in self.retry_statuses)


def _block_to_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ThinkingBlock):
        return {"type": "thinking"}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "name": block.name, "input": block.input}
    return {"type": type(block).__name__}


def _consume(message: Any, acc: Accumulated) -> None:
    """Fold one streamed message into the accumulator."""
    if isinstance(message, AssistantMessage):
        acc.turns += 1
        blocks = [_block_to_dict(b) for b in message.content]
        acc.transcript.append({"role": "assistant", "content": blocks})
        acc.texts += [b.text for b in message.content if isinstance(b, TextBlock)]
        if getattr(message, "usage", None):
            acc.usage = dict(message.usage)
    elif isinstance(message, SystemMessage):
        if message.subtype == "thinking_tokens":
            # Hundreds of these arrive per run; a count is the whole signal.
            acc.thinking_events += 1
            return
        acc.transcript.append({"role": "system", "subtype": message.subtype})
        if message.subtype == "api_retry":
            status = (message.data or {}).get("error_status")
            if isinstance(status, int):
                acc.retry_statuses.append(status)
    elif isinstance(message, ResultMessage):
        acc.result = message
        acc.usage = dict(message.usage or acc.usage)
        acc.usd = float(message.total_cost_usd or 0.0)
        acc.structured = getattr(message, "structured_output", None)
        acc.api_error_status = getattr(message, "api_error_status", None)
        acc.terminal_reason = getattr(message, "terminal_reason", None)
        if (message.subtype or "").endswith("max_turns") or acc.terminal_reason == "max_turns":
            acc.max_turns_hit = True
        acc.transcript.append({
            "role": "result", "subtype": message.subtype,
            "is_error": message.is_error, "turns": message.num_turns,
        })
    else:
        acc.transcript.append({"role": type(message).__name__})


def build_prompt(request: WorkerRequest, state: str | None) -> str:
    """charter goes in system_prompt; this is the per-task part."""
    parts = [f"# 任務\n{request.task}"]
    if request.lane.scope:
        parts.append(f"# 這條 lane 的範圍\n{request.lane.scope}")
    parts.append(
        "# 你目前的累積認知\n"
        + (state if state else "（這是這條 lane 的第一個任務，尚無累積認知）")
    )
    if request.inputs:
        listed = "\n".join(f"- {i}" for i in request.inputs)
        parts.append(f"# 你被授權存取的資料\n{listed}")
    parts.append(
        "# 完成方式\n"
        "1. 用 write_finding 寫下完整分析。\n"
        "2. 用 update_state 更新累積認知（只寫結論與開放問題，不寫細節）。\n"
        "3. 最後回覆一個 handle，指向你寫的 finding。**不要在回覆裡重述分析內容。**"
    )
    return "\n\n".join(parts)


def _options(
    request: WorkerRequest,
    profile: BackendProfile,
    toolbox: WorkerToolbox,
    *,
    charter: str,
    enforce_schema: bool,
) -> ClaudeAgentOptions:
    lane_type = request.lane.type
    kwargs: dict[str, Any] = {
        "model": lane_type.model(),
        "system_prompt": charter,
        "mcp_servers": {"lane": toolbox.build_server()},
        "allowed_tools": toolbox.tool_names(),
        # Only disallowed_tools removes the ~18.9k of builtin definitions from
        # the request; allowed_tools does not (spikes/RESULTS.md §Spike #2b).
        "disallowed_tools": BackendProfile.disallowed_for(()),
        "strict_mcp_config": True,
        "setting_sources": [],
        "permission_mode": "bypassPermissions",
        "max_turns": lane_type.max_turns,
        "env": profile.to_sdk_env(),
    }
    if enforce_schema:
        kwargs["output_format"] = {"type": "json_schema", "schema": HANDLE_SCHEMA}
    if profile.supports(BackendCapability.TASK_BUDGET):
        kwargs["task_budget"] = {"total": lane_type.token_budget}
    return ClaudeAgentOptions(**kwargs)


async def _run_once(
    request: WorkerRequest,
    profile: BackendProfile,
    toolbox: WorkerToolbox,
    transport: WorkerTransport,
    *,
    prompt: str,
    charter: str,
    enforce_schema: bool,
) -> tuple[Accumulated, BaseException | None]:
    acc = Accumulated()
    options = _options(request, profile, toolbox, charter=charter, enforce_schema=enforce_schema)
    try:
        async for message in transport.stream(prompt, options):
            _consume(message, acc)
            # Local ceiling for backends that cannot enforce one server-side.
            if not profile.supports(BackendCapability.TASK_BUDGET):
                if acc.tokens_in + acc.tokens_out > request.lane.type.token_budget:
                    return acc, _LocalBudgetExceeded()
    except BaseException as exc:  # noqa: BLE001 - classified below, never re-raised
        return acc, exc
    return acc, None


class _LocalBudgetExceeded(Exception):
    """Raised by our own token counter, not by the backend."""


class _ResultReportedError(Exception):
    """The run ended cleanly but the result said it failed."""

    def __init__(self, acc: "Accumulated") -> None:
        super().__init__(
            (acc.text or "run reported is_error")[:200]
        )


async def run_lane_worker(
    request: WorkerRequest,
    *,
    store: ArtifactStore,
    event_log: EventLog,
    transport: WorkerTransport | None = None,
) -> LaneHandle:
    """Run one lane task. Always returns a handle; never raises for a failure."""
    transport = transport or SdkTransport()
    lane, lane_type = request.lane, request.lane.type
    profile = lane_type.backend_profile()
    charter = lane_type.charter()

    grants = GrantSet.for_lane(request.job_id, lane.namespace, request.inputs)
    toolbox = WorkerToolbox(
        store=store, job_id=request.job_id, lane=lane, grants=grants,
        read_budget=lane_type.input_token_budget,
    )
    state, toolbox.state_revision = await _load_state(store, request, grants)

    enforce = profile.supports(BackendCapability.STRUCTURED_OUTPUT)
    path = ContractPath.ENFORCED if enforce else ContractPath.DEGRADED

    await event_log.append(
        request.job_id, DISPATCH_START, id=request.dispatch_id, lane=lane.id,
        task=request.task, inputs=list(request.inputs), model=lane_type.model(),
        backend=profile.name, charter=lane_type.charter_hash(), contract_path=str(path),
    )

    prompt = build_prompt(request, state)
    acc, handle = await _attempt_all(
        request, profile, toolbox, transport,
        prompt=prompt, charter=charter, enforce=enforce,
    )

    transcript_id = await _persist_transcript(store, request, acc)
    handle = clamp_handle(
        LaneHandle(
            artifact=handle.artifact or (toolbox.last_finding or ""),
            headline=handle.headline, confidence=handle.confidence, status=handle.status,
            metrics=handle.metrics, followups=handle.followups, truncated=handle.truncated,
            lane=lane.id, dispatch_id=request.dispatch_id, transcript=transcript_id,
            partial=handle.partial or (toolbox.last_finding if not handle.ok else None),
            suggest=handle.suggest, detail=handle.detail,
        )
    )

    if toolbox.state_rejected and handle.ok:
        # The analysis landed but the lane's memory did not: the next task will
        # not see it, so this run is degraded even though the work succeeded.
        handle = clamp_handle(
            LaneHandle(
                **{**_as_kwargs(handle), "status": HandleStatus.STATE_REJECTED,
                   "suggest": "lane state 未更新，下一次任務不會看到這次的結論"}
            )
        )

    await event_log.append(
        request.job_id, DISPATCH_END, id=request.dispatch_id, lane=lane.id,
        status=str(handle.status), artifact=handle.artifact or None,
        tokens={"in": acc.tokens_in, "out": acc.tokens_out}, turns=acc.turns,
        usd=acc.usd, transcript=transcript_id, contract_path=str(path),
        headline=handle.headline, partial=handle.partial, suggest=handle.suggest,
    )
    await event_log.append(
        request.job_id, CTX, who=f"lane:{lane.id}", used=acc.tokens_in,
        pct=round(acc.tokens_in / lane_type.token_budget, 3) if lane_type.token_budget else 0,
    )
    return handle


def _as_kwargs(handle: LaneHandle) -> dict[str, Any]:
    return {
        "artifact": handle.artifact, "headline": handle.headline,
        "confidence": handle.confidence, "status": handle.status,
        "metrics": handle.metrics, "followups": handle.followups,
        "truncated": handle.truncated, "lane": handle.lane,
        "dispatch_id": handle.dispatch_id, "transcript": handle.transcript,
        "partial": handle.partial, "suggest": handle.suggest, "detail": handle.detail,
    }


async def _attempt_all(
    request: WorkerRequest,
    profile: BackendProfile,
    toolbox: WorkerToolbox,
    transport: WorkerTransport,
    *,
    prompt: str,
    charter: str,
    enforce: bool,
) -> tuple[Accumulated, LaneHandle]:
    """Transient retries on the outside, schema retries on the inside."""
    acc = Accumulated()
    schema_problems: tuple[str, ...] = ()
    current_prompt = prompt

    for transient_attempt in range(MAX_TRANSIENT_RETRIES + 1):
        for schema_attempt in range(MAX_SCHEMA_RETRIES + 1):
            acc, exc = await _run_once(
                request, profile, toolbox, transport,
                prompt=current_prompt, charter=charter, enforce_schema=enforce,
            )

            if exc is not None:
                status = _classify(acc, exc, profile)
                if status is HandleStatus.BACKEND_UNAVAILABLE:
                    break  # fall through to the transient-retry loop
                return acc, _failure_from(acc, status, toolbox, exc)

            if acc.max_turns_hit:
                return acc, failure_handle(
                    HandleStatus.MAX_TURNS,
                    headline=f"用盡 {request.lane.type.max_turns} 回合仍未產出 handle",
                    suggest="縮小任務範圍，或提高 max_turns",
                )

            # A run can end without raising and still have failed: the CLI
            # reports is_error=True after exhausting its own retries. Parsing a
            # handle out of "API Error: Request rejected (429)" and re-prompting
            # would triple the load on a backend that is already refusing us.
            if acc.result is not None and acc.result.is_error:
                if acc.saw_transient or acc.api_error_status in TRANSIENT_STATUSES:
                    break
                return acc, _failure_from(
                    acc, _classify(acc, _ResultReportedError(acc), profile),
                    toolbox, _ResultReportedError(acc),
                )

            outcome = _outcome_from(acc, enforce)
            if outcome.ok:
                return acc, outcome.handle

            schema_problems = outcome.problems
            if schema_attempt == MAX_SCHEMA_RETRIES:
                break
            # Semantic failures are never auto-retried; a malformed handle is a
            # formatting failure, which a re-prompt legitimately fixes.
            current_prompt = f"{prompt}\n\n{reprompt_text(schema_problems)}"
        else:  # pragma: no cover - loop always breaks or returns
            break

        run_failed = acc.result is None or acc.result.is_error
        if not (acc.saw_transient and run_failed):
            break
        if transient_attempt < MAX_TRANSIENT_RETRIES and acc.saw_transient:
            await anyio.sleep(TRANSIENT_BACKOFF_S[min(transient_attempt, len(TRANSIENT_BACKOFF_S) - 1)])
            current_prompt = prompt
            continue
        break

    if acc.saw_transient and (acc.result is None or acc.result.is_error):
        return acc, failure_handle(
            HandleStatus.BACKEND_UNAVAILABLE,
            headline="後端持續不可用（速率限制或伺服器錯誤）",
            detail=f"observed statuses: {sorted(set(acc.retry_statuses))}",
            suggest="稍後重試，或改用其他 backend profile",
        )
    return acc, failure_handle(
        HandleStatus.SCHEMA_VIOLATION,
        headline="worker 未能產出符合契約的 handle",
        detail="; ".join(schema_problems)[:400] or (acc.text[:200] or None),
        partial=toolbox.last_finding,
        suggest="檢查 charter 是否清楚說明 handle 格式",
    )


def _classify(acc: Accumulated, exc: BaseException, profile: BackendProfile) -> HandleStatus:
    """Work out what went wrong from observed state, not from the message text.

    The SDK's budget error reads ``"...error result: success"``, which is far too
    ambiguous to parse (design.md risk mitigation).
    """
    if isinstance(exc, _LocalBudgetExceeded):
        return HandleStatus.BUDGET_EXCEEDED
    if acc.max_turns_hit:
        return HandleStatus.MAX_TURNS
    if acc.api_error_status in TRANSIENT_STATUSES or (
        acc.saw_transient and acc.result is None
    ):
        return HandleStatus.BACKEND_UNAVAILABLE
    if profile.supports(BackendCapability.TASK_BUDGET):
        # With an API-side budget in play, the request being rejected outright
        # (400, no output) or the stream dying before any result both mean the
        # budget could not cover the task (spikes/RESULTS.md §Spike #6).
        if acc.result is None or acc.api_error_status == 400:
            return HandleStatus.BUDGET_EXCEEDED
    return HandleStatus.TOOL_FAILURE


def _failure_from(
    acc: Accumulated, status: HandleStatus, toolbox: WorkerToolbox, exc: BaseException
) -> LaneHandle:
    headlines = {
        HandleStatus.BUDGET_EXCEEDED: "token 預算耗盡，任務未完成",
        HandleStatus.TOOL_FAILURE: "執行中斷",
        HandleStatus.BACKEND_UNAVAILABLE: "後端不可用",
    }
    suggests = {
        HandleStatus.BUDGET_EXCEEDED: "縮小任務範圍重派，或接受部分結果",
        HandleStatus.TOOL_FAILURE: "檢查工具與輸入，或改派其他 lane",
        HandleStatus.BACKEND_UNAVAILABLE: "稍後重試，或改用其他 backend profile",
    }
    partial = toolbox.last_finding
    return failure_handle(
        status,
        headline=headlines.get(status, "執行失敗"),
        partial=partial,
        suggest=suggests.get(status),
        detail=f"{type(exc).__name__}: {exc}"[:400],
        metrics={"turns": float(acc.turns), "findings": float(len(toolbox.findings))},
    )


@dataclass(frozen=True, slots=True)
class _Outcome:
    handle: LaneHandle | None
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.handle is not None


def _outcome_from(acc: Accumulated, enforce: bool) -> _Outcome:
    payload = acc.structured if enforce else None
    if payload is None:
        payload = extract_json_object(acc.text)
    if not isinstance(payload, dict):
        return _Outcome(None, ("(root): no JSON object in the worker's reply",))
    validated = validate_payload(payload)
    return _Outcome(validated.handle, validated.problems)


async def _load_state(
    store: ArtifactStore, request: WorkerRequest, grants: GrantSet
) -> tuple[str | None, int]:
    aid = ArtifactId(request.job_id, "note", request.lane.state_name)
    try:
        meta = await store.stat(aid, grants=grants)
    except Exception:
        return None, 0
    try:
        text = await store.read_note(
            aid, grants=grants, max_tokens=request.lane.type.state_max_tokens
        )
    except Exception:
        return None, meta.revision
    return text, meta.revision


async def _persist_transcript(
    store: ArtifactStore, request: WorkerRequest, acc: Accumulated
) -> str:
    """A transcript is stored as a blob: it must never be read into a context."""
    body = "\n".join(json.dumps(row, ensure_ascii=False) for row in acc.transcript)
    meta = await store.put_blob(
        request.job_id, f"traces/{request.dispatch_id}",
        data=body.encode("utf-8"), produced_by=f"lane:{request.lane.id}",
        schema={"format": "jsonl", "rows": len(acc.transcript)},
    )
    return str(meta.id)
