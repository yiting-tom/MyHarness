"""`myharness`: one entry point, with the serving commands forwarded intact."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from myharness import cli


def test_every_command_is_listed(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    for name in ("jobs", "inspect", "report", "monitor", "mcp", "a2a", "golden"):
        assert name in out


@pytest.mark.parametrize("command", sorted(cli.FORWARDED))
def test_forwarded_help_is_the_modules_own_parser(command, capsys):
    """-h after the subcommand reaches the module that knows the options."""
    with pytest.raises(SystemExit) as caught:
        cli.main([command, "--help"])
    assert caught.value.code == 0
    assert f"usage: myharness {command}" in capsys.readouterr().out


def test_the_shared_root_and_the_rest_are_forwarded(monkeypatch):
    seen: dict = {}

    def fake(argv, *, prog):
        seen.update(argv=argv, prog=prog)
        return 0

    import myharness.goldens
    monkeypatch.setattr(myharness.goldens, "main", fake)
    assert cli.main(["--root", "somewhere", "golden", "--job-id", "g9"]) == 0
    assert seen == {"argv": ["--root", "somewhere", "--job-id", "g9"], "prog": "myharness golden"}


def test_without_a_shared_root_each_command_keeps_its_default(monkeypatch):
    seen: dict = {}

    def fake(argv, *, prog):
        seen["argv"] = argv
        return 0

    import myharness.goldens
    monkeypatch.setattr(myharness.goldens, "main", fake)
    cli.main(["golden"])
    assert seen["argv"] == [], "golden's own default root applies"


def test_views_still_refuse_unknown_options(capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["jobs", "--bogus"])
    assert caught.value.code == 2
    assert "--bogus" in capsys.readouterr().err


def test_views_use_the_shared_root(tmp_path: Path, capsys):
    assert cli.main(["--root", str(tmp_path), "jobs"]) == 1
    assert str(tmp_path) in capsys.readouterr().out, "it looked where it was told"


def test_a_missing_extra_is_named_not_thrown(monkeypatch, capsys):
    def missing(name):
        raise ModuleNotFoundError("No module named 'uvicorn'", name="uvicorn")

    monkeypatch.setattr(cli.importlib, "import_module", missing)
    assert cli.main(["a2a"]) == 2
    err = capsys.readouterr().err
    assert "uvicorn" in err and ".[a2a]" in err


def test_the_network_surface_is_not_imported_unless_asked():
    """a2a is the one endpoint on a socket; it starts only when typed."""
    code = ("import sys, myharness.cli; "
            "print('myharness.a2a.server' in sys.modules, 'myharness.mcp.server' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True).stdout.split()
    assert out == ["False", "False"]


async def test_myharness_mcp_speaks_clean_json_rpc_on_stdout(tmp_path: Path):
    """stdio MCP breaks on a single stray print before the handshake."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "myharness.cli", "--root", str(tmp_path), "mcp"],
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    # stderr goes to a file: pytest's captured stderr has no descriptor to hand
    # a subprocess, and stdout is the channel under test anyway.
    with (tmp_path / "stderr.log").open("w") as errlog:
        async with (stdio_client(params, errlog=errlog) as (read, write),
                    ClientSession(read, write) as session):
            await session.initialize()
            names = {t.name for t in (await session.list_tools()).tools}
    assert "analysis_start" in names
