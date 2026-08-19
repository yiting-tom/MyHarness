"""The plan: the orchestrator's state, kept outside its context.

Stored as an ordinary note artifact rather than as a bespoke mechanism, so it
inherits the pre-read token check, revisions, event trail and resumability
already built for notes (design.md D5). A handoff restart is then just "read the
plan, open a fresh client".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from myharness.artifacts.errors import ArtifactError
from myharness.artifacts.ids import ArtifactId
from myharness.artifacts.store import ArtifactStore
from myharness.artifacts.types import GrantSet

PLAN_NAME = "plan"
PLAN_MAX_TOKENS = 12_000

PLAN_TEMPLATE = """\
# 目標
{goal}

## 已確認結論
（尚無）

## 決策與理由
（尚無）

## Lane 狀態
（尚無）

## 開放問題
（尚無）
"""


@dataclass(frozen=True, slots=True)
class LaneSpec:
    """One lane the orchestrator wants to exist."""

    id: str
    type: str
    scope: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LaneSpec:
        return cls(id=str(raw["id"]), type=str(raw["type"]),
                   scope=str(raw.get("scope", "")))


def plan_id(job_id: str) -> ArtifactId:
    return ArtifactId(job_id, "note", PLAN_NAME)


async def read_plan(store: ArtifactStore, job_id: str) -> tuple[str | None, int]:
    """Return the plan text and its revision, or (None, 0) if there is none."""
    aid = plan_id(job_id)
    grants = GrantSet.unrestricted(job_id)
    try:
        meta = await store.stat(aid, grants=grants)
    except ArtifactError:
        return None, 0
    try:
        return await store.read_note(aid, grants=grants, max_tokens=PLAN_MAX_TOKENS), meta.revision
    except ArtifactError:
        return None, meta.revision


async def write_plan(
    store: ArtifactStore, job_id: str, text: str, *, produced_by: str = "orchestrator"
) -> int:
    meta = await store.put_note(job_id, PLAN_NAME, text, produced_by=produced_by)
    return meta.revision


def initial_plan(goal: str) -> str:
    return PLAN_TEMPLATE.format(goal=goal)
