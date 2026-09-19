"""`myharness` -- one entry point for everything the package can be asked to do.

    myharness jobs | inspect | report | monitor   read-only views of a job
    myharness mcp                                  serve over MCP (stdio)
    myharness a2a                                  serve over A2A (loopback HTTP)
    myharness golden                               run the end-to-end golden job

The serving and running commands own their options: ``myharness mcp --help``
is ``myharness.mcp.server``'s own parser, reached by forwarding whatever follows
the subcommand. A second copy of those options here would be a second place for
them to drift -- the same reason the web monitor and the terminal one share one
``Activity``.

Each of those modules is imported only when its subcommand is chosen. For
``a2a`` that is the point, not an optimisation: it is the one surface on a
network, and it must start only when somebody types it (a2a/server.py).
``mcp`` also has to keep stdout clean for JSON-RPC, so nothing here prints
before handing over.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Final

from myharness.monitor import cli as views

#: Subcommand -> (module whose ``main(argv, prog=...)`` runs it, one-line help).
FORWARDED: Final[dict[str, tuple[str, str]]] = {
    "mcp": ("myharness.mcp.server", "以 MCP（stdio）提供分析服務"),
    "a2a": ("myharness.a2a.server",
            "以 A2A（只綁 loopback 的 HTTP）提供分析服務；需要 [a2a] extra"),
    "golden": ("myharness.goldens", "跑端到端的 golden job"),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="myharness",
        description="MyHarness：多 agent 資料分析 harness。",
        epilog="mcp／a2a／golden 的選項看 `myharness <command> --help`。",
    )
    parser.add_argument(
        "--root", type=Path, default=None,
        help="job 儲存根目錄，所有子命令共用；不給時各子命令用自己的預設"
             f"（檢視：{views.DEFAULT_ROOT}；mcp／a2a：myharness-jobs；golden：jobs-scratch/golden）",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")
    views.add_view_commands(sub)
    for name, (_, text) in FORWARDED.items():
        # No help of their own: -h after the subcommand belongs to the module's
        # parser, which is the one that knows the options.
        sub.add_parser(name, help=text, add_help=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args, rest = parser.parse_known_args(argv)

    if args.command in FORWARDED:
        module, _ = FORWARDED[args.command]
        forwarded = (["--root", str(args.root)] if args.root is not None else []) + rest
        try:
            entry: Callable[..., int] = importlib.import_module(module).main
            return entry(forwarded, prog=f"myharness {args.command}")
        except ModuleNotFoundError as exc:
            # The a2a extra is optional, and uvicorn is imported only once the
            # server starts -- so the miss can surface from either line. Say
            # which install is missing rather than a traceback from the SDK.
            hint = ' 安裝：uv pip install -e ".[a2a]"' if args.command == "a2a" else ""
            print(f"myharness {args.command} 缺少相依：{exc.name}。{hint}", file=sys.stderr)
            return 2

    if rest:
        parser.error(f"unrecognized arguments: {' '.join(rest)}")
    if args.root is None:
        args.root = views.DEFAULT_ROOT
    exit_code: int = args.func(args)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
