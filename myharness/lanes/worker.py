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
import math
from dataclasses import dataclass, field
from typing import Any, Final

from claude_agent_sdk import (
    AssistantMessage,
    ToolResultBlock,
    UserMessage,
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
from myharness.lanes.budget import estimate as estimate_budget_tokens, split_chars
from myharness.backends.gate import BackendGate, ThrottleReport, gates
from myharness.backends.profile import BackendCapability, BackendProfile
from myharness.events.log import EventLog
from myharness.events.types import (
    CTX,
    DISPATCH_END,
    DISPATCH_START,
    THROTTLE_COOLDOWN,
    THROTTLE_GAVE_UP,
    THROTTLE_WAIT,
)
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

#: Runaway backstop only. The *real* limit is the gate's time budget: a rate
#: limit recovers on the order of minutes, so a fixed count of short backoffs
#: gives up long before the backend does (spikes/RESULTS.md §Spike #6). Set high
#: enough that the budget is what normally ends the loop.
MAX_TRANSIENT_RETRIES = 32

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
    #: Conversation so far, ascii and non-ascii counted apart. Two numbers
    #: rather than one because they cost very differently, and because keeping
    #: them lets the rates be re-derived from a recorded run instead of being
    #: inferred from a transcript (golden run #15).
    conversation_ascii: int = 0
    conversation_cjk: int = 0
    #: The same split for what the model itself produced, so input and output
    #: can be estimated separately.
    output_ascii: int = 0
    output_cjk: int = 0
    #: Thinking, measured but deliberately not charged. Golden #16's estimate
    #: ran a uniform 1.37x low across three lanes that each streamed three or
    #: four ThinkingBlocks, every one of them free -- but whether this backend
    #: re-sends them as input is unknown, and folding them in would prejudge it.
    thinking_ascii: int = 0
    thinking_cjk: int = 0
    #: The conversation as the estimate actually charges it: summed once per
    #: request, the way estimated_tokens_in is. The plain split says what the
    #: last request carried; this says what every request carried, which is the
    #: quantity the coefficients have to satisfy. Golden #17 had three completed
    #: lanes disagreeing with the rates in both directions at once and no way to
    #: solve for better ones without replaying transcripts again.
    charged_ascii: int = 0
    charged_cjk: int = 0
    #: API round trips so far. One goes out with the opening prompt and one
    #: more each time tool results come back -- which is NOT the same as the
    #: number of AssistantMessages. Golden #14's d1 streamed 27 of those for
    #: 13 requests (thinking, tool_use and text arrive as separate messages),
    #: so charging a turn's cost per message doubled the estimate.
    requests: int = 1
    #: What every request re-sends regardless of the conversation: the CLI's
    #: own system prompt, the charter, and the tool definitions. Set once at
    #: dispatch, because none of it is visible in the stream.
    fixed_tokens_per_request: int = 0
    #: Running estimate of cumulative input tokens. ``usage`` arrives only with
    #: the final message, so it is worth nothing to anything that has to act
    #: while the run is still going (golden run #11).
    estimated_tokens_in: int = 0
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
    def conversation_tokens(self) -> int:
        return estimate_budget_tokens(self.conversation_ascii, self.conversation_cjk)

    @property
    def estimated_tokens_out(self) -> int:
        return estimate_budget_tokens(self.output_ascii, self.output_cjk)

    @property
    def token_breakdown(self) -> dict[str, Any]:
        """Fresh / cached / written input, kept apart.

        Summing them into one number makes prompt-cache effectiveness invisible
        -- and the whole ephemeral-worker cost model rests on the charter prefix
        being cached.

        When nothing was reported at all, the estimate stands in for the
        totals. ``usage`` arrives with the final message, so a run the local
        ceiling interrupts never gets one -- and reporting zero would make the
        runs that cost the most the ones that count as free (golden run #13).
        The cache columns stay at zero rather than being invented, so
        ``cache_hit_ratio`` keeps measuring only what a backend actually said.
        """
        reported = {
            "in": self.tokens_in,
            "out": self.tokens_out,
            "fresh_in": _as_int(self.usage.get("input_tokens")),
            "cache_read": _as_int(self.usage.get("cache_read_input_tokens")),
            "cache_write": _as_int(self.usage.get("cache_creation_input_tokens")),
        }
        if any(reported.values()):
            return reported
        return {
            **reported,
            "in": self.estimated_tokens_in,
            "out": self.estimated_tokens_out,
            # Present only when the numbers are ours, so a consumer that does
            # not know about the key still reads a plausible total, and one
            # that does can decline to treat it as measurement.
            "estimated": True,
        }

    @property
    def estimate_breakdown(self) -> dict[str, int]:
        """What the estimate was built from, whether or not it was used."""
        return {
            "requests": self.requests,
            "conversation_tokens": self.conversation_tokens,
            # The raw split too: with it, one run is enough to solve for the
            # rates. Without it, every calibration is transcript archaeology
            # against excerpted tool results.
            "conversation_ascii": self.conversation_ascii,
            "conversation_cjk": self.conversation_cjk,
            # Measured, not charged. Recorded so that one run can say whether
            # this is the residual the estimate keeps missing.
            "thinking_ascii": self.thinking_ascii,
            "thinking_cjk": self.thinking_cjk,
            # Summed the way the estimate sums it, so the rates are solvable
            # from one recorded run:
            #     tokens_in == requests * fixed_per_request
            #                  + charged_ascii / A + charged_cjk * C
            "charged_ascii": self.charged_ascii,
            "charged_cjk": self.charged_cjk,
            "fixed_per_request": self.fixed_tokens_per_request,
            "tokens_in": self.estimated_tokens_in,
            # The ceiling judges input and output together; recording only the
            # input half left half of what it acts on unaccounted for.
            "tokens_out": self.estimated_tokens_out,
        }

    @property
    def budget_tokens(self) -> int:
        """Best available count of what this run has consumed.

        Prefers what the backend reported and falls back to the estimate,
        because the reported figure is authoritative but arrives too late to
        act on: ``usage`` is populated by the final message, so during the run
        it reads zero no matter how much has been spent.

        Both sides must answer the same question. The reported side has always
        summed input and output; the estimated side counted input alone, so the
        signal a lane reads all run long was blind to a term it would be judged
        on. Golden #16's d4 finished at 48,705 against a 40,000 budget with its
        estimate showing 56%, having never crossed the 75% warning -- and 37% of
        what it spent was output.
        """
        reported = self.tokens_in + self.tokens_out
        return max(reported, self.estimated_tokens_in + self.estimated_tokens_out)

    @property
    def saw_transient(self) -> bool:
        return any(s in TRANSIENT_STATUSES for s in self.retry_statuses)


#: Per tool result kept in the transcript. The transcript is a blob and never
#: reaches anyone's context, but a run making two dozen queries over a 138KB
#: table would otherwise write megabytes of duplicated rows.
MAX_TOOL_RESULT_CHARS: Final = 2_000


def _excerpt(text: str, limit: int = MAX_TOOL_RESULT_CHARS) -> str:
    """Head and tail, never head alone.

    The harness appends its own annotations -- the budget warning, refusal
    hints -- to the *end* of a tool result, so trimming from the back removes
    precisely what someone reading the transcript is looking for.
    """
    if len(text) <= limit:
        return text
    head, tail = limit * 2 // 3, limit // 3
    return f"{text[:head]}\n…[略過 {len(text) - head - tail} 字元]…\n{text[-tail:]}"


def _block_to_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ThinkingBlock):
        # The text is dropped -- a run streams a great deal of it and none of it
        # is analysis. The length is not: golden #16 could not test its own
        # leading hypothesis because this was the term the record did not keep.
        return {"type": "thinking", "chars": len(block.thinking)}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "name": block.name, "input": block.input}
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result", "tool_use_id": block.tool_use_id,
            "is_error": bool(getattr(block, "is_error", False)),
            "content": _excerpt(_tool_result_text(block)),
        }
    return {"type": type(block).__name__}


def _tool_result_text(block: Any) -> str:
    """A tool result's text, whatever shape the SDK hands it back in."""
    content = getattr(block, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return "" if content is None else str(content)


#: What one SDK request costs before a single word of conversation: the CLI's
#: own system prompt and the tool definitions. Derived by
#: spikes/spike15_token_rates.py, whose baseline probe cost 672 tokens for one
#: request carrying a charter this module prices at 240. Measured with a
#: two-tool lane, so a lane declaring more tools pays somewhat more than this.
#: Re-run the spike rather than adjusting it by feel.
FRAMEWORK_TOKENS_PER_REQUEST: Final = 432


def _estimated_request_cost(acc: Accumulated) -> int:
    """What the next request costs: everything re-sent, plus the conversation.

    Both halves matter and the earlier version had only one. Counting the
    conversation alone made golden #14's d1 estimate 45k against a reported
    62k; charging it once per streamed message rather than once per request
    made golden #13's d1 estimate 101k against a reported 72k. Same expression,
    opposite errors, because the ratio of conversation to overhead differs from
    run to run.
    """
    return acc.fixed_tokens_per_request + acc.conversation_tokens


def _charge_request(acc: Accumulated) -> None:
    """Charge one request to the estimate, and record what it was charged for.

    Recording the running sum rather than only the conversation's current split
    is what makes the rates solvable from a single run. The estimate sums the
    conversation once per request, so a calibration has to be able to see that
    same sum -- the final split alone leaves one equation in two unknowns.
    """
    acc.charged_ascii += acc.conversation_ascii
    acc.charged_cjk += acc.conversation_cjk
    acc.estimated_tokens_in += _estimated_request_cost(acc)


def _message_chars(message: Any) -> tuple[int, int]:
    """What this message adds to the conversation, ascii and non-ascii apart."""
    ascii_chars = cjk_chars = 0
    for block in _content_blocks(message):
        text = ""
        if isinstance(block, TextBlock):
            text = block.text
        elif isinstance(block, ToolUseBlock):
            text = json.dumps(block.input, ensure_ascii=False)
        elif isinstance(block, ToolResultBlock):
            text = _tool_result_text(block)
        a, c = split_chars(text)
        ascii_chars += a
        cjk_chars += c
    return ascii_chars, cjk_chars


def _thinking_chars(message: Any) -> tuple[int, int]:
    """Thinking, counted apart from the conversation it is not charged to.

    ``_message_chars`` recognises text, tool calls and tool results; a
    ThinkingBlock fell past all three and so cost nothing. Whether the backend
    re-sends it as input is what golden #17 is meant to settle, so this measures
    without yet deciding.
    """
    ascii_chars = cjk_chars = 0
    for block in _content_blocks(message):
        if isinstance(block, ThinkingBlock):
            a, c = split_chars(block.thinking)
            ascii_chars += a
            cjk_chars += c
    return ascii_chars, cjk_chars


def _consume(message: Any, acc: Accumulated) -> None:
    """Fold one streamed message into the accumulator."""
    ascii_chars, cjk_chars = _message_chars(message)
    acc.conversation_ascii += ascii_chars
    acc.conversation_cjk += cjk_chars
    if isinstance(message, AssistantMessage):
        acc.output_ascii += ascii_chars
        acc.output_cjk += cjk_chars
        thinking_ascii, thinking_cjk = _thinking_chars(message)
        acc.thinking_ascii += thinking_ascii
        acc.thinking_cjk += thinking_cjk
        acc.turns += 1
        blocks = [_block_to_dict(b) for b in message.content]
        acc.transcript.append({"role": "assistant", "content": blocks})
        acc.texts += [b.text for b in message.content if isinstance(b, TextBlock)]
        if getattr(message, "usage", None):
            acc.usage = dict(message.usage)
    elif isinstance(message, UserMessage):
        # Tool results arriving means another request is about to go out
        # carrying everything so far, which is the moment the estimate grows.
        acc.requests += 1
        _charge_request(acc)
        # Recording only the role -- which is what the catch-all below used to
        # do -- left the transcript with half the conversation: every question
        # the model asked was present and every answer it got was gone, so
        # "what did the model actually see" had no answer after the fact
        # (golden run #10).
        acc.transcript.append({
            "role": "user",
            "content": [_block_to_dict(b) for b in _content_blocks(message)],
        })
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


def _content_blocks(message: Any) -> list[Any]:
    """A message's blocks, tolerating a plain string body."""
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return [TextBlock(text=content)]
    return list(content or ())


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
    # Seeded rather than empty: the opening request already carries the charter,
    # the tool definitions and the task, and none of that ever appears in the
    # stream. A run that is cut off after two requests was never free.
    charter_ascii, charter_cjk = split_chars(charter)
    prompt_ascii, prompt_cjk = split_chars(prompt)
    acc = Accumulated(
        fixed_tokens_per_request=(
            FRAMEWORK_TOKENS_PER_REQUEST
            + estimate_budget_tokens(charter_ascii, charter_cjk)
        ),
        conversation_ascii=prompt_ascii,
        conversation_cjk=prompt_cjk,
    )
    _charge_request(acc)
    options = _options(request, profile, toolbox, charter=charter, enforce_schema=enforce_schema)
    try:
        budget = request.lane.type.token_budget
        async for message in transport.stream(prompt, options):
            _consume(message, acc)
            # The lane cannot see its own consumption; the toolbox attaches
            # this to tool results so it can stop and write before it is cut
            # off. Golden run #9 spent a whole budget on 24 queries and never
            # called write_finding -- the analysis died undocumented, and the
            # lane had no way to know it was about to.
            spent = acc.budget_tokens
            if budget:
                toolbox.budget_used = spent / budget
            # Local ceiling for backends that cannot enforce one server-side.
            # Reading acc.tokens_in here meant the ceiling only ever tripped on
            # the final message -- it relabelled a finished run rather than
            # stopping one (golden run #11).
            if not profile.supports(BackendCapability.TASK_BUDGET):
                if spent > budget:
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
        tokens=acc.token_breakdown, turns=acc.turns,
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
