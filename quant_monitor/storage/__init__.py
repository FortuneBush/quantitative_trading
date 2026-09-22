"""Persistence adapters."""

from .migrations import (
    InconsistentSchemaError,
    MigrationChecksumError,
    SchemaMigrationError,
    UnsupportedSchemaVersionError,
)
from .sqlite import SQLiteRepository
from .backup import (
    BackupFreshness,
    BackupResult,
    BackupVerification,
    RestoreDrillResult,
    backup_sqlite,
    check_backup_freshness,
    restore_sqlite_to_empty_dir,
    verify_sqlite,
)

__all__ = [
    "InconsistentSchemaError",
    "MigrationChecksumError",
    "SQLiteRepository",
    "BackupFreshness",
    "BackupResult",
    "BackupVerification",
    "RestoreDrillResult",
    "backup_sqlite",
    "check_backup_freshness",
    "restore_sqlite_to_empty_dir",
    "verify_sqlite",
    "SchemaMigrationError",
    "UnsupportedSchemaVersionError",
]
