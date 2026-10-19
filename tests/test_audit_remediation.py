"""Regression tests for the defects found by the independent audit.

Each test exercises the database or the HTTP surface that previously let the
defect through; none of them inspects source text.
"""

from datetime import date

import pytest
from sqlalchemy import text
from sqlmodel import Session
from starlette.testclient import TestClient

from proptally.db.connection import get_engine
from proptally.db.models import Document, JournalEntry
from proptally.db.schema import (
    TRIGGER_NAMES,
    _rebuild_table_for_constraints,
    init_schema,
)
from proptally.domain.accounting.ledger import (
    add_journal_line,
    create_journal_entry,
    post_journal_entry,
)
from proptally.web.auth import LoginRateLimiter


def _line_entry(session: Session, *, description: str = "entry") -> str:
    entry = create_journal_entry(
        session, date(2026, 1, 1), "EXPENSE_POST", description
    )
    add_journal_line(session, entry.id, 5040, debit_cents=100)
    add_journal_line(session, entry.id, 3010, credit_cents=100)
    session.commit()
    return entry.id


def test_posted_lines_cannot_be_moved_into_a_posted_entry(session):
    posted_id = _line_entry(session, description="posted")
    post_journal_entry(session, posted_id)
    draft_id = _line_entry(session, description="draft")
    draft_line = session.execute(
        text("SELECT id FROM journal_entry_lines WHERE journal_entry_id=:id LIMIT 1"),
        {"id": draft_id},
    ).scalar_one()
    session.commit()

    with pytest.raises(Exception) as exc:
        session.execute(
            text("UPDATE journal_entry_lines SET journal_entry_id=:posted WHERE id=:line"),
            {"posted": posted_id, "line": draft_line},
        )
        session.commit()
    assert "ImmutableLedger" in str(exc.value)


def test_draft_cannot_become_posted_without_balanced_lines(session):
    empty = create_journal_entry(session, date(2026, 1, 1), "EXPENSE_POST", "empty")
    session.commit()

    with pytest.raises(Exception) as exc:
        session.execute(
            text("UPDATE journal_entries SET status='POSTED' WHERE id=:id"),
            {"id": empty.id},
        )
        session.commit()
    assert "UnbalancedJournalEntry" in str(exc.value)

    with pytest.raises(Exception) as exc:
        session.execute(
            text(
                "INSERT INTO journal_entries "
                "(id, entry_date, entry_type, status, description, created_at) "
                "VALUES ('born_posted', '2026-01-01', 'EXPENSE_POST', 'POSTED', 'x', '2026-01-01')"
            )
        )
        session.commit()
    assert "ImmutableLedger" in str(exc.value)


def test_constraint_rebuild_preserves_custom_index(tmp_path):
    db = tmp_path / "rebuild.db"
    engine = get_engine(f"sqlite:///{db}")
    init_schema(engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE INDEX ix_custom_entry_date ON journal_entries (entry_date)"))
        # The migration drops its own triggers before a rebuild because they
        # reference the table being renamed.
        for name in TRIGGER_NAMES:
            conn.execute(text(f'DROP TRIGGER IF EXISTS "{name}"'))

    _rebuild_table_for_constraints(engine, JournalEntry.__table__)

    with engine.connect() as conn:
        indexes = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='journal_entries'")
            )
        }
        conn.execute(text("ALTER TABLE journal_entries ADD COLUMN legacy_extra TEXT"))
    assert "ix_custom_entry_date" in indexes

    with pytest.raises(RuntimeError, match="legacy_extra"):
        _rebuild_table_for_constraints(engine, JournalEntry.__table__)


def test_failed_persist_does_not_delete_a_committed_documents_vault_file(
    session, tmp_path, monkeypatch
):
    from proptally.ingestion import service

    digest = "a" * 64
    vault = tmp_path / "vault"
    stored = vault / digest[:2] / f"{digest}.pdf"
    stored.parent.mkdir(parents=True)
    stored.write_bytes(b"%PDF-1.4 shared bytes")
    session.add(Document(
        id="doc_existing", file_sha256=digest, original_filename="existing.pdf",
        storage_path=str(stored), file_size_bytes=stored.stat().st_size,
    ))
    session.commit()

    # Simulate this transaction having "created" the content-addressed file while a
    # concurrent ingest has already committed a Document pointing at it.
    monkeypatch.setattr(
        service, "_copy_to_vault",
        lambda source, digest, vault_dir, suffix=".pdf": (stored, True),
    )
    source = tmp_path / "incoming.pdf"
    source.write_bytes(b"%PDF-1.4 shared bytes")

    with Session(session.get_bind()) as loser:
        with pytest.raises(Exception):
            service._persist_document(source, digest, vault, loser, None)

    assert stored.exists(), "a live document's vault file was deleted"


def test_rate_limiter_normalises_the_email_key():
    limiter = LoginRateLimiter(limit=3, window_seconds=60)
    for _ in range(3):
        limiter.record_failure("203.0.113.9", "  Victim@Example.com ")
    assert limiter.is_blocked("203.0.113.9", "victim@example.com")
    limiter.clear("203.0.113.9", "VICTIM@EXAMPLE.COM")
    assert not limiter.is_blocked("203.0.113.9", "victim@example.com")


def test_tampered_login_csrf_is_rejected_with_403(owner):
    signed_in = owner()
    with TestClient(signed_in.app) as client:
        page = client.get("/auth/login")
        assert page.status_code == 200, page.text[:200]
        token = client.cookies.get("pt_csrf")
        assert token
        response = client.post(
            "/auth/login",
            data={"email": signed_in.email, "password": signed_in.password,
                  "csrf_token": "not-the-cookie"},
            follow_redirects=False,
        )
    assert response.status_code == 403
    assert "timed out" in response.text.lower()


def test_production_app_factory_has_no_shared_database_override():
    import inspect

    from proptally.web.app import create_app

    assert "db_url" not in inspect.signature(create_app).parameters


def test_extraction_provider_accepts_only_resolved_private_hosts(monkeypatch):
    from proptally.onboarding.extraction import RuleBasedExtractor
    from proptally.onboarding.providers import HttpJsonExtractor, structured_extractor

    monkeypatch.setenv("PROPTALLY_EXTRACTION_URL", "http://127.0.0.1:9999")
    assert isinstance(structured_extractor(), HttpJsonExtractor)

    # 172.200/15 was admitted by the old "172.2" string prefix.
    monkeypatch.setenv("PROPTALLY_EXTRACTION_URL", "http://172.200.1.10:9999")
    assert isinstance(structured_extractor(), RuleBasedExtractor)

    monkeypatch.setenv("PROPTALLY_EXTRACTION_URL", "http://8.8.8.8:9999")
    assert isinstance(structured_extractor(), RuleBasedExtractor)

    monkeypatch.setenv("PROPTALLY_ALLOW_REMOTE_EXTRACTION", "true")
    assert isinstance(structured_extractor(), HttpJsonExtractor)


def test_extraction_provider_refuses_redirects(monkeypatch):
    import httpx

    from proptally.onboarding.providers import HttpJsonExtractor, ProviderError

    class RedirectResponse:
        is_redirect = True

        def raise_for_status(self):
            raise AssertionError("redirect branch must run before status handling")

    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: RedirectResponse())
    extractor = HttpJsonExtractor(url="http://127.0.0.1:9999", model="")
    with pytest.raises(ProviderError, match="redirect"):
        extractor.extract(category="DEED", filename="deed.pdf", pages=["text"])
