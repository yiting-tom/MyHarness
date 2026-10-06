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
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
)

from myharness.artifacts.errors import ArtifactError
from myharness.artifacts.ids import ArtifactId
from myharness.artifacts.store import ArtifactStore
from myharness.artifacts.types import GrantSet
from myharness.backends.gate import BackendGate, ThrottleReport, gates
from myharness.backends.profile import BackendCapability, BackendProfile
from myharness.events.log import EventLog
from myharness.events.types import (
    ARTIFACT_READ,
    CTX,
    DISPATCH_END,
    DISPATCH_START,
    LANE_STEP,
    THROTTLE_COOLDOWN,
    THROTTLE_GAVE_UP,
    THROTTLE_WAIT,
)
from myharness.lanes.budget import count as count_text
from myharness.lanes.contract import (
    MAX_SCHEMA_RETRIES,
    ContractPath,
    extract_json_object,
    failure_handle,
    handle_format_text,
    handle_only_text,
    reprompt_text,
    validate_payload,
)
from myharness.lanes.handle import HANDLE_SCHEMA, HandleStatus, LaneHandle, clamp_handle
from myharness.lanes.stream import (
    FRAMEWORK_TOKENS_PER_REQUEST,
    TOKENS_PER_TOOL_DECLARATION,
    TRANSIENT_STATUSES,
    Accumulated,
    _charge_request,
    _consume,
    _turns_affordable,
    request_footprint,
    step_event,
)
from myharness.lanes.tools import WorkerToolbox
from myharness.lanes.transport import SdkTransport, WorkerTransport
from myharness.lanes.types import LaneInstance

#: Runaway backstop only. The *real* limit is the gate's time budget: a rate
#: limit recovers on the order of minutes, so a fixed count of short backoffs
#: gives up long before the backend does (spikes/RESULTS.md §Spike #6). Set high
#: enough that the budget is what normally ends the loop.
MAX_TRANSIENT_RETRIES = 32


@dataclass(frozen=True, slots=True)
class WorkerRequest:
    job_id: str
    lane: LaneInstance
    task: str
    dispatch_id: str
    inputs: tuple[str, ...] = ()




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
        "3. 最後回覆一個 handle，指向你寫的 finding。**不要在回覆裡重述分析內容。**\n\n"
        + handle_format_text()
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
    carried: int = 0,
    carried_io: tuple[int, int] = (0, 0),
    carried_transcript: list[dict[str, Any]] | None = None,
    attempt: int = 1,
    budget: int | None = None,
) -> tuple[Accumulated, BaseException | None]:
    # Seeded rather than empty: the opening request already carries the charter,
    # the tool definitions and the task, and none of that ever appears in the
    # stream. A run that is cut off after two requests was never free.
    acc = Accumulated(
        fixed_tokens_per_request=(
            FRAMEWORK_TOKENS_PER_REQUEST
            + TOKENS_PER_TOOL_DECLARATION * len(toolbox.tool_names())
            + count_text(charter).tokens
        ),
        conversation=count_text(prompt),
        carried_tokens=carried,
        carried_io=carried_io,
        carried_transcript=list(carried_transcript or ()),
        attempt=attempt,
        caches_prompts=profile.supports(BackendCapability.PROMPT_CACHING),
    )
    _charge_request(acc)
    # Passed in rather than read here: a re-prompt runs against a raised
    # ceiling, and only the caller knows which attempt this is.
    if budget is None:
        budget = request.lane.type.token_budget
    # Asked before the request goes out, not after it comes back. A re-prompt
    # inherits what the dispatch has already spent (golden #18), and golden #19
    # then sent three attempts that were over budget before the model saw them:
    # 2,115 tokens against 41 of headroom, and nothing to show for it. The
    # in-stream ceiling below cannot help -- by the time a message streams, the
    # request has been billed.
    if not profile.supports(BackendCapability.TASK_BUDGET) and acc.budget_tokens > budget:
        return acc, _LocalBudgetExceeded()

    options = _options(request, profile, toolbox, charter=charter, enforce_schema=enforce_schema)
    try:
        async for message in transport.stream(prompt, options):
            _consume(message, acc)
            if toolbox.on_step is not None:
                step = step_event(message, acc, budget)
                if step is not None:
                    await toolbox.on_step(step)
            # The lane cannot see its own consumption; the toolbox attaches
            # this to tool results so it can stop and write before it is cut
            # off. Golden run #9 spent a whole budget on 24 queries and never
            # called write_finding -- the analysis died undocumented, and the
            # lane had no way to know it was about to.
            spent = acc.budget_tokens
            if budget:
                toolbox.budget_used = spent / budget
                toolbox.turns_affordable = _turns_affordable(acc, budget - spent)
            # max_turns ends a run as surely as the budget does. compare-live-2's
            # d2 had budget to spare, so the gate never closed, and it spent
            # all 32 turns querying with nothing written.
            toolbox.turns_affordable = min(
                toolbox.turns_affordable, request.lane.type.max_turns - acc.requests
            )
            # Local ceiling for backends that cannot enforce one server-side.
            # Reading acc.tokens_in here meant the ceiling only ever tripped on
            # the final message -- it relabelled a finished run rather than
            # stopping one (golden run #11).
            if not profile.supports(BackendCapability.TASK_BUDGET) and spent > budget:
                return acc, _LocalBudgetExceeded()
    except BaseException as exc:  # noqa: BLE001 - classified below, never re-raised
        return acc, exc
    return acc, None


class _LocalBudgetExceeded(Exception):
    """Raised by our own token counter, not by the backend."""


class _ResultReportedError(Exception):
    """The run ended cleanly but the result said it failed."""

    def __init__(self, acc: Accumulated) -> None:
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
    grants = GrantSet.for_lane(request.job_id, request.lane.namespace, request.inputs)
    toolbox = WorkerToolbox(
        store=store, job_id=request.job_id, lane=request.lane, grants=grants,
        read_budget=request.lane.type.input_token_budget,
    )
    # Blobs the worker localised stay readable until the run is over, however it
    # ends (design.md D8). Tying their lifetime to the run rather than to a
    # single tool call is the whole point.
    async with toolbox:
        return await _run_with_toolbox(
            request, toolbox=toolbox, grants=grants,
            store=store, event_log=event_log, transport=transport,
        )


async def _run_with_toolbox(
    request: WorkerRequest,
    *,
    toolbox: WorkerToolbox,
    grants: GrantSet,
    store: ArtifactStore,
    event_log: EventLog,
    transport: WorkerTransport | None,
) -> LaneHandle:
    transport = transport or SdkTransport()
    lane, lane_type = request.lane, request.lane.type

    async def emit_read(artifact: str) -> None:
        """A read edge, written as it happens rather than inferred from grants."""
        await event_log.append(
            request.job_id, ARTIFACT_READ, dispatch=request.dispatch_id,
            lane=lane.id, artifact=artifact,
        )

    # Set here rather than at construction: which dispatch a read belongs to is
    # a property of the run, and the toolbox outlives neither.
    toolbox.on_read = emit_read

    async def emit_step(step: dict[str, Any]) -> None:
        """What the lane is doing, while it is doing it (see ``step_event``)."""
        await event_log.append(
            request.job_id, LANE_STEP, dispatch=request.dispatch_id, lane=lane.id, **step,
        )

    toolbox.on_step = emit_step
    profile = lane_type.backend_profile()
    charter = lane_type.charter()

    state, toolbox.state_revision = await _load_state(store, request, grants)

    enforce = profile.supports(BackendCapability.STRUCTURED_OUTPUT)
    path = ContractPath.ENFORCED if enforce else ContractPath.DEGRADED

    await event_log.append(
        request.job_id, DISPATCH_START, id=request.dispatch_id, lane=lane.id,
        task=request.task, inputs=list(request.inputs), model=lane_type.model(),
        backend=profile.name, charter=lane_type.charter_hash(), contract_path=str(path),
    )

    prompt = build_prompt(request, state)
    gate = gates.for_backend(profile.name)
    throttle = ThrottleReport()
    async with gate.acquire():
        await gate.wait_for_clearance(throttle)
        acc, handle = await _attempt_all(
            request, profile, toolbox, transport,
            prompt=prompt, charter=charter, enforce=enforce,
            gate=gate, throttle=throttle,
        )
    await _emit_throttle(event_log, request, profile, throttle)

    transcript_id = await _persist_transcript(store, request, acc)
    handle = clamp_handle(
        LaneHandle(
            artifact=_resolve_artifact(handle.artifact, toolbox.findings),
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
        tokens=acc.token_breakdown, turns=acc.turns,
        requests=request_footprint(acc),
        # The estimate's own inputs, so its error against the reported figure
        # is readable from the event stream. Three golden runs were diagnosed
        # by replaying transcripts, and transcripts excerpt tool results --
        # which is exactly the term that dominates.
        estimate=acc.estimate_breakdown,
        # Whether the budget gate refused anything, rather than leaving it to be
        # inferred from a transcript.
        gated=toolbox.gated,
        usd=acc.usd, transcript=transcript_id, contract_path=str(path),
        headline=handle.headline, partial=handle.partial, suggest=handle.suggest,
    )
    # The same fallback as the breakdown: an interrupted run reports no usage,
    # and a lane shown at 0% is exactly the lane that ran out.
    used = int(acc.token_breakdown["in"])
    # Two quantities, and this event used to divide one by the other's
    # denominator: golden #16 recorded critic-1 at 76.8% in the very run that
    # ended it for going over. `used` is context occupancy, which is input and
    # is what context_peak() reads; `spent` is what the ceiling actually judges.
    spent = acc.budget_tokens
    await event_log.append(
        request.job_id, CTX, who=f"lane:{lane.id}", used=used, spent=spent,
        pct=round(spent / lane_type.token_budget, 3) if lane_type.token_budget else 0,
    )
    return handle


async def _emit_throttle(
    event_log: EventLog, request: WorkerRequest, profile: BackendProfile,
    throttle: ThrottleReport,
) -> None:
    """Rate-limit waiting must be visible, or it reads as a slow model."""
    if throttle.cooldowns_triggered:
        await event_log.append(
            request.job_id, THROTTLE_COOLDOWN, backend=profile.name, lane=request.lane.id,
            count=throttle.cooldowns_triggered, reason="rate_limit",
        )
    if throttle.waits:
        await event_log.append(
            request.job_id, THROTTLE_WAIT, backend=profile.name, lane=request.lane.id,
            seconds=round(throttle.waited_s, 3), waits=throttle.waits,
        )
    if throttle.gave_up:
        await event_log.append(
            request.job_id, THROTTLE_GAVE_UP, backend=profile.name, lane=request.lane.id,
            waited_s=round(throttle.waited_s, 3),
        )


def _resolve_artifact(named: str, written: Sequence[str]) -> str:
    """Reconcile what the handle points at with what the lane actually wrote.

    ``write_finding`` hands the real id back in its result, and the lane's job
    is to copy it. Golden #23's d3 and d5 -- the two dispatches that were
    re-prompted -- returned ``lanes/critic-1/findings/critique`` for an
    artifact stored as ``golden23/note/lanes/critic-1/findings/critique``: the
    job-qualified head dropped, because the re-prompt's example showed an
    unqualified placeholder and a model shown a path composes a path. The
    handle validated, the report still shipped, and the dataflow graph grew a
    second node for the same finding that nothing read -- reported as an orphan
    that was not one.

    A suffix has to match exactly one written artifact. Two candidates is not a
    match and neither is none: never invent an id, because a wrong pointer that
    reads as right is worse than one that reads as wrong (golden run #4).
    """
    if not named:
        return written[-1] if written else ""
    if named in written:
        return named
    candidates = [w for w in written if w.endswith(f"/{named}")]
    return candidates[0] if len(candidates) == 1 else named


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
    gate: BackendGate,
    throttle: ThrottleReport,
) -> tuple[Accumulated, LaneHandle]:
    """Transient retries on the outside, schema retries on the inside.

    The outer loop defers to the backend's shared gate rather than backing off
    on its own, so concurrent lanes do not each rediscover the same rate limit.
    """
    acc = Accumulated()
    schema_problems: tuple[str, ...] = ()
    current_prompt = prompt
    # Both survive the rebinding of `acc` below, which is the whole point: one
    # so the ceiling sees the dispatch rather than the attempt, the other so a
    # reader does.
    carried = 0
    carried_io = (0, 0)
    carried_rows: list[dict[str, Any]] = []
    attempt = 0
    budget = request.lane.type.token_budget

    for transient_attempt in range(MAX_TRANSIENT_RETRIES + 1):
        for schema_attempt in range(MAX_SCHEMA_RETRIES + 1):
            attempt += 1
            acc, exc = await _run_once(
                request, profile, toolbox, transport,
                prompt=current_prompt, charter=charter, enforce_schema=enforce,
                carried=carried, carried_io=carried_io, carried_transcript=carried_rows,
                attempt=attempt, budget=budget,
            )
            # Whatever happens next -- a return, a re-prompt, a back-off -- this
            # attempt has been paid for, and what it said is on the record.
            carried = acc.budget_tokens
            spent_io = acc.token_breakdown
            carried_io = (spent_io["in"], spent_io["out"])
            carried_rows = acc.full_transcript

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
            if outcome.handle is not None:
                return acc, outcome.handle

            schema_problems = outcome.problems
            if schema_attempt == MAX_SCHEMA_RETRIES:
                break
            # Semantic failures are never auto-retried; a malformed handle is a
            # formatting failure, which a re-prompt legitimately fixes.
            current_prompt = f"{prompt}\n\n{reprompt_text(schema_problems)}"
            if toolbox.findings:
                # The work is on file; only its handle was malformed. Without
                # this the re-prompt is a fresh run that redoes the analysis:
                # compare-live-2's d3 wrote its finding, answered in prose,
                # and spent another 48k tokens querying everything again.
                toolbox.handle_only = True
                written = {fid: await _finding_text(toolbox, fid) for fid in toolbox.findings}
                current_prompt = f"{prompt}\n\n{handle_only_text(written)}" \
                                 f"\n\n{reprompt_text(schema_problems)}"
            # Raised, not reset. The re-prompt redoes the task, so it needs room
            # of its own -- but `carried` keeps running, so the ceiling and the
            # dispatch event still say what the whole dispatch spent.
            budget += request.lane.type.retry_budget()
        else:  # pragma: no cover - loop always breaks or returns
            break

        run_failed = acc.result is None or acc.result.is_error
        if not (acc.saw_transient and run_failed):
            break
        if acc.saw_transient:
            if transient_attempt >= MAX_TRANSIENT_RETRIES - 1:
                # Hit the backstop rather than the budget; still a giving-up.
                throttle.gave_up = True
                break
            if not await gate.back_off(transient_attempt, throttle):
                break
            current_prompt = prompt
            continue
        break

    if acc.saw_transient and (acc.result is None or acc.result.is_error):
        return acc, failure_handle(
            HandleStatus.BACKEND_UNAVAILABLE,
            headline=(
                f"後端持續限流，等待 {throttle.waited_s:.0f}s 後放棄"
                if throttle.waited_s else "後端不可用（速率限制或伺服器錯誤）"
            ),
            detail=f"observed statuses: {sorted(set(acc.retry_statuses))}",
            suggest="稍後重試，改用其他 backend profile，或調高 retry budget",
            metrics={"throttle_waited_s": round(throttle.waited_s, 1)},
        )
    return acc, failure_handle(
        HandleStatus.SCHEMA_VIOLATION,
        headline="worker 未能產出符合契約的 handle",
        detail="; ".join(schema_problems)[:400] or (acc.text[:200] or None),
        partial=toolbox.last_finding,
        suggest="檢查 charter 是否清楚說明 handle 格式",
    )


#: Most a handle-only re-prompt carries of one finding. Above it the id alone
#: goes in: a handle needs the conclusion, not the whole write-up.
HANDLE_ONLY_FINDING_TOKENS = 6_000


async def _finding_text(toolbox: WorkerToolbox, finding: str) -> str | None:
    try:
        return await toolbox.store.read_note(
            ArtifactId.parse(finding), grants=toolbox.grants,
            max_tokens=HANDLE_ONLY_FINDING_TOKENS,
        )
    except (ArtifactError, ValueError):
        return None


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
    # With an API-side budget in play, the request being rejected outright
    # (400, no output) or the stream dying before any result both mean the
    # budget could not cover the task (spikes/RESULTS.md §Spike #6).
    if profile.supports(BackendCapability.TASK_BUDGET) and (
        acc.result is None or acc.api_error_status == 400
    ):
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
    except Exception:  # noqa: BLE001 - no state note means empty state, not failure
        return None, 0
    try:
        text = await store.read_note(
            aid, grants=grants, max_tokens=request.lane.type.state_max_tokens
        )
    except Exception:  # noqa: BLE001 - an unreadable note degrades to none
        return None, meta.revision
    return text, meta.revision


async def _persist_transcript(
    store: ArtifactStore, request: WorkerRequest, acc: Accumulated
) -> str:
    """A transcript is stored as a blob: it must never be read into a context."""
    rows = acc.full_transcript
    body = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
    meta = await store.put_blob(
        request.job_id, f"traces/{request.dispatch_id}",
        data=body.encode("utf-8"), produced_by=f"lane:{request.lane.id}",
        schema={"format": "jsonl", "rows": len(rows)},
    )
    return str(meta.id)
