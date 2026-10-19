#!/usr/bin/env bash
# Fast iterative loop: focused tests, then restart the live service.
#
#   scripts/dev_cycle.sh                     # focused tests + restart
#   scripts/dev_cycle.sh --no-restart        # tests only
#   scripts/dev_cycle.sh --full              # full suite + restart
#   scripts/dev_cycle.sh tests/test_x.py     # specific tests + restart
#
# A code change needs a process restart. A template/CSS/JS change does not
# (Jinja auto-reloads and static files are read per request), but restarting is
# harmless. Environment/plist changes need bootout/bootstrap, not kickstart.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

FULL=0
RESTART=1
while [[ $# -gt 0 ]]; do
    case "$1" in
        --full) FULL=1; shift ;;
        --no-restart) RESTART=0; shift ;;
        --) shift; break ;;
        *) break ;;
    esac
done

if [[ "$FULL" -eq 1 ]]; then
    PYTHONPATH=src .venv/bin/python -m pytest -q
else
    if [[ $# -eq 0 ]]; then
        set -- \
            tests/test_setup_wizard.py \
            tests/test_property_location.py \
            tests/test_property_customizations.py
    fi
    PYTHONPATH=src .venv/bin/python -m pytest -q "$@"
fi

if [[ "$RESTART" -eq 0 ]]; then
    exit 0
fi

launchctl kickstart -k "gui/$(id -u)/com.proptally.mt"
for _ in $(seq 1 20); do
    HEALTH_URL="${PROPTALLY_HEALTH_URL:-http://127.0.0.1:8141}"
    if curl -fsS -o /dev/null "$HEALTH_URL/auth/login"; then
        echo "restarted: $HEALTH_URL"
        exit 0
    fi
    sleep 0.5
done

echo "service did not answer after restart" >&2
exit 1
