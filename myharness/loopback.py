"""One rule about bind addresses, for every surface that listens on a port.

Two of them now do: the A2A endpoint and the web monitor. Neither has
authentication, so neither has any business on a public interface, and the rule
lives here rather than in either of them -- `myharness/a2a/server.py` states as
an invariant that nothing else in the package imports it, and it is worth
keeping, since importing it drags in the optional A2A SDK.
"""

from __future__ import annotations

import ipaddress


class NotLoopback(ValueError):
    """Raised for a bind address a local-only surface will not serve on."""

    def __init__(self, host: str, why: str = "") -> None:
        super().__init__(
            f"refusing to bind {host!r}: "
            + (why or "this endpoint has no authentication, so it serves "
                      "loopback only")
        )
        self.host = host


def require_loopback(host: str, why: str = "") -> str:
    """A hostname is not evidence. An address that resolves to loopback is.

    "localhost" is accepted because it is the name of the thing; anything else
    has to *be* a loopback address, so a public interface cannot arrive through
    a name that happens to point at one today.
    """
    if host == "localhost":
        return host
    try:
        if ipaddress.ip_address(host).is_loopback:
            return host
    except ValueError:
        pass
    raise NotLoopback(host, why)


__all__ = ["NotLoopback", "require_loopback"]
