"""Token estimation for the budget ceiling, kept apart from the pricelist's.

``myharness/artifacts/tokens.py`` estimates deliberately high: its numbers
price a section before anyone reads it, and an overestimate there only makes a
reader think something is more expensive than it is. This one decides whether
to stop a run, where an overestimate kills work that was going fine. Same
question, opposite consequence for being wrong -- so the coefficients are
separate rather than shared (golden run #15).

The coefficients are measured against a live backend rather than assumed, by
``spikes/spike26_solve_rates.py``. They belong to a tokenizer, not to this
repository, so re-run it when the backend changes. Measure with real text: a
repeated filler string measures the tokenizer's merges rather than the text.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Final

#: A run of ascii letters and digits. Usually one BPE token, sometimes two for
#: a long or unusual one, which is why the coefficient below is a little over 1.
WORDS: Final = re.compile(r"[A-Za-z0-9]+")

#: Solved over 35 real requests recorded across two runs of spike #26, by
#: regressing reported input tokens on what each request carried.
#:
#: Counting ascii as one undivided class is what made the earlier coefficients
#: unstable: the two runs solved it to 3.14 and 3.91 chars/token, a 25% spread
#: that looked like noise and was not. English prose tokenizes near four
#: characters a token; a JSON tool call, dense with quotes, braces and artifact
#: ids, near three. A single rate over a mixture measures that run's mixture,
#: so it moved whenever the mixture did. Split apart, the same two runs agree to
#: within 3% and each predicts the other to within 1.7%.
TOKENS_PER_WORD: Final = 1.12
TOKENS_PER_PUNCT: Final = 0.64

#: Non-ascii, still one class. Chinese is close to uniform here and the two runs
#: agreed on it all along (0.660 and 0.682); it was never the unstable half.
CJK_TOKENS_PER_CHAR: Final = 0.67


@dataclass(frozen=True, slots=True)
class TextCount:
    """Text measured in the three quantities the rates are priced against.

    Kept as counts rather than collapsed straight into tokens so that a
    recorded run can be re-solved for better coefficients without replaying
    transcripts -- which excerpt tool results at 2,000 characters, the dominant
    term (golden run #17).
    """

    words: int = 0
    punct: int = 0
    cjk: int = 0

    def __add__(self, other: TextCount) -> TextCount:
        return TextCount(self.words + other.words, self.punct + other.punct,
                         self.cjk + other.cjk)

    @property
    def tokens(self) -> int:
        return math.ceil(self.words * TOKENS_PER_WORD
                         + self.punct * TOKENS_PER_PUNCT
                         + self.cjk * CJK_TOKENS_PER_CHAR)

    def to_dict(self, prefix: str) -> dict[str, int]:
        return {f"{prefix}_words": self.words, f"{prefix}_punct": self.punct,
                f"{prefix}_cjk": self.cjk}


def count(text: str) -> TextCount:
    """Words, punctuation and non-ascii, counted apart.

    Whitespace gets no column at all: BPE folds a leading space into the word
    after it, so a space is usually free. Giving it one lets the fit hand it a
    negative rate -- it did, -0.36 -- which then extrapolates badly onto text
    with a different line length.
    """
    cjk = sum(1 for ch in text if ord(ch) > 127)
    ascii_chars = len(text) - cjk
    alnum = sum(1 for ch in text if ch.isascii() and ch.isalnum())
    space = sum(1 for ch in text if ch.isascii() and ch.isspace())
    return TextCount(words=len(WORDS.findall(text)),
                     punct=ascii_chars - alnum - space, cjk=cjk)


def estimate(text: str) -> int:
    """Tokens for one piece of text, at the measured rates."""
    return count(text).tokens


__all__ = ["CJK_TOKENS_PER_CHAR", "TOKENS_PER_PUNCT", "TOKENS_PER_WORD",
           "TextCount", "count", "estimate"]
