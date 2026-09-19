"""The A2A endpoint: the second way in, and the first one that is on a network.

The same width as ``myharness/mcp/server.py`` and at the same layer: build the
card, wrap the service in an executor, hand both to the SDK's request handler,
and return an app. `AnalysisService` is constructed by the caller and passed in
unchanged (expose-any-a2a D3).

Two things this module does NOT do, on purpose:

- **It is not in any default startup path.** It starts only as
  ``myharness a2a`` (or ``python -m myharness.a2a.server``) -- a subcommand
  somebody has to type, which ``myharness.cli`` imports only when that
  subcommand is chosen; nothing else in the package imports it. ``myharness mcp``
  starts an endpoint reachable only by whoever can spawn the process; this one
  is reachable by whoever can open a socket. Those are not the same risk, and
  the second one should start only when somebody says so. It has no console
  script of its own for the same reason.
- **It binds loopback and refuses to be talked out of it.** Authentication,
  multi-tenancy and rate limiting are all out of scope for this change, and a
  boundary with none of them has no business listening on a public interface.
  `serve()` raises rather than accepting a host it cannot vouch for.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

from myharness.a2a.card import build_agent_card
from myharness.a2a.executor import AnalysisExecutor
from myharness.loopback import NotLoopback
from myharness.loopback import require_loopback as _require_loopback
from myharness.mcp.service import AnalysisService

if TYPE_CHECKING:  # the a2a extra is optional; only the annotation needs it
    from starlette.applications import Starlette

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8973
RPC_PATH = "/a2a/v1"


#: Re-exported: the rule is shared with the web monitor and lives in
#: `myharness.loopback`, because this module drags in the optional A2A SDK and
#: says so in its own docstring.
_WHY: Final = ("the A2A endpoint has no authentication yet "
               "(expose-over-a2a, Non-Goals), so it serves loopback only")


def require_loopback(host: str) -> str:
    """A hostname is not evidence. An address that resolves to loopback is."""
    return _require_loopback(host, _WHY)


def build_app(service: AnalysisService, *, url: str) -> Starlette:
    """A Starlette app serving the agent card and the JSON-RPC routes.

    Imported here rather than at module scope: the a2a extra is optional, and
    someone who only speaks MCP should get an ImportError when they ask for this
    endpoint, not when they import the package (D6).
    """
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.routes.agent_card_routes import create_agent_card_routes
    from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
    from starlette.applications import Starlette

    from myharness.a2a.store import EventLogTaskStore

    card = build_agent_card(url)
    handler = DefaultRequestHandler(
        agent_executor=AnalysisExecutor(service),
        # Not the SDK's in-memory store: a GetTask for a job this process did
        # not run would 404 on something that is on disk and readable (D5).
        task_store=EventLogTaskStore(service),
        agent_card=card,
    )
    routes = [
        *create_agent_card_routes(card),
        *create_jsonrpc_routes(handler, rpc_url=RPC_PATH),
    ]
    return Starlette(routes=routes)


def endpoint_url(host: str, port: int) -> str:
    """What the card promises. A wrong address here is worse than none."""
    return f"http://{host}:{port}{RPC_PATH}"


def serve(service: AnalysisService, *, host: str = DEFAULT_HOST,
          port: int = DEFAULT_PORT) -> None:
    import uvicorn

    host = require_loopback(host)
    app = build_app(service, url=endpoint_url(host, port))
    uvicorn.run(app, host=host, port=port, log_level="info")


def main(argv: list[str] | None = None, *,
         prog: str = "python -m myharness.a2a.server") -> int:
    """Deliberately not a console script of its own -- see the module docstring.

    Run it as ``myharness a2a``, which is a sentence somebody has to type.
    """
    from myharness.mcp.server import DEFAULT_CHARTERS, DEFAULT_ROOT, default_lanes

    parser = argparse.ArgumentParser(
        prog=prog,
        description="MyHarness over A2A. Loopback only; no authentication yet.",
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--charters", type=Path, default=DEFAULT_CHARTERS)
    parser.add_argument("--backend", default="openrouter")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)

    try:
        host = require_loopback(args.host)
    except NotLoopback as exc:
        print(exc, file=sys.stderr)
        return 2

    service = AnalysisService(
        args.root,
        lanes=default_lanes(args.charters, backend=args.backend),
        backend=args.backend,
    )
    print(f"A2A endpoint on {endpoint_url(host, args.port)}", file=sys.stderr)
    print(f"agent card at http://{host}:{args.port}/.well-known/agent-card.json",
          file=sys.stderr)
    serve(service, host=host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "RPC_PATH",
    "NotLoopback",
    "build_app",
    "main",
    "require_loopback",
    "serve",
]
