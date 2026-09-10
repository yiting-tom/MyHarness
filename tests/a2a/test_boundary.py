"""The A2A boundary: the same answers as MCP, and one more way to get them wrong.

Driven through the real SDK against a fake AnalysisService, so these exercise
the wiring without a model, a network or a job (change task 8.1).
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("a2a", reason="the a2a extra is optional (D6)")

from myharness.a2a.card import PRICE_LIST_EXTENSION, SKILL_FULL_TEXT, SKILL_PRICE_LIST
from myharness.a2a.server import NotLoopback, RPC_PATH, build_app, require_loopback

PRICE_LIST = {
    "ok": True,
    "job_id": "j1",
    "status": "complete",
    "executive_summary": "765 個不重複帳戶，平均金額最低的 channel 是 app。",
    "sections": [
        {"id": "方法", "title": "方法與可複查性", "est_tokens": 179},
        {"id": "限制", "title": "限制", "est_tokens": 186},
    ],
    "report_artifact": "j1/note/lanes/synth-1/findings/report",
}


class FakeService:
    """Answers the two methods the executor is allowed to know about."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def result(self, job_id: str):
        self.calls.append(("result", job_id))
        if job_id != "j1":
            return {"ok": False, "code": "no_such_job",
                    "message": f"no analysis with id {job_id}"}
        return dict(PRICE_LIST)

    async def drill_section(self, job_id: str, section_id: str, **kw):
        self.calls.append(("drill", job_id, section_id))
        if section_id != "方法":
            return {"ok": False, "code": "no_such_section", "message": "不在這份報告裡"}
        return {"ok": True, "section_id": section_id, "text": "以 duckdb 對原始 CSV..."}


@pytest.fixture
def client():
    from starlette.testclient import TestClient

    service = FakeService()
    app = build_app(service, url=f"http://127.0.0.1:8973{RPC_PATH}")
    with TestClient(app) as c:
        c.service = service  # type: ignore[attr-defined]
        yield c


#: The RPC names from a2a.proto, not the v0.3 "message/send" spelling. The SDK
#: dispatches on these unless enable_v0_3_compat is asked for, and this endpoint
#: does not ask for it.
SEND = "SendMessage"

#: Omitting it is not neutral: the SDK reads a missing `A2A-Version` as 0.3 and
#: the 1.0 handler refuses it, so every request here carries the version the
#: card declares.
VERSION_HEADERS = {"A2A-Version": "1.0"}


def send(client, payload: dict) -> dict:
    body = {
        "jsonrpc": "2.0", "id": 1, "method": SEND,
        "params": {"message": {
            "messageId": "m1", "role": "ROLE_USER",
            "parts": [{"text": json.dumps(payload, ensure_ascii=False)}],
        }},
    }
    response = client.post(RPC_PATH, json=body, headers=VERSION_HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def task_of(answer: dict) -> dict:
    """The 1.0 JSON-RPC binding nests the task: result.task, not result."""
    return (answer.get("result") or {}).get("task") or {}


def artifacts_of(answer: dict) -> list[dict]:
    return task_of(answer).get("artifacts") or []


def data_of(artifact: dict) -> dict:
    return artifact["parts"][0]["data"]


# --- the card -------------------------------------------------------------


def test_the_card_declares_both_ways_to_ask(client):
    card = client.get("/.well-known/agent-card.json").json()
    assert [s["id"] for s in card["skills"]] == [SKILL_PRICE_LIST, SKILL_FULL_TEXT]


def test_the_price_list_convention_is_declared_and_required(client):
    """A client that does not know it is told so, rather than reading a menu
    as though it were the meal (spike #13)."""
    card = client.get("/.well-known/agent-card.json").json()
    (extension,) = card["capabilities"]["extensions"]
    assert extension["uri"] == PRICE_LIST_EXTENSION
    assert extension["required"] is True


def test_the_card_says_where_the_endpoint_actually_is(client):
    card = client.get("/.well-known/agent-card.json").json()
    (interface,) = card["supportedInterfaces"]
    assert interface["url"].endswith(RPC_PATH)
    assert interface["protocolBinding"] == "JSONRPC"


def test_the_modes_are_not_declared_as_output_modes(client):
    """They are media types. Declaring 'price list' there would be a misuse of
    the field rather than a use of it -- the whole reason for two skills."""
    card = client.get("/.well-known/agent-card.json").json()
    for mode in card.get("defaultOutputModes", []):
        assert "/" in mode, f"{mode!r} is not a media type"


# --- the default answer ---------------------------------------------------


def test_asking_for_a_result_returns_the_price_list_not_the_report(client):
    answer = send(client, {"job_id": "j1"})
    (artifact,) = artifacts_of(answer)
    data = data_of(artifact)
    assert [s["id"] for s in data["sections"]] == ["方法", "限制"]
    assert "text" not in data, "the report body must not ride along"


def test_the_price_list_is_marked_as_one(client):
    (artifact,) = artifacts_of(send(client, {"job_id": "j1"}))
    assert artifact["extensions"] == [PRICE_LIST_EXTENSION]


def test_the_price_list_says_how_to_spend_it(client):
    """Section ids and prices with no way to buy anything is half an answer."""
    data = data_of(artifacts_of(send(client, {"job_id": "j1"}))[0])
    assert SKILL_FULL_TEXT in data["hint"]


# --- the explicit answer --------------------------------------------------


def test_a_section_comes_back_as_content(client):
    answer = send(client, {"job_id": "j1", "section_id": "方法"})
    (artifact,) = artifacts_of(answer)
    assert "duckdb" in data_of(artifact)["text"]


def test_numbers_come_back_as_json_numbers(client):
    """Part.data is a protobuf Value, so an int arrives as a float. Worth
    pinning: a client formatting est_tokens will see 179.0, not 179."""
    data = data_of(artifacts_of(send(client, {"job_id": "j1"}))[0])
    assert data["sections"][0]["est_tokens"] == 179


def test_a_section_is_not_marked_as_a_price_list(client):
    """This artifact IS the content; marking it would say the opposite."""
    (artifact,) = artifacts_of(send(client, {"job_id": "j1", "section_id": "方法"}))
    assert not artifact.get("extensions")


def test_the_service_is_reached_through_its_own_two_methods(client):
    send(client, {"job_id": "j1"})
    send(client, {"job_id": "j1", "section_id": "方法"})
    assert client.service.calls == [("result", "j1"), ("drill", "j1", "方法")]


# --- refusals -------------------------------------------------------------


def test_a_request_with_no_job_is_refused_terminally(client):
    answer = send(client, {})
    status = task_of(answer)["status"]
    assert status["state"] == "TASK_STATE_FAILED", "a caller must stop waiting"


def test_an_unknown_job_is_refused_with_its_own_code(client):
    answer = send(client, {"job_id": "nope"})
    message = task_of(answer)["status"]["message"]
    assert message["parts"][0]["data"]["code"] == "no_such_job"


def test_a_plain_job_id_works_without_a_structured_client(client):
    """The MCP boundary takes both shapes; this one should not be harder to try."""
    body = {
        "jsonrpc": "2.0", "id": 1, "method": SEND,
        "params": {"message": {"messageId": "m1", "role": "ROLE_USER",
                               "parts": [{"text": "j1"}]}},
    }
    answer = client.post(RPC_PATH, json=body, headers=VERSION_HEADERS).json()
    assert artifacts_of(answer), answer


# --- what it refuses to listen on ----------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_is_allowed(host):
    assert require_loopback(host) == host


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com", ""])
def test_anything_else_is_refused(host):
    """No authentication yet, so no public interface. A hostname that resolves
    to loopback today is not evidence about tomorrow."""
    with pytest.raises(NotLoopback):
        require_loopback(host)
