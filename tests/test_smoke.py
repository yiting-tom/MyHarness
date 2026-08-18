"""Smoke test: the package imports and pytest is wired up."""

import myharness


def test_package_imports():
    assert myharness.__name__ == "myharness"
