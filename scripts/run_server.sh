#!/usr/bin/env bash
# scripts/run_server.sh - Starts the PropTally web service (this deployment).
#
# Storage is multi-tenant: storage/system.db is the control plane (accounts,
# portfolios, sessions) and each portfolio keeps its own database and vault under
# storage/tenants/<id>/. So there is no single database path to configure; use
# PROPTALLY_STORAGE_ROOT to relocate the whole tree.
#
# This checkout is the multi-tenant deployment and listens on 8141 by default, so it
# can run alongside the older single-deployment checkout on 8080 without either
# stealing the other's port.
#
# The server binds loopback by default. To serve a specific LAN or Tailscale address,
# set PROPTALLY_HOST to it (the launchd agent does this per machine; machine-specific
# addresses are deliberately not recorded in the repository). Binding every interface
# additionally requires PROPTALLY_ALLOW_WIDE_BIND=true, because this process holds
# several owners' records.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"

# --- Interpreter -----------------------------------------------------------
#
# Prefer this checkout's own virtualenv. Falling back to a bare `python3` is what a
# broken deployment looks like: on macOS that is the system 3.9, which cannot import
# the package at all, so the service dies with a traceback instead of a reason. A
# missing or too-old interpreter is therefore a startup error with the fix in it.
MIN_PYTHON="3.11"

_python_ok() {
    [ -x "$1" ] || return 1
    "$1" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>/dev/null
}

PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
if ! _python_ok "$PYTHON_BIN"; then
    PYTHON_BIN=""
    for candidate in python3.13 python3.12 python3.11 python3; do
        if resolved="$(command -v "$candidate" 2>/dev/null)" && _python_ok "$resolved"; then
            PYTHON_BIN="$resolved"
            break
        fi
    done
fi

if [ -z "$PYTHON_BIN" ]; then
    echo "No Python >= $MIN_PYTHON found for this deployment." >&2
    echo "Create the virtualenv once, then run this script again:" >&2
    echo "" >&2
    echo "  python3.13 -m venv \"$PROJECT_ROOT/.venv\"" >&2
    echo "  \"$PROJECT_ROOT/.venv/bin/python\" -m pip install -e \".[dev]\"" >&2
    echo "" >&2
    exit 1
fi

if [ "$PYTHON_BIN" != "$PROJECT_ROOT/.venv/bin/python" ]; then
    echo "warning: running on $PYTHON_BIN, not this checkout's .venv." >&2
    echo "         Create it so the deployment is reproducible: python3.13 -m venv .venv" >&2
fi

export PYTHONPATH="$PROJECT_ROOT/src"
export PROPTALLY_STORAGE_ROOT="${PROPTALLY_STORAGE_ROOT:-$PROJECT_ROOT/storage}"
export PROPTALLY_HOST="${PROPTALLY_HOST:-127.0.0.1}"
export PROPTALLY_PORT="${PROPTALLY_PORT:-8141}"
export RELOAD="false"

exec "$PYTHON_BIN" main.py
