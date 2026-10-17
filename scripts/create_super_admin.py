#!/usr/bin/env python3
"""Create or update the platform's super admin on this host.

The super admin is the application's own backend operator: the principal that administers
the software and the tenants on it. It is **not** a portfolio role, not a membership, and
not a tenant. That is why it is created here, on the host, and why no web route anywhere
can mint one -- tenant registration deliberately confers no platform authority.

Run it again to rotate the password or change the display name; it is idempotent and
revokes any existing session for the account.

Usage:
    python scripts/create_super_admin.py --email ops@example.com
    python scripts/create_super_admin.py --email ops@example.com --name "Ops"
    python scripts/create_super_admin.py --email ops@example.com --password '...'

Prefer the prompt (omit --password): a password on the command line lands in your shell
history and in the process list.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from proptally.db.tenant_manager import (
    get_system_engine,
    init_system_schema,
    system_db_path,
)
from proptally.web.auth import create_super_admin


def _resolve_password(args: argparse.Namespace) -> str:
    if args.password:
        return args.password
    first = getpass.getpass("Super admin password: ")
    second = getpass.getpass("Repeat password: ")
    if first != second:
        raise SystemExit("Those passwords did not match.")
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True, help="The operator's sign-in address")
    parser.add_argument(
        "--password",
        default="",
        help="Password. Omit to be prompted, which keeps it out of your shell history.",
    )
    parser.add_argument("--name", default="", help="Optional display name")
    args = parser.parse_args(argv)

    engine = get_system_engine()
    try:
        # A fresh host may never have been started: create the control plane if needed,
        # so the operator can be established before the first request is ever served.
        init_system_schema(engine)
        password = _resolve_password(args)
        try:
            user = create_super_admin(
                engine, email=args.email, password=password, display_name=args.name
            )
        except ValueError as exc:
            parser.error(str(exc))
    finally:
        engine.dispose()

    print(f"Super admin ready: {user.email}  ({user.id})")
    print(f"Control plane:     {system_db_path()}")
    print("Sign in at /admin/login. No portfolio or membership was created, and none is needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
