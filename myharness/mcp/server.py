"""The stdio MCP server an external client connects to.

Not to be confused with ``create_sdk_mcp_server``, which builds an in-process
object for the SDK's own use -- that is how the lane and orchestrator tool
surfaces are made, and no outside client can reach one (design.md D7). This is
``mcp.server.Server`` speaking the protocol over stdin and stdout.

Everything above this file is protocol-free, so this module stays thin: list
the tools, route a call, and keep stdout clean.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import mcp.types as types
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

from myharness.lanes.types import LaneRegistry, LaneType
from myharness.mcp.service import AnalysisService
from myharness.mcp.tools import TOOL_DESCRIPTIONS, TOOL_SCHEMAS, build_handlers, call

SERVER_NAME = "myharness"
SERVER_VERSION = "0.1.0"

#: Not "jobs": the layout already puts a jobs/ directory under the root, so
#: that default would produce jobs/jobs/<job_id>. The layout-privacy test
#: catches the name collision, which is the point of it.
DEFAULT_ROOT = Path("myharness-jobs")
DEFAULT_CHARTERS = Path("charters")


def default_lanes(charters: Path = DEFAULT_CHARTERS, backend: str = "openrouter"):
    """The lane types shipped with the harness.

    A deployment with its own charters passes its own registry; this is the
    set that makes the server useful out of the box.
    """
    return LaneRegistry(
        LaneType(
            name="tabular-analyst",
            charter_path=charters / "tabular-analyst.md",
            tools=("read_note", "write_finding", "update_state",
                   "localize_blob", "inspect_blob", "duckdb_query"),
            model_tier="strong", backend=backend,
            token_budget=60_000, max_turns=12, state_max_tokens=2_000,
            description="表格與交易資料的統計分析；可直接查詢大型 CSV/Parquet",
        ),
        LaneType(
            name="synthesizer",
            charter_path=charters / "synthesizer.md",
            tools=("read_note", "write_finding"),
            model_tier="strong", backend=backend,
            token_budget=40_000, max_turns=8, state_max_tokens=1_000,
            description="讀取多份 finding 並收斂成一份給人閱讀的報告",
        ),
    )


def build_server(service: AnalysisService) -> Server:
    server = Server(SERVER_NAME, version=SERVER_VERSION)
    handlers = build_handlers(service)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=name,
                description=TOOL_DESCRIPTIONS[name],
                inputSchema=TOOL_SCHEMAS[name],
            )
            for name in TOOL_SCHEMAS
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
        text = await call(handlers, name, arguments or {})
        return [types.TextContent(type="text", text=text)]

    return server


async def serve(service: AnalysisService) -> None:
    server = build_server(service)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name=SERVER_NAME,
                server_version=SERVER_VERSION,
                capabilities=server.get_capabilities(
                    notification_options=_NoNotifications(), experimental_capabilities={}
                ),
            ),
        )


class _NoNotifications:
    """The server pushes nothing; clients poll. Long-poll is the push."""

    prompts_changed = False
    resources_changed = False
    tools_changed = False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="myharness-mcp", description="MyHarness as an MCP server over stdio."
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT,
                        help=f"where jobs are stored (default: ./{DEFAULT_ROOT})")
    parser.add_argument("--charters", type=Path, default=DEFAULT_CHARTERS)
    parser.add_argument("--backend", default="openrouter")
    parser.add_argument("--max-concurrent-jobs", type=int, default=None)
    args = parser.parse_args(argv)

    from myharness.mcp.manager import JobManager

    manager = (
        JobManager(max_concurrent=args.max_concurrent_jobs)
        if args.max_concurrent_jobs
        else JobManager()
    )
    service = AnalysisService(
        args.root,
        lanes=default_lanes(args.charters, args.backend),
        backend=args.backend,
        manager=manager,
    )

    async def run() -> None:
        try:
            await serve(service)
        finally:
            await service.aclose()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:  # pragma: no cover - operator action
        pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
