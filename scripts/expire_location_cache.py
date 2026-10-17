"""Delete property coordinates whose permitted cache window has elapsed.

Google's Places terms allow caching latitude/longitude for 30 consecutive
calendar days. The map is rendered from the place ID, so clearing coordinates
never removes a property's pinned location.

    python scripts/expire_location_cache.py            # dry run
    python scripts/expire_location_cache.py --apply    # clears stale rows
"""

from __future__ import annotations

import argparse

from sqlmodel import Session, select

from proptally.db.system_models import TENANT_ACTIVE, Tenant
from proptally.db.tenant_manager import TenantManager, get_system_engine
from proptally.web.property_forms import expire_stale_coordinates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Commit the deletions.")
    args = parser.parse_args()

    with Session(get_system_engine()) as session:
        tenants = list(
            session.exec(
                select(Tenant).where(Tenant.status == TENANT_ACTIVE).order_by(Tenant.name)
            ).all()
        )

    manager = TenantManager()
    total = 0
    for tenant in tenants:
        engine = manager.engine_for(tenant.id)
        with Session(engine) as session:
            count = expire_stale_coordinates(session)
            total += count
            if args.apply:
                session.commit()
        print(f"{tenant.id}  {tenant.name}  {count} stale")

    print(f"\n{total} stale coordinate set(s)." + ("" if args.apply else " Dry run."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
