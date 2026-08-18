"""design.md D6: only the layout module may know the on-disk layout.

The backend abstraction is decoration unless this holds -- the moment a lane
tool composes ``jobs/<id>/blobs/...`` itself, swapping in MinIO stops being a
new class and becomes a rewrite. Code review cannot hold this line reliably,
so it is a failing test instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

import myharness

PACKAGE_ROOT = Path(myharness.__file__).parent
LAYOUT_MODULE = PACKAGE_ROOT / "local_layout.py"

# Directory and file names that only the layout module may spell out.
LAYOUT_LITERALS = frozenset(
    {"jobs", "blobs", "notes", "traces", "index.sqlite", "events.jsonl"}
)


def _string_constants(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append((node.lineno, node.value))
    return found


def _modules_under_test() -> list[Path]:
    return [
        p
        for p in sorted(PACKAGE_ROOT.rglob("*.py"))
        if p != LAYOUT_MODULE and "__pycache__" not in p.parts
    ]


def test_no_module_outside_layout_composes_artifact_paths():
    violations: list[str] = []
    for module in _modules_under_test():
        docstring_lines = _docstring_lines(module)
        for lineno, value in _string_constants(module):
            if lineno in docstring_lines:
                continue
            if value in LAYOUT_LITERALS:
                rel = module.relative_to(PACKAGE_ROOT.parent)
                violations.append(f"{rel}:{lineno} composes layout literal {value!r}")
    assert not violations, (
        "these modules must go through myharness.local_layout instead:\n  "
        + "\n  ".join(violations)
    )


def _docstring_lines(path: Path) -> set[int]:
    """Line numbers occupied by docstrings, which may mention the layout."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is None:
                continue
            body = node.body[0]
            lines.update(range(body.lineno, (body.end_lineno or body.lineno) + 1))
    return lines


def test_layout_module_exists_and_is_the_only_exception():
    assert LAYOUT_MODULE.exists()
    assert LAYOUT_MODULE not in _modules_under_test()
