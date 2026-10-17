"""Apply pending schema migrations to every active portfolio.

This is a dry run unless ``--apply`` is passed. Portfolio databases are live
financial records, so applying a migration is a deliberate operator action, not
a side effect of starting the server.

    python scripts/apply_tenant_migrations.py
    python scripts/apply_tenant_migrations.py --apply
    python scripts/apply_tenant_migrations.py --apply --tenant tnt_...
"""

from __future__ import annotations

import argparse
import sys

from sqlmodel import Session, select

from proptally.db.schema import init_schema
from proptally.db.system_models import TENANT_ACTIVE, Tenant
from proptally.db.tenant_manager import TenantManager, get_system_engine, tenant_db_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the migrations. Without this the command only lists portfolios.",
    )
    parser.add_argument(
        "--tenant",
        action="append",
        default=[],
        metavar="TENANT_ID",
        help="Limit the run to one or more tenant ids.",
    )
    args = parser.parse_args()

    control_engine = get_system_engine()
    with Session(control_engine) as session:
        tenants = list(
            session.exec(
                select(Tenant)
                .where(Tenant.status == TENANT_ACTIVE)
                .order_by(Tenant.name)
            ).all()
        )

    wanted = set(args.tenant)
    selected = [tenant for tenant in tenants if not wanted or tenant.id in wanted]
    if wanted:
        missing = wanted - {tenant.id for tenant in selected}
        if missing:
            print(f"No active portfolio: {', '.join(sorted(missing))}", file=sys.stderr)
            return 2

    if not selected:
        print("No active portfolios.")
        return 0

    manager = TenantManager()
    for tenant in selected:
        print(f"{tenant.id}  {tenant.name}  {tenant_db_path(tenant.id)}")
        if args.apply:
            init_schema(manager.engine_for(tenant.id))
            print("  applied")

    if not args.apply:
        print("\nDry run. Re-run with --apply to migrate these portfolios.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
