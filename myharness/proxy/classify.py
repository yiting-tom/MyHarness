"""One model call that answers one question: whose data is this?

Single-shot and stateless. It sees the routing table and a bounded sample, and
nothing else -- not the plan, not the goal, not a finding. DESIGN §4.2 states
that as "zero context sharing", and the reason it is enforced here rather than
left as an intention is that the tempting improvement is always to give the
classifier more to go on. A classifier that can see the plan is a second
planner, with none of the first one's budget controls (design.md D3).

Failure never blocks ingress. Timeout, a dead endpoint, a hallucinated lane, no
table at all: every one of them comes back as "unrouted, and here is why"
(design.md D5).
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions

from myharness.backends.profile import BackendProfile, ModelTier
from myharness.lanes.transport import SdkTransport, WorkerTransport
from myharness.orchestrator.routing import RoutingTable
from myharness.proxy.sample import Sample

#: Classification is one short call. Running long means something is wrong, not
#: that it is nearly done (design.md D6).
DEFAULT_TIMEOUT_S = 20.0
#: The model gets one turn. There is nothing to iterate on.
MAX_TURNS = 1

SYSTEM_PROMPT = (
    "你是一個資料分流器。你只做一件事：判斷一份剛進來的資料屬於哪一條 lane。"
    "你看不到這個 job 的目標與計畫，也不需要看 —— 只根據下面的 lane 清單與資料樣本判斷。"
    "不確定時回 null，不要硬猜。"
)

PROMPT = """\
# 可用的 lane

{routing}

# 這份資料

{meta}

## 開頭樣本{truncation}

```
{sample}
```

# 你的回覆

只輸出一個 JSON 物件，不要有其他文字：

{{"lane": "<lane 名稱，或 null>", "confidence": "high|medium|low", "reason": "<一句話>"}}

lane 必須是上面清單裡的名稱之一。判斷不出來就用 null。
"""


class Unrouted(StrEnum):
    """Why a payload has no lane. The three mean different things to a caller."""

    #: The orchestrator has not published a routing table yet.
    NO_TABLE = "no_table"
    #: The classifier ran and said it could not tell.
    NO_MATCH = "no_match"
    #: The classifier did not produce a usable answer.
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Routing:
    """The classifier's answer. ``lane`` is None when nothing was chosen."""

    lane: str | None
    confidence: str = "low"
    reason: str = ""
    unrouted: Unrouted | None = None
    usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    #: Recorded because "which model actually ran" was unanswerable from the
    #: event stream the first time the numbers looked wrong.
    model: str = ""

    @property
    def routed(self) -> bool:
        return self.lane is not None

    def to_event(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "confidence": self.confidence,
            "reason": self.reason[:200],
            "unrouted": str(self.unrouted) if self.unrouted else None,
            "model": self.model,
            "usd": round(self.usd, 6),
            "tokens": {"in": self.tokens_in, "out": self.tokens_out},
        }


def build_prompt(table: RoutingTable, meta_text: str, sample: Sample) -> str:
    """Everything the classifier will ever see.

    Deliberately takes the sample and the metadata as arguments rather than a
    job id: there is no handle here through which the plan could later be
    reached.
    """
    truncation = ""
    if sample.truncated:
        truncation = "（已截斷，只取開頭）"
    return PROMPT.format(
        routing=table.describe(),
        meta=meta_text,
        truncation=truncation,
        sample=sample.text or "(empty)",
    )


async def classify(
    table: RoutingTable,
    meta_text: str,
    sample: Sample,
    *,
    profile: BackendProfile,
    transport: WorkerTransport | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Routing:
    """Ask which lane. Never raises; every failure is an unrouted value."""
    if not table.open_entries:
        return Routing(None, unrouted=Unrouted.NO_TABLE,
                       reason="orchestrator 尚未宣告任何開放的 lane")

    transport = transport or SdkTransport()
    model = profile.resolve_model(ModelTier.CHEAP)
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        max_turns=MAX_TURNS,
        # The classifier has no tools. It reads and answers.
        allowed_tools=[],
        disallowed_tools=BackendProfile.disallowed_for(()),
        env=profile.to_sdk_env(),
    )
    prompt = build_prompt(table, meta_text, sample)

    try:
        text, usd, tin, tout = await asyncio.wait_for(
            _collect(transport, prompt, options), timeout_s
        )
    except TimeoutError:
        return Routing(None, unrouted=Unrouted.FAILED,
                       reason=f"分流器逾時（{timeout_s:.0f}s）", model=model)
    except Exception as exc:  # noqa: BLE001 - ingress must not depend on this
        return Routing(None, unrouted=Unrouted.FAILED,
                       reason=f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}",
                       model=model)

    answer = _parse(text)
    if answer is None:
        return Routing(None, unrouted=Unrouted.FAILED,
                       reason="分流器沒有回傳可解析的 JSON",
                       usd=usd, tokens_in=tin, tokens_out=tout, model=model)

    lane = answer.get("lane")
    confidence = str(answer.get("confidence") or "low").lower()
    reason = str(answer.get("reason") or "")[:200]

    if lane in (None, "", "null"):
        return Routing(None, confidence=confidence, reason=reason,
                       unrouted=Unrouted.NO_MATCH, usd=usd,
                       tokens_in=tin, tokens_out=tout, model=model)

    lane = str(lane).strip()
    if not table.accepts_from(lane):
        # A name the table does not have, or one that is closed. Either way it
        # is not a routing decision anyone can act on.
        return Routing(
            None, confidence=confidence,
            reason=f"分流器回了 {lane!r}，但它不在開放的 lane 清單裡",
            unrouted=Unrouted.NO_MATCH, usd=usd, tokens_in=tin, tokens_out=tout,
            model=model,
        )
    return Routing(lane, confidence=confidence, reason=reason,
                   usd=usd, tokens_in=tin, tokens_out=tout, model=model)


async def _collect(
    transport: WorkerTransport, prompt: str, options: ClaudeAgentOptions
) -> tuple[str, float, int, int]:
    texts: list[str] = []
    usd = 0.0
    tin = tout = 0
    async for message in transport.stream(prompt, options):
        for block in getattr(message, "content", None) or []:
            if (chunk := getattr(block, "text", None)) is not None:
                texts.append(str(chunk))
        usd += float(getattr(message, "total_cost_usd", 0.0) or 0.0)
        usage = getattr(message, "usage", None) or {}
        tin += _as_int(usage.get("input_tokens"))
        tout += _as_int(usage.get("output_tokens"))
    return "\n".join(texts), usd, tin, tout


def _as_int(value: Any) -> int:
    """Providers send nulls where the schema says integer."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


_JSON_OBJECT = re.compile(r"\{.*\}", re.S)


def _parse(text: str) -> dict[str, Any] | None:
    """The model was asked for bare JSON; accept it wrapped in prose or fences."""
    candidate = text.strip()
    if not candidate:
        return None
    for attempt in (candidate, _strip_fence(candidate)):
        try:
            parsed = json.loads(attempt)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    match = _JSON_OBJECT.search(candidate)
    if match:
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped.split("\n", 1)[-1]
    return body.rsplit("```", 1)[0].strip()


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "PROMPT",
    "SYSTEM_PROMPT",
    "Routing",
    "Unrouted",
    "build_prompt",
    "classify",
]
