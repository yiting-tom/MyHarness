"""Spike #20: does a conversation cost more when it is split into more messages?

Golden #18's conversation term does not fit the characters it is made of --
solving for a per-character pair gives a negative number of tokens per Chinese
character even after spike #19's measured per-request constants are substituted
in. Two models fit the two available calibration points equally well:

    per-message envelope, re-sent cumulatively   283 and 306 per message-request
    thinking, re-sent cumulatively             1,130 and 1,274 per block

Two points cannot separate two models, and another golden run does not produce
more points while the ceiling keeps stopping the analyst before it reports.

So measure the envelope directly instead of inferring it. Hold the characters
constant and change only how many messages carry them: if an envelope exists,
the same text costs more when it arrives in more parts, and the difference
divided by the extra message count is what one message's wrapper costs.

Real text, for spike #15's reason -- a repeated filler string measures the
tokenizer's merges rather than the text.

Run: set -a && . ./.env && set +a && python spikes/spike20_message_envelope.py
"""

import asyncio
import os
import sys
from pathlib import Path

import httpx

CHARTER = Path("charters/synthesizer.md")
PAYLOAD_CHARS = 8_000
SPLITS = (1, 2, 4, 8, 16)


def corpus() -> str:
    """Real tabular text: what actually fills an analyst lane's conversation."""
    csv = Path("tests/golden/fixtures/txn-2024.csv")
    if not csv.exists():
        csv = next(Path("jobs-scratch").rglob("blobs/raw/txn-2024"))
    text = csv.read_text()
    while len(text) < PAYLOAD_CHARS:  # a small fixture, never a repeated string
        csv_more = text
        text = text + csv_more
    return text[:PAYLOAD_CHARS]


def conversation(parts: int, body: str) -> list[dict]:
    """The same characters, cut into `parts` alternating messages."""
    size = len(body) // parts
    slices = [body[i * size:(i + 1) * size] for i in range(parts)]
    slices[-1] += body[parts * size:]
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": chunk}
        for i, chunk in enumerate(slices)
    ]


async def probe(client: httpx.AsyncClient, url: str, headers: dict, model: str,
                messages: list[dict]) -> int:
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": CHARTER.read_text()}] + messages,
        "temperature": 0,
        "max_tokens": 1,
    }
    response = await client.post(url, json=payload, headers=headers)
    response.raise_for_status()
    return int(response.json()["usage"]["prompt_tokens"])


async def main() -> int:
    base_url = os.environ.get("HARNESS_PROXY_BASE_URL")
    model = os.environ.get("HARNESS_PROXY_MODEL")
    if not base_url or not model:
        print("HARNESS_PROXY_BASE_URL / _MODEL unset; nothing to measure")
        return 1
    headers = {"content-type": "application/json"}
    if token := os.environ.get("HARNESS_PROXY_KEY"):
        headers["authorization"] = f"Bearer {token}"
    url = f"{base_url.rstrip('/')}/v1/chat/completions"

    body = corpus()
    async with httpx.AsyncClient(timeout=60.0) as client:
        empty = await probe(client, url, headers, model,
                            [{"role": "user", "content": ""}])
        rows = []
        for parts in SPLITS:
            total = await probe(client, url, headers, model, conversation(parts, body))
            rows.append((parts, total))

    print(f"system prompt only, one empty message: {empty:,} tokens")
    print(f"payload: {len(body):,} real ASCII characters, always the same characters\n")
    print(f"{'messages':>8} {'prompt_tokens':>14} {'over 1 message':>15} {'per extra msg':>14}")
    one = dict(rows)[1]
    for parts, total in rows:
        extra = total - one
        per = extra / (parts - 1) if parts > 1 else 0
        print(f"{parts:8} {total:14,} {extra:15,} {per:14,.1f}")

    per_values = [(total - one) / (parts - 1) for parts, total in rows if parts > 1]
    spread = max(per_values) - min(per_values)
    print()
    print(f"per-message envelope: {min(per_values):,.1f} to {max(per_values):,.1f} "
          f"tokens (spread {spread:,.1f})")
    print()
    if max(per_values) < 50:
        print("An envelope this small cannot be golden #18's residual (283 and 306")
        print("per message-request). Thinking is the surviving candidate.")
    else:
        print("Large enough to be the residual. Compare against 283 and 306.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
