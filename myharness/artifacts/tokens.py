"""Conservative token estimation.

The point of ``est_tokens`` is that the *pre-read* check exists at all, not that
it is exact (design.md D3). It must never underestimate badly, because an
underestimate is exactly the case that blows a worker's context.

A flat "4 chars per token" underestimates CJK text by roughly 4-6x, and this
harness analyses Chinese data, so ASCII and non-ASCII are counted separately.
Coefficients are module-level so they can be calibrated against real usage
recorded in the event log.
"""

from __future__ import annotations

import math
from typing import Final

ASCII_CHARS_PER_TOKEN: Final = 4.0
NON_ASCII_TOKENS_PER_CHAR: Final = 1.5


def estimate_tokens(text: str) -> int:
    """Upper-leaning estimate of the tokens ``text`` would occupy."""
    if not text:
        return 0
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    ascii_chars = len(text) - non_ascii
    return math.ceil(
        ascii_chars / ASCII_CHARS_PER_TOKEN + non_ascii * NON_ASCII_TOKENS_PER_CHAR
    )
