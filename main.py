"""Entrypoint for the PropTally multi-portfolio server.

Binding is deliberately conservative. This instance holds several owners' financial
records behind one login wall, so it listens on loopback by default and requires an
explicit opt-in to listen more widely. Serve it over TLS (a reverse proxy such as
Caddy or Nginx, or a Tailscale/HTTPS endpoint) and set ``PROPTALLY_SECURE_COOKIES``
appropriately; see ``docs/architecture/v2/06_SECURITY_AND_DEPLOYMENT.md``.
"""

import os
import socket

import uvicorn

#: Address the server binds to unless the operator says otherwise. Loopback only:
#: ``0.0.0.0`` would expose every portfolio to the whole network.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080

#: Set to a non-loopback address (or `true` for all interfaces) to bind more widely.
BIND_ALL_INTERFACES = {"1", "true", "yes", "all", "0.0.0.0", "::"}


def _resolve_host() -> str:
    """The host to bind, refusing a wide bind unless it was asked for explicitly."""
    requested = os.environ.get("PROPTALLY_HOST") or os.environ.get("HOST") or DEFAULT_HOST
    if requested.strip().lower() in BIND_ALL_INTERFACES:
        if os.environ.get("PROPTALLY_ALLOW_WIDE_BIND", "").lower() not in {"1", "true", "yes"}:
            print(
                "Refusing to bind every interface: this instance holds multiple owners' "
                "financial records. Set PROPTALLY_ALLOW_WIDE_BIND=true if you really mean "
                "it, or set PROPTALLY_HOST to one specific address (e.g. a Tailscale IP)."
            )
            return DEFAULT_HOST
        return "0.0.0.0"
    return requested


def _dual_stack_sockets(port: int) -> list[socket.socket]:
    """IPv4 and IPv6 listeners, so mDNS/Bonjour clients resolve the host."""
    sockets = []
    try:
        s_v4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s_v4.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s_v4.bind(("0.0.0.0", port))
        s_v4.listen(128)
        sockets.append(s_v4)
    except OSError:
        pass

    try:
        s_v6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        s_v6.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s_v6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        except OSError:
            pass
        s_v6.bind(("::", port))
        s_v6.listen(128)
        sockets.append(s_v6)
    except OSError:
        pass

    return sockets


if __name__ == "__main__":
    host = _resolve_host()
    port = int(os.environ.get("PROPTALLY_PORT") or os.environ.get("PORT") or DEFAULT_PORT)
    reload_enabled = os.environ.get("RELOAD", "false").lower() in ("true", "1")

    if not reload_enabled and host == "0.0.0.0":
        sockets = _dual_stack_sockets(port)
        if sockets:
            uvicorn.Server(uvicorn.Config("proptally.web.app:app", log_level="info")).run(sockets=sockets)
        else:
            uvicorn.run("proptally.web.app:app", host=host, port=port, reload=reload_enabled)
    else:
        uvicorn.run("proptally.web.app:app", host=host, port=port, reload=reload_enabled)
