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
    state: int = 0
    price_list: dict[str, Any] = field(default_factory=dict)
    marked_artifacts: int = 0
    revisions: list[float] = field(default_factory=list)
    section_id: str = ""
    section: dict[str, Any] = field(default_factory=dict)
    section_marked: bool = False


async def drive(port: int, task_text: str, *, timeout_s: float = 1_800.0) -> ChainResult:
    """Start an analysis, watch it, price it, then buy one section of it."""
    import httpx
    from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
    from a2a.types import GetTaskRequest, Message, Part, Role, SendMessageRequest

    from myharness.a2a.card import PRICE_LIST_EXTENSION

    out = ChainResult()
    async with httpx.AsyncClient(timeout=timeout_s) as http:
        # Resolving the card over HTTP is half the point: a remote agent
        # discovers the two skills before it asks for anything. The resolver is
        # separate from the client because the client does not hand its card
        # back -- `get_extended_agent_card` is a different question.
        card = await A2ACardResolver(http, f"http://127.0.0.1:{port}").get_agent_card()
        out.skills = [s.id for s in card.skills]
        out.extensions = [e.uri for e in card.capabilities.extensions]

        factory = ClientFactory(ClientConfig(httpx_client=http, streaming=False))
        client = factory.create(card)

        task = await _send(client, {"task": task_text}, "m1")
        out.state = task.status.state
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


async def _send(client, payload: dict, message_id: str):
    from a2a.types import Message, Part, Role, SendMessageRequest

    request = SendMessageRequest(message=Message(
        message_id=message_id, role=Role.ROLE_USER,
        parts=[Part(text=json.dumps(payload, ensure_ascii=False))],
    ))
    task = None
    async for event in client.send_message(request):
        if event.WhichOneof("payload") == "task":
            task = event.task
    assert task is not None, f"no task came back for {payload}"
    return task


__all__ = ["ChainResult", "data_parts", "drive", "free_port", "running"]
