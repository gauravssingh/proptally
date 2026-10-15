"""Entrypoint to run PropTally Client Demo MVP."""

import os
import socket
import uvicorn

# Production POC environment configuration
os.environ.setdefault("PROPTALLY_DB_PATH", "storage/client-portfolio.db")
os.environ.setdefault("PROPTALLY_DEMO_SEED", "false")


def _create_dual_stack_sockets(port: int) -> list[socket.socket]:
    sockets = []
    # IPv4 listener (0.0.0.0)
    try:
        s_v4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s_v4.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s_v4.bind(("0.0.0.0", port))
        s_v4.listen(128)
        sockets.append(s_v4)
    except Exception:
        pass

    # IPv6 listener (::) for mDNS *.local and macOS/iOS resolution
    try:
        s_v6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        s_v6.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s_v6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        except Exception:
            pass
        s_v6.bind(("::", port))
        s_v6.listen(128)
        sockets.append(s_v6)
    except Exception:
        pass

    return sockets


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 8080))
    reload = os.environ.get("RELOAD", "false").lower() in ("true", "1")

    if not reload and host == "0.0.0.0":
        sockets = _create_dual_stack_sockets(port)
        if sockets:
            config = uvicorn.Config("proptally.web.app:app", log_level="info")
            server = uvicorn.Server(config)
            server.run(sockets=sockets)
        else:
            uvicorn.run("proptally.web.app:app", host=host, port=port, reload=reload)
    else:
        uvicorn.run("proptally.web.app:app", host=host, port=port, reload=reload)
