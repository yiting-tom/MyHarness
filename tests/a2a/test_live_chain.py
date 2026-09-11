"""The whole chain, over a socket, with a real A2A client and a real model.

Everything else in this package drives the boundary in-process against a fake
service. This one is the claim the change is actually making: a remote agent can
reach this harness, start an analysis, watch it, and read the result without the
report ever arriving unasked.

Real all the way down -- uvicorn on a loopback port, `a2a.client` resolving the
agent card over HTTP, a real `AnalysisService`, real lanes, a real backend. It
costs money and needs a key, so it is marked live and deselected by default.

    set -a && . ./.env && set +a && pytest -m live tests/a2a
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from pathlib import Path

import pytest

pytest.importorskip("a2a", reason="the a2a extra is optional (D6)")

from myharness.a2a.card import PRICE_LIST_EXTENSION
from myharness.a2a.server import RPC_PATH, build_app, endpoint_url
from myharness.backends.profile import registry, self_hosted_from_env
from myharness.mcp.server import default_lanes
from myharness.mcp.service import AnalysisService

pytestmark = [pytest.mark.live, pytest.mark.anyio] if False else pytest.mark.live

TASK = (
    "分析這份交易資料，找出異常樣態。報告中必須給出不重複帳戶的總數，"
    "以及平均交易金額最低的 channel。"
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.asynccontextmanager
async def running(app, port: int):
    """uvicorn on a loopback port, torn down however the test ends."""
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
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=10)


def data_parts(message_or_artifact) -> list[dict]:
    from google.protobuf.json_format import MessageToDict

    out = []
    for part in message_or_artifact.parts:
        if part.WhichOneof("content") == "data":
            out.append(MessageToDict(part.data))
    return out


@pytest.mark.live
async def test_a_remote_agent_can_run_and_read_an_analysis(tmp_path: Path):
    from a2a.client import ClientConfig, ClientFactory
    from a2a.types import GetTaskRequest, Message, Part, Role, SendMessageRequest

    profile = self_hosted_from_env()
    if profile is None:
        pytest.skip("HARNESS_PROXY_BASE_URL / _MODEL unset")
    registry.register(profile)

    csv = next(Path("jobs-scratch").rglob("blobs/raw/txn-2024"), None)
    if csv is None:
        pytest.skip("no txn-2024 fixture in jobs-scratch; run a golden job first")

    service = AnalysisService(
        tmp_path / "root",
        lanes=default_lanes(Path("charters"), backend=profile.name),
        backend=profile.name,
    )
    port = free_port()
    app = build_app(service, url=endpoint_url("127.0.0.1", port))

    try:
        async with running(app, port):
            import httpx

            async with httpx.AsyncClient(timeout=1200.0) as http:
                factory = ClientFactory(ClientConfig(httpx_client=http,
                                                     streaming=False))
                # Resolving the card over HTTP is half the point: a remote agent
                # discovers the two skills before it asks for anything.
                client = await factory.create_from_url(f"http://127.0.0.1:{port}")

                request = SendMessageRequest(message=Message(
                    message_id="m1", role=Role.ROLE_USER,
                    parts=[Part(text=json.dumps({"task": TASK}, ensure_ascii=False))],
                ))
                task = None
                async for event in client.send_message(request):
                    if event.WhichOneof("payload") == "task":
                        task = event.task
                assert task is not None, "no task came back"
                assert task.status.state.__str__().endswith("COMPLETED"), task.status

                # 1. the price list, marked as one
                (artifact,) = [a for a in task.artifacts
                               if PRICE_LIST_EXTENSION in a.extensions]
                (price_list,) = data_parts(artifact)
                sections = price_list["sections"]
                assert sections, "a finished report has sections to price"
                assert "text" not in price_list, "the report body must not ride along"

                # 2. the history a dropped stream would have needed
                fetched = await client.get_task(
                    GetTaskRequest(id=task.id, history_length=50)
                )
                revisions = [p["revision"] for m in fetched.history
                             for p in data_parts(m) if "revision" in p]
                assert revisions == sorted(revisions), revisions

                # 3. one section, explicitly, by an id from the price list
                section_id = sections[0]["id"]
                ask_section = SendMessageRequest(message=Message(
                    message_id="m2", role=Role.ROLE_USER,
                    parts=[Part(text=json.dumps(
                        {"job_id": task.id, "section_id": section_id},
                        ensure_ascii=False))],
                ))
                body = None
                async for event in client.send_message(ask_section):
                    if event.WhichOneof("payload") == "task":
                        body = event.task
                assert body is not None
                (section,) = [p for a in body.artifacts for p in data_parts(a)]
                assert section["text"].strip(), "an empty section is not a section"
                assert not [a for a in body.artifacts
                            if PRICE_LIST_EXTENSION in a.extensions], \
                    "content must not be marked as a price list"
    finally:
        await service.aclose()
