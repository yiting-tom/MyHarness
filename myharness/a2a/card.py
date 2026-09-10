"""The agent card: what this harness says it is, before anyone asks it anything.

Two skills rather than two output modes. `AgentSkill.output_modes` and
`AgentCard.default_output_modes` are MEDIA TYPES -- declaring "price list" as an
output mode would be a misuse of the field, not a use of it (spike #13). The
distinction rides on two skills instead, which is where it belongs: they are the
two things a caller can ask for, and they map onto `analysis_result` and
`analysis_drill`, which already exist on the MCP boundary.

The price-list convention is declared as an extension with `required=true`. The
proto's words for that field are "the client must understand and comply", which
is exactly the property this boundary needs: a client that does not know a price
list from a report is told so, instead of quietly reading a table of contents as
though it were the analysis.
"""

from __future__ import annotations

from a2a.types import (
    AgentCapabilities, AgentCard, AgentExtension, AgentInterface, AgentSkill,
)
from a2a.utils.constants import PROTOCOL_VERSION_CURRENT

#: The convention: an artifact carrying this URI is a table of contents with a
#: price against each entry, never the content itself. Versioned in the URI so a
#: later change is a different extension rather than a silent reinterpretation.
PRICE_LIST_EXTENSION = "https://myharness.dev/a2a/ext/section-price-list/v1"

SKILL_PRICE_LIST = "analysis.result"
SKILL_FULL_TEXT = "analysis.sections"

AGENT_NAME = "MyHarness"
AGENT_VERSION = "0.1.0"

#: The proto calls protocol_binding "an open form string"; JSONRPC is the one
#: this endpoint speaks. Named here so the card and the routes cannot drift.
PROTOCOL_BINDING = "JSONRPC"

#: Declared, because the wire default is not this. A request that omits the
#: `A2A-Version` header is read as 0.3 and refused by the 1.0 handler, so a
#: client that never saw a version on the card would meet that as a puzzle.
PROTOCOL_VERSION = PROTOCOL_VERSION_CURRENT


def price_list_extension() -> AgentExtension:
    return AgentExtension(
        uri=PRICE_LIST_EXTENSION,
        required=True,
        description=(
            "分析結果預設回傳摘要與章節價目表，不是報告全文。每一節帶 est_tokens，"
            "呼叫方看完價錢再決定讀哪幾節。不理解這個約定的客戶端會把目錄當成報告讀，"
            "所以這個 extension 是 required。"
        ),
    )


def build_agent_card(url: str) -> AgentCard:
    """The card served at /.well-known/agent-card.json.

    `url` is where this endpoint actually listens. It is a parameter rather than
    a constant because the card is a promise about a reachable address, and a
    wrong one is worse than none.
    """
    return AgentCard(
        name=AGENT_NAME,
        version=AGENT_VERSION,
        supported_interfaces=[
            AgentInterface(url=url, protocol_binding=PROTOCOL_BINDING,
                           protocol_version=PROTOCOL_VERSION),
        ],
        description=(
            "多 agent 資料分析 harness。外部化狀態、短命執行者，"
            "原始資料不會進到呼叫方的 context。"
        ),
        capabilities=AgentCapabilities(
            streaming=True,
            # The MCP boundary pushes nothing and neither does this one: the
            # client polls, or holds a stream open. "Long-poll is the push."
            push_notifications=False,
            extensions=[price_list_extension()],
        ),
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["application/json"],
        skills=[
            AgentSkill(
                id=SKILL_PRICE_LIST,
                name="分析結果（摘要與章節價目表）",
                description=(
                    "回傳執行摘要、關鍵發現，以及報告每一節的 id 與 est_tokens。"
                    "**不回報告全文。** 這是預設，也是這個 harness 最外層的那道閘："
                    "呼叫方先看見價錢，再決定把哪幾節讀進自己的 context。"
                ),
                tags=["analysis", "summary", "price-list", "default"],
                examples=["分析這份交易資料，找出異常樣態"],
            ),
            AgentSkill(
                id=SKILL_FULL_TEXT,
                name="章節全文（逐節取得）",
                description=(
                    "取得指定章節的全文。逐節取，沿用與價目表同一組章節 id 與同一道"
                    "token 上限 —— 沒有「把整份報告倒出來」的路徑，因為那會是第二道"
                    "上限，而這個專案的上限是實際執行的，不是宣告的。"
                ),
                tags=["analysis", "sections", "full-text", "explicit"],
                examples=["取得「方法與可複查性」這一節"],
            ),
        ],
    )


__all__ = [
    "AGENT_NAME", "AGENT_VERSION", "PRICE_LIST_EXTENSION", "PROTOCOL_BINDING",
    "PROTOCOL_VERSION",
    "SKILL_FULL_TEXT", "SKILL_PRICE_LIST",
    "build_agent_card", "price_list_extension",
]
