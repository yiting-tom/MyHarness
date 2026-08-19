"""The orchestrator's entire surface: six tools, and no way to reach raw data.

The size of this set is the point. It does not grow with the number of lanes or
the volume of data, and no member of it can return a blob's contents — so the
guarantee that raw data never enters the orchestrator's context is structural
rather than a matter of prompting (DESIGN.md decision #6).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool

from myharness.artifacts.errors import ArtifactError
from myharness.artifacts.ids import ArtifactId
from myharness.artifacts.tokens import estimate_tokens
from myharness.artifacts.types import GrantSet
from myharness.events.types import PEEK, PLAN_UPDATE
from myharness.jobs.channel import Question, QuestionKind
from myharness.jobs.runner import JobRunner
from myharness.jobs.spec import JobPhase
from myharness.lanes.types import LaneRegistry
from myharness.orchestrator.plan import LaneSpec, write_plan

SERVER_NAME = "harness"

#: Below this, the remaining peek budget cannot fund a useful read. Reporting it
#: as exhausted is clearer than letting the orchestrator collect
#: token_budget_exceeded forever against a balance it can never spend.
MIN_USEFUL_PEEK_TOKENS = 200

TOOL_NAMES = (
    "plan_update", "dispatch", "await_tasks", "peek", "ask_user", "finish",
)


def _ok(payload: Any) -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}]}


def _err(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return _ok({"error": code, "message": message, **extra})


@dataclass
class OrchestratorTools:
    """The six tools, bound to one job."""

    runner: JobRunner
    lanes: LaneRegistry
    handlers: dict[str, Any] = field(default_factory=dict)
    finished: bool = False

    @property
    def job_id(self) -> str:
        return self.runner.spec.job_id

    def build_server(self):
        read_only = ToolAnnotations(readOnlyHint=True)
        mutating = ToolAnnotations(readOnlyHint=False)

        @tool(
            "plan_update",
            "Record the plan and declare which lanes should exist. Call this "
            "before dispatching, and again whenever your understanding changes.",
            {"plan": str, "lanes": list},
            annotations=mutating,
        )
        async def plan_update(args):
            text = str(args.get("plan", "")).strip()
            if not text:
                return _err("empty_plan", "plan must not be empty")

            created: list[str] = []
            for raw in args.get("lanes") or []:
                if not isinstance(raw, dict):
                    return _err("bad_lane", f"lane entries must be objects: {raw!r}")
                try:
                    spec = LaneSpec.from_dict(raw)
                except KeyError as exc:
                    return _err("bad_lane", f"lane entry missing {exc}")
                if spec.id in self.runner.lanes:
                    continue
                try:
                    instance = self.lanes.create(spec.id, spec.type, scope=spec.scope)
                except Exception as exc:
                    return _err("unknown_lane_type", str(exc))
                self.runner.register_lane(instance)
                created.append(spec.id)

            revision = await write_plan(self.runner.store, self.job_id, text)
            await self.runner.events.append(
                self.job_id, PLAN_UPDATE, revision=revision,
                lanes=sorted(self.runner.lanes), created=created,
                plan_tokens=estimate_tokens(text),
            )
            return _ok({"plan_revision": revision, "lanes": sorted(self.runner.lanes),
                        "created": created})

        @tool(
            "dispatch",
            "Give a lane a task. Returns immediately with a task id — collect it "
            "with await_tasks. Issue several dispatches before collecting so the "
            "lanes run in parallel.",
            {"lane": str, "task": str, "inputs": list},
            annotations=mutating,
        )
        async def dispatch(args):
            lane = str(args.get("lane", "")).strip()
            task = str(args.get("task", "")).strip()
            if not lane or not task:
                return _err("bad_request", "lane and task are required")
            inputs = [str(i) for i in (args.get("inputs") or [])]
            result = await self.runner.dispatch(lane, task, inputs)
            payload = result.to_dict()
            if notice := self.runner.wrap_up_notice():
                payload["notice"] = notice
            return _ok(payload)

        @tool(
            "await_tasks",
            "Wait for dispatched tasks and return their handles. mode='all' waits "
            "for every one, mode='any' returns as soon as one finishes.",
            {"task_ids": list, "mode": str, "timeout": float},
            annotations=mutating,
        )
        async def await_tasks(args):
            ids = [str(i) for i in (args.get("task_ids") or [])]
            if not ids:
                return _err("bad_request", "task_ids must not be empty")
            mode = str(args.get("mode") or "all")
            timeout = float(args.get("timeout") or 600.0)
            result = await self.runner.await_tasks(ids, mode=mode, timeout=timeout)
            payload = result.to_dict()
            if notice := self.runner.wrap_up_notice():
                payload["notice"] = notice
            if self.runner.no_progress:
                payload["warning"] = (
                    f"連續 {self.runner.state.no_progress_streak} 次派工沒有新產出。"
                    "換一種做法，或改問使用者，或收工。"
                )
            return _ok(payload)

        @tool(
            "peek",
            "Read a slice of a note a lane produced. Budgeted for the whole job: "
            "when it runs out, dispatch a lane to read instead.",
            {"artifact": str, "section": str, "max_tokens": int},
            annotations=read_only,
        )
        async def peek(args):
            raw = str(args.get("artifact", "")).strip()
            section = (args.get("section") or "").strip() or None
            asked = int(args.get("max_tokens") or 2000)

            remaining = self.runner.state.peek_remaining
            if remaining < MIN_USEFUL_PEEK_TOKENS:
                return _err(
                    "peek_budget_exhausted",
                    "此 job 的窺看預算已用盡。請改派一條 lane 去讀這份內容。",
                    remaining=remaining,
                )
            try:
                aid = ArtifactId.parse(raw)
            except ValueError as exc:
                return _err("bad_artifact_id", str(exc))

            budget = min(asked, remaining)
            try:
                text = await self.runner.store.read_note(
                    aid, grants=GrantSet.unrestricted(self.job_id),
                    max_tokens=budget, section=section,
                )
            except ArtifactError as exc:
                detail = exc.to_dict()
                if detail.get("code") == "token_budget_exceeded":
                    detail["peek_remaining"] = remaining
                    detail["hint"] = "指定 section，或改派一條 lane 去讀"
                return _ok(detail)

            spent = estimate_tokens(text)
            self.runner.state.peek_spent_tokens += spent
            await self.runner.events.append(
                self.job_id, PEEK, artifact=str(aid), section=section,
                tokens=spent, remaining=self.runner.state.peek_remaining,
            )
            return _ok({"text": text, "tokens": spent,
                        "peek_remaining": self.runner.state.peek_remaining})

        @tool(
            "ask_user",
            "Ask the user something you cannot determine yourself. Quota-limited: "
            "check the lanes first.",
            {"question": str, "default": str, "kind": str, "options": list},
            annotations=mutating,
        )
        async def ask_user(args):
            text = str(args.get("question", "")).strip()
            if not text:
                return _err("bad_request", "question must not be empty")
            kind = str(args.get("kind") or QuestionKind.ANSWERABLE_BY_HOST)
            question = Question(
                id=f"q{self.runner.state.questions_asked + 1}",
                text=text,
                kind=QuestionKind(kind) if kind in set(QuestionKind) else QuestionKind.ANSWERABLE_BY_HOST,
                options=tuple(str(o) for o in (args.get("options") or [])),
                default=str(args.get("default") or ""),
            )
            answer = await self.runner.ask_user(question)
            return _ok({"answer": answer.text, "defaulted": answer.defaulted,
                        "reason": answer.reason,
                        "questions_remaining": self.runner.state.questions_remaining})

        @tool(
            "finish",
            "Finish the job, naming the report artifact a synthesis lane wrote. "
            "Do not write the report yourself.",
            {"report_artifact": str, "summary": str},
            annotations=mutating,
        )
        async def finish(args):
            report = str(args.get("report_artifact", "")).strip()
            if not report:
                return _err("bad_request", "report_artifact is required")
            try:
                aid = ArtifactId.parse(report)
            except ValueError as exc:
                return _err("bad_artifact_id", str(exc))
            try:
                await self.runner.store.stat(aid, grants=GrantSet.unrestricted(self.job_id))
            except ArtifactError as exc:
                return _err("no_such_report", exc.message,
                            hint="先派一條 synthesis lane 寫出報告，再呼叫 finish")

            self.runner.state.report_artifact = str(aid)
            self.runner.state.phase = JobPhase.COMPLETE
            self.finished = True
            return _ok({"status": "complete", "report": str(aid)})

        available = {
            "plan_update": plan_update, "dispatch": dispatch,
            "await_tasks": await_tasks, "peek": peek,
            "ask_user": ask_user, "finish": finish,
        }
        self.handlers = {name: fn.handler for name, fn in available.items()}
        return create_sdk_mcp_server(
            name=SERVER_NAME, version="1.0.0", tools=list(available.values())
        )

    def tool_names(self) -> list[str]:
        return [f"mcp__{SERVER_NAME}__{name}" for name in TOOL_NAMES]
