#!/usr/bin/env bash
# scripts/restore.sh - Verifies and restores a PropTally backup archive.
#
# Understands both archive layouts produced by scripts/backup.sh:
#
#   multi-tenant  system/database.sqlite plus tenants/<id>/proptally.db and
#                 tenants/<id>/vault for every portfolio. Restored under
#                 --target-storage (default storage/). --tenant <id> restores one.
#   legacy        database.sqlite at the archive root, restored to --target-db
#                 with its vault at --target-vault.
#
# Every staged database is checked with `PRAGMA integrity_check` before anything
# is written, and an existing target is copied aside to a timestamped .bak file
# first (unless --force).
set -euo pipefail

ARCHIVE_PATH=""
TARGET_DB=""
TARGET_VAULT=""
TARGET_STORAGE=""
TENANT_ID=""
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
        --target-storage)
            TARGET_STORAGE="$2"
            shift 2
            ;;
        --tenant)
            TENANT_ID="$2"
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
            echo "Usage: $0 [--archive ARCHIVE_PATH] [--target-storage ROOT] [--tenant ID]"
            echo "       $0 --archive ARCHIVE_PATH [--target-db DB_PATH] [--target-vault VAULT_DIR]"
            echo "           [--skip-checksum] [--force]"
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

if [ -z "$TARGET_STORAGE" ]; then
    TARGET_STORAGE="${PROPTALLY_STORAGE_ROOT:-$REPO_ROOT/storage}"
fi

echo "=== PropTally Automated Disaster Recovery & Restore ==="
echo "Backup Archive: $ARCHIVE_PATH"

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

# verify_integrity LABEL DB_PATH
# Fails the whole restore (writing nothing) if any database is corrupt.
verify_integrity() {
    local label="$1"
    local database="$2"

    set +e
    local integrity
    integrity="$(sqlite3 "$database" "PRAGMA integrity_check;" 2>&1)"
    local status=$?
    set -e

    if [ $status -ne 0 ] || [ "$integrity" != "ok" ]; then
        echo "ERROR: SQLite integrity check failed for '$label': $integrity" >&2
        exit 5
    fi
    echo "✓ Database integrity verified ($label): $integrity"
}

# require_stopped PATH
# Refuses to overwrite a database a running process still has open.
require_stopped() {
    local target="$1"
    if command -v lsof >/dev/null 2>&1 && lsof -- "$target" >/dev/null 2>&1; then
        echo "ERROR: '$target' is open by a running process. Stop the service first." >&2
        exit 7
    fi
}

# backup_existing PATH
# A consistent copy, including committed WAL transactions (a plain cp of the
# main file silently omits them and is not a usable rollback point).
backup_existing() {
    local target="$1"
    if [ "$FORCE" = true ] || [ ! -f "$target" ]; then
        return 0
    fi
    local bak="${target}.bak.$(date +"%Y%m%d_%H%M%S")"
    echo "Backing up existing database to '$bak'..."
    sqlite3 "$target" "VACUUM INTO '$bak';"
    rm -f "${bak}-wal" "${bak}-shm"
}

# restore_database STAGED TARGET
# Swaps in a fully written copy and clears stale sidecars so SQLite cannot replay
# an old WAL over the restored file.
restore_database() {
    local staged="$1"
    local target="$2"
    require_stopped "$target"
    rm -f "${target}-wal" "${target}-shm"
    local staged_target="${target}.restore.$$"
    cp "$staged" "$staged_target"
    mv -f "$staged_target" "$target"
    chmod 600 "$target"
}

MULTI=false
if [ -d "$TEMP_STAGE/tenants" ] || [ -f "$TEMP_STAGE/system/database.sqlite" ]; then
    MULTI=true
fi

if [ "$MULTI" = true ]; then
    # --- Multi-tenant restore ---------------------------------------------
    echo "Layout:      multi-tenant"
    echo "Target root: $TARGET_STORAGE"

    if [ ! -d "$TEMP_STAGE/tenants" ] && [ -z "$TENANT_ID" ]; then
        echo "Warning: archive contains no tenants directory." >&2
    fi

    # Verify every staged database before touching the target.
    if [ -f "$TEMP_STAGE/system/database.sqlite" ]; then
        verify_integrity "system" "$TEMP_STAGE/system/database.sqlite"
    fi
    TENANT_LIST=""
    if [ -d "$TEMP_STAGE/tenants" ]; then
        if [ -n "$TENANT_ID" ]; then
            if [ ! -d "$TEMP_STAGE/tenants/$TENANT_ID" ]; then
                echo "ERROR: Archive contains no tenant '$TENANT_ID'." >&2
                exit 1
            fi
            TENANT_LIST="$TEMP_STAGE/tenants/$TENANT_ID"
        else
            shopt -s nullglob
            for candidate in "$TEMP_STAGE"/tenants/*/; do
                TENANT_LIST="${TENANT_LIST}${candidate}
"
            done
            shopt -u nullglob
        fi
    fi

    while IFS= read -r tenant_dir; do
        [ -n "$tenant_dir" ] || continue
        tenant_id="$(basename "${tenant_dir%/}")"
        if [ ! -f "$tenant_dir/proptally.db" ]; then
            echo "ERROR: Archive tenant '$tenant_id' has no proptally.db." >&2
            exit 4
        fi
        verify_integrity "tenant:$tenant_id" "$tenant_dir/proptally.db"
    done <<< "$TENANT_LIST"

    # Nothing failed verification; now write.
    if [ -f "$TEMP_STAGE/system/database.sqlite" ]; then
        mkdir -p "$TARGET_STORAGE"
        backup_existing "$TARGET_STORAGE/system.db"
        echo "Restoring control plane to '$TARGET_STORAGE/system.db'..."
        restore_database "$TEMP_STAGE/system/database.sqlite" "$TARGET_STORAGE/system.db"
    fi

    while IFS= read -r tenant_dir; do
        [ -n "$tenant_dir" ] || continue
        tenant_id="$(basename "${tenant_dir%/}")"
        target_tenant="$TARGET_STORAGE/tenants/$tenant_id"
        mkdir -p "$target_tenant"
        backup_existing "$target_tenant/proptally.db"
        echo "Restoring portfolio '$tenant_id' to '$target_tenant/proptally.db'..."
        restore_database "$tenant_dir/proptally.db" "$target_tenant/proptally.db"
        if [ -d "$tenant_dir/vault" ]; then
            mkdir -p "$target_tenant/vault"
            cp -R "$tenant_dir/vault/." "$target_tenant/vault/"
        fi
        if [ -d "$tenant_dir/photos" ]; then
            mkdir -p "$target_tenant/photos"
            cp -R "$tenant_dir/photos/." "$target_tenant/photos/"
        fi
    done <<< "$TENANT_LIST"

    if [ -f "$TARGET_STORAGE/system.db" ]; then
        echo "✓ Post-restore integrity (system): $(sqlite3 "$TARGET_STORAGE/system.db" "PRAGMA integrity_check;")"
    fi
    echo "=== Restore completed successfully ==="
    exit 0
fi

# --- Legacy single-database restore ----------------------------------------
if [ ! -f "$TEMP_STAGE/database.sqlite" ]; then
    echo "ERROR: Corrupt archive - missing 'database.sqlite'" >&2
    exit 4
fi

# Resolve legacy targets
if [ -z "$TARGET_DB" ]; then
    TARGET_DB="${PROPTALLY_DB_PATH:-$REPO_ROOT/storage/proptally.db}"
fi
if [ -z "$TARGET_VAULT" ]; then
    TARGET_VAULT="${PROPTALLY_VAULT_DIR:-$REPO_ROOT/storage/vault}"
fi

echo "Layout:      legacy single-database"
echo "Target DB:   $TARGET_DB"
echo "Target Vault: $TARGET_VAULT"

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
backup_existing "$TARGET_DB"

# 5. Restore Database & Vault
echo "Restoring database to '$TARGET_DB'..."
restore_database "$TEMP_STAGE/database.sqlite" "$TARGET_DB"

if [ -d "$TEMP_STAGE/vault" ]; then
    echo "Restoring vault documents to '$TARGET_VAULT'..."
    cp -R "$TEMP_STAGE/vault/." "$TARGET_VAULT/"
fi
if [ -d "$TEMP_STAGE/photos" ]; then
    LEGACY_PHOTOS="$(dirname "$TARGET_DB")/photos"
    echo "Restoring photos to '$LEGACY_PHOTOS'..."
    mkdir -p "$LEGACY_PHOTOS"
    cp -R "$TEMP_STAGE/photos/." "$LEGACY_PHOTOS/"
fi

# 6. Post-restore Verification
FINAL_CHECK="$(sqlite3 "$TARGET_DB" "PRAGMA integrity_check;")"
TABLE_COUNT="$(sqlite3 "$TARGET_DB" "SELECT count(*) FROM sqlite_master WHERE type='table';")"

echo "✓ Post-restore integrity: $FINAL_CHECK"
echo "✓ Tables loaded: $TABLE_COUNT"
echo "=== Restore completed successfully ==="
