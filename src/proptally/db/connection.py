"""SQLite database connection and PRAGMA configuration."""

import os
import sqlite3
from pathlib import Path
from typing import Any
from sqlalchemy import create_engine, event
from sqlmodel import Session

DEFAULT_DB_PATH = os.environ.get(
    "PROPTALLY_DB_PATH", 
    str(Path(__file__).resolve().parent.parent.parent.parent / "storage" / "proptally.db")
)


def configure_sqlite_connection(dbapi_connection: sqlite3.Connection, connection_record: Any = None) -> None:
    """Enforce WAL mode, foreign key constraints, and 10-second busy timeout."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode = WAL;")
    cursor.execute("PRAGMA synchronous = NORMAL;")
    cursor.execute("PRAGMA foreign_keys = ON;")
    cursor.execute("PRAGMA busy_timeout = 10000;")
    cursor.close()


def get_engine(db_url: str = None):
    """Factory creating an engine with enforced SQLite connection pragmas."""
    if db_url is None:
        db_path = Path(DEFAULT_DB_PATH)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_url = f"sqlite:///{db_path}"

    connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
    engine = create_engine(db_url, connect_args=connect_args)

    if db_url.startswith("sqlite"):
        event.listen(engine, "connect", configure_sqlite_connection)

    return engine
