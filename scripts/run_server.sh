#!/usr/bin/env bash
# scripts/run_server.sh - Starts PropTally web service on all interfaces (0.0.0.0:8080)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"

export PYTHONPATH="$PROJECT_ROOT/src"
export PROPTALLY_DB_PATH="${PROPTALLY_DB_PATH:-$PROJECT_ROOT/storage/client-portfolio.db}"
export PROPTALLY_DEMO_SEED="${PROPTALLY_DEMO_SEED:-false}"
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-8080}"
export RELOAD="false"

PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(command -v python3)"
fi

exec "$PYTHON_BIN" main.py
