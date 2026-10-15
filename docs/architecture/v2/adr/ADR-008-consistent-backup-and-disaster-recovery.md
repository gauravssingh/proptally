# ADR-008: Consistent Hot Backup & Disaster Recovery Architecture

* **Status:** Accepted
* **Date:** October 2026
* **Deciders:** Infrastructure Architect, Database Architect

## Context
PropTally stores the client's financial history, ledger balances, and source PDFs locally. The initial V1 exploration proposed a simple `backup.sh` shell script that copied the active SQLite database file.

Directly copying a live SQLite database while in Write-Ahead Logging (WAL) mode is unsafe: if a write transaction is in progress, the resulting copy can be incomplete or corrupted. A production-grade backup strategy must guarantee point-in-time consistency and provide a verified restore procedure.

## Decision
1. **SQLite Online Backup API:**
   Backups must use SQLite's native backup mechanism (via `sqlite3.Connection.backup()` in Python or `sqlite3 proptally.db "VACUUM INTO 'backup.db'"`), which holds a shared read lock and guarantees a 100% consistent snapshot without locking out web dashboard readers.
2. **Encrypted Snapshot Archives:**
   The consistent SQLite snapshot, configuration files, and `vault/` directory are archived into a compressed tarball (`.tar.gz`) and encrypted using **Age** or **GPG** symmetric encryption.
3. **Automated Verification:**
   Before marking a backup successful, the backup script runs:
   `sqlite3 snapshot.db "PRAGMA integrity_check;"`
   If the integrity check fails, an alert is triggered, and the bad backup is discarded.
4. **Verified Restore Procedure:**
   A dedicated `scripts/restore.sh` script automates decryption, integrity validation, and atomic database replacement.

## Consequences
### Positive
* **Zero Corruption Risk:** Eliminates WAL copy hazards and ensures reliable disaster recovery.
* **Non-Blocking Operation:** Hot backups run in the background without downtime or user disruption.
* **Encrypted Cold Storage:** Encrypted archive can be safely synced to an external local NAS or USB drive without exposing raw financial records.

### Negative / Trade-Offs
* Requires disk space during backup creation to hold the temporary snapshot alongside the active database.
