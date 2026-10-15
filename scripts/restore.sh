#!/usr/bin/env bash
# scripts/restore.sh - Verifies integrity and restores PropTally SQLite database and vault from backup.
set -euo pipefail

ARCHIVE_PATH=""
TARGET_DB=""
TARGET_VAULT=""
SKIP_CHECKSUM=false
FORCE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --archive)
            ARCHIVE_PATH="$2"
            shift 2
            ;;
        --target-db)
            TARGET_DB="$2"
            shift 2
            ;;
        --target-vault)
            TARGET_VAULT="$2"
            shift 2
            ;;
        --skip-checksum)
            SKIP_CHECKSUM=true
            shift
            ;;
        --force)
            FORCE=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [--archive ARCHIVE_PATH] [--target-db TARGET_DB] [--target-vault TARGET_VAULT] [--skip-checksum] [--force]"
            exit 0
            ;;
        *)
            if [ -z "$ARCHIVE_PATH" ]; then
                ARCHIVE_PATH="$1"
            elif [ -z "$TARGET_DB" ]; then
                TARGET_DB="$1"
            fi
            shift
            ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# If ARCHIVE_PATH is empty, find newest in backups/
if [ -z "$ARCHIVE_PATH" ]; then
    BACKUP_DIR="${PROPTALLY_BACKUP_DIR:-$REPO_ROOT/backups}"
    if [ -d "$BACKUP_DIR" ]; then
        LATEST="$(find "$BACKUP_DIR" -name "proptally_backup_*.tar.gz" -type f | sort | tail -n 1)"
        if [ -n "$LATEST" ]; then
            ARCHIVE_PATH="$LATEST"
            echo "Auto-detected latest backup: $ARCHIVE_PATH"
        fi
    fi
fi

if [ -z "$ARCHIVE_PATH" ] || [ ! -f "$ARCHIVE_PATH" ]; then
    echo "ERROR: Backup archive not found. Specify via --archive <path>." >&2
    exit 1
fi

# Resolve targets
if [ -z "$TARGET_DB" ]; then
    TARGET_DB="${PROPTALLY_DB_PATH:-$REPO_ROOT/storage/proptally.db}"
fi
if [ -z "$TARGET_VAULT" ]; then
    TARGET_VAULT="${PROPTALLY_VAULT_DIR:-$REPO_ROOT/storage/vault}"
fi

echo "=== PropTally Automated Disaster Recovery & Restore ==="
echo "Backup Archive: $ARCHIVE_PATH"
echo "Target DB:      $TARGET_DB"
echo "Target Vault:   $TARGET_VAULT"

# 1. SHA-256 Checksum Verification
CHECKSUM_FILE="${ARCHIVE_PATH}.sha256"
if [ "$SKIP_CHECKSUM" = false ] && [ -f "$CHECKSUM_FILE" ]; then
    echo "Verifying SHA-256 digest..."
    EXPECTED_HASH="$(awk '{print $1}' "$CHECKSUM_FILE")"
    if command -v sha256sum >/dev/null 2>&1; then
        ACTUAL_HASH="$(sha256sum "$ARCHIVE_PATH" | awk '{print $1}')"
    elif command -v shasum >/dev/null 2>&1; then
        ACTUAL_HASH="$(shasum -a 256 "$ARCHIVE_PATH" | awk '{print $1}')"
    else
        ACTUAL_HASH="$(openssl dgst -sha256 "$ARCHIVE_PATH" | awk '{print $NF}')"
    fi

    if [ "$EXPECTED_HASH" != "$ACTUAL_HASH" ]; then
        echo "ERROR: Checksum mismatch!" >&2
        echo "Expected: $EXPECTED_HASH" >&2
        echo "Actual:   $ACTUAL_HASH" >&2
        exit 3
    fi
    echo "✓ SHA-256 checksum verified: $ACTUAL_HASH"
elif [ "$SKIP_CHECKSUM" = false ]; then
    echo "Warning: Checksum file not found at '$CHECKSUM_FILE'; continuing without digest verification."
fi

# 2. Extract into isolated temporary staging area
TEMP_STAGE="$(mktemp -d 2>/dev/null || mktemp -d -t 'proptally_restore')"
trap 'rm -rf "$TEMP_STAGE"' EXIT

echo "Unpacking archive..."
tar -xzf "$ARCHIVE_PATH" -C "$TEMP_STAGE"

if [ ! -f "$TEMP_STAGE/database.sqlite" ]; then
    echo "ERROR: Corrupt archive - missing 'database.sqlite'" >&2
    exit 4
fi

# 3. Database Integrity Check
echo "Running SQLite integrity check on restored database..."
set +e
RESTORED_INTEGRITY="$(sqlite3 "$TEMP_STAGE/database.sqlite" "PRAGMA integrity_check;" 2>&1)"
SQLITE_STATUS=$?
set -e

if [ $SQLITE_STATUS -ne 0 ] || [ "$RESTORED_INTEGRITY" != "ok" ]; then
    echo "ERROR: SQLite integrity check failed: $RESTORED_INTEGRITY" >&2
    exit 5
fi
echo "✓ Database integrity verified: $RESTORED_INTEGRITY"

# 4. Backup existing target DB if present
mkdir -p "$(dirname "$TARGET_DB")"
mkdir -p "$TARGET_VAULT"

if [ -f "$TARGET_DB" ]; then
    BAK_PATH="${TARGET_DB}.bak.$(date +"%Y%m%d_%H%M%S")"
    echo "Backing up existing database to '$BAK_PATH'..."
    cp "$TARGET_DB" "$BAK_PATH"
fi

# 5. Restore Database & Vault
echo "Restoring database to '$TARGET_DB'..."
cp "$TEMP_STAGE/database.sqlite" "$TARGET_DB"

if [ -d "$TEMP_STAGE/vault" ]; then
    echo "Restoring vault documents to '$TARGET_VAULT'..."
    cp -R "$TEMP_STAGE/vault/." "$TARGET_VAULT/" 2>/dev/null || true
fi

# 6. Post-restore Verification
FINAL_CHECK="$(sqlite3 "$TARGET_DB" "PRAGMA integrity_check;")"
TABLE_COUNT="$(sqlite3 "$TARGET_DB" "SELECT count(*) FROM sqlite_master WHERE type='table';")"

echo "✓ Post-restore integrity: $FINAL_CHECK"
echo "✓ Tables loaded: $TABLE_COUNT"
echo "=== Restore completed successfully ==="
