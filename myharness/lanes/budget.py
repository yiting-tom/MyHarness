"""Token estimation for the budget ceiling, kept apart from the pricelist's.

``myharness/artifacts/tokens.py`` estimates deliberately high: its numbers
price a section before anyone reads it, and an overestimate there only makes a
reader think something is more expensive than it is. This one decides whether
to stop a run, where an overestimate kills work that was going fine. Same
question, opposite consequence for being wrong -- so the coefficients are
separate rather than shared (golden run #15).

Both coefficients are measured against a live backend rather than assumed, by
``spikes/spike15_token_rates.py``. They belong to a tokenizer, not to this
repository, so re-run it when the backend changes. Measure with real text: a
repeated filler string measures the tokenizer's merges rather than the text.
"""

from __future__ import annotations

import math
from typing import Final

#: Measured on aird-35b with 2,000 characters of txn CSV. Query output is what
#: fills a lane's context, and it is far denser than prose -- digits and commas
#: tokenize badly, so the prose figure of 4.0 would understate it by ~1.8x.
ASCII_CHARS_PER_TOKEN: Final = 2.18

#: Measured on the same backend with 2,000 Chinese characters of real prose.
#: The pricelist's estimator uses 1.5, nearly five times this. That direction
#: is safe when pricing a read and dangerous when ending a run.
CJK_TOKENS_PER_CHAR: Final = 0.31


def split_chars(text: str) -> tuple[int, int]:
    """(ascii, non-ascii) character counts. Recorded so rates stay derivable."""
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    return len(text) - non_ascii, non_ascii


def estimate(ascii_chars: int, cjk_chars: int) -> int:
    return math.ceil(ascii_chars / ASCII_CHARS_PER_TOKEN
                     + cjk_chars * CJK_TOKENS_PER_CHAR)


__all__ = ["ASCII_CHARS_PER_TOKEN", "CJK_TOKENS_PER_CHAR", "estimate", "split_chars"]
