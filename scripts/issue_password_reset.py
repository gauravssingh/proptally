#!/usr/bin/env python3
"""Issue a single-use password-reset link for an operator to deliver out of band.

The public web page never reveals or issues reset tokens. This command requires access to
this host's control-plane database, then prints a short-lived link for the operator to give
directly to the verified account holder.

Usage:
    python scripts/issue_password_reset.py --email user@example.com
    python scripts/issue_password_reset.py --email user@example.com \
        --base-url https://proptally.example.test
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from proptally.db.tenant_manager import get_system_engine, system_db_path
from proptally.web.auth import issue_password_reset


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True, help="Account email to issue a reset link for")
    parser.add_argument(
        "--base-url",
        default="",
        help="Optional externally reachable origin, e.g. https://proptally.example.test",
    )
    args = parser.parse_args(argv)

    database = system_db_path()
    if not database.is_file():
        parser.error(f"control-plane database does not exist: {database}")

    engine = get_system_engine()
    try:
        token = issue_password_reset(engine, args.email)
    finally:
        engine.dispose()

    if token is None:
        parser.error("no account exists for that email address")

    base_url = args.base_url.rstrip("/")
    link = f"{base_url}/auth/reset/{token}" if base_url else f"/auth/reset/{token}"
    print("Single-use password reset link (valid for two hours):")
    print(link)
    print("Deliver it directly to the verified account holder over a trusted channel; do not post it publicly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
