"""One HTTP request, without the agent framework underneath it.

Spike #12 measured a classification at 8,991 input tokens, of which 8,372 were
the Claude Code CLI's own base system prompt -- 93% of a request whose own
prompt is ~600 tokens. ``disallowed_tools`` does not reach that; it is not a
tool definition. A lane worker pays it for tools, multiple turns and session
management. The classifier is single-shot, tool-free and stateless, so it pays
for nothing it uses.

This module is the other path: build the two messages, POST them, read the
answer. Measured against the same prompt on a self-hosted endpoint, input fell
from 8,991 to 588 -- which is simply what our own prompt costs. The fixed
overhead is not reduced here, it is absent.

Failures are values, like everywhere else a model is called: a timeout, a 502
or an unparseable body all come back as a reason the caller can act on, because
the caller degrades to "unrouted" rather than raising (design.md D5).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from myharness.backends.gate import BackendGate, ThrottleReport, gates
from myharness.backends.profile import BackendProfile

#: Same set the worker retries on. One list, so "what counts as transient" does
#: not drift between the two paths.
from myharness.lanes.worker import TRANSIENT_STATUSES

#: A classification is one short call. Running long means something is wrong,
#: not that it is nearly done.
DEFAULT_TIMEOUT_S = 20.0
#: Enough for a one-line JSON answer and nothing else.
DEFAULT_MAX_TOKENS = 300


class DirectError(Exception):
    """Raised inside this module; the caller turns it into a value."""


@dataclass(frozen=True, slots=True)
class Completion:
    """The same shape the SDK path collects, so callers cannot tell them apart."""

    text: str
    usd: float
    tokens_in: int
    tokens_out: int


class DirectTransport(Protocol):
    """The seam that lets the offline suite drive this without a network."""

    async def complete(
        self,
        *,
        base_url: str,
        token: str | None,
        model: str,
        system: str,
        user: str,
        timeout_s: float,
    ) -> Completion:  # pragma: no cover - protocol
        ...


class HttpDirect:
    """POST to an OpenAI-compatible ``/v1/chat/completions``."""

    async def complete(
        self,
        *,
        base_url: str,
        token: str | None,
        model: str,
        system: str,
        user: str,
        timeout_s: float,
    ) -> Completion:
        headers = {"content-type": "application/json"}
        if token:
            headers["authorization"] = f"Bearer {token}"
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # The classifier picks one lane from a list. Sampling adds nothing
            # and makes the same payload route differently on a re-run.
            "temperature": 0,
            "max_tokens": DEFAULT_MAX_TOKENS,
        }
        url = f"{base_url.rstrip('/')}/v1/chat/completions"
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.post(url, json=payload, headers=headers)
        if response.status_code != 200:
            raise DirectStatusError(response.status_code, _body_excerpt(response))
        return _read(response.json())


class DirectStatusError(DirectError):
    """A non-2xx. Carries the status so the caller can tell 429 from 400."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body}")
        self.status = status

    @property
    def transient(self) -> bool:
        return self.status in TRANSIENT_STATUSES


def _body_excerpt(response: httpx.Response, limit: int = 200) -> str:
    try:
        return response.text.strip().replace("\n", " ")[:limit]
    except Exception:  # noqa: BLE001 - diagnostics must not raise
        return "<unreadable body>"


def _read(body: dict[str, Any]) -> Completion:
    """Pull the answer out, or say precisely what was missing.

    A KeyError here would reach the caller as "KeyError: 'choices'", which says
    nothing about which endpoint returned what. The endpoint is configurable,
    so the shape is not guaranteed and the message has to carry the evidence.
    """
    try:
        choices = body["choices"]
        text = choices[0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise DirectError(
            f"response has no choices[0].message.content (keys: {sorted(body)[:8]})"
        ) from exc
    usage = body.get("usage") or {}
    return Completion(
        text=str(text),
        # Reported as zero rather than estimated. Spike #12 recorded a $0.05
        # figure for a request that cost a fraction of that; a number the
        # endpoint did not give us is not better than an honest zero.
        usd=0.0,
        tokens_in=int(usage.get("prompt_tokens") or 0),
        tokens_out=int(usage.get("completion_tokens") or 0),
    )


async def complete_via_gate(
    profile: BackendProfile,
    *,
    model: str,
    system: str,
    user: str,
    transport: DirectTransport | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    gate: BackendGate | None = None,
    max_attempts: int = 3,
) -> tuple[Completion, ThrottleReport]:
    """One completion, governed by the backend's existing shared gate.

    Going direct means owning retry and throttling, and the danger is quietly
    owning them *twice*: a second policy here would mean the effective limit is
    whichever of the two is looser, and nobody would notice until a live run
    (the proposal's stated risk). So this defers to the same per-backend gate
    the lane workers use, rather than counting its own attempts against its own
    schedule.
    """
    transport = transport or HttpDirect()
    gate = gate or gates.for_backend(profile.name)
    report = ThrottleReport()
    if not profile.base_url:
        raise DirectError(f"backend {profile.name!r} has no base_url to call")
    token = profile.credential()

    last: DirectError | None = None
    async with gate.acquire():
        for attempt in range(max_attempts):
            await gate.wait_for_clearance(report)
            try:
                completion = await transport.complete(
                    base_url=profile.base_url, token=token, model=model,
                    system=system, user=user, timeout_s=timeout_s,
                )
            except DirectStatusError as exc:
                last = exc
                if not exc.transient:
                    raise
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = DirectError(f"{type(exc).__name__}: {exc}")
            else:
                return completion, report

            if attempt == max_attempts - 1:
                break
            # back_off returns False when the shared time budget is spent, and
            # that is a give-up, not another attempt.
            if not await gate.back_off(attempt, report):
                report.gave_up = True
                break

    raise last or DirectError("no attempt was made")


__all__ = [
    "Completion", "DirectError", "DirectStatusError", "DirectTransport",
    "HttpDirect", "complete_via_gate",
]
