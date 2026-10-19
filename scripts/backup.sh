#!/usr/bin/env bash
# scripts/backup.sh - Hot non-blocking backup for PropTally's control plane,
# every portfolio database and each portfolio's tamper-evident vault.
#
# Two layouts are supported:
#
#   multi-tenant (default)  storage/system.db plus storage/tenants/<id>/proptally.db
#                           and storage/tenants/<id>/vault for every portfolio.
#                           Pass --tenant <id> to export a single portfolio.
#   legacy single-portfolio  one --db file and its --vault directory, staged as
#                           database.sqlite at the archive root (older archives).
#
# Each database is copied with `sqlite3 ... "VACUUM INTO"` and verified with
# `PRAGMA integrity_check` before it is staged, then the whole staging tree is
# bundled into a compressed tar with a SHA-256 sidecar.
set -euo pipefail

# Parse arguments or use environment / default fallbacks
DB_PATH=""
VAULT_DIR=""
BACKUP_DIR=""
STORAGE_ROOT=""
TENANT_ID=""
DB_GIVEN=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --db)
            DB_PATH="$2"
            DB_GIVEN=true
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
        --storage)
            STORAGE_ROOT="$2"
            shift 2
            ;;
        --tenant)
            TENANT_ID="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [--storage STORAGE_ROOT] [--tenant TENANT_ID] [--out BACKUP_DIR]"
            echo "       $0 --db DB_PATH [--vault VAULT_DIR] [--out BACKUP_DIR]"
            echo
            echo "  (no --db)      Back up storage/system.db and every storage/tenants/*/proptally.db"
            echo "                 with each tenant's vault. --tenant <id> exports just that"
            echo "                 portfolio (control plane excluded)."
            echo "  --db DB_PATH   Legacy single-portfolio backup of one database and its vault."
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

# Resolve the storage root (multi-tenant layout).
if [ -z "$STORAGE_ROOT" ]; then
    STORAGE_ROOT="${PROPTALLY_STORAGE_ROOT:-$REPO_ROOT/storage}"
fi

# Decide the layout. An explicit --db always means the legacy single-database
# layout, so an operator backing up one file never has the repository's own
# storage/ directory silently pulled in.
MODE="legacy"
if [ "$DB_GIVEN" = false ]; then
    if [ -f "$STORAGE_ROOT/system.db" ] || [ -d "$STORAGE_ROOT/tenants" ]; then
        MODE="multi"
    fi
fi

if [ "$MODE" = "legacy" ]; then
    if [ -z "$DB_PATH" ]; then
        if [ -n "${PROPTALLY_DB_PATH:-}" ]; then
            DB_PATH="$PROPTALLY_DB_PATH"
        elif [ -f "$STORAGE_ROOT/client-portfolio.db" ]; then
            DB_PATH="$STORAGE_ROOT/client-portfolio.db"
        elif [ -f "$STORAGE_ROOT/proptally.db" ]; then
            DB_PATH="$STORAGE_ROOT/proptally.db"
        else
            DB_PATH="$STORAGE_ROOT/proptally.db"
        fi
    fi
    if [ -z "$VAULT_DIR" ]; then
        if [ -n "${PROPTALLY_VAULT_DIR:-}" ]; then
            VAULT_DIR="$PROPTALLY_VAULT_DIR"
        elif [ -d "$STORAGE_ROOT/vault" ]; then
            VAULT_DIR="$STORAGE_ROOT/vault"
        elif [ -d "$STORAGE_ROOT/documents" ]; then
            VAULT_DIR="$STORAGE_ROOT/documents"
        else
            VAULT_DIR="$STORAGE_ROOT/vault"
        fi
    fi
fi

# Resolve BACKUP_DIR
if [ -z "$BACKUP_DIR" ]; then
    BACKUP_DIR="${PROPTALLY_BACKUP_DIR:-$REPO_ROOT/backups}"
fi

mkdir -p "$BACKUP_DIR"

TIMESTAMP="$(date +"%Y%m%d_%H%M%S")"
SUFFIX=""
if [ -n "$TENANT_ID" ]; then
    SUFFIX="_${TENANT_ID}"
fi
BACKUP_NAME="proptally_backup_${TIMESTAMP}${SUFFIX}"
ARCHIVE_FILE="$BACKUP_DIR/${BACKUP_NAME}.tar.gz"
CHECKSUM_FILE="${ARCHIVE_FILE}.sha256"

TEMP_STAGE="$(mktemp -d 2>/dev/null || mktemp -d -t 'proptally_backup')"
trap 'rm -rf "$TEMP_STAGE"' EXIT

echo "=== PropTally Automated Hot Backup ==="
echo "Mode:        $MODE"
echo "Storage:     $STORAGE_ROOT"
echo "Destination: $ARCHIVE_FILE"

# stage_database SOURCE_DESTINATION STAGE_PATH
# Hot-copies one SQLite database with VACUUM INTO and verifies its integrity.
stage_database() {
    local source="$1"
    local destination="$2"

    if [ ! -f "$source" ]; then
        echo "ERROR: SQLite database file not found at '$source'" >&2
        exit 1
    fi

    mkdir -p "$(dirname "$destination")"
    echo "Executing SQLite VACUUM INTO for '$(basename "$(dirname "$source")")/$(basename "$source")'..."
    sqlite3 "$source" "VACUUM INTO '$destination';"

    set +e
    local integrity
    integrity="$(sqlite3 "$destination" "PRAGMA integrity_check;" 2>&1)"
    local status=$?
    set -e

    if [ $status -ne 0 ] || [ "$integrity" != "ok" ]; then
        echo "ERROR: Integrity check failed on backup copy of '$source': $integrity" >&2
        exit 2
    fi
    echo "✓ Database integrity verified: $integrity"
}

# stage_vault SOURCE_DIR STAGE_DIR
# Copies a directory tree, then proves every source file landed. A failed copy
# must fail the backup instead of producing a manifest that says success.
stage_vault() {
    local source="$1"
    local destination="$2"

    mkdir -p "$destination"
    local source_count=0 destination_count
    if [ -d "$source" ]; then
        cp -R "$source/." "$destination/"
        source_count="$(find "$source" -type f | wc -l | tr -d ' ')"
    fi
    destination_count="$(find "$destination" -type f | wc -l | tr -d ' ')"
    if [ "$source_count" -ne "$destination_count" ]; then
        echo "ERROR: $source copied $destination_count of $source_count files." >&2
        exit 6
    fi
}

VAULT_COUNT=0
TENANTS_JSON="[]"
INCLUDES_SYSTEM="false"

if [ "$MODE" = "multi" ]; then
    # A single-tenant export is portfolio-only: it must not carry (and on restore
    # silently overwrite) the control plane that describes every other portfolio.
    if [ -n "$TENANT_ID" ]; then
        echo "Single-tenant export: control plane (system.db) excluded."
    elif [ -f "$STORAGE_ROOT/system.db" ]; then
        stage_database "$STORAGE_ROOT/system.db" "$TEMP_STAGE/system/database.sqlite"
        INCLUDES_SYSTEM="true"
    else
        echo "Warning: '$STORAGE_ROOT/system.db' not found; backing up portfolios only." >&2
    fi

    TENANT_ENTRIES=""
    TENANT_LIST=""
    if [ -n "$TENANT_ID" ]; then
        if [ ! -d "$STORAGE_ROOT/tenants/$TENANT_ID" ]; then
            echo "ERROR: No such tenant directory: $STORAGE_ROOT/tenants/$TENANT_ID" >&2
            exit 1
        fi
        TENANT_LIST="$STORAGE_ROOT/tenants/$TENANT_ID"
    else
        shopt -s nullglob
        for candidate in "$STORAGE_ROOT"/tenants/*/; do
            TENANT_LIST="${TENANT_LIST}${candidate}
"
        done
        shopt -u nullglob
    fi

    if [ -z "$TENANT_LIST" ]; then
        echo "Warning: no tenants found under '$STORAGE_ROOT/tenants'." >&2
    fi

    while IFS= read -r tenant_dir; do
        [ -n "$tenant_dir" ] || continue
        tenant_id="$(basename "${tenant_dir%/}")"
        tenant_db="$tenant_dir/proptally.db"
        if [ ! -f "$tenant_db" ]; then
            continue
        fi
        stage_database "$tenant_db" "$TEMP_STAGE/tenants/$tenant_id/proptally.db"
        stage_vault "$tenant_dir/vault" "$TEMP_STAGE/tenants/$tenant_id/vault"
        # Photos are portfolio data too, not derived cache.
        stage_vault "$tenant_dir/photos" "$TEMP_STAGE/tenants/$tenant_id/photos"
        tenant_vault_files="$(find "$TEMP_STAGE/tenants/$tenant_id/vault" -type f | wc -l | tr -d ' ')"
        tenant_photo_files="$(find "$TEMP_STAGE/tenants/$tenant_id/photos" -type f | wc -l | tr -d ' ')"
        VAULT_COUNT=$((VAULT_COUNT + tenant_vault_files + tenant_photo_files))
        echo "✓ Portfolio '$tenant_id' staged ($tenant_vault_files vault, $tenant_photo_files photo files)"
        TENANT_ENTRIES="${TENANT_ENTRIES}${TENANT_ENTRIES:+, }\"$tenant_id\""
    done <<< "$TENANT_LIST"
    TENANTS_JSON="[$TENANT_ENTRIES]"
else
    echo "Source DB:   $DB_PATH"
    echo "Vault Dir:   $VAULT_DIR"
    stage_database "$DB_PATH" "$TEMP_STAGE/database.sqlite"
    stage_vault "$VAULT_DIR" "$TEMP_STAGE/vault"
    VAULT_COUNT="$(find "$TEMP_STAGE/vault" -type f | wc -l | tr -d ' ')"
    if [ -d "${STORAGE_ROOT}/photos" ]; then
        stage_vault "${STORAGE_ROOT}/photos" "$TEMP_STAGE/photos"
        PHOTO_COUNT="$(find "$TEMP_STAGE/photos" -type f | wc -l | tr -d ' ')"
        VAULT_COUNT=$((VAULT_COUNT + PHOTO_COUNT))
    fi
    echo "✓ Document vault and photos staged ($VAULT_COUNT files)"
fi

# Generate manifest
cat <<EOF > "$TEMP_STAGE/manifest.json"
{
  "timestamp": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "mode": "$MODE",
  "storage_root": "$STORAGE_ROOT",
  "includes_system": $INCLUDES_SYSTEM,
  "tenants": $TENANTS_JSON,
  "db_integrity": "ok",
  "vault_file_count": $VAULT_COUNT,
  "backup_name": "$BACKUP_NAME"
}
EOF

# Create compressed tarball
tar -czf "$ARCHIVE_FILE" -C "$TEMP_STAGE" .
echo "✓ Compressed archive created: $(du -h "$ARCHIVE_FILE" | awk '{print $1}')"

# Compute SHA-256 digest
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
