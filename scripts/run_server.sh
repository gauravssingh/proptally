#!/usr/bin/env bash
# scripts/run_server.sh - Starts the PropTally web service.
#
# Storage is multi-tenant: storage/system.db is the control plane (accounts,
# portfolios, sessions) and each portfolio keeps its own database and vault under
# storage/tenants/<id>/. So there is no single database path to configure; use
# PROPTALLY_STORAGE_ROOT to relocate the whole tree.
#
# The server binds loopback by default. To serve a specific LAN or Tailscale address,
# set PROPTALLY_HOST to it. Binding every interface additionally requires
# PROPTALLY_ALLOW_WIDE_BIND=true, because this process holds several owners' records.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"

export PYTHONPATH="$PROJECT_ROOT/src"
export PROPTALLY_STORAGE_ROOT="${PROPTALLY_STORAGE_ROOT:-$PROJECT_ROOT/storage}"
export PROPTALLY_HOST="${PROPTALLY_HOST:-127.0.0.1}"
export PROPTALLY_PORT="${PROPTALLY_PORT:-8080}"
export RELOAD="false"

PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(command -v python3)"
fi

exec "$PYTHON_BIN" main.py
