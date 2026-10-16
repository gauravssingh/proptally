#!/usr/bin/env python3
"""Resume a failed tenant provision without making an existing request create storage.

A PENDING or FAILED tenant is safe to retry. ``--recover-stale`` is an explicit
operator assertion that no other process is currently provisioning the named tenant.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from sqlmodel import Session, select

from proptally.db.system_models import Tenant
from proptally.db.tenant_manager import (
    TenantManager,
    get_system_engine,
    init_system_schema,
)
from proptally.web.auth import TenantProvisioningError, provision_tenant


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True, help="Tenant id or slug to resume.")
    parser.add_argument(
        "--recover-stale",
        action="store_true",
        help="Also take over PROVISIONING state; use only after confirming its process stopped.",
    )
    args = parser.parse_args(argv)

    control_engine = get_system_engine()
    init_system_schema(control_engine)
    with Session(control_engine) as session:
        tenant = session.get(Tenant, args.tenant)
        if tenant is None:
            tenant = session.exec(select(Tenant).where(Tenant.slug == args.tenant)).first()
    if tenant is None:
        parser.error(f"No portfolio matches {args.tenant!r}.")

    try:
        tenant = provision_tenant(
            control_engine,
            TenantManager(),
            tenant.id,
            recover_stale=args.recover_stale,
        )
    except (TenantProvisioningError, RuntimeError) as exc:
        print(f"Provisioning remains incomplete for tenant {tenant.id}: {exc}", file=sys.stderr)
        return 1
    print(f"Tenant {tenant.id} is {tenant.status}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
