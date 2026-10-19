"""Integration tests for automated disaster recovery hot backup and restore scripts."""

import hashlib
import subprocess
import tarfile
from pathlib import Path

import pytest
from sqlmodel import Session

from proptally.db.connection import get_engine
from proptally.db.models import FinancialAccount, FinancialInstitution
from proptally.db.schema import init_schema

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKUP_SCRIPT = REPO_ROOT / "scripts" / "backup.sh"
RESTORE_SCRIPT = REPO_ROOT / "scripts" / "restore.sh"


@pytest.fixture
def disaster_recovery_env(tmp_path):
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    backup_dir = tmp_path / "backups"

    source_dir.mkdir(parents=True, exist_ok=True)
    target_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)

    db_path = source_dir / "proptally.db"
    vault_path = source_dir / "vault"
    vault_path.mkdir(parents=True, exist_ok=True)

    # Populate source DB
    engine = get_engine(f"sqlite:///{db_path}")
    init_schema(engine)
    with Session(engine) as session:
        inst = FinancialInstitution(id="inst_test", name="Test Bank")
        session.add(inst)
        session.flush()
        acct = FinancialAccount(
            id="acct_test_01",
            institution_id="inst_test",
            account_name="Test Checking",
            account_type="CHECKING",
            account_number_masked="...1234",
            mixed_use_status="DEDICATED_RENTAL",
        )
        session.add(acct)
        session.commit()

    # Populate sample vault files
    file1 = vault_path / "doc1.txt"
    file1.write_text("Authoritative document 1 content")
    file2 = vault_path / "doc2.txt"
    file2.write_text("Authoritative document 2 content")

    return {
        "db_path": db_path,
        "vault_path": vault_path,
        "backup_dir": backup_dir,
        "target_dir": target_dir,
    }


def test_hot_backup_creation_and_checksum(disaster_recovery_env):
    db_path = disaster_recovery_env["db_path"]
    vault_path = disaster_recovery_env["vault_path"]
    backup_dir = disaster_recovery_env["backup_dir"]

    cmd = [
        str(BACKUP_SCRIPT),
        "--db", str(db_path),
        "--vault", str(vault_path),
        "--out", str(backup_dir),
    ]

    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, f"backup.sh failed:\n{res.stderr}\n{res.stdout}"
    assert "Database integrity verified: ok" in res.stdout
    assert "Backup completed successfully" in res.stdout

    archives = list(backup_dir.glob("proptally_backup_*.tar.gz"))
    assert len(archives) == 1
    archive_file = archives[0]
    checksum_file = backup_dir / f"{archive_file.name}.sha256"
    assert checksum_file.exists()

    # Verify SHA-256 in file matches actual archive hash
    expected_hash = checksum_file.read_text().split()[0]
    actual_hash = hashlib.sha256(archive_file.read_bytes()).hexdigest()
    assert expected_hash == actual_hash


def test_restore_valid_archive(disaster_recovery_env):
    db_path = disaster_recovery_env["db_path"]
    vault_path = disaster_recovery_env["vault_path"]
    backup_dir = disaster_recovery_env["backup_dir"]
    target_dir = disaster_recovery_env["target_dir"]

    # 1. Run backup
    subprocess.run(
        [str(BACKUP_SCRIPT), "--db", str(db_path), "--vault", str(vault_path), "--out", str(backup_dir)],
        check=True,
        capture_output=True,
    )
    archive_file = list(backup_dir.glob("proptally_backup_*.tar.gz"))[0]

    # 2. Run restore to target
    target_db = target_dir / "restored.db"
    target_vault = target_dir / "restored_vault"

    res = subprocess.run(
        [
            str(RESTORE_SCRIPT),
            "--archive", str(archive_file),
            "--target-db", str(target_db),
            "--target-vault", str(target_vault),
        ],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"restore.sh failed:\n{res.stderr}\n{res.stdout}"
    assert "Restore completed successfully" in res.stdout

    # 3. Verify target database content
    engine = get_engine(f"sqlite:///{target_db}")
    with Session(engine) as session:
        acct = session.get(FinancialAccount, "acct_test_01")
        assert acct is not None
        assert acct.account_name == "Test Checking"

    # 4. Verify target vault files
    assert (target_vault / "doc1.txt").exists()
    assert (target_vault / "doc1.txt").read_text() == "Authoritative document 1 content"
    assert (target_vault / "doc2.txt").exists()


def test_restore_tampered_archive_fails_verification(disaster_recovery_env):
    db_path = disaster_recovery_env["db_path"]
    vault_path = disaster_recovery_env["vault_path"]
    backup_dir = disaster_recovery_env["backup_dir"]
    target_dir = disaster_recovery_env["target_dir"]

    # 1. Run backup
    subprocess.run(
        [str(BACKUP_SCRIPT), "--db", str(db_path), "--vault", str(vault_path), "--out", str(backup_dir)],
        check=True,
        capture_output=True,
    )
    archive_file = list(backup_dir.glob("proptally_backup_*.tar.gz"))[0]

    # Tamper with archive bytes
    data = bytearray(archive_file.read_bytes())
    data[-1] = (data[-1] + 1) % 256
    archive_file.write_bytes(bytes(data))

    # 2. Attempt restore
    target_db = target_dir / "tampered.db"
    res = subprocess.run(
        [str(RESTORE_SCRIPT), "--archive", str(archive_file), "--target-db", str(target_db)],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 3
    assert "Checksum mismatch" in res.stderr
    assert not target_db.exists()


def test_restore_corrupt_sqlite_fails_integrity(disaster_recovery_env, tmp_path):
    target_dir = disaster_recovery_env["target_dir"]
    backup_dir = disaster_recovery_env["backup_dir"]

    # Create an archive containing a corrupted SQLite file
    corrupt_stage = tmp_path / "corrupt_stage"
    corrupt_stage.mkdir()
    corrupt_db = corrupt_stage / "database.sqlite"
    corrupt_db.write_text("NOT A VALID SQLITE HEADER")

    corrupt_archive = backup_dir / "proptally_backup_corrupted.tar.gz"
    with tarfile.open(corrupt_archive, "w:gz") as tar:
        tar.add(corrupt_db, arcname="database.sqlite")

    # Generate matching checksum for this corrupted archive
    checksum_file = backup_dir / f"{corrupt_archive.name}.sha256"
    h = hashlib.sha256(corrupt_archive.read_bytes()).hexdigest()
    checksum_file.write_text(f"{h}  {corrupt_archive.name}\n")

    target_db = target_dir / "should_not_exist.db"
    res = subprocess.run(
        [str(RESTORE_SCRIPT), "--archive", str(corrupt_archive), "--target-db", str(target_db)],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 5
    assert "SQLite integrity check failed" in res.stderr
    assert not target_db.exists()


def test_hot_backup_during_active_wal_transaction(disaster_recovery_env):
    db_path = disaster_recovery_env["db_path"]
    vault_path = disaster_recovery_env["vault_path"]
    backup_dir = disaster_recovery_env["backup_dir"]

    import sqlite3
    # Open an active connection and keep an uncommitted transaction open
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = WAL;")
    cursor = conn.cursor()
    cursor.execute("BEGIN IMMEDIATE;")
    cursor.execute("UPDATE financial_accounts SET account_name = 'Pending Name' WHERE id = 'acct_test_01';")

    try:
        # Run hot backup while transaction is active
        cmd = [
            str(BACKUP_SCRIPT),
            "--db", str(db_path),
            "--vault", str(vault_path),
            "--out", str(backup_dir),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        assert res.returncode == 0
        assert "Database integrity verified: ok" in res.stdout

        archive_file = next(backup_dir.glob("proptally_backup_*.tar.gz"))
        with tarfile.open(archive_file, "r:gz") as archive:
            database_member = next(
                member for member in archive.getmembers()
                if member.name.removeprefix("./") == "database.sqlite"
            )
            snapshot = archive.extractfile(database_member)
            assert snapshot is not None
            assert snapshot.read()

        restored_db = disaster_recovery_env["target_dir"] / "wal-snapshot.db"
        restore = subprocess.run(
            [str(RESTORE_SCRIPT), "--archive", str(archive_file), "--target-db", str(restored_db)],
            capture_output=True,
            text=True,
        )
        assert restore.returncode == 0, restore.stderr
        restored_engine = get_engine(f"sqlite:///{restored_db}")
        with Session(restored_engine) as session:
            account = session.get(FinancialAccount, "acct_test_01")
            assert account is not None
            assert account.account_name == "Test Checking"
        restored_engine.dispose()
    finally:
        conn.rollback()
        conn.close()

