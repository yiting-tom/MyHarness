"""Running the orchestrator: one conversation, bounded, with a way out.

The orchestrator keeps its reasoning continuity across a job (DESIGN.md decision
#7) because that continuity is the most valuable thing it has. The cost is that
its context only grows. Three things bound it:

* ``peek`` draws on a hard budget, turning the layer's largest variable into a
  constant;
* the plan lives outside the conversation, so a fresh client can take over;
* at a threshold the loop asks for a handoff and restarts from that plan.

The restart is a handoff, not amnesia. A normal job never reaches it, but a long
one dies without it -- so it has to be exercised, not merely documented.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
)

from myharness.backends.profile import BackendProfile, registry as backends
from myharness.events.types import CTX, HANDOFF_RESTART, JOB_FINISH, JOB_START
from myharness.jobs.runner import JobRunner
from myharness.jobs.spec import JobPhase
from myharness.lanes.types import LaneRegistry
from myharness.orchestrator.plan import initial_plan, read_plan, write_plan
from myharness.orchestrator.session import SdkSessionFactory, SessionFactory
from myharness.orchestrator.tools import OrchestratorTools

#: Hard stop on conversation turns, independent of the job's own ceilings.
MAX_TURNS_PER_SESSION = 40

HANDOFF_REQUEST = """\
【系統】你的 context 已達 {pct:.0%}，即將交接給一個全新的 orchestrator。

請立刻呼叫 plan_update，把計畫更新到**足以讓接手者無縫接續**的程度：
已確認的結論、已做的決策與理由、每條 lane 的狀態、尚未收割的任務、以及下一步。
接手者看不到這段對話，只看得到你寫下的計畫。

寫完計畫後就停止，不要再派工。
"""

RESUME_NOTICE = """\
【系統】你接手了一個進行中的 job。上一位 orchestrator 的 context 已滿並交接給你。
以下是它留下的計畫，以及目前的 job 狀態。你看不到先前的對話，計畫就是全部。

# 目前計畫
{plan}

# Job 狀態
{status}

請從這裡繼續。
"""

KICKOFF = """\
你負責統籌一項資料分析工作。

# 目標
{goal}

# 可用的 lane 型別
{lane_types}

# 你的做法
1. 先呼叫 plan_update 寫下計畫並宣告需要的 lane。
2. 用 dispatch 派工 —— 一次派多個，它會立刻返回，之後用 await_tasks 一起收割。
3. 需要細節時用 peek，但它有整個 job 的預算上限；預算緊時改派 lane 去讀。
4. 最後派一條 synthesis lane 寫報告，再用 finish 收工。**不要自己寫報告。**

你看不到原始資料，也不需要看。你的工作是判斷與調度。
"""


@dataclass
class LoopOutcome:
    """What a job run ended as."""

    phase: JobPhase
    report_artifact: str | None
    turns: int
    handoffs: int
    context_peak: int
    #: Why the loop stopped -- distinct from whether the harness had to write
    #: the delivery itself.
    reason: str = ""
    salvaged: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": str(self.phase), "report": self.report_artifact,
            "turns": self.turns, "handoffs": self.handoffs,
            "context_peak": self.context_peak, "reason": self.reason,
            "salvaged": self.salvaged,
        }


@dataclass
class OrchestratorLoop:
    """Drives one job from kickoff to delivery."""

    runner: JobRunner
    lanes: LaneRegistry
    backend: str = "anthropic"
    model_tier: str = "strong"
    sessions: SessionFactory = field(default_factory=SdkSessionFactory)
    tools: OrchestratorTools | None = None
    context_peak: int = 0
    turns: int = 0

    def __post_init__(self) -> None:
        if self.tools is None:
            self.tools = OrchestratorTools(runner=self.runner, lanes=self.lanes)

    @property
    def profile(self) -> BackendProfile:
        return backends.get(self.backend)

    def _options(self) -> ClaudeAgentOptions:
        assert self.tools is not None
        return ClaudeAgentOptions(
            model=self.profile.resolve_model(self.model_tier),
            system_prompt=(
                "你是一個資料分析 harness 的 orchestrator。你只能透過提供的工具行動。"
                "你的回覆會被記錄但不會被使用者看到 —— 真正的交付是報告 artifact。"
            ),
            mcp_servers={"harness": self.tools.build_server()},
            allowed_tools=self.tools.tool_names(),
            disallowed_tools=BackendProfile.disallowed_for(()),
            strict_mcp_config=True,
            setting_sources=[],
            permission_mode="bypassPermissions",
            max_turns=MAX_TURNS_PER_SESSION,
            env=self.profile.to_sdk_env(),
        )

    async def run(self) -> LoopOutcome:
        spec = self.runner.spec
        await self.runner.events.append(
            spec.job_id, JOB_START, goal=spec.goal,
            budget_usd=spec.max_budget_usd, max_dispatches=spec.max_dispatches,
        )
        if (await read_plan(self.runner.store, spec.job_id))[0] is None:
            await write_plan(self.runner.store, spec.job_id, initial_plan(spec.goal))

        prompt = KICKOFF.format(goal=spec.goal, lane_types=self.lanes.describe_types())
        reason = "finished"

        while True:
            handed_off = await self._one_session(prompt)
            if not handed_off:
                break
            if self.runner.state.handoffs >= 3:
                reason = "handoff_limit"
                break
            plan, _ = await read_plan(self.runner.store, spec.job_id)
            prompt = RESUME_NOTICE.format(
                plan=plan or initial_plan(spec.goal),
                status=self.runner.status(),
            )

        await self.runner.settle()
        assert self.tools is not None
        salvaged = not self.tools.finished
        if salvaged:
            await self._salvage()

        await self.runner.events.append(
            spec.job_id, JOB_FINISH, report=self.runner.state.report_artifact,
            phase=str(self.runner.state.phase), turns=self.turns,
            handoffs=self.runner.state.handoffs, reason=reason, salvaged=salvaged,
        )
        return LoopOutcome(
            phase=self.runner.state.phase,
            report_artifact=self.runner.state.report_artifact,
            turns=self.turns, handoffs=self.runner.state.handoffs,
            context_peak=self.context_peak, reason=reason, salvaged=salvaged,
        )

    async def _one_session(self, first_prompt: str) -> bool:
        """Run one conversation. Returns True if it ended in a handoff."""
        assert self.tools is not None
        spec = self.runner.spec
        threshold = spec.handoff_threshold_tokens

        async with self.sessions.open(self._options(), limit=spec.context_window) as session:
            prompt: str | None = first_prompt
            while prompt is not None:
                self.turns += 1
                async for message in session.send(prompt):
                    _ = message  # tool calls are handled by the SDK, not here

                usage = await session.context_usage()
                self.context_peak = max(self.context_peak, usage.used)
                await self.runner.events.append(
                    spec.job_id, CTX, who="orchestrator", used=usage.used,
                    pct=round(usage.ratio, 3), turn=self.turns,
                )

                if self.tools.finished:
                    return False
                if self.turns >= MAX_TURNS_PER_SESSION:
                    return False

                if usage.used >= threshold:
                    self.runner.state.handoffs += 1
                    await self.runner.events.append(
                        spec.job_id, HANDOFF_RESTART, used=usage.used,
                        pct=round(usage.ratio, 3), handoff=self.runner.state.handoffs,
                    )
                    async for _ in session.send(HANDOFF_REQUEST.format(pct=usage.ratio)):
                        pass
                    return True

                prompt = self._next_prompt()
        return False

    def _next_prompt(self) -> str | None:
        """What to say next, if anything, without a human in the loop."""
        if self.runner.must_abort:
            return None
        if notice := self.runner.wrap_up_notice():
            return notice
        if self.runner.no_progress:
            return (
                f"【系統】連續 {self.runner.state.no_progress_streak} 次派工沒有新產出。"
                "換一種做法、改問使用者，或收工。"
            )
        if self.runner.state.running() or self.runner.state.uncollected():
            return "【系統】仍有未收割的任務。請用 await_tasks 收割後再決定下一步。"
        return None

    async def _salvage(self) -> None:
        """Deliver something when the orchestrator did not finish itself.

        Grace is bounded, so past that point the fallback delivery is written by
        code rather than by the model (design.md D4).
        """
        state = self.runner.state
        findings = sorted(state.findings_seen)
        lines = [
            f"# {self.runner.spec.goal}",
            "",
            "## 說明",
            "此報告由 harness 自動產出 —— orchestrator 未能自行收工"
            f"（{'觸及 ' + str(state.limit_hit) + ' 上限' if state.limit_hit else '對話結束'}）。",
            "",
            "## 已完成的分析",
        ]
        lines += [f"- {f}" for f in findings] or ["（無）"]
        meta = await self.runner.store.put_note(
            self.runner.spec.job_id, "report", "\n".join(lines) + "\n",
            produced_by="harness:salvage",
        )
        state.report_artifact = str(meta.id)
        state.phase = JobPhase.ABORTED if state.limit_hit else JobPhase.COMPLETE


def collect_text(messages: Sequence[Any]) -> str:
    return "\n".join(
        block.text
        for message in messages
        if isinstance(message, AssistantMessage)
        for block in message.content
        if isinstance(block, TextBlock)
    )
