"""Pytest configuration and database fixtures for PropTally test suite."""

import pytest
from sqlalchemy import create_engine, event
from sqlmodel import Session, SQLModel
from proptally.db.connection import configure_sqlite_connection
from proptally.db.schema import init_schema
from proptally.domain.accounting.coa import ensure_entity_ledger_accounts


@pytest.fixture(name="engine")
def engine_fixture():
    """Provides an isolated in-memory SQLite database with WAL and triggers configured."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    event.listen(engine, "connect", configure_sqlite_connection)
    init_schema(engine)
    return engine


@pytest.fixture(name="session")
def session_fixture(engine):
    """Provides a transactional database session seeded with standard Chart of Accounts."""
    with Session(engine) as session:
        ensure_entity_ledger_accounts(session)
        yield session
