"""The direct path: one HTTP request, and the framework's prompt absent.

Spike #12 measured 8,372 tokens per classification that nobody in this
repository wrote. The point of this path is not that it is faster -- it is that
the request contains our two messages and nothing else. So the test that
matters most here is the one asserting exactly that, in the same spirit as
test_classify's "no plan, no goal": it is a commitment that will otherwise be
traded away the first time someone reaches for the SDK's conveniences.
"""

from __future__ import annotations

import httpx
import pytest

from myharness.backends.gate import BackendGate, ThrottleReport
from myharness.backends.profile import (
    BackendProfile,
    ModelTier,
    WireFormat,
    registry,
)
from myharness.proxy.classify import SYSTEM_PROMPT, Unrouted, build_prompt, classify
from myharness.proxy.direct import (
    Completion,
    DirectError,
    DirectStatusError,
    complete_via_gate,
)
from myharness.orchestrator.routing import RoutingTable
from myharness.proxy.sample import Sample

DIRECT = BackendProfile(
    name="direct-test",
    models={tier: "aird-35b" for tier in ModelTier},
    capabilities=frozenset(),
    base_url="http://endpoint.invalid",
    auth_token_env=None,
    direct_wire=WireFormat.OPENAI,
)
SDK = registry.get("anthropic")


def table() -> RoutingTable:
    return RoutingTable.from_raw([
        {"lane": "txn-2024", "accepts": "2024 年交易明細與金流紀錄"},
        {"lane": "kyc-docs", "accepts": "身分與 KYC 文件"},
    ])


def sample(text="txn_id,ts,amount\nT001,2024-01-01,100\n") -> Sample:
    return Sample(text, 2, False, False)


class Recorder:
    """A DirectTransport that answers from a script and keeps every call."""

    def __init__(self, *answers) -> None:
        self._answers = list(answers)
        self.calls: list[dict] = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        # The last answer repeats. A scripted transport that falls back to a
        # canned success turns "it failed every time" into "it returned {}",
        # and the retry loop hides which one actually happened.
        answer = self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def instant_gate(name="direct-test", **kw) -> BackendGate:
    """A gate whose clock and sleep are ours.

    Not a convenience: the default gate backs off with real jitter up to 60s,
    so a test that lets the retry loop touch asyncio.sleep pays for it in wall
    clock and stops being deterministic.
    """
    now = [0.0]

    async def sleep(seconds):
        now[0] += seconds

    return BackendGate(name, clock=lambda: now[0], sleep=sleep, **kw)


def ok(lane="txn-2024", tin=588, tout=30) -> Completion:
    return Completion(
        f'{{"lane": "{lane}", "confidence": "high", "reason": "看起來像交易明細"}}',
        0.0, tin, tout,
    )


# ---- what the request contains ------------------------------------------


async def test_direct_request_carries_only_our_two_prompts():
    """The whole reason this path exists (spec: 直接路徑的請求只含自己的提示)."""
    rec = Recorder(ok())
    await classify(table(), "txn.csv", sample(), profile=DIRECT, direct=rec)

    call = rec.calls[0]
    assert call["system"] == SYSTEM_PROMPT
    assert call["user"] == build_prompt(table(), "txn.csv", sample())
    # Nothing that would carry a framework preamble, a tool list, or a session.
    assert set(call) == {"base_url", "token", "model", "system", "user", "timeout_s"}


async def test_direct_path_sees_no_plan_and_no_goal():
    rec = Recorder(ok())
    await classify(table(), "txn.csv", sample(), profile=DIRECT, direct=rec)
    blob = rec.calls[0]["system"] + rec.calls[0]["user"]
    for leak in ("找出異常", "plan", "goal", "finding"):
        assert leak not in blob


# ---- routing between the two paths ---------------------------------------


async def test_backend_without_a_wire_format_still_classifies():
    """Spec: 不支援直接呼叫的後端仍可分類."""
    assert not SDK.has_direct_path

    class Block:
        def __init__(self, text): self.text = text

    class Msg:
        content = ()
        total_cost_usd = 0.002
        usage = {"input_tokens": 8991, "output_tokens": 30}

        def __init__(self, text):
            self.content = [Block(text)]

    class Sdk:
        def stream(self, prompt, options):
            async def gen():
                yield Msg('{"lane": "txn-2024", "confidence": "high", "reason": "x"}')
            return gen()

    routing = await classify(table(), "txn.csv", sample(), profile=SDK, transport=Sdk())
    assert routing.lane == "txn-2024"


async def test_both_paths_produce_the_same_shape():
    """Spec: 兩條路徑的結果形狀相同."""
    class Block:
        def __init__(self, text): self.text = text

    class Msg:
        def __init__(self, text):
            self.content = [Block(text)]
            self.total_cost_usd = 0.002
            self.usage = {"input_tokens": 8991, "output_tokens": 30}

    class Sdk:
        def stream(self, prompt, options):
            async def gen():
                yield Msg('{"lane": "txn-2024", "confidence": "high", "reason": "x"}')
            return gen()

    via_direct = await classify(table(), "m", sample(), profile=DIRECT, direct=Recorder(ok()))
    via_sdk = await classify(table(), "m", sample(), profile=SDK, transport=Sdk())

    assert type(via_direct) is type(via_sdk)
    assert via_direct.to_event().keys() == via_sdk.to_event().keys()
    assert via_direct.lane == via_sdk.lane == "txn-2024"
    # The difference the change is about, and the only one.
    assert via_direct.tokens_in < via_sdk.tokens_in / 10


# ---- failure is a value --------------------------------------------------


@pytest.mark.parametrize("boom", [
    DirectStatusError(400, "bad request"),
    DirectError("response has no choices[0].message.content (keys: ['error'])"),
    httpx.ConnectError("no route to host"),
])
async def test_every_failure_degrades_to_unrouted(boom):
    routing = await classify(
        table(), "txn.csv", sample(), profile=DIRECT, direct=Recorder(boom),
        gate=instant_gate(),
    )
    assert routing.unrouted is Unrouted.FAILED
    assert not routing.routed
    assert routing.reason  # the caller is an agent; it needs to be told why


async def test_unparseable_json_is_failed_not_no_match():
    """An answer that is not JSON is a broken endpoint, not "cannot tell"."""
    routing = await classify(
        table(), "txn.csv", sample(), profile=DIRECT,
        direct=Recorder(Completion("I'm happy to help!", 0.0, 588, 12)),
    )
    assert routing.unrouted is Unrouted.FAILED
    assert routing.tokens_in == 588  # still accounted for; the call happened


async def test_a_lane_that_is_not_in_the_table_is_no_match():
    routing = await classify(
        table(), "txn.csv", sample(), profile=DIRECT, direct=Recorder(ok("invented")),
    )
    assert routing.unrouted is Unrouted.NO_MATCH


# ---- the gate ------------------------------------------------------------


async def test_a_cooling_backend_makes_the_call_wait():
    """Spec: 分類請求經過節流閘 -- not a second retry policy of its own."""
    gate = instant_gate()
    gate.trigger_cooldown(7.5)

    completion, report = await complete_via_gate(
        DIRECT, model="aird-35b", system="s", user="u",
        transport=Recorder(ok()), gate=gate,
    )
    assert completion.tokens_in == 588
    assert report.waited_s == 7.5
    assert report.waits == 1


async def test_transient_status_retries_through_the_gate():
    gate = instant_gate()
    rec = Recorder(DirectStatusError(429, "slow down"), ok())

    completion, report = await complete_via_gate(
        DIRECT, model="aird-35b", system="s", user="u", transport=rec, gate=gate,
    )
    assert completion.tokens_in == 588
    assert len(rec.calls) == 2
    assert report.cooldowns_triggered == 1


async def test_a_permanent_status_is_not_retried():
    rec = Recorder(DirectStatusError(400, "bad model"), ok())
    with pytest.raises(DirectStatusError):
        await complete_via_gate(
            DIRECT, model="aird-35b", system="s", user="u", transport=rec,
            gate=instant_gate(),
        )
    assert len(rec.calls) == 1


async def test_giving_up_still_degrades_to_unrouted():
    """Spec: 放棄時仍降級為未路由 -- ingress never depends on the classifier."""
    gate = instant_gate(retry_budget_s=0.0)
    routing = await classify(
        table(), "txn.csv", sample(), profile=DIRECT, gate=gate,
        direct=Recorder(DirectStatusError(503, "upstream down")),
    )
    assert routing.unrouted is Unrouted.FAILED
    assert "503" in routing.reason


# ---- the profile ---------------------------------------------------------


def test_base_url_alone_does_not_imply_a_direct_path():
    """OpenRouter has a base_url and speaks Anthropic. It must stay on the SDK."""
    openrouter = registry.get("openrouter")
    assert openrouter.base_url
    assert not openrouter.has_direct_path


def test_direct_profile_comes_from_the_environment(monkeypatch):
    from myharness.backends import profile as mod

    monkeypatch.setenv(mod.DIRECT_BASE_URL_ENV, "http://192.0.2.1:8000/")
    monkeypatch.setenv(mod.DIRECT_MODEL_ENV, "some-model")
    monkeypatch.delenv(mod.DIRECT_KEY_ENV, raising=False)

    built = mod.direct_openai_from_env()
    assert built is not None
    assert built.has_direct_path
    assert built.base_url == "http://192.0.2.1:8000"  # trailing slash trimmed
    assert built.resolve_model(ModelTier.CHEAP) == "some-model"
    # An endpoint named by an env var has proved nothing.
    assert built.capabilities == frozenset()
    # No key set: an auth-free local vLLM must not raise on credential().
    assert built.credential() is None


def test_no_direct_profile_when_unconfigured(monkeypatch):
    from myharness.backends import profile as mod

    monkeypatch.delenv(mod.DIRECT_BASE_URL_ENV, raising=False)
    monkeypatch.delenv(mod.DIRECT_MODEL_ENV, raising=False)
    assert mod.direct_openai_from_env() is None


# ---- the actual request on the wire --------------------------------------
#
# Everything above drives a stand-in transport, which proves the arguments are
# right and nothing about what is sent. The guarantee this change exists for
# lives in the request body, so it is asserted there.


def wired(handler) -> "HttpDirect":
    """An HttpDirect whose client is driven by httpx's MockTransport."""
    import myharness.proxy.direct as mod

    real = httpx.AsyncClient

    class Patched(real):  # type: ignore[misc]
        def __init__(self, **kw):
            super().__init__(transport=httpx.MockTransport(handler), **kw)

    mod.httpx.AsyncClient = Patched
    return mod.HttpDirect()


@pytest.fixture(autouse=True)
def _restore_client():
    import myharness.proxy.direct as mod

    original = mod.httpx.AsyncClient
    yield
    mod.httpx.AsyncClient = original


async def test_the_request_body_carries_two_messages_and_no_more():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"lane": "txn-2024"}'}}],
            "usage": {"prompt_tokens": 588, "completion_tokens": 30},
        })

    result = await wired(handler).complete(
        base_url="http://endpoint.invalid/", token="sk-test", model="aird-35b",
        system=SYSTEM_PROMPT, user="USER PROMPT", timeout_s=5.0,
    )

    assert seen["url"] == "http://endpoint.invalid/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test"
    # Exactly two messages, and they are ours. No framework preamble, no tools.
    assert seen["body"]["messages"] == [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "USER PROMPT"},
    ]
    assert "tools" not in seen["body"]
    assert seen["body"]["temperature"] == 0
    assert result.tokens_in == 588


async def test_no_token_means_no_authorization_header():
    """A local vLLM with no auth must not get a 'Bearer None'."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "{}"}}], "usage": {},
        })

    await wired(handler).complete(
        base_url="http://endpoint.invalid", token=None, model="m",
        system="s", user="u", timeout_s=5.0,
    )
    assert seen["auth"] is None


@pytest.mark.parametrize("status", [429, 503])
async def test_transient_status_is_flagged_transient(status):
    def handler(request):
        return httpx.Response(status, text="slow down")

    with pytest.raises(DirectStatusError) as caught:
        await wired(handler).complete(
            base_url="http://endpoint.invalid", token=None, model="m",
            system="s", user="u", timeout_s=5.0,
        )
    assert caught.value.status == status
    assert caught.value.transient


async def test_a_400_is_not_transient_and_carries_the_body():
    def handler(request):
        return httpx.Response(400, text="model 'typo' not found")

    with pytest.raises(DirectStatusError) as caught:
        await wired(handler).complete(
            base_url="http://endpoint.invalid", token=None, model="typo",
            system="s", user="u", timeout_s=5.0,
        )
    assert not caught.value.transient
    # The endpoint is configurable, so the message has to say what it said.
    assert "model 'typo' not found" in str(caught.value)


async def test_a_response_of_the_wrong_shape_names_what_was_missing():
    def handler(request):
        return httpx.Response(200, json={"error": {"message": "nope"}})

    with pytest.raises(DirectError) as caught:
        await wired(handler).complete(
            base_url="http://endpoint.invalid", token=None, model="m",
            system="s", user="u", timeout_s=5.0,
        )
    assert "choices" in str(caught.value)
    assert "error" in str(caught.value)  # the keys it did have
