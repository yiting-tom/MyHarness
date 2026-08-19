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


#: Ways a path actually gets composed. A bare string that merely happens to
#: read "jobs" -- an argparse subcommand, say -- is not a layout violation, and
#: flagging it would train people to ignore this test.
_PATH_CALLS = frozenset({"Path", "PurePath", "joinpath", "join", "with_name",
                         "with_suffix", "glob", "rglob", "open"})


def _path_literals(path: Path) -> list[tuple[int, str]]:
    """String literals used to build a path, with their line numbers."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []

    def note(node: ast.AST) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append((node.lineno, node.value))
        elif isinstance(node, ast.JoinedStr):
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    found.append((node.lineno, part.value.strip("/")))

    for node in ast.walk(tree):
        # root / "jobs" / job_id
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            note(node.left)
            note(node.right)
        # Path("jobs"), x.joinpath("jobs"), os.path.join(..., "jobs")
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name in _PATH_CALLS:
                for arg in node.args:
                    note(arg)
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
        for lineno, value in _path_literals(module):
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


def test_the_check_still_catches_a_real_composition(tmp_path: Path):
    """Narrowing the check to path context must not have defanged it."""
    probe = tmp_path / "probe.py"
    probe.write_text('from pathlib import Path\nBAD = Path("x") / "jobs" / "y"\n',
                     encoding="utf-8")
    assert any(v in LAYOUT_LITERALS for _, v in _path_literals(probe))


def test_the_check_ignores_a_coincidental_word(tmp_path: Path):
    probe = tmp_path / "probe.py"
    probe.write_text('parser.add_parser("jobs", help="list jobs")\n', encoding="utf-8")
    assert not any(v in LAYOUT_LITERALS for _, v in _path_literals(probe))
