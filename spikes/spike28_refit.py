"""Spike #28: re-fit the rates with the block envelope in the model.

Spike #27 found the estimator's input gap is two things. One is structural:
``_message_count`` charges a block's payload and the wire also charges for the
wrapper -- "type", a 32-hex id, an ``mcp__lane__``-prefixed name -- 35.9 tokens
a block, re-sent with every later request. The other is the rates themselves,
which look about 19% low on dense analytical Chinese.

Adding the envelope as a constant and leaving the rest alone halved the bias
and widened the spread, because every other coefficient was fitted without it
and had absorbed part of it. Spike #26 learned this once already: a term added
to a model that was fitted without it moves error around rather than removing
it. So all of them get solved together, or none of them do.

Three candidate shapes:

  payload      what the estimator counts now -- block payloads only
  wire         the same three rates over the whole serialised request
  payload+blk  payload counting, plus a column for the number of blocks

And the scoring matters as much as the shapes. Leave-one-out *within* a capture
is nearly free here: request i+1 of a run is request i plus a little, so the
held-out point is almost inside the training set. The honest test is to fit on
two captures and predict the third, which is the comparison that killed run 1
of spike #26 (2.2% in-sample, 16% on the next run's largest request).

Run: .venv/bin/python spikes/spike28_refit.py      (no backend needed)
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, "spikes")

import spike26_solve_rates as s26  # noqa: E402

CAPTURES = {
    "run2": "spikes/spike26_exchanges_run2.json",
    "run3": "spikes/spike26_exchanges_20260913T064218Z.json",
    "critic": "spikes/spike27_exchanges.json",
}


def _wire_text(request: dict) -> str:
    """Everything the request carried, serialised as it went out."""
    return (s26._text_of(request.get("system")) + s26._text_of(request.get("tools"))
            + s26._text_of(request.get("messages")))


def _blocks(request: dict) -> float:
    """Content blocks this request carried, thinking stubs excluded.

    Excluded because they are wrapper with nothing in them and are already a
    separate known quantity (spike #27: 221 tokens over the same capture).
    """
    n = 0
    for m in request.get("messages") or ():
        content = m.get("content")
        if isinstance(content, list):
            n += sum(1 for b in content
                     if isinstance(b, dict) and b.get("type") != "thinking")
    return float(n)


def features(request: dict) -> dict[str, float]:
    payload = s26._features(s26._request_text(request))
    wire = s26._features(_wire_text(request))
    return {
        **payload,
        "w_words": wire["words"], "w_punct": wire["punct"], "w_cjk": wire["cjk"],
        "blocks": _blocks(request),
    }


SHAPES = {
    "payload    ": ["words", "punct", "cjk", "one"],
    "wire       ": ["w_words", "w_punct", "w_cjk", "one"],
    "payload+blk": ["words", "punct", "cjk", "blocks", "one"],
}


def fit(keys, rows):
    return s26._solve([[f[k] for k in keys] for f, _ in rows], [t for _, t in rows])


def predict(keys, coefficients, f):
    return sum(f[k] * c for k, c in zip(keys, coefficients, strict=True))


def main() -> int:
    data = {}
    for name, path in CAPTURES.items():
        rows = json.loads(Path(path).read_text())
        data[name] = [(features(e["request"]), float(e["usage"]["input_tokens"]))
                      for e in rows if e["usage"].get("input_tokens")]
        print(f"{name:7} {len(data[name]):3} priced requests   {path}")

    every = [r for rows in data.values() for r in rows]
    print(f"{'total':7} {len(every):3}\n")

    print("held out a whole capture, fitted on the other two:")
    for shape, keys in SHAPES.items():
        worsts, means = [], []
        for held in CAPTURES:
            train = [r for name, rows in data.items() if name != held for r in rows]
            coefficients = fit(keys, train)
            errs = [abs(predict(keys, coefficients, f) - t) / t for f, t in data[held]]
            worsts.append(max(errs))
            means.append(sum(errs) / len(errs))
            print(f"  {shape}  預測 {held:7} worst {max(errs)*100:5.1f}%  "
                  f"mean {sum(errs)/len(errs)*100:5.1f}%")
        print(f"  {shape}  ---------------  worst {max(worsts)*100:5.1f}%  "
              f"mean {sum(means)/len(means)*100:5.1f}%\n")

    print("fitted on everything:")
    for shape, keys in SHAPES.items():
        coefficients = fit(keys, every)
        worst = max(abs(predict(keys, coefficients, f) - t) / t for f, t in every)
        print(f"  {shape}  in-sample worst {worst*100:5.1f}%")
        print("    " + "  ".join(f"{k.strip()}={c:.4f}"
                                 for k, c in zip(keys, coefficients, strict=True)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
