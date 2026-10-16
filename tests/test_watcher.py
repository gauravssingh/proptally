"""Tests for automated hot-folder watcher and inbox intake."""

import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from sqlmodel import Session, select

from proptally.db.connection import get_engine
from proptally.db.models import (
    Document,
    FinancialAccount,
    FinancialInstitution,
    Statement,
)
from proptally.db.schema import init_schema
from proptally.db.system_models import TenantOperation
from proptally.db.tenant_manager import (
    TenantManager,
    get_system_engine,
    init_system_schema,
)
from proptally.ingestion.watcher import process_inbox_once

# The statement PDFs these tests exercise live outside the repository. Point
# PROPTALLY_TEST_DOCS_DIR at the folder holding them when running locally; with
# the variable unset the tests skip rather than hardcode a machine path.
DOCS_BASE = Path(
    os.environ.get(
        "PROPTALLY_TEST_DOCS_DIR",
        str(Path.home() / "Documents" / "client-statements"),
    )
)


@pytest.fixture
def test_env(tmp_path):
    inbox_dir = tmp_path / "inbox"
    vault_dir = tmp_path / "vault"
    db_file = tmp_path / "watcher_test.db"

    inbox_dir.mkdir(parents=True, exist_ok=True)
    vault_dir.mkdir(parents=True, exist_ok=True)

    engine = get_engine(f"sqlite:///{db_file}")
    init_schema(engine)

    # The statements these tests drop in the inbox belong to a checking account
    # the portfolio holds: a statement is only booked against a registered
    # account, so the account is registered here first.
    with Session(engine) as session:
        session.add(FinancialInstitution(id="inst_bofa", name="Bank of America"))
        session.flush()
        session.add(FinancialAccount(
            id="acct_bofa_chk_9456",
            institution_id="inst_bofa",
            account_name="Bank of America Advantage Plus Checking",
            account_type="CHECKING",
            account_number_masked="...9456",
        ))
        session.commit()

    return {
        "inbox_dir": inbox_dir,
        "vault_dir": vault_dir,
        "db_file": db_file,
        "engine": engine,
    }


def test_watcher_process_valid_pdf_auto_import(test_env):
    inbox_dir = test_env["inbox_dir"]
    vault_dir = test_env["vault_dir"]
    engine = test_env["engine"]

    sample_source = DOCS_BASE / "BOFA Checking" / "eStmt_2026-01-15.pdf"
    if not sample_source.exists():
        pytest.skip("Test sample PDF not found")

    target_pdf = inbox_dir / "eStmt_2026-01-15.pdf"
    shutil.copy2(sample_source, target_pdf)

    with Session(engine) as session:
        results = process_inbox_once(
            inbox_dir=inbox_dir,
            session=session,
            vault_dir=vault_dir,
            archive_subfolders=True,
            auto_import=True,
        )

    assert len(results) == 1
    res = results[0]
    assert res.status == "PROCESSED"
    assert res.document_id is not None
    assert res.statement_id is not None

    # Verify file was moved from inbox to inbox/processed
    assert not target_pdf.exists()
    assert (inbox_dir / "processed" / "eStmt_2026-01-15.pdf").exists()

    # Verify database persistence
    with Session(engine) as session:
        doc = session.get(Document, res.document_id)
        assert doc is not None
        stmt = session.get(Statement, res.statement_id)
        assert stmt is not None
        assert stmt.statement_type == "CHECKING"


def test_watcher_duplicate_handling(test_env):
    inbox_dir = test_env["inbox_dir"]
    vault_dir = test_env["vault_dir"]
    engine = test_env["engine"]

    sample_source = DOCS_BASE / "BOFA Checking" / "eStmt_2026-01-15.pdf"
    if not sample_source.exists():
        pytest.skip("Test sample PDF not found")

    target_pdf = inbox_dir / "sample_copy1.pdf"
    shutil.copy2(sample_source, target_pdf)

    with Session(engine) as session:
        res1 = process_inbox_once(inbox_dir, session, vault_dir)
        assert res1[0].status == "PROCESSED"

    # Now drop the exact same statement again under a different filename
    target_pdf2 = inbox_dir / "sample_copy2.pdf"
    shutil.copy2(sample_source, target_pdf2)

    with Session(engine) as session:
        res2 = process_inbox_once(inbox_dir, session, vault_dir)
        assert len(res2) == 1
        assert res2[0].status == "DUPLICATE"
        assert res2[0].document_id == res1[0].document_id

    # Duplicate file should be archived into processed
    assert not target_pdf2.exists()
    assert (inbox_dir / "processed" / "sample_copy2.pdf").exists()


def test_watcher_unsupported_and_quarantine(test_env):
    inbox_dir = test_env["inbox_dir"]
    vault_dir = test_env["vault_dir"]
    engine = test_env["engine"]

    # 1. Non-PDF file
    text_file = inbox_dir / "notes.txt"
    text_file.write_text("Hello world")

    # 2. Unsupported PDF (empty or unrecognized)
    dummy_pdf = inbox_dir / "unsupported.pdf"
    import fitz  # PyMuPDF
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 50), "This is an unsupported generic invoice.")
    doc.save(dummy_pdf)
    doc.close()

    with Session(engine) as session:
        results = process_inbox_once(inbox_dir, session, vault_dir)

    assert len(results) == 2
    for r in results:
        assert r.status == "UNSUPPORTED"

    # Both non-PDF and unrecognized PDF should be moved to quarantine
    assert not text_file.exists()
    assert not dummy_pdf.exists()
    assert (inbox_dir / "quarantine" / "notes.txt").exists()
    assert (inbox_dir / "quarantine" / "unsupported.pdf").exists()


def test_watcher_auto_import_false_moves_to_review(test_env):
    inbox_dir = test_env["inbox_dir"]
    vault_dir = test_env["vault_dir"]
    engine = test_env["engine"]

    sample_source = DOCS_BASE / "BOFA Checking" / "eStmt_2026-02-12.pdf"
    if not sample_source.exists():
        pytest.skip("Test sample PDF not found")

    target_pdf = inbox_dir / "eStmt_2026-02-12.pdf"
    shutil.copy2(sample_source, target_pdf)

    with Session(engine) as session:
        results = process_inbox_once(
            inbox_dir=inbox_dir,
            session=session,
            vault_dir=vault_dir,
            archive_subfolders=True,
            auto_import=False,  # Needs manual review
        )

    assert len(results) == 1
    assert results[0].status == "NEEDS_REVIEW"
    assert results[0].statement_id is None

    # Should be moved to inbox/review
    assert not target_pdf.exists()
    assert (inbox_dir / "review" / "eStmt_2026-02-12.pdf").exists()


def _tenant_watcher_environment(tmp_path, monkeypatch):
    from proptally.web.auth import register_portfolio

    storage = tmp_path / "tenant-storage"
    monkeypatch.setenv("PROPTALLY_STORAGE_ROOT", str(storage))
    monkeypatch.setenv("PROPTALLY_SYSTEM_DB_PATH", str(storage / "system.db"))
    control = get_system_engine()
    init_system_schema(control)
    manager = TenantManager()
    _owner, tenant, _ = register_portfolio(
        control,
        manager,
        email="watcher-owner@example.com",
        password="watcher-owner-password",
        display_name="Watcher",
        portfolio_name="Watcher Portfolio",
    )
    paths = manager.paths(tenant.id)
    with Session(manager.engine_for(tenant.id)) as session:
        session.add(FinancialInstitution(id="inst_bofa", name="Bank of America"))
        session.flush()
        session.add(FinancialAccount(
            id="acct_bofa_chk_9456",
            institution_id="inst_bofa",
            account_name="Bank of America Advantage Plus Checking",
            account_type="CHECKING",
            account_number_masked="...9456",
        ))
        session.commit()
    return control, manager, tenant, paths


def test_cli_watch_once_flag_uses_active_tenant_context(tmp_path, monkeypatch, capsys):
    sample_source = DOCS_BASE / "BOFA Checking" / "eStmt_2026-03-17-1.pdf"
    if not sample_source.exists():
        pytest.skip("Test sample PDF not found")

    control, manager, tenant, paths = _tenant_watcher_environment(tmp_path, monkeypatch)
    paths.inbox.mkdir(parents=True, exist_ok=True)
    shutil.copy2(sample_source, paths.inbox / "stmt3.pdf")
    from proptally.ingestion.cli import main

    test_args = ["proptally-ingest", "--watch", "--once", "--tenant", tenant.id]
    monkeypatch.setattr(sys, "argv", test_args)
    main()
    data = json.loads(capsys.readouterr().out)
    assert data["processed_count"] == 1
    assert data["results"][0]["status"] == "PROCESSED"
    assert (paths.inbox / "processed" / "stmt3.pdf").exists()
    assert any(paths.vault.iterdir())
    with Session(control) as session:
        assert not session.exec(select(TenantOperation)).all()

    override_vault = tmp_path / "other-tenant-vault"
    monkeypatch.setattr(sys, "argv", test_args + ["--vault-dir", str(override_vault)])
    with pytest.raises(SystemExit):
        main()
    assert not override_vault.exists()
    for engine in manager._engines.values():
        engine.dispose()
    control.dispose()


def test_watcher_rejects_global_or_database_fallback(tmp_path, monkeypatch):
    from proptally.ingestion.cli import main

    for args in (
        ["proptally-ingest", "--watch", "--once"],
        ["proptally-ingest", "--watch", "--once", "--db", str(tmp_path / "legacy.db")],
    ):
        monkeypatch.setattr(sys, "argv", args)
        with pytest.raises(SystemExit):
            main()
    assert not (tmp_path / "legacy.db").exists()
