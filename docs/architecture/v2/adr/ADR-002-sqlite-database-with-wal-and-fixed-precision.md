# ADR-002: SQLite in WAL Mode with Integer Cent Precision

* **Status:** Accepted
* **Date:** October 2026
* **Deciders:** Principal Database Architect, Financial Systems Architect

## Context
PropTally requires a reliable, zero-ops, local relational database. The initial V1 design proposed SQLite with floating-point `REAL` columns. We must evaluate:
1. Whether SQLite is sufficient compared to PostgreSQL.
2. How to eliminate IEEE 754 floating-point rounding errors in monetary fields.
3. How to handle concurrent writes between the background ingestion watcher and the web dashboard.

## Decision
1. We select **SQLite** running in **Write-Ahead Logging (WAL)** mode.
2. All monetary columns are stored strictly as **exact integer minor currency units (cents)** in `INTEGER` columns (e.g. $1,801.48 $\rightarrow$ `180148`). Python domain models interface with these fields using `decimal.Decimal`.
3. Concurrency is configured with `PRAGMA synchronous = NORMAL`, `PRAGMA foreign_keys = ON`, and `PRAGMA busy_timeout = 10000` (10 seconds).

## Consequences
### Positive
* **Exact Mathematical Precision:** Rounding drift is completely eliminated. Debits and credits balance down to the exact penny ($0.01).
* **Zero Infrastructure Overhead:** Single self-contained database file (`proptally.db`), requiring no running daemon, no port management, and no database user administration.
* **Concurrent Reads & Background Ingestion:** WAL mode allows unlimited concurrent readers in the web UI while the background ingestion daemon commits statement batches.
* **Future PostgreSQL Portability:** The schema is written using standard ANSI SQL via SQLAlchemy 2.0. Migrating to PostgreSQL in the future requires zero schema redesign.

### Negative / Trade-Offs
* Multi-process writers must be coordinated through application connection scoping and SQLite's 10-second busy timeout.
