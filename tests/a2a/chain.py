"""Driving the boundary the way a remote agent would, over a real socket.

Shared by two tests that differ only in what is behind the endpoint: one puts a
fake service there and costs nothing, the other puts a real model there and
costs money. The point of sharing is that the cheap one fails first.

It has already earned that. The live test's first run spent twenty minutes on a
real analysis and then failed on `str(task.status.state).endswith("COMPLETED")`
-- the protobuf enum is an int, so that string is "3" and the assertion could
never have passed. Nothing about it needed a model to discover.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from dataclasses import dataclass, field
from typing import Any


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.asynccontextmanager
async def running(app, port: int):
    """uvicorn on a loopback port, torn down however the caller ends."""
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "uvicorn never came up"
    try:
        yield
    finally:
        server.should_exit = True
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=10)


def data_parts(carrier) -> list[dict]:
    """Every structured part of a message or artifact, as plain dicts."""
    from google.protobuf.json_format import MessageToDict

    return [MessageToDict(p.data) for p in carrier.parts
            if p.WhichOneof("content") == "data"]


@dataclass
class ChainResult:
    """What one pass through the boundary produced, for a caller to assert on."""

    skills: list[str] = field(default_factory=list)
    extensions: list[str] = field(default_factory=list)
    started_state: int = 0
    state: int = 0
    price_list: dict[str, Any] = field(default_factory=dict)
    marked_artifacts: int = 0
    revisions: list[float] = field(default_factory=list)
    section_id: str = ""
    section: dict[str, Any] = field(default_factory=dict)
    section_marked: bool = False
    #: Whatever a failed task said about itself, so a live failure explains
    #: itself instead of being a state number.
    refusal: dict[str, Any] = field(default_factory=dict)


async def drive(port: int, task_text: str, *, timeout_s: float = 1_800.0,
                after_start=None, during=None) -> ChainResult:
    """Start an analysis, watch it, price it, then buy one section of it.

    `after_start` is called once with the new job id before the wait begins, and
    `during` runs alongside the wait until the task reaches a terminal state.
    Both exist for the same reason: neither providing data nor answering a
    question is an A2A operation yet -- whether they become second messages or
    task inputs is an open question the change deliberately left open -- so a
    caller that needs either reaches around the boundary to the service it
    already owns. Having to pass them in is the shape of that gap.
    """
    import httpx
    from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
    from a2a.types import GetTaskRequest, Message, Part, Role, SendMessageRequest

    from myharness.a2a.card import PRICE_LIST_EXTENSION

    out = ChainResult()
    # Per-request, not per-analysis: nothing is long-lived now that the start
    # returns immediately, and a client timeout measured in analyses is how the
    # first live run spent thirty minutes finding out it was misconfigured.
    async with httpx.AsyncClient(timeout=120.0) as http:
        # Resolving the card over HTTP is half the point: a remote agent
        # discovers the two skills before it asks for anything. The resolver is
        # separate from the client because the client does not hand its card
        # back -- `get_extended_agent_card` is a different question.
        card = await A2ACardResolver(http, f"http://127.0.0.1:{port}").get_agent_card()
        out.skills = [s.id for s in card.skills]
        out.extensions = [e.uri for e in card.capabilities.extensions]

        factory = ClientFactory(ClientConfig(httpx_client=http, streaming=False))
        client = factory.create(card)

        # Non-blocking, because an analysis runs for tens of minutes and a
        # blocking send holds the HTTP request open for all of it. The first
        # live run died on exactly that: a read timeout at 30 minutes, with the
        # analysis still going. `return_immediately` is the protocol's answer
        # and the executor keeps running behind it.
        task = await _send(client, {"task": task_text}, "m1", immediate=True)
        out.started_state = task.status.state
        if after_start is not None:
            await after_start(task.id)
        helper = asyncio.create_task(during(task.id)) if during else None
        try:
            task = await _await_terminal(client, task.id, timeout_s=timeout_s)
        finally:
            if helper is not None:
                helper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await helper
        out.state = task.status.state
        if task.status.HasField("message"):
            parts = data_parts(task.status.message)
            out.refusal = parts[0] if parts else {}
        marked = [a for a in task.artifacts if PRICE_LIST_EXTENSION in a.extensions]
        out.marked_artifacts = len(marked)
        if marked:
            (out.price_list,) = data_parts(marked[0])

        fetched = await client.get_task(
            GetTaskRequest(id=task.id, history_length=50)
        )
        out.revisions = [p["revision"] for m in fetched.history
                         for p in data_parts(m) if "revision" in p]

        sections = out.price_list.get("sections") or []
        if sections:
            out.section_id = sections[0]["id"]
            body = await _send(
                client, {"job_id": task.id, "section_id": out.section_id}, "m2"
            )
            parts = [p for a in body.artifacts for p in data_parts(a)]
            out.section = parts[0] if parts else {}
            out.section_marked = any(
                PRICE_LIST_EXTENSION in a.extensions for a in body.artifacts
            )
    return out


async def _await_terminal(client, task_id: str, *, timeout_s: float):
    """Poll GetTask until the task stops moving, the way a remote agent would.

    Not a stream: this is the path that survives a client restart, and it is the
    same one an agent uses to check back on an analysis somebody else started.
    """
    from a2a.types import GetTaskRequest, TaskState

    terminal = {
        TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_FAILED,
        TaskState.TASK_STATE_CANCELED, TaskState.TASK_STATE_REJECTED,
    }
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        task = await client.get_task(GetTaskRequest(id=task_id))
        if task.status.state in terminal:
            return task
        if asyncio.get_running_loop().time() > deadline:
            # What it was doing matters more than that it stopped: a job waiting
            # on an unanswered question and one grinding through dispatches look
            # identical from out here, and only one of them is a bug in the
            # boundary.
            detail = await client.get_task(
                GetTaskRequest(id=task_id, history_length=5)
            )
            trail = [p for m in detail.history for p in data_parts(m)]
            raise AssertionError(
                f"task {task_id} still in state {task.status.state} after "
                f"{timeout_s:.0f}s; last ticks: {trail[-3:]}"
            )
        await asyncio.sleep(5.0)


async def _send(client, payload: dict, message_id: str, *, immediate: bool = False):
    from a2a.types import (
        Message, Part, Role, SendMessageConfiguration, SendMessageRequest,
    )

    request = SendMessageRequest(message=Message(
        message_id=message_id, role=Role.ROLE_USER,
        parts=[Part(text=json.dumps(payload, ensure_ascii=False))],
    ))
    if immediate:
        request.configuration.CopyFrom(
            SendMessageConfiguration(return_immediately=True)
        )
    task = None
    async for event in client.send_message(request):
        if event.WhichOneof("payload") == "task":
            task = event.task
    assert task is not None, f"no task came back for {payload}"
    return task


__all__ = ["ChainResult", "data_parts", "drive", "free_port", "running"]
