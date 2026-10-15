#!/usr/bin/env bash
# scripts/backup.sh - Hot non-blocking backup for PropTally SQLite database and tamper-evident vault.
set -euo pipefail

# Parse arguments or use environment / default fallbacks
DB_PATH=""
VAULT_DIR=""
BACKUP_DIR=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --db)
            DB_PATH="$2"
            shift 2
            ;;
        --vault)
            VAULT_DIR="$2"
            shift 2
            ;;
        --out|--backup-dir)
            BACKUP_DIR="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [--db DB_PATH] [--vault VAULT_DIR] [--out BACKUP_DIR]"
            exit 0
            ;;
        *)
            if [ -z "$DB_PATH" ]; then
                DB_PATH="$1"
            elif [ -z "$BACKUP_DIR" ]; then
                BACKUP_DIR="$1"
            fi
            shift
            ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Resolve DB_PATH
if [ -z "$DB_PATH" ]; then
    if [ -n "${PROPTALLY_DB_PATH:-}" ]; then
        DB_PATH="$PROPTALLY_DB_PATH"
    elif [ -f "$REPO_ROOT/storage/client-portfolio.db" ]; then
        DB_PATH="$REPO_ROOT/storage/client-portfolio.db"
    elif [ -f "$REPO_ROOT/storage/proptally.db" ]; then
        DB_PATH="$REPO_ROOT/storage/proptally.db"
    else
        DB_PATH="$REPO_ROOT/storage/proptally.db"
    fi
fi

# Resolve VAULT_DIR
if [ -z "$VAULT_DIR" ]; then
    if [ -n "${PROPTALLY_VAULT_DIR:-}" ]; then
        VAULT_DIR="$PROPTALLY_VAULT_DIR"
    elif [ -d "$REPO_ROOT/storage/vault" ]; then
        VAULT_DIR="$REPO_ROOT/storage/vault"
    elif [ -d "$REPO_ROOT/storage/documents" ]; then
        VAULT_DIR="$REPO_ROOT/storage/documents"
    else
        VAULT_DIR="$REPO_ROOT/storage/vault"
    fi
fi

# Resolve BACKUP_DIR
if [ -z "$BACKUP_DIR" ]; then
    BACKUP_DIR="${PROPTALLY_BACKUP_DIR:-$REPO_ROOT/backups}"
fi

mkdir -p "$BACKUP_DIR"

if [ ! -f "$DB_PATH" ]; then
    echo "ERROR: SQLite database file not found at '$DB_PATH'" >&2
    exit 1
fi

TIMESTAMP="$(date +"%Y%m%d_%H%M%S")"
BACKUP_NAME="proptally_backup_${TIMESTAMP}"
ARCHIVE_FILE="$BACKUP_DIR/${BACKUP_NAME}.tar.gz"
CHECKSUM_FILE="${ARCHIVE_FILE}.sha256"

TEMP_STAGE="$(mktemp -d 2>/dev/null || mktemp -d -t 'proptally_backup')"
trap 'rm -rf "$TEMP_STAGE"' EXIT

echo "=== PropTally Automated Hot Backup ==="
echo "Source DB:   $DB_PATH"
echo "Vault Dir:   $VAULT_DIR"
echo "Destination: $ARCHIVE_FILE"

# 1. Hot SQLite non-blocking atomic backup using VACUUM INTO
TEMP_DB="$TEMP_STAGE/database.sqlite"
echo "Executing SQLite VACUUM INTO..."
sqlite3 "$DB_PATH" "VACUUM INTO '$TEMP_DB';"

# 2. Run integrity check on the isolated copy
set +e
INTEGRITY="$(sqlite3 "$TEMP_DB" "PRAGMA integrity_check;" 2>&1)"
SQLITE_STATUS=$?
set -e

if [ $SQLITE_STATUS -ne 0 ] || [ "$INTEGRITY" != "ok" ]; then
    echo "ERROR: Integrity check failed on backup copy: $INTEGRITY" >&2
    exit 2
fi
echo "✓ Database integrity verified: $INTEGRITY"

# 3. Copy vault documents if existing
STAGE_VAULT="$TEMP_STAGE/vault"
mkdir -p "$STAGE_VAULT"
VAULT_COUNT=0
if [ -d "$VAULT_DIR" ]; then
    cp -R "$VAULT_DIR/." "$STAGE_VAULT/" 2>/dev/null || true
    VAULT_COUNT=$(find "$STAGE_VAULT" -type f | wc -l | tr -d ' ')
fi
echo "✓ Document vault staged ($VAULT_COUNT files)"

# 4. Generate manifest
cat <<EOF > "$TEMP_STAGE/manifest.json"
{
  "timestamp": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "source_db": "$DB_PATH",
  "db_integrity": "$INTEGRITY",
  "vault_file_count": $VAULT_COUNT,
  "backup_name": "$BACKUP_NAME"
}
EOF

# 5. Create compressed tarball
tar -czf "$ARCHIVE_FILE" -C "$TEMP_STAGE" .
echo "✓ Compressed archive created: $(du -h "$ARCHIVE_FILE" | awk '{print $1}')"

# 6. Compute SHA-256 digest
if command -v sha256sum >/dev/null 2>&1; then
    (cd "$BACKUP_DIR" && sha256sum "$(basename "$ARCHIVE_FILE")") > "$CHECKSUM_FILE"
elif command -v shasum >/dev/null 2>&1; then
    (cd "$BACKUP_DIR" && shasum -a 256 "$(basename "$ARCHIVE_FILE")") > "$CHECKSUM_FILE"
else
    DIGEST="$(openssl dgst -sha256 "$ARCHIVE_FILE" | awk '{print $NF}')"
    echo "$DIGEST  $(basename "$ARCHIVE_FILE")" > "$CHECKSUM_FILE"
fi

DIGEST_VAL="$(awk '{print $1}' "$CHECKSUM_FILE")"
echo "✓ SHA-256 checksum: $DIGEST_VAL"
echo "=== Backup completed successfully ==="
echo "Archive:  $ARCHIVE_FILE"
echo "Checksum: $CHECKSUM_FILE"
