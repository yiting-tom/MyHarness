"""What one streamed run has told us so far, and what it has cost.

Split out of worker.py: the worker decides *when* to run, retry and stop; this
module only folds each streamed message into an ``Accumulated`` and prices the
conversation against the measured rates. Everything here is usable mid-stream,
because an exhausted ``task_budget`` raises before any ``ResultMessage``
arrives (spikes/RESULTS.md §Spike #3c).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Final

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from myharness.lanes.budget import TextCount, chargeable
from myharness.lanes.budget import count as count_text

#: HTTP statuses the SDK reports through ``api_retry`` system messages.
TRANSIENT_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


@dataclass
class Accumulated:
    """What we know so far, usable even if the run dies mid-stream."""

    texts: list[str] = field(default_factory=list)
    transcript: list[dict[str, Any]] = field(default_factory=list)
    structured: Any = None
    result: ResultMessage | None = None
    turns: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    #: Conversation so far, counted in the three quantities the rates are
    #: priced against rather than in characters. Kept as counts so the rates can
    #: be re-derived from a recorded run instead of being inferred from a
    #: transcript (golden run #15).
    conversation: TextCount = field(default_factory=TextCount)
    #: The same count for what the model itself produced, so input and output
    #: can be estimated separately.
    output: TextCount = field(default_factory=TextCount)
    #: Thinking, measured but deliberately not charged. Golden #16's estimate
    #: ran a uniform 1.37x low across three lanes that each streamed three or
    #: four ThinkingBlocks, every one of them free -- but whether this backend
    #: re-sends them as input is unknown, and folding them in would prejudge it.
    thinking: TextCount = field(default_factory=TextCount)
    #: The conversation as the estimate actually charges it: summed once per
    #: request, the way estimated_tokens_in is. The plain split says what the
    #: last request carried; this says what every request carried, which is the
    #: quantity the coefficients have to satisfy. Golden #17 had three completed
    #: lanes disagreeing with the rates in both directions at once and no way to
    #: solve for better ones without replaying transcripts again.
    charged: TextCount = field(default_factory=TextCount)
    #: Transcript rows from earlier attempts of this same dispatch. ``_run_once``
    #: builds a fresh accumulator per attempt, which is what keeps a re-prompt's
    #: context clean -- and took the evidence with it: goldens #21 and #22
    #: re-prompted four dispatches, and for every one of them the output that
    #: provoked the re-prompt was unrecoverable. carried_tokens already survived
    #: that rebinding; this is the same trick for the rows.
    carried_transcript: list[dict[str, Any]] = field(default_factory=list)
    #: What earlier attempts of this same dispatch already spent. A schema
    #: re-prompt starts a fresh run against the backend, and the accumulator
    #: used to start fresh with it: spike #21 recorded 17 requests on the wire
    #: for a dispatch whose accountant saw 5. Three attempts spent three budgets
    #: and the ceiling, which is the only thing standing between a lane and an
    #: unbounded bill, never saw the first two.
    carried_tokens: int = 0
    #: Which attempt this is, counted from one. Recorded so a dispatch says it
    #: was re-prompted instead of leaving it to be inferred.
    attempt: int = 1
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
    #: Whether this run's backend caches the repeated prefix, from the profile's
    #: PROMPT_CACHING. Declared rather than probed, like every other capability:
    #: an unknown proxy that does not cache must not be given the discount, and
    #: SELF_HOSTED reports fresh_in == in on every golden run since #10.
    caches_prompts: bool = False
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
    def full_transcript(self) -> list[dict[str, Any]]:
        """Every attempt of this dispatch, oldest first."""
        return [*self.carried_transcript, *self.transcript]

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
    def chargeable_tokens_in(self) -> int:
        """What the reported input cost, as opposed to how large it was."""
        return chargeable(
            _as_int(self.usage.get("input_tokens")),
            _as_int(self.usage.get("cache_read_input_tokens")),
            _as_int(self.usage.get("cache_creation_input_tokens")),
        )

    @property
    def conversation_tokens(self) -> int:
        return self.conversation.tokens

    @property
    def estimated_cached_tokens(self) -> int:
        """Of the estimated input, how much a caching backend re-reads.

        Request *i* carries the fixed block and everything said up to request
        *i-1*; all of it went out unchanged one request ago, which is exactly
        what a cache prefix is. So the first request pays in full and each one
        after it re-reads ``fixed + conversation-as-of-the-previous-request``.
        Summed over the run that is ``(requests - 1) * fixed`` plus every
        conversation total except the current one, and ``charged`` is already
        the sum of all of them.

        The per-turn injections are left on the fresh side even though they too
        become prefix a request later. Under-discounting is the safe direction
        for a ceiling, and they are 6% of a run where the fixed block is 36%.
        """
        if not self.caches_prompts or self.requests < 2:
            return 0
        conversation_prefix = max(0, self.charged.tokens - self.conversation.tokens)
        return (self.requests - 1) * self.fixed_tokens_per_request + conversation_prefix

    @property
    def estimated_chargeable_tokens_in(self) -> int:
        """The estimate, priced the way the reported figure is priced."""
        cached = min(self.estimated_cached_tokens, self.estimated_tokens_in)
        return chargeable(self.estimated_tokens_in - cached, cached)

    @property
    def estimated_tokens_out(self) -> int:
        return self.output.tokens

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
            # What the ceiling acted on. Without it a dispatch stopped at
            # 60,000 whose breakdown reads 63,752 cannot be checked against the
            # budget it was judged by, because the two are in different units.
            "chargeable": self.chargeable_tokens_in,
        }
        if any(reported.values()):
            return reported
        return {
            **reported,
            "in": self.estimated_tokens_in,
            "out": self.estimated_tokens_out,
            "chargeable": self.estimated_chargeable_tokens_in,
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
            **self.conversation.to_dict("conversation"),
            # Measured, not charged. Recorded so that one run can say whether
            # this is the residual the estimate keeps missing.
            **self.thinking.to_dict("thinking"),
            # Summed the way the estimate sums it, so the rates are solvable
            # from one recorded run:
            #     tokens_in == requests * fixed_per_request
            #                  + TOKENS_PER_TURN_INJECTION * requests(requests+1)/2
            #                  + charged_words * W + charged_punct * P
            #                  + charged_cjk * C
            **self.charged.to_dict("charged"),
            # What the re-prompts before this attempt cost, and how many there
            # were. Without these a retried dispatch reads as a cheap one.
            "carried_tokens": self.carried_tokens,
            "attempts": self.attempt,
            "fixed_per_request": self.fixed_tokens_per_request,
            "tokens_in": self.estimated_tokens_in,
            # The ceiling judges input and output together; recording only the
            # input half left half of what it acts on unaccounted for.
            "tokens_out": self.estimated_tokens_out,
            # tokens_in stays the raw estimate, so the identity above keeps
            # solving against what a backend reports. This is the part of it
            # that went out as a repeated prefix and was therefore charged at
            # CACHE_READ_PRICE -- zero on a backend that does not declare
            # caching, which is what makes the discount auditable after the run.
            "cached_tokens": self.estimated_cached_tokens,
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

        Both sides are priced rather than counted. Adding fresh, cached and
        written input together charges a re-sent prefix at the price of new
        text, which is most of what a multi-turn lane sends: golden #22's d1
        was stopped at 63,752 for a conversation of 6,795, and its last request
        carried 13% of the model's window.
        """
        reported = self.chargeable_tokens_in + self.tokens_out
        return self.carried_tokens + max(
            reported,
            self.estimated_chargeable_tokens_in + self.estimated_tokens_out,
        )

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


#: What one SDK request costs before a single word of conversation and before
#: any tool is declared. Read off the wire by spikes/spike26_solve_rates.py:
#: 118 tokens of the CLI's own system prompt, 261 tokens of text the CLI injects
#: into the first user message and then re-sends on every request -- a
#: <system-reminder> carrying CLAUDE.md, plus a <total_tokens> note per turn --
#: and 41 tokens the regression could not attribute to any text at all.
#:
#: That injected block is why six golden runs estimated 29-34% low while both
#: candidate explanations measured near zero. It never appears in the stream, so
#: the accumulator cannot see it; it is only visible from in front of the CLI.
#: It scales with the project's CLAUDE.md, so a deployment with a much larger
#: one should re-run the spike rather than trust this number.
FRAMEWORK_TOKENS_PER_REQUEST: Final = 336

#: Every declared tool's JSON definition rides along on every request. Measured
#: at 665 ascii characters for two tools, priced at the rates above. The old
#: flat constant charged a six-tool analyst what a two-tool critic pays, and
#: spike #19 had already seen the gap without being able to size it.
TOKENS_PER_TOOL_DECLARATION: Final = 98

#: And what each turn leaves behind. The CLI appends a note of its own to every
#: request -- the remaining context window, and whatever else it has to say --
#: and those notes accumulate: request i carries i of them. They arrive as
#: system-role messages the SDK does not stream, so the accumulator is blind to
#: them, and a flat per-request constant charges the tenth request exactly what
#: it charged the first.
#:
#: This is the term that was making the rates look unstable. Without it the
#: ascii rate had to absorb a cost that grows with the turn count, so it came
#: out high on short runs and low on long ones. With it, spike #26's two runs
#: solve to coefficients within 3% of each other and each predicts the other's
#: requests to within 1.7% -- against 38.8% before (spikes/RESULTS.md §Spike #26).
TOKENS_PER_TURN_INJECTION: Final = 34


def _estimated_request_cost(acc: Accumulated) -> int:
    """What the next request costs: everything re-sent, plus the conversation.

    Both halves matter and the earlier version had only one. Counting the
    conversation alone made golden #14's d1 estimate 45k against a reported
    62k; charging it once per streamed message rather than once per request
    made golden #13's d1 estimate 101k against a reported 72k. Same expression,
    opposite errors, because the ratio of conversation to overhead differs from
    run to run.
    """
    return (acc.fixed_tokens_per_request
            + TOKENS_PER_TURN_INJECTION * acc.requests
            + acc.conversation_tokens)


def _turns_affordable(acc: Accumulated, remaining: int) -> float:
    """How many more requests this run can pay for at its current size.

    The quantity the budget signal is expressed in, because a share of the
    budget is not a unit of work: 10% of 60,000 buys nine requests at turn
    three and less than one at turn fifteen, when the conversation being
    re-sent has grown to fill it. Six dispatches were gated on the share and
    all six landed nothing (tools.py, GATE_TURNS_LEFT).

    Priced at the full re-send cost even on a backend that caches, where the
    marginal request is cheaper than this says. That over-states the cost and
    so signals early, which is the safe direction: signalling early costs a
    lane a query or two, and signalling late has cost six of them everything.
    """
    cost = _estimated_request_cost(acc)
    if cost <= 0:
        return math.inf
    return max(0.0, remaining / cost)


def _charge_request(acc: Accumulated) -> None:
    """Charge one request to the estimate, and record what it was charged for.

    Recording the running sum rather than only the conversation's current split
    is what makes the rates solvable from a single run. The estimate sums the
    conversation once per request, so a calibration has to be able to see that
    same sum -- the final split alone leaves one equation in two unknowns.
    """
    acc.charged += acc.conversation
    acc.estimated_tokens_in += _estimated_request_cost(acc)


def _message_count(message: Any) -> TextCount:
    """What this message adds to the conversation, counted for pricing."""
    total = TextCount()
    for block in _content_blocks(message):
        text = ""
        if isinstance(block, TextBlock):
            text = block.text
        elif isinstance(block, ToolUseBlock):
            text = json.dumps(block.input, ensure_ascii=False)
        elif isinstance(block, ToolResultBlock):
            text = _tool_result_text(block)
        total += count_text(text)
    return total


def _thinking_count(message: Any) -> TextCount:
    """Thinking, counted apart from the conversation it is not charged to.

    ``_message_chars`` recognises text, tool calls and tool results; a
    ThinkingBlock fell past all three and so cost nothing. Whether the backend
    re-sends it as input is what golden #17 is meant to settle, so this measures
    without yet deciding.
    """
    total = TextCount()
    for block in _content_blocks(message):
        if isinstance(block, ThinkingBlock):
            total += count_text(block.thinking)
    return total


def _consume(message: Any, acc: Accumulated) -> None:
    """Fold one streamed message into the accumulator."""
    added = _message_count(message)
    acc.conversation += added
    if isinstance(message, AssistantMessage):
        acc.output += added
        acc.thinking += _thinking_count(message)
        acc.turns += 1
        blocks = [_block_to_dict(b) for b in message.content]
        acc.transcript.append({"role": "assistant", "content": blocks})
        acc.texts += [b.text for b in message.content if isinstance(b, TextBlock)]
        usage = getattr(message, "usage", None)
        if usage:
            acc.usage = dict(usage)
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


#: How much of a tool call's arguments a step event carries. Enough to tell
#: one query from the next while it scrolls past; the transcript has the rest.
STEP_ARG_CHARS: Final = 160


def _arg_excerpt(value: Any) -> str:
    """A tool call's input on one line, bounded.

    ``key=value`` rather than JSON: a monitor shows this in a node a few hundred
    pixels wide, and quotes and braces are most of what JSON would spend it on.
    """
    if isinstance(value, dict):
        text = " ".join(
            f"{k}={v if isinstance(v, str | int | float | bool) else _compact(v)}"
            for k, v in value.items()
        )
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= STEP_ARG_CHARS else text[: STEP_ARG_CHARS - 1] + "…"


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _short_tool(name: str) -> str:
    """``mcp__lane__run_query`` -> ``run_query``."""
    return name.rsplit("__", 1)[-1] if name.startswith("mcp__") else name


def step_event(message: Any, acc: Accumulated, budget: int) -> dict[str, Any] | None:
    """What one streamed message did, as a ``lane.step`` payload -- or None.

    Written while the lane runs, because the transcript is written when it
    ends: golden #25's d1 spent six minutes and 22 queries and wrote nothing,
    and all anyone watching could see for those six minutes was a timer.

    Sizes, never contents. Reasoning text is not kept anywhere, and a tool
    result's body belongs to the transcript -- the event stream is for watching
    and auditing, not a second copy of the conversation.
    """
    spent = acc.budget_tokens
    common: dict[str, Any] = {
        "attempt": acc.attempt, "turn": acc.turns, "spent": spent,
        "pct": round(spent / budget, 3) if budget else None,
    }
    if isinstance(message, AssistantMessage):
        blocks = list(message.content)
        return {
            "phase": "turn", **common,
            "calls": [{"tool": _short_tool(b.name), "arg": _arg_excerpt(b.input)}
                      for b in blocks if isinstance(b, ToolUseBlock)],
            "thinking_chars": sum(len(b.thinking) for b in blocks
                                  if isinstance(b, ThinkingBlock)),
            # Counted apart from the characters: the self-hosted backend sends
            # thinking blocks with nothing in them, and "0 characters" alone
            # cannot tell an empty block from no block at all.
            "thinking_blocks": sum(isinstance(b, ThinkingBlock) for b in blocks),
            "text_chars": sum(len(b.text) for b in blocks if isinstance(b, TextBlock)),
        }
    if isinstance(message, UserMessage):
        results = [b for b in _content_blocks(message) if isinstance(b, ToolResultBlock)]
        if not results:
            return None
        return {
            "phase": "results", **common,
            "results": [{"chars": len(_tool_result_text(b)),
                         "error": bool(getattr(b, "is_error", False))} for b in results],
        }
    return None


def _content_blocks(message: Any) -> list[Any]:
    """A message's blocks, tolerating a plain string body."""
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return [TextBlock(text=content)]
    return list(content or ())

