"""M5: verified reviewer identity, single→multi-tenant migration, multi-tenant backup.

Three promises are covered end to end:

1. A posting on behalf of a verified ``CPA_REVIEWER`` member is recorded on the
   ``PostingAuthorization`` with that identity and role, while an owner
   self-attestation is recorded as a self-attestation and never promoted to a
   verified CPA authorization.
2. ``scripts/migrate_to_multitenant.py`` turns a legacy single-portfolio database
   into a tenant database with identical metrics, and refuses to run twice.
3. ``scripts/backup.sh``/``restore.sh`` round-trip a two-tenant storage layout,
   including every portfolio database and vault.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tarfile
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlmodel import Session, select

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
BACKUP_SCRIPT = SCRIPTS_DIR / "backup.sh"
RESTORE_SCRIPT = SCRIPTS_DIR / "restore.sh"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import migrate_to_multitenant as migrator  # noqa: E402

from proptally.db.connection import get_engine  # noqa: E402
from proptally.db.models import (  # noqa: E402
    Document,
    FinancialAccount,
    FinancialInstitution,
    JournalEntryLine,
    PostingAuthorization,
    Property,
    Statement,
    Transaction,
)
from proptally.db.schema import init_schema  # noqa: E402
from proptally.db.system_models import (  # noqa: E402
    ROLE_CPA_REVIEWER,
    ROLE_OWNER,
    Tenant,
    TenantMembership,
    User,
    UserSession,
)
from proptally.db.tenant_manager import get_system_engine, init_system_schema  # noqa: E402
from proptally.domain.accounting.metrics import (  # noqa: E402
    calculate_portfolio_equity,
    calculate_property_financials,
)
from reference_portfolio import seed_reference_portfolio  # noqa: E402
from proptally.domain.transactions.splits import (  # noqa: E402
    SplitItem,
    apply_transaction_splits,
    post_transaction_to_ledger,
)
from proptally.web.auth import (  # noqa: E402
    TenantContext,
    posting_actor_kwargs,
    set_current_context,
    verify_password,
)

LARKSPUR = "prop_tinsley_huntley"
CBD2_TXN = "txn_apple_store_cbd2_20260322"
CBD2_CENTS = 12900

# --- 1. Verified reviewer identity on the posting authorization record ------


@pytest.fixture(name="cbd2_env")
def cbd2_env_fixture(session: Session):
    """A mixed-use card deduction whose policy demands the CBD-2 review gate."""
    bank = FinancialInstitution(id="inst_m5", name="Bank")
    session.add(bank)
    session.flush()
    session.add(FinancialAccount(
        id="acct_m5_chk", institution_id=bank.id, account_name="Checking",
        account_type="CHECKING", account_number_masked="...1",
    ))
    session.add(FinancialAccount(
        id="acct_m5_card", institution_id=bank.id, account_name="Shared card",
        account_type="CREDIT_CARD", account_number_masked="...2",
        mixed_use_status="MIXED_USE",
    ))
    session.add(Property(
        id=LARKSPUR, name="Larkspur", address_line1="a", city="Westbrook", zip_code="60196",
        property_type="RENTAL_INVESTMENT",
    ))
    doc = Document(id="doc_m5", file_sha256="sha_m5", original_filename="f.pdf",
                   storage_path="/p", file_size_bytes=1)
    session.add(doc)
    session.flush()
    session.add(Statement(
        id="stmt_m5", document_id=doc.id, financial_account_id="acct_m5_chk",
        statement_type="CHECKING", active_parser_name="T", active_parser_version="1",
        statement_date=date(2026, 3, 31),
    ))
    session.commit()
    return session


def _cbd2_txn(session: Session, txn_id: str, **overrides) -> Transaction:
    fields = {
        "statement_id": "stmt_m5",
        "financial_account_id": "acct_m5_card",
        "transaction_date": date(2026, 3, 22),
        "amount_cents": -CBD2_CENTS,
        "direction": "OUTFLOW",
        "raw_description": "POS PURCHASE 4451",
        "requires_cpa_review": True,
    }
    fields.update(overrides)
    txn = Transaction(id=txn_id, **fields)
    session.add(txn)
    session.commit()
    return txn


def _post_with_review(session: Session, txn_id: str, **actor) -> PostingAuthorization:
    apply_transaction_splits(session, txn_id, [
        SplitItem(property_id=LARKSPUR, category="SUPPLIES", amount_cents=CBD2_CENTS,
                  tax_review_status="USER_ATTESTED_CPA_REVIEW"),
    ])
    post_transaction_to_ledger(session, txn_id, user_attested_cpa_review=True, **actor)
    return session.exec(
        select(PostingAuthorization).where(PostingAuthorization.transaction_id == txn_id)
    ).one()


def test_verified_cpa_reviewer_signoff_is_recorded_distinctly(cbd2_env):
    """A CPA_REVIEWER sign-off records the reviewer; an owner self-attestation does not."""
    session = cbd2_env

    _cbd2_txn(session, "txn_by_reviewer")
    reviewer = _post_with_review(
        session, "txn_by_reviewer",
        actor_id="usr_reviewer_1", actor_role=ROLE_CPA_REVIEWER,
        actor_verified_cpa_reviewer=True,
    )

    _cbd2_txn(session, "txn_by_owner")
    owner = _post_with_review(
        session, "txn_by_owner",
        actor_id="usr_owner_1", actor_role=ROLE_OWNER,
        actor_verified_cpa_reviewer=False,
    )

    # Both supplied the attestation gate...
    assert reviewer.user_attested_cpa_review is True
    assert owner.user_attested_cpa_review is True
    assert reviewer.user_attested_cpa_review_at is not None

    # ...but only the CPA_REVIEWER sign-off is a *verified* external review.
    assert reviewer.verified_cpa_reviewer is True
    assert owner.verified_cpa_reviewer is False
    assert reviewer.authorized_by_role == ROLE_CPA_REVIEWER
    assert owner.authorized_by_role == ROLE_OWNER
    assert reviewer.authorized_by_user_id == "usr_reviewer_1"
    assert owner.authorized_by_user_id == "usr_owner_1"
    # The core distinction the plan promises: the two records differ on verification.
    assert reviewer.verified_cpa_reviewer != owner.verified_cpa_reviewer


def test_owner_cannot_be_promoted_to_verified_by_forging_the_flag(cbd2_env):
    """A caller that claims the verified flag without the CPA role is still an owner."""
    session = cbd2_env
    _cbd2_txn(session, "txn_owner_lies")
    record = _post_with_review(
        session, "txn_owner_lies",
        actor_id="usr_owner_2", actor_role=ROLE_OWNER, actor_verified_cpa_reviewer=True,
    )
    assert record.verified_cpa_reviewer is False
    assert record.authorized_by_role == ROLE_OWNER


def test_record_without_attestation_carries_no_reviewer_identity(cbd2_env):
    """The identity fields describe the review attestation only, not mere authorship."""
    session = cbd2_env
    # A deposit-account item governed by the CBD-1 gate alone, so no review is claimed.
    _cbd2_txn(
        session, "txn_owner_quiet", financial_account_id="acct_m5_chk",
        requires_client_confirmation=True, requires_cpa_review=False,
    )
    apply_transaction_splits(session, "txn_owner_quiet", [
        SplitItem(property_id=LARKSPUR, category="SUPPLIES", amount_cents=CBD2_CENTS,
                  tax_review_status="CLIENT_CONFIRMED"),
    ])
    post_transaction_to_ledger(session, "txn_owner_quiet", client_confirmed=True,
                               actor_id="usr_owner_3", actor_role=ROLE_OWNER)
    record = session.exec(
        select(PostingAuthorization).where(
            PostingAuthorization.transaction_id == "txn_owner_quiet"
        )
    ).one()
    assert record.user_attested_cpa_review is False
    assert record.authorized_by_user_id is None
    assert record.authorized_by_role is None
    assert record.verified_cpa_reviewer is False


def test_web_request_threads_the_authenticated_role(owner):
    """The request's membership role reaches the record: an owner self-attestation."""
    signed_in = owner(reference_portfolio=True)
    response = signed_in.post(
        "/transactions/split",
        data={
            "transaction_id": CBD2_TXN,
            "property_id[]": LARKSPUR,
            "category[]": "SUPPLIES",
            "amount_dollars[]": "129.00",
            "post_to_ledger": "true",
            "user_attested_cpa_review": "true",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200, response.text[:400]

    with Session(signed_in.engine) as session:
        record = session.exec(
            select(PostingAuthorization).where(PostingAuthorization.transaction_id == CBD2_TXN)
        ).one()
        assert record.user_attested_cpa_review is True
        assert record.authorized_by_role == ROLE_OWNER
        assert record.verified_cpa_reviewer is False
        # The identity is the signed-in user, never the placeholder actor.
        assert record.authorized_by_user_id not in (None, "system")


def test_posting_actor_maps_only_cpa_role_to_a_verified_reviewer():
    """The role→verification mapping is the control-plane role, not request input."""
    user = User(id="usr_ctx", email="ctx@example.com", password_hash="h", display_name="Ctx")
    tenant = Tenant(id="tnt_ctx", name="Ctx", slug="ctx")
    session = UserSession(id="sess_ctx", user_id="usr_ctx", csrf_token="c",
                          expires_at=datetime.now(timezone.utc))

    reviewer = TenantContext(user=user, tenant=tenant, role=ROLE_CPA_REVIEWER,
                             session=session, engine=None, paths=None)
    owner_ctx = TenantContext(user=user, tenant=tenant, role=ROLE_OWNER,
                              session=session, engine=None, paths=None)

    assert reviewer.posting_actor()["actor_verified_cpa_reviewer"] is True
    assert reviewer.posting_actor()["actor_role"] == ROLE_CPA_REVIEWER
    assert owner_ctx.posting_actor()["actor_verified_cpa_reviewer"] is False

    # Outside a request the services fall back to the unauthenticated actor.
    set_current_context(None)
    assert posting_actor_kwargs() == {
        "actor_id": "system", "actor_role": None, "actor_verified_cpa_reviewer": False,
    }


# --- 2. Single-portfolio → multi-tenant migration --------------------------


def _journal_line_count(session: Session) -> int:
    from sqlalchemy import func

    return session.exec(select(func.count()).select_from(JournalEntryLine)).one()


def _seed_legacy_database(db_path: Path) -> None:
    engine = get_engine(f"sqlite:///{db_path}")
    init_schema(engine)
    with Session(engine) as session:
        seed_reference_portfolio(session)


def _make_pre_m5(db_path: Path) -> None:
    """Remove the reviewer columns and forget that migration, as a pre-M5 database would be.

    The migration is identified by what it does rather than by its number: numbers are
    a shared namespace and this code line deliberately uses a reserved range, so a
    hard-coded number here would quietly stop simulating the old shape.
    """
    engine = get_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        for column in ("authorized_by_user_id", "authorized_by_role", "verified_cpa_reviewer"):
            conn.exec_driver_sql(
                f"ALTER TABLE posting_authorizations DROP COLUMN {column}"
            )
        conn.exec_driver_sql(
            "DELETE FROM schema_migrations WHERE description LIKE '%reviewer identity%'"
        )
    engine.dispose()


def test_migration_preserves_metrics_and_refuses_a_second_run(tmp_path):
    legacy_dir = tmp_path / "legacy"
    vault = legacy_dir / "vault"
    vault.mkdir(parents=True)
    (vault / "lease.pdf").write_text("the authoritative lease")
    source_db = legacy_dir / "client-portfolio.db"

    _seed_legacy_database(source_db)
    _make_pre_m5(source_db)

    before_engine = get_engine(f"sqlite:///{source_db}")
    with Session(before_engine) as session:
        before_equity = calculate_portfolio_equity(session)
        before_property = calculate_property_financials(session, LARKSPUR)
        before_lines = _journal_line_count(session)
    before_engine.dispose()

    storage_root = tmp_path / "storage"
    result = migrator.migrate_to_multitenant(
        source_db,
        storage_root=storage_root,
        email="owner@example.com",
        password="a-strong-password",
        display_name="Legacy Owner",
        portfolio_name="Legacy Portfolio",
        apply=True,
        keep_source=True,
    )
    assert result.applied is True

    tenant_db = result.plan.tenant_paths.database
    assert tenant_db.exists(), "the migration must create the tenant database"

    # Control plane: an owner account and its portfolio membership exist.
    with Session(get_system_engine(f"sqlite:///{result.plan.system_db}")) as control:
        user = control.exec(select(User).where(User.email == "owner@example.com")).one()
        assert verify_password(user.password_hash, "a-strong-password")
        tenant = control.get(Tenant, result.plan.tenant_id)
        assert tenant is not None
        membership = control.exec(
            select(TenantMembership).where(TenantMembership.tenant_id == tenant.id)
        ).one()
        assert membership.role == ROLE_OWNER

    # Identical portfolio metrics and ledger volume in the tenant database.
    tenant_engine = get_engine(f"sqlite:///{tenant_db}")
    with Session(tenant_engine) as session:
        after_equity = calculate_portfolio_equity(session)
        after_property = calculate_property_financials(session, LARKSPUR)
        after_lines = _journal_line_count(session)

        # The migration brought the schema up to date, columns and version record.
        connection = session.connection()
        columns = {
            row[1] for row in connection.exec_driver_sql(
                "PRAGMA table_info(posting_authorizations)"
            )
        }
        assert {"authorized_by_user_id", "authorized_by_role", "verified_cpa_reviewer"} <= columns
        descriptions = {
            row[0] for row in connection.exec_driver_sql(
                "SELECT description FROM schema_migrations"
            )
        }
        # Identified by what it did, not by its number: those are a shared namespace.
        assert any("reviewer identity" in d for d in descriptions)

    assert after_equity["net_equity_cents"] == before_equity["net_equity_cents"]
    assert after_equity["total_valuation_cents"] == before_equity["total_valuation_cents"]
    assert after_property == before_property
    assert after_lines == before_lines

    # The legacy vault moved (copied here) in beside the portfolio database.
    assert (result.plan.tenant_paths.vault / "lease.pdf").read_text() == "the authoritative lease"

    # A second run over the same data refuses.
    with pytest.raises(migrator.MigrationError, match="already migrated"):
        migrator.migrate_to_multitenant(
            source_db,
            storage_root=storage_root,
            email="someone-else@example.com",
            password="a-strong-password",
            apply=True,
            keep_source=True,
        )

    # The refusal keys on the source path rather than its bytes, because these
    # databases run in WAL mode and a later checkpoint rewrites the main file even
    # though the data is identical. Prove it: an explicit override proceeds, so a
    # deliberately restored copy is not a dead end.
    forced = migrator.migrate_to_multitenant(
        source_db,
        storage_root=storage_root,
        email="another-owner@example.com",
        password="a-strong-password",
        apply=True,
        keep_source=True,
        force=True,
    )
    assert forced.applied is True
    assert forced.plan.tenant_paths.database.exists()


def test_a_different_legacy_file_with_identical_contents_is_not_blocked(tmp_path):
    """Identity is the file's path, so a copy at a new path migrates cleanly."""
    first = tmp_path / "one"
    first.mkdir()
    (first / "vault").mkdir()
    source_one = first / "client-portfolio.db"
    _seed_legacy_database(source_one)

    second = tmp_path / "two"
    second.mkdir()
    (second / "vault").mkdir()
    source_two = second / "client-portfolio.db"
    shutil.copy2(source_one, source_two)

    storage_root = tmp_path / "storage"
    for source, email in ((source_one, "one@example.com"), (source_two, "two@example.com")):
        result = migrator.migrate_to_multitenant(
            source,
            storage_root=storage_root,
            email=email,
            password="a-strong-password",
            apply=True,
            keep_source=True,
        )
        assert result.applied is True, source


def test_migration_is_a_dry_run_by_default(tmp_path):
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    source_db = legacy_dir / "proptally.db"
    _seed_legacy_database(source_db)

    storage_root = tmp_path / "storage"
    result = migrator.migrate_to_multitenant(
        source_db, storage_root=storage_root, email="owner@example.com",
        password="a-strong-password", portfolio_name="Dry Run",
    )
    assert result.applied is False
    assert source_db.exists()
    assert not (storage_root / "system.db").exists()
    assert not (storage_root / "tenants").exists()


def test_migration_moves_the_database_by_default(tmp_path):
    """The default is a move, and the moved data survives even with a live WAL."""
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    source_db = legacy_dir / "proptally.db"
    # The seeding engine is deliberately left open, so committed pages live in the
    # write-ahead log: a raw file copy would lose them.
    _seed_legacy_database(source_db)

    result = migrator.migrate_to_multitenant(
        source_db, storage_root=tmp_path / "storage", email="owner@example.com",
        password="a-strong-password", portfolio_name="Moved", apply=True,
    )
    assert result.applied is True
    assert not source_db.exists(), "the default migration moves the legacy database"

    tenant_engine = get_engine(f"sqlite:///{result.plan.tenant_paths.database}")
    with Session(tenant_engine) as session:
        assert session.exec(select(Property)).first() is not None
        assert _journal_line_count(session) > 0


# --- 3. Multi-tenant backup / restore round trip ---------------------------


def _make_tenant(storage_root: Path, tenant_id: str, account_name: str) -> None:
    directory = storage_root / "tenants" / tenant_id
    (directory / "vault").mkdir(parents=True)
    engine = get_engine(f"sqlite:///{directory / 'proptally.db'}")
    init_schema(engine)
    with Session(engine) as session:
        session.add(FinancialInstitution(id=f"inst_{tenant_id}", name="Bank"))
        session.flush()
        session.add(FinancialAccount(
            id=f"acct_{tenant_id}", institution_id=f"inst_{tenant_id}",
            account_name=account_name, account_type="CHECKING", account_number_masked="...1",
        ))
        session.commit()
    (directory / "vault" / "doc.txt").write_text(f"vault-{tenant_id}")


def _seed_two_tenant_storage(storage_root: Path) -> None:
    storage_root.mkdir(parents=True, exist_ok=True)
    control_engine = get_system_engine(f"sqlite:///{storage_root / 'system.db'}")
    init_system_schema(control_engine)
    with Session(control_engine) as session:
        for tenant_id, name in (("tnt_one", "One"), ("tnt_two", "Two")):
            session.add(User(id=f"usr_{tenant_id}", email=f"{tenant_id}@example.com",
                             password_hash="h", display_name=name))
            session.add(Tenant(id=tenant_id, name=name, slug=tenant_id))
        session.flush()
        for tenant_id in ("tnt_one", "tnt_two"):
            session.add(TenantMembership(id=f"mem_{tenant_id}", user_id=f"usr_{tenant_id}",
                                         tenant_id=tenant_id, role=ROLE_OWNER))
        session.commit()
    control_engine.dispose()

    _make_tenant(storage_root, "tnt_one", "One Checking")
    _make_tenant(storage_root, "tnt_two", "Two Checking")


def test_backup_and_restore_round_trip_over_two_tenants(tmp_path):
    """Both portfolios (database + vault) survive backup and restore intact."""
    source_storage = tmp_path / "storage"
    _seed_two_tenant_storage(source_storage)
    backup_dir = tmp_path / "backups"

    backup = subprocess.run(
        [str(BACKUP_SCRIPT), "--storage", str(source_storage), "--out", str(backup_dir)],
        capture_output=True, text=True,
    )
    assert backup.returncode == 0, f"backup.sh failed:\n{backup.stderr}\n{backup.stdout}"
    assert backup.stdout.count("Database integrity verified: ok") == 3  # system + 2 tenants
    archive = sorted(backup_dir.glob("proptally_backup_*.tar.gz"))[0]
    assert archive.with_name(archive.name + ".sha256").exists()

    with tarfile.open(archive) as tar:
        members = tar.getnames()
    assert any(m.endswith("system/database.sqlite") for m in members)
    assert any("tnt_one/proptally.db" in m for m in members)
    assert any("tnt_two/proptally.db" in m for m in members)
    assert any("tnt_one/vault/doc.txt" in m for m in members)
    assert any("tnt_two/vault/doc.txt" in m for m in members)

    target_storage = tmp_path / "restored"
    restore = subprocess.run(
        [str(RESTORE_SCRIPT), "--archive", str(archive), "--target-storage", str(target_storage)],
        capture_output=True, text=True,
    )
    assert restore.returncode == 0, f"restore.sh failed:\n{restore.stderr}\n{restore.stdout}"
    assert "Restore completed successfully" in restore.stdout

    assert (target_storage / "system.db").exists()
    for tenant_id, account_name in (("tnt_one", "One Checking"), ("tnt_two", "Two Checking")):
        tenant_engine = get_engine(f"sqlite:///{target_storage / 'tenants' / tenant_id / 'proptally.db'}")
        with Session(tenant_engine) as session:
            account = session.get(FinancialAccount, f"acct_{tenant_id}")
            assert account is not None, f"portfolio {tenant_id} lost its data"
            assert account.account_name == account_name
        vault_file = target_storage / "tenants" / tenant_id / "vault" / "doc.txt"
        assert vault_file.read_text() == f"vault-{tenant_id}"


def test_single_tenant_export_contains_only_that_portfolio(tmp_path):
    """--tenant exports one portfolio and leaves the shared control plane out."""
    source_storage = tmp_path / "storage"
    _seed_two_tenant_storage(source_storage)
    backup_dir = tmp_path / "backups"

    backup = subprocess.run(
        [str(BACKUP_SCRIPT), "--storage", str(source_storage), "--tenant", "tnt_one",
         "--out", str(backup_dir)],
        capture_output=True, text=True,
    )
    assert backup.returncode == 0, f"backup.sh failed:\n{backup.stderr}\n{backup.stdout}"
    archive = sorted(backup_dir.glob("proptally_backup_*_tnt_one.tar.gz"))[0]

    with tarfile.open(archive) as tar:
        members = tar.getnames()
    assert any("tnt_one/proptally.db" in m for m in members)
    assert any("tnt_one/vault/doc.txt" in m for m in members)
    assert not any("tnt_two" in m for m in members)
    assert not any(m.endswith("system/database.sqlite") for m in members)

    # A portfolio-only archive restores into a fresh root without a control plane.
    target_storage = tmp_path / "restored"
    restore = subprocess.run(
        [str(RESTORE_SCRIPT), "--archive", str(archive), "--target-storage", str(target_storage)],
        capture_output=True, text=True,
    )
    assert restore.returncode == 0, f"restore.sh failed:\n{restore.stderr}\n{restore.stdout}"
    assert not (target_storage / "system.db").exists()
    tenant_engine = get_engine(
        f"sqlite:///{target_storage / 'tenants' / 'tnt_one' / 'proptally.db'}"
    )
    with Session(tenant_engine) as session:
        assert session.get(FinancialAccount, "acct_tnt_one") is not None
    assert (target_storage / "tenants" / "tnt_one" / "vault" / "doc.txt").exists()


#: The tables the ledger allocator touches, as they looked before per-entity codes.
LEGACY_TABLES = (
    """CREATE TABLE financial_institutions (
        id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL, routing_number VARCHAR,
        website VARCHAR, created_at TIMESTAMP NOT NULL)""",
    """CREATE TABLE ledger_accounts (
        code INTEGER PRIMARY KEY, name VARCHAR NOT NULL, account_type VARCHAR NOT NULL,
        normal_balance VARCHAR NOT NULL)""",
    """CREATE TABLE properties (
        id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL, address_line1 VARCHAR NOT NULL,
        address_line2 VARCHAR, city VARCHAR NOT NULL, state VARCHAR NOT NULL,
        zip_code VARCHAR NOT NULL, property_type VARCHAR NOT NULL, acquisition_date DATE,
        purchase_price_cents INTEGER, land_basis_cents INTEGER, building_basis_cents INTEGER,
        is_active BOOLEAN NOT NULL, description TEXT, google_maps_url TEXT,
        cover_photo_id VARCHAR, created_at TIMESTAMP NOT NULL)""",
    """CREATE TABLE financial_accounts (
        id VARCHAR PRIMARY KEY, institution_id VARCHAR NOT NULL, account_name VARCHAR NOT NULL,
        account_type VARCHAR NOT NULL, account_number_masked VARCHAR NOT NULL,
        mixed_use_status VARCHAR NOT NULL, currency VARCHAR NOT NULL, is_active BOOLEAN NOT NULL,
        created_at TIMESTAMP NOT NULL)""",
    """CREATE TABLE loan_accounts (
        id VARCHAR PRIMARY KEY, financial_account_id VARCHAR NOT NULL, property_id VARCHAR NOT NULL,
        loan_number VARCHAR NOT NULL, origination_date DATE, original_principal_cents INTEGER,
        term_months INTEGER NOT NULL, current_interest_rate_bps INTEGER, is_active BOOLEAN NOT NULL,
        created_at TIMESTAMP NOT NULL)""",
    """CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY, description TEXT NOT NULL, applied_at TEXT NOT NULL)""",
)


def _build_legacy_database(db_path):
    """A database in the shape it had before per-entity ledger codes, plus a foreign record."""
    from proptally.db.connection import get_engine

    engine = get_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        for ddl in LEGACY_TABLES:
            conn.exec_driver_sql(ddl)
        # The versions this code line shares with the original sequence.
        for version, description in (
            (1, "add post-initial-schema columns"),
            (2, "install missing CHECK constraints"),
            (3, "create missing indexes"),
            (4, "add property description, maps url, and photo columns"),
            (5, "create vendor_rules table and seed standard rules"),
        ):
            conn.exec_driver_sql(
                "INSERT INTO schema_migrations (version, description, applied_at) "
                "VALUES (?, ?, '2026-10-12T00:00:00')",
                (version, description),
            )
        # And a version another code line claimed for something else entirely -- the case
        # that broke this migration on a real portfolio database.
        conn.exec_driver_sql(
            "INSERT INTO schema_migrations (version, description, applied_at) "
            "VALUES (6, 'create property_annual_tax_depreciation table', '2026-10-14T06:05:51')"
        )
    return engine


def test_a_database_written_by_another_code_line_still_migrates(tmp_path):
    """Migration numbers are a shared namespace, and another line may have taken one.

    A real portfolio database recorded version 6 as "create
    property_annual_tax_depreciation table", written by a different tool sharing the same
    migration table. This code line's own sixth change was then skipped as "already
    applied", and the step that allocates per-entity ledger accounts ran against columns
    that had never been added -- so migrating that portfolio failed outright.

    Two things now prevent it: this code line's numbers start at 101, so nothing it
    defines can be displaced; and the allocation step ensures its own columns first.
    """
    from proptally.db.schema import SCHEMA_MIGRATIONS, run_migrations

    # 1. The numbering cannot collide with a number another line has taken.
    defined = {version for version, _, _ in SCHEMA_MIGRATIONS}
    assert defined & set(range(1, 100)) == {1, 2, 3, 4, 5}, (
        "numbers 6-99 belong to whatever else has written to these databases"
    )
    assert min(v for v in defined if v > 5) >= 100

    engine = _build_legacy_database(tmp_path / "legacy.db")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO financial_institutions (id, name, created_at) VALUES ('inst', 'Bank', '2026-01-01')"
        )
        conn.exec_driver_sql(
            "INSERT INTO properties (id, name, address_line1, city, state, zip_code, "
            "property_type, is_active, created_at) VALUES "
            "('prop_legacy', 'Legacy Door', '1 Old St', 'Austin', 'TX', '78701', "
            "'RENTAL_INVESTMENT', 1, '2026-01-01')"
        )
        conn.exec_driver_sql(
            "INSERT INTO financial_accounts (id, institution_id, account_name, account_type, "
            "account_number_masked, mixed_use_status, currency, is_active, created_at) VALUES "
            "('acct_legacy', 'inst', 'Checking', 'CHECKING', '...1', 'UNCLASSIFIED', 'USD', 1, '2026-01-01')"
        )

    run_migrations(engine)

    with engine.begin() as conn:
        prop_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(properties)")}
        acct_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(financial_accounts)")}
        versions = {row[0] for row in conn.exec_driver_sql("SELECT version FROM schema_migrations")}
        prop_code = conn.exec_driver_sql(
            "SELECT asset_ledger_code FROM properties WHERE id='prop_legacy'"
        ).scalar()
        acct_code = conn.exec_driver_sql(
            "SELECT ledger_account_code FROM financial_accounts WHERE id='acct_legacy'"
        ).scalar()

    assert "asset_ledger_code" in prop_cols, "the column was added despite the foreign version 6"
    assert "ledger_account_code" in acct_cols
    assert prop_code is not None and acct_code is not None, "and then actually allocated"
    assert {101, 102, 103} <= versions
    assert 6 in versions, "the other code line's record is left alone"
