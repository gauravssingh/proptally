"""Failure-path and concurrency regressions for tenant lifecycle boundaries."""

import argparse
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from proptally.db import portfolio_lifecycle
from proptally.db.models import FinancialInstitution
from proptally.db.portfolio_lifecycle import PortfolioDeletionError, delete_portfolio
from proptally.db.system_models import (
    ROLE_OWNER,
    ROLE_READ_ONLY,
    TENANT_ACTIVE,
    TENANT_DELETED,
    TENANT_DELETING,
    TENANT_FAILED,
    Tenant,
    TenantMembership,
    TenantOperation,
    User,
)
from proptally.db.tenant_manager import (
    TenantManager,
    TenantStorageError,
    get_system_engine,
    init_system_schema,
)
from proptally.web.auth import (
    AuthorizationError,
    NoActivePortfolio,
    TenantProvisioningError,
    _acquire_tenant_operation,
    _release_tenant_operation,
    accept_invite,
    create_invite,
    issue_session,
    register_portfolio,
    remove_member,
    resolve_context,
    retry_incomplete_provisioning,
    revoke_session,
)

PASSWORD = "hardening-test-password"


@pytest.fixture

def tenant_control(tmp_path):
    storage = tmp_path / "storage"
    control = get_system_engine(f"sqlite:///{storage / 'system.db'}")
    init_system_schema(control)
    manager = TenantManager(storage_root_path=storage)
    yield control, manager, storage
    for engine in list(manager._engines.values()):
        engine.dispose()
    control.dispose()


def test_tenant_manager_uses_production_root_and_test_database_override(tmp_path, monkeypatch):
    production_root = tmp_path / "production-storage"
    monkeypatch.setenv("PROPTALLY_STORAGE_ROOT", str(production_root))
    production = TenantManager()
    assert production.root == production_root
    production.provision_tenant_storage("tnt_production")
    assert production.paths("tnt_production").database.is_file()

    override_root = tmp_path / "harness-storage"
    shared_database = tmp_path / "harness.db"
    harness = TenantManager(
        db_url=f"sqlite:///{shared_database}", storage_root_path=override_root
    )
    assert harness.root == override_root
    harness.provision_tenant_storage("tnt_harness")
    assert harness.paths("tnt_harness").root.is_dir()
    assert harness.engine_for("tnt_harness").url.database == str(shared_database)
    assert shared_database.is_file()
    for engine in (*production._engines.values(), *harness._engines.values()):
        engine.dispose()

def _register(control, manager, email: str, name: str):
    return register_portfolio(
        control,
        manager,
        email=email,
        password=PASSWORD,
        display_name=email.split("@", 1)[0],
        portfolio_name=name,
    )


def _deletable_portfolio(control, manager, prefix="delete"):
    keeper_user, keeper_tenant, _ = _register(
        control, manager, f"{prefix}-keeper@example.com", f"{prefix} Keeper"
    )
    owner_user, doomed_tenant, _ = _register(
        control, manager, f"{prefix}-owner@example.com", f"{prefix} Doomed"
    )
    _, token = create_invite(
        control,
        tenant_id=keeper_tenant.id,
        email=owner_user.email,
        role=ROLE_READ_ONLY,
        invited_by_user_id=keeper_user.id,
    )
    accept_invite(control, manager, token=token, password=PASSWORD)
    return owner_user, doomed_tenant



def test_failed_only_membership_can_reach_login_and_logout(owner):
    signed_in = owner(email="unavailable@example.com", portfolio_name="Unavailable")
    with Session(signed_in.control_engine) as session:
        tenant = session.get(Tenant, signed_in.tenant_id)
        tenant.status = TENANT_FAILED
        session.add(tenant)
        session.commit()

    login = signed_in.client.get("/auth/login", follow_redirects=False)
    assert login.status_code == 200
    assert "Sign in" in login.text
    logout = signed_in.client.post("/auth/logout", data={}, follow_redirects=False)
    assert logout.status_code == 303
    assert logout.headers["location"] == "/auth/login"
def test_registration_failure_stays_unusable_and_startup_retries(tenant_control, monkeypatch):
    control, manager, _storage = tenant_control
    original_provision = manager.provision_tenant_storage

    def fail_provision(_tenant_id):
        raise OSError("injected filesystem failure")

    monkeypatch.setattr(manager, "provision_tenant_storage", fail_provision)
    with pytest.raises(TenantProvisioningError, match="safe to retry"):
        _register(control, manager, "pending@example.com", "Pending Portfolio")

    with Session(control) as session:
        user = session.exec(select(User).where(User.email == "pending@example.com")).one()
        tenant = session.exec(select(Tenant).where(Tenant.name == "Pending Portfolio")).one()
    assert tenant.status == TENANT_FAILED
    assert not manager.paths(tenant.id).database.exists()
    token, _ = issue_session(control, user, tenant.id)
    with pytest.raises(NoActivePortfolio, match="no active portfolio"):
        resolve_context(control, manager, token)

    monkeypatch.setattr(manager, "provision_tenant_storage", original_provision)
    retry_incomplete_provisioning(control, manager)
    with Session(control) as session:
        tenant = session.get(Tenant, tenant.id)
    assert tenant.status == TENANT_ACTIVE
    assert manager.paths(tenant.id).database.is_file()



def test_failed_first_membership_does_not_shadow_an_active_workspace(tenant_control):
    control, manager, _storage = tenant_control
    user, failed_tenant, _ = _register(control, manager, "shadowed@example.com", "Failed")
    failed_tenant_id = failed_tenant.id
    active_owner, active_tenant, _ = _register(
        control, manager, "active-owner@example.com", "Active"
    )
    _, invite_token = create_invite(
        control,
        tenant_id=active_tenant.id,
        email=user.email,
        role=ROLE_READ_ONLY,
        invited_by_user_id=active_owner.id,
    )
    accept_invite(control, manager, token=invite_token, password=PASSWORD)
    with Session(control) as session:
        failed_tenant = session.get(Tenant, failed_tenant_id)
        failed_tenant.status = TENANT_FAILED
        session.add(failed_tenant)
        session.commit()
    token, _ = issue_session(control, user, failed_tenant_id)

    context = resolve_context(control, manager, token)
    assert context.tenant.id == active_tenant.id
    assert context.role == ROLE_READ_ONLY
def test_missing_active_database_fails_closed_without_cross_tenant_fallback(tenant_control):

    control, manager, _storage = tenant_control
    user_a, tenant_a, _ = _register(control, manager, "a@example.com", "A")
    _user_b, tenant_b, _ = _register(control, manager, "b@example.com", "B")
    token, _ = issue_session(control, user_a, tenant_a.id)
    missing_path = manager.paths(tenant_a.id).database
    missing_path.unlink()

    with pytest.raises(TenantStorageError, match="database is missing"):
        resolve_context(control, manager, token)
    assert not missing_path.exists(), "request resolution must not create an empty database"
    assert manager.paths(tenant_b.id).database.is_file()
    assert manager.engine_for(tenant_b.id).url.database == str(manager.paths(tenant_b.id).database)
    assert not manager.paths(tenant_a.id).root.joinpath("proptally.db").exists()


def test_deletion_does_not_count_a_failed_membership_as_another_usable_portfolio(tenant_control):
    control, manager, _storage = tenant_control
    owner_user, target, _ = _register(control, manager, "last-active@example.com", "Active")
    failed_owner, failed_tenant, _ = _register(
        control, manager, "failed-owner@example.com", "Failed"
    )
    failed_tenant_id = failed_tenant.id
    _, invite_token = create_invite(
        control,
        tenant_id=failed_tenant_id,
        email=owner_user.email,
        role=ROLE_READ_ONLY,
        invited_by_user_id=failed_owner.id,
    )
    accept_invite(control, manager, token=invite_token, password=PASSWORD)
    with Session(control) as session:
        failed_row = session.get(Tenant, failed_tenant_id)
        failed_row.status = TENANT_FAILED
        session.add(failed_row)
        session.commit()

    with pytest.raises(PortfolioDeletionError, match="no other active portfolio"):
        delete_portfolio(control, manager, tenant_id=target.id, actor_user_id=owner_user.id)
    with Session(control) as session:
        assert session.get(Tenant, target.id).status == TENANT_ACTIVE
    assert manager.paths(target.id).root.is_dir()


def test_deletion_refuses_when_other_active_membership_has_missing_storage(tenant_control):
    control, manager, _storage = tenant_control
    owner_user, target = _deletable_portfolio(control, manager, "missing-fallback")
    with Session(control) as session:
        fallback_id = session.exec(
            select(TenantMembership.tenant_id).where(
                TenantMembership.user_id == owner_user.id,
                TenantMembership.tenant_id != target.id,
            )
        ).one()
    missing_database = manager.paths(fallback_id).database
    missing_database.unlink()

    with pytest.raises(PortfolioDeletionError, match="usable storage"):
        delete_portfolio(
            control, manager, tenant_id=target.id, actor_user_id=owner_user.id
        )

    with Session(control) as session:
        assert session.get(Tenant, target.id).status == TENANT_ACTIVE
        assert session.exec(
            select(TenantMembership).where(
                TenantMembership.user_id == owner_user.id,
                TenantMembership.tenant_id == target.id,
            )
        ).one()
    assert manager.paths(target.id).root.is_dir()


def test_invitation_claim_and_membership_are_atomic_under_concurrent_acceptance(tenant_control):
    control, manager, _storage = tenant_control
    owner_user, tenant, _ = _register(control, manager, "inviter@example.com", "Shared")
    _invite, invite_token = create_invite(
        control,
        tenant_id=tenant.id,
        email="joined@example.com",
        role=ROLE_READ_ONLY,
        invited_by_user_id=owner_user.id,
    )
    start = threading.Barrier(2)
    results = []

    def accept():
        start.wait(timeout=5)
        try:
            results.append(("accepted", accept_invite(
                control, manager, token=invite_token, password=PASSWORD
            )))
        except ValueError as exc:
            results.append(("refused", str(exc)))

    threads = [threading.Thread(target=accept) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert [result[0] for result in results].count("accepted") == 1
    refused = [result[1] for result in results if result[0] == "refused"]
    assert len(refused) == 1
    assert "invalid, already used, or has expired" in refused[0]
    with Session(control) as session:
        user = session.exec(select(User).where(User.email == "joined@example.com")).one()
        membership = session.exec(
            select(TenantMembership).where(
                TenantMembership.user_id == user.id,
                TenantMembership.tenant_id == tenant.id,
            )
        ).one()
        assert membership.role == ROLE_READ_ONLY
        assert len(session.exec(select(TenantMembership).where(
            TenantMembership.user_id == user.id,
            TenantMembership.tenant_id == tenant.id,
        )).all()) == 1


def test_engine_lru_disposes_only_idle_handles(tenant_control, monkeypatch):
    _control, manager, _storage = tenant_control
    monkeypatch.setattr("proptally.db.tenant_manager.ENGINE_CACHE_SIZE", 2)
    manager.provision_tenant_storage("tnt_cache_a")
    engine_a = manager.engine_for("tnt_cache_a")
    disposed = set()

    def track_disposal(engine, label):
        dispose = engine.dispose

        def wrapped_dispose():
            disposed.add(label)
            dispose()

        monkeypatch.setattr(engine, "dispose", wrapped_dispose)

    track_disposal(engine_a, "a")
    manager.begin_operation("tnt_cache_a")
    manager.provision_tenant_storage("tnt_cache_b")
    engine_b = manager.engine_for("tnt_cache_b")
    track_disposal(engine_b, "b")
    manager.provision_tenant_storage("tnt_cache_c")

    assert "b" in disposed
    assert "a" not in disposed
    assert "tnt_cache_a" in manager._engines
    manager.end_operation("tnt_cache_a")
    manager.provision_tenant_storage("tnt_cache_d")
    assert "a" in disposed
    assert manager.paths("tnt_cache_a").database.is_file()


def test_instance_admin_is_not_membership_or_role_override(tenant_control):
    control, manager, _storage = tenant_control
    _owner_user, tenant, _owner_membership = _register(
        control, manager, "infra-owner@example.com", "Owner"
    )
    infrastructure_only = User(
        id="usr_infrastructure_only",
        email="infra-only@example.com",
        password_hash="unused",
        display_name="Infrastructure only",
        is_instance_admin=True,
    )
    read_only_admin = User(
        id="usr_readonly_admin",
        email="readonly-admin@example.com",
        password_hash="unused",
        display_name="Read-only admin",
        is_instance_admin=True,
    )
    infrastructure_user_id = infrastructure_only.id
    read_only_user_id = read_only_admin.id
    with Session(control) as session:
        session.add(infrastructure_only)
        session.add(read_only_admin)
        session.flush()
        session.add(TenantMembership(
            id="mem_readonly_admin", user_id=read_only_admin.id,
            tenant_id=tenant.id, role=ROLE_READ_ONLY,
        ))
        session.commit()

    infrastructure_token, _ = issue_session(
        control, SimpleNamespace(id=infrastructure_user_id), tenant.id
    )
    assert resolve_context(control, manager, infrastructure_token) is None

    read_only_token, _ = issue_session(
        control, SimpleNamespace(id=read_only_user_id), tenant.id
    )
    context = resolve_context(control, manager, read_only_token)
    assert context.user.is_instance_admin is True
    assert context.role == ROLE_READ_ONLY
    assert context.can_mutate is False
    with pytest.raises(AuthorizationError):
        context.require_mutation()


def test_revoked_membership_and_session_block_later_resolution(tenant_control):
    control, manager, _storage = tenant_control
    owner_user, tenant, _ = _register(control, manager, "member-owner@example.com", "Shared")
    _, token = create_invite(
        control,
        tenant_id=tenant.id,
        email="revoked@example.com",
        role=ROLE_READ_ONLY,
        invited_by_user_id=owner_user.id,
    )
    member, _joined_tenant, membership = accept_invite(
        control, manager, token=token, password=PASSWORD
    )
    member_token, _ = issue_session(control, member, tenant.id)
    assert resolve_context(control, manager, member_token) is not None
    remove_member(control, membership.id, tenant_id=tenant.id)
    assert resolve_context(control, manager, member_token) is None

    owner_token, _ = issue_session(control, owner_user, tenant.id)
    assert resolve_context(control, manager, owner_token) is not None
    revoke_session(control, owner_token)
    assert resolve_context(control, manager, owner_token) is None


def test_delete_waits_for_inflight_operation_and_revokes_sessions(tenant_control, monkeypatch):
    control, manager, _storage = tenant_control
    owner_user, tenant = _deletable_portfolio(control, manager, "inflight")
    token, row = issue_session(control, owner_user, tenant.id)
    operation_id = _acquire_tenant_operation(
        control, tenant.id, owner_user.id, row.id, ROLE_OWNER
    )
    assert operation_id is not None
    manager.begin_operation(tenant.id, operation_id)

    waiting = threading.Event()
    resume = threading.Event()

    def pause_deletion(_delay):
        waiting.set()
        assert resume.wait(timeout=5)

    monkeypatch.setattr(portfolio_lifecycle.time, "sleep", pause_deletion)
    outcomes = []

    def delete():
        try:
            outcomes.append(delete_portfolio(
                control, manager, tenant_id=tenant.id, actor_user_id=owner_user.id
            ))
        except Exception as exc:  # noqa: BLE001 - assert thread failures in the parent
            outcomes.append(exc)

    worker = threading.Thread(target=delete)
    worker.start()
    assert waiting.wait(timeout=5)
    with Session(control) as session:
        deleting = session.get(Tenant, tenant.id)
    assert deleting.status == TENANT_DELETING
    assert manager.paths(tenant.id).root.is_dir()
    assert resolve_context(control, manager, token) is None

    _release_tenant_operation(control, manager, operation_id, tenant.id)
    resume.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert len(outcomes) == 1 and not isinstance(outcomes[0], Exception)
    with Session(control) as session:
        deleted = session.get(Tenant, tenant.id)
    assert deleted.status == TENANT_DELETED
    assert not manager.paths(tenant.id).root.exists()



def test_stale_deletion_claim_is_reclaimed_only_after_worker_is_dead(tenant_control, monkeypatch):
    control, manager, _storage = tenant_control
    owner_user, tenant = _deletable_portfolio(control, manager, "stale-delete")
    with Session(control) as session:
        row = session.get(Tenant, tenant.id)
        row.status = TENANT_DELETING
        row.deletion_token = "stale-deletion-token"
        row.deletion_process_id = 987654321
        row.deletion_host_name = portfolio_lifecycle.socket.gethostname()
        session.add(row)
        session.commit()

    monkeypatch.setattr(portfolio_lifecycle, "_local_process_is_alive", lambda _pid: True)
    with pytest.raises(PortfolioDeletionError, match="live or unverifiable worker"):
        delete_portfolio(control, manager, tenant_id=tenant.id, actor_user_id=owner_user.id)
    assert manager.paths(tenant.id).root.is_dir()
    with Session(control) as session:
        assert session.get(Tenant, tenant.id).deletion_token == "stale-deletion-token"

    monkeypatch.setattr(portfolio_lifecycle, "_local_process_is_alive", lambda _pid: False)
    delete_portfolio(control, manager, tenant_id=tenant.id, actor_user_id=owner_user.id)
    with Session(control) as session:
        deleted = session.get(Tenant, tenant.id)
    assert deleted.status == TENANT_DELETED
    assert deleted.deletion_token is None
    assert not manager.paths(tenant.id).root.exists()
def test_failed_filesystem_cleanup_remains_deleting_and_can_be_retried(tenant_control, monkeypatch):
    control, manager, _storage = tenant_control
    owner_user, tenant = _deletable_portfolio(control, manager, "retry-delete")
    original_drop = manager.drop_tenant_storage

    def fail_cleanup(_tenant_id):
        raise OSError("injected cleanup failure")

    monkeypatch.setattr(manager, "drop_tenant_storage", fail_cleanup)
    with pytest.raises(PortfolioDeletionError, match="cleanup failed"):
        delete_portfolio(control, manager, tenant_id=tenant.id, actor_user_id=owner_user.id)

    with Session(control) as session:
        deleting = session.get(Tenant, tenant.id)
        memberships = session.exec(
            select(TenantMembership).where(TenantMembership.tenant_id == tenant.id)
        ).all()
    assert deleting.status == TENANT_DELETING
    assert deleting.deletion_token is None
    assert memberships
    assert manager.paths(tenant.id).root.is_dir()

    monkeypatch.setattr(manager, "drop_tenant_storage", original_drop)
    result = delete_portfolio(control, manager, tenant_id=tenant.id, actor_user_id=owner_user.id)
    assert result.removed_files is True
    with Session(control) as session:
        assert session.get(Tenant, tenant.id).status == TENANT_DELETED
        assert not session.exec(
            select(TenantMembership).where(TenantMembership.tenant_id == tenant.id)
        ).all()


def test_concurrent_deletion_has_one_claimant(tenant_control):
    control, manager, _storage = tenant_control
    owner_user, tenant = _deletable_portfolio(control, manager, "concurrent-delete")
    start = threading.Barrier(2)
    outcomes = []

    def delete():
        start.wait(timeout=5)
        try:
            delete_portfolio(control, manager, tenant_id=tenant.id, actor_user_id=owner_user.id)
            outcomes.append("deleted")
        except PortfolioDeletionError:
            outcomes.append("refused")

    threads = [threading.Thread(target=delete) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert outcomes.count("deleted") == 1
    assert outcomes.count("refused") == 1
    with Session(control) as session:
        assert session.get(Tenant, tenant.id).status == TENANT_DELETED


def test_named_cli_job_uses_only_its_tenant_database_and_paths(tenant_control, monkeypatch):
    control, manager, storage = tenant_control
    _user_a, tenant_a, _ = _register(control, manager, "cli-a@example.com", "CLI A")
    _user_b, tenant_b, _ = _register(control, manager, "cli-b@example.com", "CLI B")
    monkeypatch.setenv("PROPTALLY_STORAGE_ROOT", str(storage))
    monkeypatch.setenv("PROPTALLY_SYSTEM_DB_PATH", str(storage / "system.db"))

    from proptally.ingestion.cli import _resolve_target, _tenant_session

    args = SimpleNamespace(tenant=tenant_a.id, db=None, vault_dir=None)
    engine, vault, inbox, job_scope, is_active = _resolve_target(
        argparse.ArgumentParser(), args
    )
    assert Path(engine.url.database) == manager.paths(tenant_a.id).database
    assert vault == manager.paths(tenant_a.id).vault
    assert inbox == manager.paths(tenant_a.id).inbox
    assert is_active()

    with Session(manager.engine_for(tenant_a.id)) as session:
        session.add(FinancialInstitution(id="inst_cli_a", name="Tenant A"))
        session.commit()
    with Session(manager.engine_for(tenant_b.id)) as session:
        session.add(FinancialInstitution(id="inst_cli_b", name="Tenant B"))
        session.commit()

    with _tenant_session(engine, job_scope) as session:
        assert session.get(FinancialInstitution, "inst_cli_a") is not None
        assert session.get(FinancialInstitution, "inst_cli_b") is None
    with Session(control) as session:
        assert not session.exec(select(TenantOperation)).all()


def test_membership_pair_has_database_enforced_uniqueness(tenant_control):
    control, manager, _storage = tenant_control
    user, tenant, membership = _register(control, manager, "unique@example.com", "Unique")
    duplicate = TenantMembership(
        id="mem_duplicate", user_id=user.id, tenant_id=tenant.id, role=ROLE_OWNER
    )
    with Session(control) as session:
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        assert session.get(TenantMembership, membership.id) is not None
