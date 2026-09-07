"""Persistence adapters."""

from .migrations import (
    InconsistentSchemaError,
    MigrationChecksumError,
    SchemaMigrationError,
    UnsupportedSchemaVersionError,
)
from .sqlite import SQLiteRepository

__all__ = [
    "InconsistentSchemaError",
    "MigrationChecksumError",
    "SQLiteRepository",
    "SchemaMigrationError",
    "UnsupportedSchemaVersionError",
]
