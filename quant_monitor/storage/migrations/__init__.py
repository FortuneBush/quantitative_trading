"""SQLite migration definitions and execution primitives."""

from .runner import (
    ColumnContract,
    IndexColumnContract,
    IndexContract,
    InconsistentSchemaError,
    LEGACY_CHECKSUM_ALLOWLIST,
    MIGRATION_TABLE,
    MIGRATION_LEDGER_CONTRACT,
    MINIMUM_SQLITE_VERSION,
    Migration,
    MigrationChecksumError,
    MigrationRunner,
    SchemaMigrationError,
    UnsupportedSchemaVersionError,
    TableContract,
)
from .schema import (
    DEFAULT_MIGRATIONS,
    SCHEMA_VERSION,
    V1_INITIAL_SCHEMA,
    V1_TABLES,
    V2_MIGRATION_METADATA,
    V3_COLLECTION_LINEAGE,
)

__all__ = [
    "DEFAULT_MIGRATIONS",
    "ColumnContract",
    "IndexColumnContract",
    "IndexContract",
    "InconsistentSchemaError",
    "LEGACY_CHECKSUM_ALLOWLIST",
    "MIGRATION_TABLE",
    "MIGRATION_LEDGER_CONTRACT",
    "MINIMUM_SQLITE_VERSION",
    "Migration",
    "MigrationChecksumError",
    "MigrationRunner",
    "SCHEMA_VERSION",
    "SchemaMigrationError",
    "UnsupportedSchemaVersionError",
    "TableContract",
    "V1_INITIAL_SCHEMA",
    "V1_TABLES",
    "V2_MIGRATION_METADATA",
    "V3_COLLECTION_LINEAGE",
]
