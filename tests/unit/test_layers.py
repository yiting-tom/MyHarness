"""DESIGN.md §7: no module imports a layer above its own.

The rule is what keeps the execution path from depending on the views -- the
break it catches is lanes/tabular borrowing monitor's padding helpers, which
held until e527aa9. A new top-level module fails here until it is given a layer.
"""

from __future__ import annotations

import ast
from pathlib import Path

import myharness

PACKAGE_ROOT = Path(myharness.__file__).parent

#: Lower number = higher layer. Same number = siblings, free to import each other.
LAYERS = {
    "cli": 0, "goldens": 0, "run": 0,
    "mcp": 1, "a2a": 1, "monitor": 1,
    "orchestrator": 2, "proxy": 2,
    "jobs": 3,
    "lanes": 4,
    "artifacts": 5, "events": 5, "backends": 5, "dataflow": 5,
    "local_layout": 6, "loopback": 6, "textwidth": 6,
}


def _top(path: Path) -> str:
    return path.relative_to(PACKAGE_ROOT).parts[0].removesuffix(".py")


def test_every_module_has_a_layer():
    tops = {_top(p) for p in PACKAGE_ROOT.rglob("*.py")} - {"__init__"}
    assert tops <= LAYERS.keys(), f"give these a layer: {sorted(tops - LAYERS.keys())}"


def test_no_module_imports_a_layer_above_it():
    upward = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        me = _top(path)
        if me == "__init__":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("myharness."):
                dep = node.module.split(".")[1]
                if LAYERS[dep] < LAYERS[me]:
                    upward.append(f"{path.relative_to(PACKAGE_ROOT)}: {me} -> {dep}")
    assert not upward, "\n".join(upward)
