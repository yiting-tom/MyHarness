"""What one dispatch actually did, step by step.

`inspect` answers the dispatch-level question -- what was this report based on.
Every problem diagnosed across four golden runs lived one level below it: a
re-prompt that returned the JSON Schema instead of an instance, a re-prompt that
returned a bare artifact id, an analyst that ran thirteen queries and returned
``artifact: null``. All three answers were in ``blobs/traces/dN``, and each was
found by a throwaway script that was then thrown away.

This module is the reader that file never had. It is pure: rows in, steps out,
no IO and no inference. Three things it deliberately cannot recover, because
they were never written down:

* **Reasoning text.** ``_block_to_dict`` keeps ``{"type": "thinking",
  "chars": N}`` and drops the words. On a backend that returns empty thinking
  blocks even N is zero. A step therefore records *where* reasoning happened,
  never what it was -- laying the tool calls out in a row under the heading
  "reasoning" would make a record that does not exist look like one that does.
* **Which result answers which call.** The transcript keeps ``tool_use_id`` on
  the result and drops it on the call, so pairing is by order. That holds only
  while no turn issues two calls at once; when it does not hold, the count of
  results that found no call is reported rather than hidden.
* **Token counts.** The transcript carries none. The only per-turn budget
  signal in existence is the note the harness appends to a tool result.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

#: The harness appends its own instruction after a tool's own output. Keeping
#: them apart matters: one is what the tool said, the other is what the harness
#: told the model to do about it, and a reader who cannot tell them apart will
#: attribute the instruction to the tool.
_HARNESS_NOTE: Final = re.compile(r"\[harness\]\s*(?P<body>.*)$", re.S)
_BUDGET_PCT: Final = re.compile(r"預算已用\s*(\d+)\s*%")
#: `_excerpt` writes exactly this when it trims the middle out of a result.
_SKIPPED: Final = re.compile(r"…\[略過 (\d+) 字元\]…")


class StepKind(StrEnum):
    #: A fresh `query()` began -- the second one on a re-prompted dispatch.
    ATTEMPT = "attempt"
    #: Reasoning happened here. Its content was not kept.
    THINK = "think"
    #: The model's own prose.
    TEXT = "text"
    CALL = "call"
    RESULT = "result"
    #: The run ended (one attempt's ResultMessage).
    END = "end"
    #: A SystemMessage that is not an init -- api_retry, and whatever comes next.
    SYSTEM = "system"
    #: A block type this parser does not know. Kept, because dropping it would
    #: make the step look like it never happened.
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class Step:
    """One thing that happened, in the order it happened."""

    kind: StepKind
    #: Which assistant turn this belongs to; 0 for rows outside a turn.
    turn: int = 0

    # -- THINK
    #: Length of the reasoning that was not kept. Zero has two meanings, which
    #: ``empty_reasoning`` separates.
    chars: int = 0

    # -- TEXT
    text: str = ""

    # -- CALL
    name: str = ""
    args: Mapping[str, Any] = field(default_factory=dict)

    # -- RESULT
    error: bool = False
    #: What the tool itself returned, with the harness's note removed.
    body: str = ""
    #: What the harness appended, if anything.
    harness: str = ""
    #: Share of the lane's budget spent, as the harness reported it in that
    #: note. The only per-turn token signal the transcript contains.
    budget_pct: int | None = None
    #: Characters `_excerpt` removed from the middle when storing this.
    skipped: int = 0
    #: Index into ``Trace.steps`` of the call this answers; None when the
    #: ordering could not pair it.
    answers: int | None = None

    # -- ATTEMPT / END / SYSTEM / OTHER
    n: int = 0
    subtype: str = ""
    turns: int = 0
    detail: str = ""

    @property
    def empty_reasoning(self) -> bool:
        """The backend returned a thinking block with nothing in it.

        Distinct from reasoning that happened and was not stored: golden #24's
        analyst emitted seven of these, 53 bytes each, and treating them as
        "we did not keep it" would credit the model with thinking it never did.
        """
        return self.kind is StepKind.THINK and self.chars == 0


@dataclass(frozen=True, slots=True)
class Trace:
    """One dispatch's transcript, parsed."""

    steps: tuple[Step, ...] = ()
    turns: int = 0
    attempts: int = 0
    #: Results that no call could be found for. Nonzero means the ordering
    #: assumption broke and the pairing shown is not trustworthy.
    unpaired_results: int = 0

    @property
    def calls(self) -> list[Step]:
        return [s for s in self.steps if s.kind is StepKind.CALL]

    @property
    def budget_marks(self) -> list[Step]:
        return [s for s in self.steps if s.budget_pct is not None]

    @property
    def failures(self) -> list[Step]:
        return [s for s in self.steps if s.kind is StepKind.RESULT and s.error]


def parse_trace(rows: Sequence[Mapping[str, Any]]) -> Trace:
    """A stored transcript into its steps."""
    steps: list[Step] = []
    turn = attempt = unpaired = 0
    open_calls: list[int] = []

    for row in rows:
        role = row.get("role")
        raw = row.get("content")
        blocks: list[Mapping[str, Any]] = raw if isinstance(raw, list) else []

        if role == "system":
            if row.get("subtype") == "init":
                attempt += 1
                steps.append(Step(StepKind.ATTEMPT, n=attempt))
            else:
                steps.append(Step(StepKind.SYSTEM, subtype=str(row.get("subtype") or "")))

        elif role == "assistant":
            turn += 1
            for block in blocks:
                if (step := _assistant_block(block, turn)) is not None:
                    if step.kind is StepKind.CALL:
                        open_calls.append(len(steps))
                    steps.append(step)

        elif role == "user":
            for block in blocks:
                if block.get("type") != "tool_result":
                    continue
                answers = open_calls.pop(0) if open_calls else None
                unpaired += answers is None
                steps.append(_result_block(block, answers))

        elif role == "result":
            steps.append(Step(
                StepKind.END, subtype=str(row.get("subtype") or ""),
                error=bool(row.get("is_error")), turns=int(row.get("turns") or 0),
            ))

    return Trace(tuple(steps), turns=turn, attempts=attempt,
                 unpaired_results=unpaired)


def _assistant_block(block: Mapping[str, Any], turn: int) -> Step | None:
    kind = block.get("type")
    if kind == "thinking":
        return Step(StepKind.THINK, turn=turn, chars=int(block.get("chars") or 0))
    if kind == "text":
        return Step(StepKind.TEXT, turn=turn, text=str(block.get("text") or ""))
    if kind == "tool_use":
        args = block.get("input")
        return Step(StepKind.CALL, turn=turn, name=str(block.get("name") or ""),
                    args=args if isinstance(args, Mapping) else {})
    return Step(StepKind.OTHER, turn=turn, detail=str(kind))


def _result_block(block: Mapping[str, Any], answers: int | None) -> Step:
    content = block.get("content")
    text = content if isinstance(content, str) else str(content or "")
    note = _HARNESS_NOTE.search(text)
    harness = note.group("body").strip() if note else ""
    pct = _BUDGET_PCT.search(harness) if harness else None
    skipped = _SKIPPED.search(text)
    return Step(
        StepKind.RESULT,
        error=bool(block.get("is_error")),
        body=(text[: note.start()] if note else text).rstrip(),
        harness=harness,
        budget_pct=int(pct.group(1)) if pct else None,
        skipped=int(skipped.group(1)) if skipped else 0,
        answers=answers,
    )


__all__ = ["Step", "StepKind", "Trace", "parse_trace"]
