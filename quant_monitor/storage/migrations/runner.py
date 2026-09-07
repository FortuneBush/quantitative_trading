"""Small, deterministic SQLite schema migration runner.

Migrations are deliberately Python data rather than an ORM dependency.  The
ordered upgrade is applied in one SQLite transaction, recorded with SHA-256
checksums, and mirrored to ``PRAGMA user_version`` for compatibility with the
legacy v1 database.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Callable, FrozenSet, Iterable, Mapping, Optional, Sequence, Tuple


MIGRATION_TABLE = "schema_migrations"
MINIMUM_SQLITE_VERSION = (3, 30, 0)


class SchemaMigrationError(RuntimeError):
    """Base class for schema migration failures."""


class UnsupportedSchemaVersionError(SchemaMigrationError):
    """Raised when the database was created by a newer application."""


class MigrationChecksumError(SchemaMigrationError):
    """Raised when an already-applied migration definition has changed."""


class InconsistentSchemaError(SchemaMigrationError):
    """Raised when version markers and schema metadata disagree."""


def _checksum(
    version: int,
    name: str,
    statements: Sequence[str],
    required_tables: Sequence[str],
    required_columns: Sequence[Tuple[str, Sequence[str]]],
    required_indexes: Sequence[str],
    table_contracts: Sequence["TableContract"],
    index_contracts: Sequence["IndexContract"],
) -> str:
    """Hash an unambiguous canonical JSON migration definition."""

    payload = json.dumps(
        {
            "format": "quant_monitor.sqlite_migration.v3",
            "name": str(name).strip(),
            "required_tables": list(required_tables),
            "required_columns": [
                [table, list(columns)] for table, columns in required_columns
            ],
            "required_indexes": list(required_indexes),
            "table_contracts": [table.as_payload() for table in table_contracts],
            "index_contracts": [index.as_payload() for index in index_contracts],
            "statements": list(statements),
            "version": int(version),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalized_sql(value: str) -> str:
    """Normalize SQL enough for stable constraint-fragment checks.

    Full ``sqlite_master.sql`` text is intentionally not compared: SQLite may
    preserve harmless whitespace and keyword casing differences.  Punctuation
    relevant to the small fragments below is instead normalized explicitly.
    """

    # Ignore comments and quoted literals/identifiers before matching required
    # constraint syntax.  Otherwise a phrase hidden in a SQL string or comment
    # could masquerade as an enforced constraint.
    sql = str(value)
    output = []
    index = 0
    while index < len(sql):
        char = sql[index]
        following = sql[index + 1] if index + 1 < len(sql) else ""
        if char == "-" and following == "-":
            index += 2
            while index < len(sql) and sql[index] not in "\r\n":
                index += 1
            output.append(" ")
            continue
        if char == "/" and following == "*":
            end = sql.find("*/", index + 2)
            index = len(sql) if end < 0 else end + 2
            output.append(" ")
            continue
        if char in "'\"`[":
            closing = "]" if char == "[" else char
            index += 1
            while index < len(sql):
                if sql[index] == closing:
                    if closing != "]" and index + 1 < len(sql) and sql[index + 1] == closing:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            output.append(" ")
            continue
        output.append(char)
        index += 1
    normalized = re.sub(r"\s+", " ", "".join(output).strip()).lower()
    return re.sub(r"\s*([(),=])\s*", r"\1", normalized)


def _canonical_schema_sql(value: Optional[str]) -> Optional[Tuple[Tuple[str, str], ...]]:
    """Tokenize persisted DDL without discarding literals or quoted names.

    SQLite preserves most of the submitted CREATE statement in
    ``sqlite_master.sql``.  Comparing lexical tokens lets harmless whitespace,
    comments and keyword case vary while retaining every behaviour-changing
    token, including DEFAULT string values, CHECK/FK clauses, STRICT and
    WITHOUT ROWID.
    """

    if value is None:
        return None
    sql = str(value)
    tokens = []
    index = 0
    while index < len(sql):
        char = sql[index]
        following = sql[index + 1] if index + 1 < len(sql) else ""
        if char.isspace():
            index += 1
            continue
        if char == "-" and following == "-":
            index += 2
            while index < len(sql) and sql[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and following == "*":
            end = sql.find("*/", index + 2)
            index = len(sql) if end < 0 else end + 2
            continue
        if char == "'":
            index += 1
            literal = []
            while index < len(sql):
                if sql[index] == "'":
                    if index + 1 < len(sql) and sql[index + 1] == "'":
                        literal.append("'")
                        index += 2
                        continue
                    index += 1
                    break
                literal.append(sql[index])
                index += 1
            else:
                raise InconsistentSchemaError("schema contains an unterminated SQL literal")
            tokens.append(("literal", "".join(literal)))
            continue
        if char in '"`[':
            closing = "]" if char == "[" else char
            index += 1
            identifier = []
            while index < len(sql):
                if sql[index] == closing:
                    if closing != "]" and index + 1 < len(sql) and sql[index + 1] == closing:
                        identifier.append(closing)
                        index += 2
                        continue
                    index += 1
                    break
                identifier.append(sql[index])
                index += 1
            else:
                raise InconsistentSchemaError("schema contains an unterminated quoted identifier")
            tokens.append(("word", "".join(identifier).casefold()))
            continue
        if char.isalnum() or char in "_$":
            start = index
            index += 1
            while index < len(sql) and (sql[index].isalnum() or sql[index] in "_$"):
                index += 1
            tokens.append(("word", sql[start:index].casefold()))
            continue
        tokens.append(("symbol", char))
        index += 1
    return tuple(tokens)


def _normalized_default(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return re.sub(r"\s+", " ", str(value).strip())


@dataclass(frozen=True)
class ColumnContract:
    """One exact SQLite column contract, as exposed by ``table_xinfo``."""

    name: str
    declared_type: str
    not_null: bool = False
    default: Optional[str] = None
    primary_key_ordinal: int = 0
    hidden: int = 0

    def __post_init__(self) -> None:
        if not str(self.name).strip():
            raise ValueError("column contract name cannot be empty")
        if not str(self.declared_type).strip():
            raise ValueError("column contract type cannot be empty")
        if isinstance(self.primary_key_ordinal, bool) or int(self.primary_key_ordinal) < 0:
            raise ValueError("primary_key_ordinal must be a non-negative integer")
        if isinstance(self.hidden, bool) or int(self.hidden) < 0:
            raise ValueError("hidden must be a non-negative integer")
        object.__setattr__(self, "name", str(self.name).strip())
        object.__setattr__(self, "declared_type", str(self.declared_type).strip().upper())
        object.__setattr__(self, "not_null", bool(self.not_null))
        object.__setattr__(self, "default", _normalized_default(self.default))
        object.__setattr__(self, "primary_key_ordinal", int(self.primary_key_ordinal))
        object.__setattr__(self, "hidden", int(self.hidden))

    def as_payload(self) -> list:
        return [
            self.name,
            self.declared_type,
            self.not_null,
            self.default,
            self.primary_key_ordinal,
            self.hidden,
        ]


@dataclass(frozen=True)
class TableContract:
    """Exact ordered columns plus normalized, required table constraints."""

    name: str
    columns: Tuple[ColumnContract, ...]
    required_sql_fragments: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        name = str(self.name).strip()
        columns = tuple(self.columns)
        fragments = tuple(_normalized_sql(item) for item in self.required_sql_fragments)
        if not name or not columns:
            raise ValueError("table contract name and columns cannot be empty")
        if any(not isinstance(item, ColumnContract) for item in columns):
            raise TypeError("table contract columns must be ColumnContract values")
        if len({item.name for item in columns}) != len(columns):
            raise ValueError("table contract column names must be unique")
        if any(not item for item in fragments):
            raise ValueError("required_sql_fragments cannot be empty")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "columns", columns)
        object.__setattr__(self, "required_sql_fragments", fragments)

    def as_payload(self) -> list:
        return [
            self.name,
            [column.as_payload() for column in self.columns],
            list(self.required_sql_fragments),
        ]


@dataclass(frozen=True)
class IndexColumnContract:
    """One key column of an SQLite index."""

    name: str
    descending: bool = False
    collation: str = "BINARY"

    def __post_init__(self) -> None:
        if not str(self.name).strip() or not str(self.collation).strip():
            raise ValueError("index column name and collation cannot be empty")
        object.__setattr__(self, "name", str(self.name).strip())
        object.__setattr__(self, "descending", bool(self.descending))
        object.__setattr__(self, "collation", str(self.collation).strip().upper())

    def as_payload(self) -> list:
        return [self.name, self.descending, self.collation]


@dataclass(frozen=True)
class IndexContract:
    """Owner, uniqueness, origin and ordered key columns for one index."""

    name: str
    table: str
    columns: Tuple[IndexColumnContract, ...]
    unique: bool = False
    origin: str = "c"
    partial: bool = False

    def __post_init__(self) -> None:
        name = str(self.name).strip()
        table = str(self.table).strip()
        columns = tuple(self.columns)
        origin = str(self.origin).strip()
        if not name or not table or not origin or not columns:
            raise ValueError("index contract fields cannot be empty")
        if any(not isinstance(item, IndexColumnContract) for item in columns):
            raise TypeError("index contract columns must be IndexColumnContract values")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "table", table)
        object.__setattr__(self, "columns", columns)
        object.__setattr__(self, "unique", bool(self.unique))
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "partial", bool(self.partial))

    def as_payload(self) -> list:
        return [
            self.name,
            self.table,
            [column.as_payload() for column in self.columns],
            self.unique,
            self.origin,
            self.partial,
        ]


MIGRATION_LEDGER_CONTRACT = TableContract(
    name="schema_migrations",
    columns=(
        ColumnContract("version", "INTEGER", primary_key_ordinal=1),
        ColumnContract("checksum", "TEXT", not_null=True),
        ColumnContract("applied_at", "TEXT", not_null=True),
    ),
    required_sql_fragments=("CHECK(length(checksum) = 64)",),
)


# Immutable compatibility data for databases created by the short-lived
# newline-framed checksum runner.  Never derive accepted legacy values from a
# current Migration object: doing so would reintroduce ambiguous framing and
# let a modified current definition authenticate an old ledger row.
LEGACY_CHECKSUM_ALLOWLIST: Mapping[int, Tuple[str, FrozenSet[str]]] = MappingProxyType(
    {
        1: (
            "b2a06d33a18b69bc4a6a254cd7a8abd93e1d26e6bcabbbe467ce03b7c81ca4e2",
            frozenset(
                {
                    "1956dc42ffceabf93a162e3d25245ce46384890dec13e916d3695086c00443f4",
                    "b696da8d758d6f6f99ecb7ae94a1ae9b20029ed27c51df63369bae878fedbe53",
                }
            ),
        ),
        2: (
            "feca13fb760a32da702b042c4b8707b41a7f3dc2f0547e85ca4a68c13f6ad8c8",
            frozenset(
                {
                    "3f20e37b9b568b4670bb4bd56bbe06ac5658cce49306428c2a883b7b563d39e3",
                    "1e34e5d2fb40c71bb2dc3ee73dc328edde2d392385523d61aa0d7e6fd94bcb31",
                }
            ),
        ),
    }
)


@dataclass(frozen=True)
class Migration:
    """One immutable, ordered schema migration."""

    version: int
    name: str
    statements: Tuple[str, ...]
    required_tables: Tuple[str, ...] = ()
    required_columns: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()
    required_indexes: Tuple[str, ...] = ()
    table_contracts: Tuple[TableContract, ...] = ()
    index_contracts: Tuple[IndexContract, ...] = ()

    def __post_init__(self) -> None:
        if int(self.version) <= 0:
            raise ValueError("migration version must be positive")
        if not str(self.name).strip():
            raise ValueError("migration name cannot be empty")
        statements = tuple(str(item).strip() for item in self.statements)
        if not statements or any(not item for item in statements):
            raise ValueError("migration statements cannot be empty")
        object.__setattr__(self, "version", int(self.version))
        object.__setattr__(self, "name", str(self.name).strip())
        object.__setattr__(self, "statements", statements)
        object.__setattr__(
            self,
            "required_tables",
            tuple(str(item).strip() for item in self.required_tables),
        )
        columns = tuple(
            (
                str(table).strip(),
                tuple(str(column).strip() for column in table_columns),
            )
            for table, table_columns in self.required_columns
        )
        if any(
            not table
            or not table_columns
            or any(not column for column in table_columns)
            for table, table_columns in columns
        ):
            raise ValueError("required_columns entries cannot be empty")
        indexes = tuple(str(item).strip() for item in self.required_indexes)
        if any(not item for item in indexes):
            raise ValueError("required_indexes entries cannot be empty")
        object.__setattr__(self, "required_columns", columns)
        object.__setattr__(self, "required_indexes", indexes)
        table_contracts = tuple(self.table_contracts)
        index_contracts = tuple(self.index_contracts)
        if any(not isinstance(item, TableContract) for item in table_contracts):
            raise TypeError("table_contracts must contain TableContract values")
        if any(not isinstance(item, IndexContract) for item in index_contracts):
            raise TypeError("index_contracts must contain IndexContract values")
        if len({item.name for item in table_contracts}) != len(table_contracts):
            raise ValueError("table contract names must be unique")
        if len({item.name for item in index_contracts}) != len(index_contracts):
            raise ValueError("index contract names must be unique")
        object.__setattr__(self, "table_contracts", table_contracts)
        object.__setattr__(self, "index_contracts", index_contracts)

    @property
    def checksum(self) -> str:
        return _checksum(
            self.version,
            self.name,
            self.statements,
            self.required_tables,
            self.required_columns,
            self.required_indexes,
            self.table_contracts,
            self.index_contracts,
        )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MigrationRunner:
    """Validate and advance one SQLite connection to the latest migration."""

    def __init__(
        self,
        migrations: Iterable[Migration],
        *,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if tuple(sqlite3.sqlite_version_info) < MINIMUM_SQLITE_VERSION:
            raise SchemaMigrationError(
                "SQLite {} or newer is required for schema validation; found {}".format(
                    ".".join(str(item) for item in MINIMUM_SQLITE_VERSION),
                    sqlite3.sqlite_version,
                )
            )
        ordered = tuple(sorted(tuple(migrations), key=lambda item: item.version))
        versions = tuple(item.version for item in ordered)
        expected = tuple(range(1, len(ordered) + 1))
        if versions != expected:
            raise ValueError("migration versions must be contiguous and start at 1")
        self.migrations = ordered
        self._by_version = {item.version: item for item in ordered}
        self._clock = clock or _utc_now

    @property
    def latest_version(self) -> int:
        return self.migrations[-1].version if self.migrations else 0

    @staticmethod
    def _user_version(connection: sqlite3.Connection) -> int:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _index_exists(connection: sqlite3.Connection, index: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
                (index,),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _table_columns(
        connection: sqlite3.Connection, table: str
    ) -> Tuple[str, ...]:
        # Table names come only from immutable in-process migration definitions.
        return tuple(
            str(row[1])
            for row in connection.execute(
                'PRAGMA table_info("{}")'.format(table.replace('"', '""'))
            ).fetchall()
        )

    @staticmethod
    def _table_contract(
        connection: sqlite3.Connection, table: str
    ) -> Tuple[ColumnContract, ...]:
        # Contract names are immutable in-process definitions, never input.
        rows = connection.execute(
            'PRAGMA table_xinfo("{}")'.format(table.replace('"', '""'))
        ).fetchall()
        if rows and any(len(row) < 7 for row in rows):
            raise SchemaMigrationError(
                "SQLite PRAGMA table_xinfo returned an unsupported column layout"
            )
        return tuple(
            ColumnContract(
                name=str(row[1]),
                declared_type=str(row[2]),
                not_null=bool(row[3]),
                default=None if row[4] is None else str(row[4]),
                primary_key_ordinal=int(row[5]),
                hidden=int(row[6]),
            )
            for row in rows
        )

    @staticmethod
    def _table_sql(connection: sqlite3.Connection, table: str) -> str:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        return "" if row is None or row[0] is None else str(row[0])

    @classmethod
    def _validate_table_contract(
        cls, connection: sqlite3.Connection, contract: TableContract
    ) -> None:
        if not cls._table_exists(connection, contract.name):
            raise InconsistentSchemaError(
                "schema is missing contracted table {}".format(contract.name)
            )
        actual = cls._table_contract(connection, contract.name)
        if actual != contract.columns:
            raise InconsistentSchemaError(
                "table {} column contract does not match; actual={} expected={}".format(
                    contract.name,
                    tuple(item.as_payload() for item in actual),
                    tuple(item.as_payload() for item in contract.columns),
                )
            )
        sql = _normalized_sql(cls._table_sql(connection, contract.name))
        missing_fragments = tuple(
            fragment for fragment in contract.required_sql_fragments if fragment not in sql
        )
        if missing_fragments:
            raise InconsistentSchemaError(
                "table {} is missing required constraints: {}".format(
                    contract.name, ", ".join(missing_fragments)
                )
            )

    @staticmethod
    def _index_details(
        connection: sqlite3.Connection, contract: IndexContract
    ) -> Optional[Tuple[str, bool, str, bool, Tuple[IndexColumnContract, ...]]]:
        master = connection.execute(
            "SELECT tbl_name FROM sqlite_master WHERE type = 'index' AND name = ?",
            (contract.name,),
        ).fetchone()
        if master is None:
            return None
        owner = str(master[0])
        index_list = connection.execute(
            'PRAGMA index_list("{}")'.format(owner.replace('"', '""'))
        ).fetchall()
        if index_list and any(len(row) < 5 for row in index_list):
            raise SchemaMigrationError(
                "SQLite PRAGMA index_list returned an unsupported column layout"
            )
        listed = next((row for row in index_list if str(row[1]) == contract.name), None)
        if listed is None:
            return None
        detail_rows = connection.execute(
            'PRAGMA index_xinfo("{}")'.format(contract.name.replace('"', '""'))
        ).fetchall()
        if detail_rows and any(len(row) < 6 for row in detail_rows):
            raise SchemaMigrationError(
                "SQLite PRAGMA index_xinfo returned an unsupported column layout"
            )
        key_rows = tuple(row for row in detail_rows if int(row[5]) == 1)
        columns = tuple(
            IndexColumnContract(
                name=(
                    str(row[2])
                    if int(row[1]) >= 0 and row[2] is not None
                    else "<expression>"
                ),
                descending=bool(row[3]),
                collation=str(row[4]),
            )
            for row in key_rows
        )
        return owner, bool(listed[2]), str(listed[3]), bool(listed[4]), columns

    @classmethod
    def _validate_index_contract(
        cls, connection: sqlite3.Connection, contract: IndexContract
    ) -> None:
        actual = cls._index_details(connection, contract)
        expected = (
            contract.table,
            contract.unique,
            contract.origin,
            contract.partial,
            contract.columns,
        )
        if actual != expected:
            raise InconsistentSchemaError(
                "index {} contract does not match; actual={} expected={}".format(
                    contract.name, actual, expected
                )
            )

    @classmethod
    def _user_tables(cls, connection: sqlite3.Connection) -> Tuple[str, ...]:
        rows = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    @staticmethod
    def _schema_objects(
        connection: sqlite3.Connection,
    ) -> Mapping[Tuple[str, str], Tuple[str, Optional[Tuple[Tuple[str, str], ...]]]]:
        """Return all application-owned schema objects and their exact DDL.

        SQLite-owned objects (for example ``sqlite_sequence`` and automatic
        primary-key indexes) are deliberately excluded. Their defining table
        semantics are still covered by the exact CREATE TABLE comparison.
        """

        rows = connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
              AND type IN ('table', 'index', 'view', 'trigger')
            ORDER BY type, name
            """
        ).fetchall()
        return {
            (str(row[0]), str(row[1])): (
                str(row[2]),
                _canonical_schema_sql(None if row[3] is None else str(row[3])),
            )
            for row in rows
        }

    def _expected_schema_objects(
        self, current: int
    ) -> Mapping[Tuple[str, str], Tuple[str, Optional[Tuple[Tuple[str, str], ...]]]]:
        """Materialize the checksummed migration definitions as the contract."""

        expected = sqlite3.connect(":memory:")
        try:
            expected.execute("PRAGMA foreign_keys = ON")
            for migration in self.migrations:
                if migration.version > current:
                    break
                for statement in migration.statements:
                    expected.execute(statement)
            return self._schema_objects(expected)
        except sqlite3.Error as exc:
            raise SchemaMigrationError(
                "migration definitions cannot materialize the expected SQLite schema"
            ) from exc
        finally:
            expected.close()

    def _validate_exact_schema(
        self, connection: sqlite3.Connection, current: int
    ) -> None:
        expected = self._expected_schema_objects(current)
        actual = self._schema_objects(connection)
        if actual == expected:
            return
        expected_keys = set(expected)
        actual_keys = set(actual)
        missing = sorted(expected_keys - actual_keys)
        extra = sorted(actual_keys - expected_keys)
        changed = sorted(
            key for key in expected_keys & actual_keys if expected[key] != actual[key]
        )
        details = []
        if missing:
            details.append("missing={}".format(missing))
        if extra:
            details.append("extra={}".format(extra))
        if changed:
            details.append("changed={}".format(changed))
        raise InconsistentSchemaError(
            "schema objects do not match the exact migration contract ({})".format(
                "; ".join(details)
            )
        )

    @classmethod
    def _migration_rows(
        cls, connection: sqlite3.Connection
    ) -> Tuple[Tuple[int, str, str], ...]:
        if not cls._table_exists(connection, MIGRATION_TABLE):
            return ()
        cls._validate_table_contract(connection, MIGRATION_LEDGER_CONTRACT)
        return tuple(
            (int(row[0]), str(row[1]), str(row[2]))
            for row in connection.execute(
                "SELECT version, checksum, applied_at "
                "FROM schema_migrations ORDER BY version"
            ).fetchall()
        )

    @staticmethod
    def _is_approved_legacy_checksum(migration: Migration, checksum: str) -> bool:
        compatibility = LEGACY_CHECKSUM_ALLOWLIST.get(migration.version)
        if compatibility is None:
            return False
        approved_current_checksum, accepted_ledger_checksums = compatibility
        return (
            migration.checksum == approved_current_checksum
            and checksum in accepted_ledger_checksums
        )

    def _validate_current_state(
        self,
        connection: sqlite3.Connection,
        current: int,
        *,
        allow_legacy_checksums: bool = False,
    ) -> None:
        if current > self.latest_version:
            raise UnsupportedSchemaVersionError(
                "database schema version {} is newer than supported version {}".format(
                    current, self.latest_version
                )
            )
        if current < 0:
            raise InconsistentSchemaError("PRAGMA user_version cannot be negative")

        rows = self._migration_rows(connection)
        if rows:
            recorded_versions = tuple(row[0] for row in rows)
            unknown = tuple(version for version in recorded_versions if version > self.latest_version)
            if unknown:
                raise UnsupportedSchemaVersionError(
                    "database records unsupported migration version {}".format(max(unknown))
                )
            expected_versions = tuple(range(1, current + 1))
            if recorded_versions != expected_versions:
                raise InconsistentSchemaError(
                    "schema_migrations versions {} do not match PRAGMA user_version {}".format(
                        recorded_versions, current
                    )
                )
            for version, checksum, applied_at in rows:
                migration = self._by_version[version]
                checksum_matches = checksum == migration.checksum
                if allow_legacy_checksums:
                    checksum_matches = checksum_matches or self._is_approved_legacy_checksum(
                        migration, checksum
                    )
                if not checksum_matches:
                    raise MigrationChecksumError(
                        "migration {} checksum differs from the applied schema".format(version)
                    )
                try:
                    parsed = datetime.fromisoformat(applied_at.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise InconsistentSchemaError(
                        "migration {} has an invalid applied_at timestamp".format(version)
                    ) from exc
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    raise InconsistentSchemaError(
                        "migration {} applied_at must be timezone-aware".format(version)
                    )
        elif current:
            # v1 is the only supported legacy schema that predates the
            # migration ledger.  Future versions must always be recorded.
            if current != 1:
                raise InconsistentSchemaError(
                    "schema version {} has no migration history".format(current)
                )
            missing = tuple(
                table
                for table in self._by_version[1].required_tables
                if not self._table_exists(connection, table)
            )
            if missing:
                raise InconsistentSchemaError(
                    "legacy v1 schema is missing tables: {}".format(", ".join(missing))
                )
        elif self._user_tables(connection):
            raise InconsistentSchemaError(
                "unversioned non-empty SQLite schema cannot be migrated safely"
            )

        missing_required = tuple(
            table
            for migration in self.migrations
            if migration.version <= current
            for table in migration.required_tables
            if not self._table_exists(connection, table)
        )
        if missing_required:
            raise InconsistentSchemaError(
                "schema version {} is missing required tables: {}".format(
                    current, ", ".join(sorted(set(missing_required)))
                )
            )
        for migration in self.migrations:
            if migration.version > current:
                continue
            for table, expected_columns in migration.required_columns:
                actual = self._table_columns(connection, table)
                if actual != tuple(expected_columns):
                    raise InconsistentSchemaError(
                        "schema version {} table {} columns {} do not match {}".format(
                            current, table, actual, tuple(expected_columns)
                        )
                    )
            missing_indexes = tuple(
                index
                for index in migration.required_indexes
                if not self._index_exists(connection, index)
            )
            if missing_indexes:
                raise InconsistentSchemaError(
                    "schema version {} is missing required indexes: {}".format(
                        current, ", ".join(missing_indexes)
                    )
                )
            for contract in migration.table_contracts:
                self._validate_table_contract(connection, contract)
            for contract in migration.index_contracts:
                self._validate_index_contract(connection, contract)

        # This check runs before a ledger is minted for legacy v1.  It rejects
        # both altered DDL (including added CHECK/FK/STRICT/WITHOUT ROWID) and
        # any unrecorded table, index, view or trigger.  The cheaper structural
        # checks above intentionally run first to keep their focused errors.
        self._validate_exact_schema(connection, current)

    def _applied_at(self) -> str:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("migration clock must return a timezone-aware datetime")
        return value.isoformat(timespec="microseconds")

    def _record_migrations(
        self,
        connection: sqlite3.Connection,
        through_version: int,
        applied_at: str,
    ) -> None:
        if not self._table_exists(connection, MIGRATION_TABLE):
            return
        for migration in self.migrations:
            if migration.version > through_version:
                break
            row = connection.execute(
                "SELECT checksum FROM schema_migrations WHERE version = ?",
                (migration.version,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO schema_migrations (version, checksum, applied_at)
                    VALUES (?, ?, ?)
                    """,
                    (migration.version, migration.checksum, applied_at),
                )
                continue
            checksum = str(row[0])
            if self._is_approved_legacy_checksum(migration, checksum):
                connection.execute(
                    "UPDATE schema_migrations SET checksum = ? WHERE version = ?",
                    (migration.checksum, migration.version),
                )
            elif checksum != migration.checksum:
                raise MigrationChecksumError(
                    "migration {} checksum conflicts with the applied schema".format(
                        migration.version
                    )
                )

    def migrate(self, connection: sqlite3.Connection) -> int:
        """Apply missing migrations and return the resulting schema version."""

        if connection.in_transaction:
            raise SchemaMigrationError("migration connection already has an active transaction")
        current = 0
        applying: Optional[Migration] = None
        try:
            # Acquire the writer lock before reading version state. Concurrent
            # initializers then serialize and the later runner observes the
            # committed schema rather than replaying stale current=0 state.
            connection.execute("BEGIN IMMEDIATE")
            current = self._user_version(connection)
            self._validate_current_state(
                connection, current, allow_legacy_checksums=True
            )
            applied_at = self._applied_at()
            for migration in self.migrations:
                if migration.version <= current:
                    continue
                applying = migration
                for statement in migration.statements:
                    connection.execute(statement)
                current = migration.version
                self._record_migrations(connection, current, applied_at)
                connection.execute(
                    "PRAGMA user_version = {}".format(int(current))
                )

            # Canonicalize old pre-release checksums even when no DDL is due.
            self._record_migrations(connection, current, applied_at)
            # Structural, ledger and version validation happens before the
            # only commit, so a migration that breaks an earlier invariant is
            # rolled back together with its ledger/user_version changes.
            self._validate_current_state(connection, current)
            connection.commit()
            return current
        except Exception as exc:
            connection.rollback()
            if isinstance(exc, SchemaMigrationError):
                raise
            # Repository initialization deliberately retries lock contention.
            # Preserve the native exception instead of hiding it behind a
            # generic migration error that makes the retry policy ineffective.
            if isinstance(exc, sqlite3.OperationalError) and any(
                marker in str(exc).casefold() for marker in ("locked", "busy")
            ):
                raise
            if applying is None:
                raise SchemaMigrationError("failed to validate schema migration state") from exc
            raise SchemaMigrationError(
                "failed to apply migration {} ({})".format(
                    applying.version, applying.name
                )
            ) from exc


__all__ = [
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
    "SchemaMigrationError",
    "UnsupportedSchemaVersionError",
    "TableContract",
]
