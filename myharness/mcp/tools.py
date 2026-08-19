"""The outward tool surface: names, schemas, and the dispatch to the service.

Schemas are written out in full rather than generated. The last change learned
this the hard way -- a shorthand that marks every property required would make
`wait` and `since` mandatory here, and a client would have to supply a cursor
before it had ever polled.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from myharness.mcp.service import DEFAULT_POLL_WAIT_S, MAX_POLL_WAIT_S, AnalysisService

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def _obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "analysis_start": _obj(
        {
            "task": {
                "type": "string",
                "description": "What to analyse. Name the artifacts if you have "
                               "already provided any.",
            },
            "job_id": {
                "type": "string",
                "description": "Optional. Choose the id instead of being given one.",
            },
        },
        ["task"],
    ),
    "analysis_poll": _obj(
        {
            "job_id": {"type": "string"},
            "wait": {
                "type": "number",
                "description": f"Seconds to wait for something to happen before "
                               f"returning. 0 for an immediate snapshot, up to "
                               f"{MAX_POLL_WAIT_S:.0f}. Default "
                               f"{DEFAULT_POLL_WAIT_S:.0f}. Returning empty-handed "
                               f"is not an error.",
            },
            "since": {
                "type": "integer",
                "description": "The `revision` from your last poll. Pass it and "
                               "you will be told immediately if something "
                               "happened while you were away.",
            },
        },
        ["job_id"],
    ),
    "analysis_provide": _obj(
        {
            "job_id": {"type": "string"},
            "payload": {
                "type": "string",
                "description": "The data itself. It is stored as a blob and never "
                               "enters anyone's context.",
            },
            "name": {
                "type": "string",
                "description": "Optional. A short file-like name, e.g. 'kyc.csv'.",
            },
        },
        ["job_id", "payload"],
    ),
    "analysis_answer": _obj(
        {
            "job_id": {"type": "string"},
            "question_id": {
                "type": "string",
                "description": "The id from a pending question in analysis_poll.",
            },
            "text": {"type": "string"},
        },
        ["job_id", "question_id", "text"],
    ),
    "analysis_result": _obj({"job_id": {"type": "string"}}, ["job_id"]),
    "analysis_drill": _obj(
        {
            "job_id": {"type": "string"},
            "section_id": {
                "type": "string",
                "description": "A section id from analysis_result's menu.",
            },
        },
        ["job_id", "section_id"],
    ),
}

TOOL_DESCRIPTIONS: dict[str, str] = {
    "analysis_start": (
        "Start a data analysis. Returns immediately with a job_id -- the work "
        "continues in the background. Poll it to follow along."
    ),
    "analysis_poll": (
        "Wait for the analysis to do something, then report a short progress "
        "summary and any questions it needs answered. Blocks until there is news "
        "or the wait elapses; an empty return means 'still working'."
    ),
    "analysis_provide": (
        "Hand the analysis more data mid-run. Stored as a blob, so the content "
        "never enters your context or the orchestrator's. Nothing routes it to a "
        "particular lane yet -- the orchestrator decides."
    ),
    "analysis_answer": (
        "Answer a question the analysis is waiting on. Unanswered questions "
        "eventually time out and become caveats on the final report."
    ),
    "analysis_result": (
        "The finished analysis: an executive summary, key findings, caveats, cost, "
        "and a menu of report sections with what each would cost you to read. "
        "Works for analyses run by an earlier process."
    ),
    "analysis_drill": (
        "Read one section of the report in full, chosen from analysis_result's menu."
    ),
}


def build_handlers(service: AnalysisService) -> dict[str, Handler]:
    """Bind the tool names to one service."""

    async def start(args: dict[str, Any]) -> dict[str, Any]:
        return await service.start(
            str(args.get("task", "")), job_id=(args.get("job_id") or "").strip() or None
        )

    async def poll(args: dict[str, Any]) -> dict[str, Any]:
        since = args.get("since")
        return await service.poll(
            str(args.get("job_id", "")),
            wait=float(args.get("wait", DEFAULT_POLL_WAIT_S) or 0.0),
            since=int(since) if since is not None else None,
        )

    async def provide(args: dict[str, Any]) -> dict[str, Any]:
        return await service.provide(
            str(args.get("job_id", "")),
            str(args.get("payload", "")),
            name=str(args.get("name") or ""),
        )

    async def answer(args: dict[str, Any]) -> dict[str, Any]:
        return await service.answer(
            str(args.get("job_id", "")),
            str(args.get("question_id", "")),
            str(args.get("text", "")),
        )

    async def result(args: dict[str, Any]) -> dict[str, Any]:
        return await service.result(str(args.get("job_id", "")))

    async def drill(args: dict[str, Any]) -> dict[str, Any]:
        return await service.drill_section(
            str(args.get("job_id", "")), str(args.get("section_id", ""))
        )

    return {
        "analysis_start": start,
        "analysis_poll": poll,
        "analysis_provide": provide,
        "analysis_answer": answer,
        "analysis_result": result,
        "analysis_drill": drill,
    }


async def call(handlers: dict[str, Handler], name: str, args: dict[str, Any]) -> str:
    """Run one tool and render its result as text.

    An unexpected exception becomes a refusal rather than a protocol error: the
    client is an agent, and a message it can act on beats a transport fault it
    cannot (design.md D5).
    """
    handler = handlers.get(name)
    if handler is None:
        return json.dumps(
            {"ok": False, "error": "unknown_tool", "message": f"no tool named {name}",
             "available": sorted(handlers)},
            ensure_ascii=False,
        )
    try:
        payload = await handler(args or {})
    except Exception as exc:  # noqa: BLE001 - see docstring
        payload = {"ok": False, "error": "tool_failed",
                   "message": f"{type(exc).__name__}: {exc}"}
    return json.dumps(payload, ensure_ascii=False, indent=2)


__all__ = ["TOOL_DESCRIPTIONS", "TOOL_SCHEMAS", "build_handlers", "call"]
