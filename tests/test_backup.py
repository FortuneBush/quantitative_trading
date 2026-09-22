import hashlib
import os
import sqlite3
from pathlib import Path

import pytest
import quant_monitor.storage.backup as backup_module

from quant_monitor.storage import (
    InconsistentSchemaError,
    SQLiteRepository,
    backup_sqlite,
    restore_sqlite_to_empty_dir,
    verify_sqlite,
)


def test_online_backup_is_integrity_checked_and_reopenable(tmp_path: Path):
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "archive" / "snapshot.sqlite3"
    repository = SQLiteRepository(source)
    repository.initialize()
    result = backup_sqlite(source, destination)
    repository.close()
    assert result.integrity_check == "ok"
    assert result.page_count > 0
    assert len(result.sha256) == 64
    assert Path(result.destination) == destination.resolve()
    with sqlite3.connect(str(destination)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SQLiteRepository.SCHEMA_VERSION
    assert result.sha256 == hashlib.sha256(destination.read_bytes()).hexdigest()


def test_backup_rejects_same_or_missing_paths(tmp_path: Path):
    source = tmp_path / "source.sqlite3"
    source.touch()
    with pytest.raises(ValueError, match="differ from source"):
        backup_sqlite(source, source)
    with pytest.raises(FileNotFoundError):
        backup_sqlite(tmp_path / "missing.sqlite3", tmp_path / "copy.sqlite3")
    with pytest.raises(ValueError, match="pages_per_step"):
        backup_sqlite(source, tmp_path / "copy.sqlite3", pages_per_step=0)


def test_backup_deadline_aborts_before_publishing_destination(tmp_path: Path, monkeypatch):
    source = tmp_path / "source.sqlite3"
    repository = SQLiteRepository(source)
    repository.initialize()
    repository.close()
    destination = tmp_path / "archive" / "timed-out.sqlite3"
    clock = iter((0.0, 2.0))
    monkeypatch.setattr(backup_module.time, "monotonic", lambda: next(clock))
    with pytest.raises(TimeoutError, match="exceeded timeout_seconds"):
        backup_sqlite(source, destination, timeout_seconds=1.0)
    assert not destination.exists()


def test_verify_timeout_is_validated_and_is_available_to_offline_checks(tmp_path: Path):
    source = tmp_path / "source.sqlite3"
    repository = SQLiteRepository(source)
    repository.initialize()
    repository.close()
    with pytest.raises(ValueError, match="timeout_seconds"):
        verify_sqlite(source, timeout_seconds=0)
    result = verify_sqlite(source, timeout_seconds=0.1)
    assert result.integrity_check == "ok"


def test_restore_rejects_invalid_timeout_before_creating_destination(tmp_path: Path):
    source = tmp_path / "source.sqlite3"
    repository = SQLiteRepository(source)
    repository.initialize()
    repository.close()
    destination_dir = tmp_path / "restore-drill"
    with pytest.raises(ValueError, match="timeout_seconds"):
        restore_sqlite_to_empty_dir(source, destination_dir, timeout_seconds=0)
    assert not destination_dir.exists()


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), -float("inf")])
def test_backup_tools_reject_nonfinite_timeout(tmp_path: Path, timeout: float):
    source = tmp_path / "source.sqlite3"
    source.touch()
    with pytest.raises(ValueError, match="finite and positive"):
        backup_sqlite(source, tmp_path / "copy.sqlite3", timeout_seconds=timeout)
    with pytest.raises(ValueError, match="finite and positive"):
        verify_sqlite(source, timeout_seconds=timeout)
    with pytest.raises(ValueError, match="finite and positive"):
        restore_sqlite_to_empty_dir(source, tmp_path / "restore", timeout_seconds=timeout)
    assert not (tmp_path / "restore").exists()


def test_verify_backup_is_read_only_and_validates_schema_contract(tmp_path: Path):
    source = tmp_path / "source.sqlite3"
    repository = SQLiteRepository(source)
    repository.initialize()
    repository.close()
    wal_before = (tmp_path / "source.sqlite3-wal").exists()
    shm_before = (tmp_path / "source.sqlite3-shm").exists()
    result = verify_sqlite(source)
    assert result.integrity_check == "ok"
    assert result.schema_version == SQLiteRepository.SCHEMA_VERSION
    assert result.table_count > 0
    # A read-only verification must not create SQLite sidecars or alter the
    # archived file itself.
    assert (tmp_path / "source.sqlite3-wal").exists() == wal_before
    assert (tmp_path / "source.sqlite3-shm").exists() == shm_before


def test_restore_drill_requires_empty_directory_and_reopens_result(tmp_path: Path):
    source = tmp_path / "source.sqlite3"
    repository = SQLiteRepository(source)
    repository.initialize()
    repository.close()
    destination_dir = tmp_path / "restore-drill"
    result = restore_sqlite_to_empty_dir(source, destination_dir)
    assert result.destination == str((destination_dir / "restored.sqlite3").resolve())
    assert result.verification.schema_version == SQLiteRepository.SCHEMA_VERSION
    assert result.verification.integrity_check == "ok"
    with pytest.raises(ValueError, match="must be empty"):
        restore_sqlite_to_empty_dir(source, destination_dir)


def test_verify_rejects_non_sqlite_file_without_creating_database(tmp_path: Path):
    source = tmp_path / "not-a-db.sqlite3"
    source.write_text("not sqlite", encoding="utf-8")
    with pytest.raises(sqlite3.DatabaseError):
        verify_sqlite(source)


def test_verify_handles_uri_special_characters_in_archive_path(tmp_path: Path):
    source = tmp_path / "archive #100% 中文.sqlite3"
    repository = SQLiteRepository(source)
    repository.initialize()
    repository.close()
    result = verify_sqlite(source)
    assert result.integrity_check == "ok"
    assert result.sha256


def test_backup_rejects_hardlink_alias_and_invalid_schema_before_publish(tmp_path: Path):
    source = tmp_path / "source.sqlite3"
    repository = SQLiteRepository(source)
    repository.initialize()
    repository.close()

    alias = tmp_path / "alias.sqlite3"
    os.link(source, alias)
    with pytest.raises(ValueError, match="alias source"):
        backup_sqlite(source, alias)

    # Corrupt the schema ledger in-place.  The destination must not be
    # published even though SQLite's low-level integrity check still passes.
    with sqlite3.connect(str(source)) as connection:
        connection.execute("PRAGMA user_version = 0")
        connection.commit()
    destination = tmp_path / "archive" / "invalid.sqlite3"
    with pytest.raises(InconsistentSchemaError):
        backup_sqlite(source, destination)
    assert not destination.exists()
