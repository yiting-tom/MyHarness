"""Fixtures shared with the dataflow tests.

`golden5` is the real fifth golden run. It belongs to whichever layer is being
asked "would you have caught this", and that is now two layers.
"""

from __future__ import annotations

from tests.dataflow.conftest import golden5

__all__ = ["golden5"]
