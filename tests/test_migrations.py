"""Offline SQLite migration compatibility and failure-semantics tests."""

from __future__ import annotations

import sqlite3
import threading
import hashlib
import time
from datetime import datetime, timezone

import pytest

from quant_monitor.storage import (
    InconsistentSchemaError,
    MigrationChecksumError,
    SQLiteRepository,
    UnsupportedSchemaVersionError,
)
from quant_monitor.storage.migrations import (
    DEFAULT_MIGRATIONS,
    Migration,
    MigrationRunner,
    SchemaMigrationError,
    V1_INITIAL_SCHEMA,
    V2_MIGRATION_METADATA,
    V3_COLLECTION_LINEAGE,
    V4_RESEARCH_RUNS,
    V5_RESEARCH_RESULTS,
    V6_COLLECTION_JOBS,
    V7_FUNDAMENTAL_OBSERVATIONS,
    V8_CORPORATE_ACTIONS_AND_ADJUSTMENT_FACTORS,
    V9_POINT_IN_TIME_UNIVERSE_SNAPSHOTS,
)


HISTORICAL_LEDGER_CHECKSUMS = (
    {
        1: "1956dc42ffceabf93a162e3d25245ce46384890dec13e916d3695086c00443f4",
        2: "3f20e37b9b568b4670bb4bd56bbe06ac5658cce49306428c2a883b7b563d39e3",
    },
    {
        1: "b696da8d758d6f6f99ecb7ae94a1ae9b20029ed27c51df63369bae878fedbe53",
        2: "1e34e5d2fb40c71bb2dc3ee73dc328edde2d392385523d61aa0d7e6fd94bcb31",
    },
)


FIXED_TIME = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


def create_legacy_v1(path):
    """Create the pre-runner schema exactly as an existing v1 file."""

    with sqlite3.connect(str(path)) as connection:
        for statement in V1_INITIAL_SCHEMA.statements:
            connection.execute(statement)
        connection.execute(
            """
            INSERT INTO bars (
                symbol, interval, timestamp, open, high, low, close,
                volume, turnover, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "600000.SH",
                "1d",
                "2026-09-03T15:30:00.000000+08:00",
                "10.01",
                "10.25",
                "9.98",
                "10.20",
                123456,
                "1256789.12",
                "fixture",
            ),
        )
        connection.execute("PRAGMA user_version = 1")
        connection.commit()


def inspect_migrations(path):
    with sqlite3.connect(str(path)) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        rows = connection.execute(
            "SELECT version, checksum, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
        return version, rows


def test_existing_v1_database_upgrades_without_changing_legacy_data(tmp_path):
    path = tmp_path / "legacy-v1.sqlite3"
    create_legacy_v1(path)

    repository = SQLiteRepository(path)
    repository.initialize()
    repository.close()

    with sqlite3.connect(str(path)) as connection:
        bar = connection.execute(
            """
            SELECT symbol, interval, timestamp, open, high, low, close,
                   volume, turnover, source
            FROM bars
            """
        ).fetchone()
        assert bar == (
            "600000.SH",
            "1d",
            "2026-09-03T15:30:00.000000+08:00",
            "10.01",
            "10.25",
            "9.98",
            "10.20",
            123456,
            "1256789.12",
            "fixture",
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    version, rows = inspect_migrations(path)
    assert version == SQLiteRepository.SCHEMA_VERSION
    assert [row[0] for row in rows] == list(range(1, SQLiteRepository.SCHEMA_VERSION + 1))
    assert [row[1] for row in rows] == [item.checksum for item in DEFAULT_MIGRATIONS]
    assert all(datetime.fromisoformat(row[2]).utcoffset() is not None for row in rows)


def test_v2_to_v3_is_additive_and_does_not_backfill_untrusted_legacy_bars(tmp_path):
    path = tmp_path / "legacy-v2.sqlite3"
    with sqlite3.connect(str(path)) as connection:
        MigrationRunner((V1_INITIAL_SCHEMA, V2_MIGRATION_METADATA)).migrate(connection)
        connection.execute(
            """
            INSERT INTO bars (
                symbol, interval, timestamp, open, high, low, close,
                volume, turnover, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "600000.SH", "1d", "2026-09-03T15:30:00.000000+08:00",
                "10", "11", "9", "10.5", 100, "1050", "legacy",
            ),
        )
        connection.commit()

    repository = SQLiteRepository(path)
    repository.initialize()
    repository.close()

    with sqlite3.connect(str(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SQLiteRepository.SCHEMA_VERSION
        assert connection.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM bar_observations"
        ).fetchone()[0] == 0
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("historical_checksums", HISTORICAL_LEDGER_CHECKSUMS)
def test_historical_ledgers_are_canonicalized_to_current_checksums(
    tmp_path, historical_checksums
):
    path = tmp_path / "historical-v2-ledger.sqlite3"
    repository = SQLiteRepository(path)
    repository.initialize()
    repository.close()
    with sqlite3.connect(str(path)) as connection:
        for version, checksum in historical_checksums.items():
            connection.execute(
                "UPDATE schema_migrations SET checksum = ? WHERE version = ?",
                (checksum, version),
            )

    reopened = SQLiteRepository(path)
    reopened.initialize()
    reopened.close()

    _, rows = inspect_migrations(path)
    assert [row[1] for row in rows] == [item.checksum for item in DEFAULT_MIGRATIONS]


def test_fresh_database_uses_runner_and_second_initialize_is_idempotent(tmp_path):
    path = tmp_path / "fresh.sqlite3"
    first = SQLiteRepository(path)
    first.initialize()
    first.close()
    before = inspect_migrations(path)

    second = SQLiteRepository(path)
    second.initialize()
    second.close()
    after = inspect_migrations(path)

    assert before == after
    assert before[0] == SQLiteRepository.SCHEMA_VERSION
    with sqlite3.connect(str(path)) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {
            "instruments",
            "quotes",
            "bars",
            "metrics",
            "alerts",
            "signals",
            "schema_migrations",
        }.issubset(tables)


def test_unknown_higher_user_version_is_rejected_without_downgrade(tmp_path):
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(str(path)) as connection:
        connection.execute("PRAGMA user_version = 99")

    repository = SQLiteRepository(path)
    with pytest.raises(UnsupportedSchemaVersionError, match="newer"):
        repository.initialize()
    assert repository.initialized is False
    with sqlite3.connect(str(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 99


def test_applied_migration_checksum_drift_is_rejected(tmp_path):
    path = tmp_path / "checksum.sqlite3"
    repository = SQLiteRepository(path)
    repository.initialize()
    repository.close()
    with sqlite3.connect(str(path)) as connection:
        connection.execute(
            "UPDATE schema_migrations SET checksum = ? WHERE version = 1",
            ("0" * 64,),
        )

    reopened = SQLiteRepository(path)
    with pytest.raises(MigrationChecksumError, match="checksum"):
        reopened.initialize()
    assert reopened.initialized is False


def test_one_migration_rolls_back_schema_ledger_and_version_on_failure(tmp_path):
    path = tmp_path / "atomic.sqlite3"
    create_legacy_v1(path)
    broken_v2 = Migration(
        version=2,
        name="broken_metadata_foundation",
        statements=V2_MIGRATION_METADATA.statements
        + (
            "CREATE TABLE must_roll_back (id INTEGER PRIMARY KEY)",
            "INSERT INTO table_that_does_not_exist VALUES (1)",
        ),
    )

    with sqlite3.connect(str(path)) as connection:
        runner = MigrationRunner(
            (V1_INITIAL_SCHEMA, broken_v2), clock=lambda: FIXED_TIME
        )
        with pytest.raises(SchemaMigrationError, match="failed to apply migration 2"):
            runner.migrate(connection)

    with sqlite3.connect(str(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "schema_migrations" not in names
        assert "must_roll_back" not in names
        assert connection.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 1


def test_version_markers_must_be_consistent(tmp_path):
    path = tmp_path / "drift.sqlite3"
    repository = SQLiteRepository(path)
    repository.initialize()
    repository.close()
    with sqlite3.connect(str(path)) as connection:
        connection.execute("PRAGMA user_version = 1")

    with sqlite3.connect(str(path)) as connection:
        with pytest.raises(InconsistentSchemaError, match="do not match"):
            MigrationRunner(DEFAULT_MIGRATIONS).migrate(connection)


def test_unversioned_nonempty_database_is_not_guessed(tmp_path):
    path = tmp_path / "unknown.sqlite3"
    with sqlite3.connect(str(path)) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")

    repository = SQLiteRepository(path)
    with pytest.raises(InconsistentSchemaError, match="unversioned non-empty"):
        repository.initialize()
    assert repository.initialized is False


def test_migration_definitions_require_contiguous_versions():
    with pytest.raises(ValueError, match="contiguous"):
        MigrationRunner(
            (
                Migration(1, "first", ("CREATE TABLE one (id INTEGER)",)),
                Migration(3, "third", ("CREATE TABLE three (id INTEGER)",)),
            )
        )


def test_legacy_v1_with_wrong_columns_is_rejected(tmp_path):
    path = tmp_path / "fake-v1.sqlite3"
    with sqlite3.connect(str(path)) as connection:
        for table in ("instruments", "quotes", "bars", "metrics", "alerts", "signals"):
            connection.execute("CREATE TABLE {} (wrong TEXT)".format(table))
        connection.execute("PRAGMA user_version = 1")

    repository = SQLiteRepository(path)
    with pytest.raises(InconsistentSchemaError, match="columns"):
        repository.initialize()
    assert repository.initialized is False


def test_migration_that_breaks_prior_schema_rolls_back_everything(tmp_path):
    path = tmp_path / "breaks-prior-schema.sqlite3"
    base = SQLiteRepository(path)
    base.initialize()
    base.close()
    destructive = Migration(
        version=SQLiteRepository.SCHEMA_VERSION + 1,
        name="invalid_drop_required_table",
        statements=("DROP TABLE bars",),
    )

    with sqlite3.connect(str(path)) as connection:
        with pytest.raises(InconsistentSchemaError, match="missing required tables"):
            MigrationRunner(DEFAULT_MIGRATIONS + (destructive,)).migrate(connection)

    with sqlite3.connect(str(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SQLiteRepository.SCHEMA_VERSION
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bars'"
        ).fetchone()
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0] == SQLiteRepository.SCHEMA_VERSION


def test_repository_does_not_repair_schema_outside_migration_ledger(tmp_path):
    path = tmp_path / "missing-index.sqlite3"
    repository = SQLiteRepository(path)
    repository.initialize()
    repository.close()
    with sqlite3.connect(str(path)) as connection:
        connection.execute("DROP INDEX idx_bars_symbol_interval_time")

    reopened = SQLiteRepository(path)
    with pytest.raises(InconsistentSchemaError, match="missing required indexes"):
        reopened.initialize()
    assert reopened.initialized is False


def test_checksum_framing_distinguishes_newline_boundaries():
    first = Migration(1, "alpha\nbeta", ("gamma",))
    second = Migration(1, "alpha", ("beta\ngamma",))

    first_legacy = hashlib.sha256("1\nalpha\nbeta\ngamma".encode()).hexdigest()
    second_legacy = hashlib.sha256("1\nalpha\nbeta\ngamma".encode()).hexdigest()
    assert first_legacy == second_legacy
    assert first.checksum != second.checksum


def test_legacy_newline_collision_cannot_authenticate_changed_current_definition(
    tmp_path,
):
    """A collision in the obsolete framing is not a compatibility credential."""

    path = tmp_path / "newline-collision.sqlite3"
    repository = SQLiteRepository(path)
    repository.initialize()
    repository.close()
    collision_a = Migration(1, "alpha\nbeta", ("gamma",))
    collision_b = Migration(1, "alpha", ("beta\ngamma",))
    obsolete_collision = hashlib.sha256(
        "1\nalpha\nbeta\ngamma".encode("utf-8")
    ).hexdigest()
    with sqlite3.connect(str(path)) as connection:
        connection.execute(
            "UPDATE schema_migrations SET checksum = ? WHERE version = 1",
            (obsolete_collision,),
        )
        connection.commit()
        before = connection.execute(
            "SELECT version, checksum, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
        with pytest.raises(MigrationChecksumError, match="checksum"):
            MigrationRunner(
                    (
                        collision_b,
                        V2_MIGRATION_METADATA,
                        V3_COLLECTION_LINEAGE,
                        V4_RESEARCH_RUNS,
                        V5_RESEARCH_RESULTS,
                        V6_COLLECTION_JOBS,
                        V7_FUNDAMENTAL_OBSERVATIONS,
                        V8_CORPORATE_ACTIONS_AND_ADJUSTMENT_FACTORS,
                        V9_POINT_IN_TIME_UNIVERSE_SNAPSHOTS,
                    )
            ).migrate(connection)
        after = connection.execute(
            "SELECT version, checksum, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()

    assert collision_a.checksum != collision_b.checksum
    assert after == before


@pytest.mark.parametrize(
    "replacement, message",
    (
        (
            "CREATE TABLE instruments ("
            "symbol INTEGER PRIMARY KEY, name TEXT NOT NULL, exchange TEXT NOT NULL, "
            "security_type TEXT NOT NULL, listed_at TEXT, lot_size INTEGER NOT NULL, "
            "price_tick TEXT NOT NULL, active INTEGER NOT NULL, "
            "metadata TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL)",
            "column contract",
        ),
        (
            "CREATE TABLE instruments ("
            "symbol TEXT, name TEXT NOT NULL, exchange TEXT NOT NULL, "
            "security_type TEXT NOT NULL, listed_at TEXT, lot_size INTEGER NOT NULL, "
            "price_tick TEXT NOT NULL, active INTEGER NOT NULL, "
            "metadata TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL, "
            "PRIMARY KEY (name))",
            "column contract",
        ),
        (
            "CREATE TABLE instruments ("
            "symbol TEXT PRIMARY KEY, name TEXT, exchange TEXT NOT NULL, "
            "security_type TEXT NOT NULL, listed_at TEXT, lot_size INTEGER NOT NULL, "
            "price_tick TEXT NOT NULL, active INTEGER NOT NULL, "
            "metadata TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL)",
            "column contract",
        ),
        (
            "CREATE TABLE instruments ("
            "symbol TEXT PRIMARY KEY, name TEXT NOT NULL, exchange TEXT NOT NULL, "
            "security_type TEXT NOT NULL, listed_at TEXT, lot_size INTEGER NOT NULL, "
            "price_tick TEXT NOT NULL, active INTEGER NOT NULL, "
            "metadata TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL)",
            "column contract",
        ),
    ),
)
def test_legacy_v1_rejects_wrong_type_pk_nullability_or_default(
    tmp_path, replacement, message
):
    path = tmp_path / "bad-table-contract.sqlite3"
    with sqlite3.connect(str(path)) as connection:
        connection.execute(replacement)
        for statement in V1_INITIAL_SCHEMA.statements:
            if "CREATE TABLE instruments" in statement:
                continue
            if "CREATE TABLE" in statement or "CREATE INDEX" in statement:
                connection.execute(statement)
        connection.execute("PRAGMA user_version = 1")

    repository = SQLiteRepository(path)
    with pytest.raises(InconsistentSchemaError, match=message):
        repository.initialize()
    assert repository.initialized is False


def test_existing_database_rejects_same_name_index_with_wrong_order(tmp_path):
    path = tmp_path / "bad-index-contract.sqlite3"
    repository = SQLiteRepository(path)
    repository.initialize()
    repository.close()
    with sqlite3.connect(str(path)) as connection:
        connection.execute("DROP INDEX idx_bars_symbol_interval_time")
        connection.execute(
            "CREATE INDEX idx_bars_symbol_interval_time "
            "ON bars(symbol, interval, timestamp ASC)"
        )

    reopened = SQLiteRepository(path)
    with pytest.raises(InconsistentSchemaError, match="index .* contract"):
        reopened.initialize()
    assert reopened.initialized is False


def test_existing_database_rejects_same_name_index_with_wrong_owner(tmp_path):
    path = tmp_path / "bad-index-owner.sqlite3"
    repository = SQLiteRepository(path)
    repository.initialize()
    repository.close()
    with sqlite3.connect(str(path)) as connection:
        connection.execute("DROP INDEX idx_alerts_time")
        connection.execute("CREATE INDEX idx_alerts_time ON signals(timestamp DESC)")

    reopened = SQLiteRepository(path)
    with pytest.raises(InconsistentSchemaError, match="index .* contract"):
        reopened.initialize()
    assert reopened.initialized is False


def test_existing_database_rejects_same_name_expression_index(tmp_path):
    path = tmp_path / "bad-expression-index.sqlite3"
    repository = SQLiteRepository(path)
    repository.initialize()
    repository.close()
    with sqlite3.connect(str(path)) as connection:
        connection.execute("DROP INDEX idx_alerts_time")
        connection.execute(
            "CREATE INDEX idx_alerts_time ON alerts(lower(timestamp) DESC)"
        )

    reopened = SQLiteRepository(path)
    with pytest.raises(InconsistentSchemaError, match="index .* contract"):
        reopened.initialize()
    assert reopened.initialized is False


def test_schema_migrations_contract_is_validated_before_rows_are_trusted(tmp_path):
    path = tmp_path / "bad-ledger.sqlite3"
    repository = SQLiteRepository(path)
    repository.initialize()
    repository.close()
    with sqlite3.connect(str(path)) as connection:
        rows = connection.execute(
            "SELECT version, checksum, applied_at FROM schema_migrations"
        ).fetchall()
        connection.execute("DROP TABLE schema_migrations")
        connection.execute(
            "CREATE TABLE schema_migrations ("
            "version TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO schema_migrations VALUES (?, ?, ?)", rows
        )

    reopened = SQLiteRepository(path)
    with pytest.raises(InconsistentSchemaError, match="column contract"):
        reopened.initialize()
    assert reopened.initialized is False


def test_concurrent_fresh_database_initialization_converges(tmp_path):
    path = tmp_path / "concurrent.sqlite3"
    barrier = threading.Barrier(2)
    failures = []

    def initialize():
        repository = SQLiteRepository(path)
        try:
            barrier.wait(timeout=5)
            repository.initialize()
        except BaseException as exc:  # captured for the parent assertion
            failures.append(exc)
        finally:
            repository.close()

    threads = [threading.Thread(target=initialize) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    version, rows = inspect_migrations(path)
    assert version == SQLiteRepository.SCHEMA_VERSION
    assert [row[0] for row in rows] == list(range(1, SQLiteRepository.SCHEMA_VERSION + 1))


def _create_modified_legacy_v1(path, transform):
    with sqlite3.connect(str(path)) as connection:
        for statement in V1_INITIAL_SCHEMA.statements:
            connection.execute(transform(statement))
        connection.execute("PRAGMA user_version = 1")
        connection.commit()


def test_legacy_v1_rejects_extra_source_check_before_minting_ledger(tmp_path):
    path = tmp_path / "legacy-extra-check.sqlite3"

    def add_check(statement):
        if "CREATE TABLE quotes" not in statement:
            return statement
        return statement.replace(
            "source TEXT NOT NULL,\n            PRIMARY KEY",
            "source TEXT NOT NULL CHECK(source <> 'blocked'),\n            PRIMARY KEY",
        )

    _create_modified_legacy_v1(path, add_check)
    repository = SQLiteRepository(path)
    with pytest.raises(InconsistentSchemaError, match="exact migration contract"):
        repository.initialize()
    with sqlite3.connect(str(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='schema_migrations'"
        ).fetchone() is None


@pytest.mark.parametrize(
    "suffix",
    (
        " STRICT",
        " WITHOUT ROWID",
    ),
)
def test_legacy_v1_rejects_extra_table_options(tmp_path, suffix):
    path = tmp_path / ("legacy-option-" + suffix.strip().replace(" ", "-") + ".sqlite3")

    def add_option(statement):
        if "CREATE TABLE bars" not in statement:
            return statement
        return statement.rstrip() + suffix

    _create_modified_legacy_v1(path, add_option)
    with pytest.raises(InconsistentSchemaError, match="exact migration contract"):
        SQLiteRepository(path).initialize()


def test_legacy_v1_rejects_extra_foreign_key(tmp_path):
    path = tmp_path / "legacy-extra-fk.sqlite3"

    def add_fk(statement):
        if "CREATE TABLE quotes" not in statement:
            return statement
        return statement.replace(
            "PRIMARY KEY (symbol, timestamp)",
            "PRIMARY KEY (symbol, timestamp), "
            "FOREIGN KEY (symbol) REFERENCES instruments(symbol)",
        )

    _create_modified_legacy_v1(path, add_fk)
    with pytest.raises(InconsistentSchemaError, match="exact migration contract"):
        SQLiteRepository(path).initialize()


@pytest.mark.parametrize(
    "statement",
    (
        "CREATE TABLE audit_shadow (id INTEGER PRIMARY KEY)",
        "CREATE VIEW quote_symbols AS SELECT symbol FROM quotes",
        "CREATE INDEX idx_quotes_source_unrecorded ON quotes(source)",
        "CREATE TRIGGER delete_new_quote AFTER INSERT ON quotes "
        "BEGIN DELETE FROM quotes WHERE rowid = NEW.rowid; END",
    ),
)
def test_legacy_v1_rejects_unrecorded_schema_objects(tmp_path, statement):
    path = tmp_path / "legacy-extra-object.sqlite3"
    create_legacy_v1(path)
    with sqlite3.connect(str(path)) as connection:
        connection.execute(statement)
        connection.commit()

    repository = SQLiteRepository(path)
    with pytest.raises(InconsistentSchemaError, match="extra"):
        repository.initialize()
    assert repository.initialized is False


def test_versioned_v2_rejects_unrecorded_trigger(tmp_path):
    path = tmp_path / "v2-extra-trigger.sqlite3"
    repository = SQLiteRepository(path)
    repository.initialize()
    repository.close()
    with sqlite3.connect(str(path)) as connection:
        connection.execute(
            "CREATE TRIGGER delete_migration_row AFTER INSERT ON schema_migrations "
            "BEGIN DELETE FROM schema_migrations WHERE version = NEW.version; END"
        )
        connection.commit()

    reopened = SQLiteRepository(path)
    with pytest.raises(InconsistentSchemaError, match="extra"):
        reopened.initialize()
    assert reopened.initialized is False


@pytest.mark.parametrize(
    "statement",
    (
        "CREATE TABLE v2_shadow (id INTEGER PRIMARY KEY)",
        "CREATE VIEW v2_quote_symbols AS SELECT symbol FROM quotes",
        "CREATE INDEX idx_v2_quotes_source ON quotes(source)",
    ),
)
def test_versioned_v2_rejects_other_unrecorded_schema_objects(tmp_path, statement):
    path = tmp_path / "v2-extra-object.sqlite3"
    repository = SQLiteRepository(path)
    repository.initialize()
    repository.close()
    with sqlite3.connect(str(path)) as connection:
        connection.execute(statement)
        connection.commit()

    reopened = SQLiteRepository(path)
    with pytest.raises(InconsistentSchemaError, match="extra"):
        reopened.initialize()
    assert reopened.initialized is False


def test_minimum_sqlite_version_has_clear_error(monkeypatch):
    from quant_monitor.storage.migrations import runner as runner_module

    monkeypatch.setattr(runner_module.sqlite3, "sqlite_version_info", (3, 29, 0))
    monkeypatch.setattr(runner_module.sqlite3, "sqlite_version", "3.29.0")
    with pytest.raises(SchemaMigrationError, match="SQLite 3.30.0 or newer"):
        MigrationRunner(DEFAULT_MIGRATIONS)


def test_unsupported_pragma_layout_has_clear_error():
    class _Rows:
        @staticmethod
        def fetchall():
            return [(0, "symbol", "TEXT", 1, None, 1)]

    class _Connection:
        @staticmethod
        def execute(_statement):
            return _Rows()

    with pytest.raises(SchemaMigrationError, match="unsupported column layout"):
        MigrationRunner._table_contract(_Connection(), "quotes")


def test_runner_preserves_native_lock_error_for_repository_retry(tmp_path):
    path = tmp_path / "locked.sqlite3"
    blocker = sqlite3.connect(str(path), timeout=0.01)
    contender = sqlite3.connect(str(path), timeout=0.01)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        contender.execute("PRAGMA busy_timeout = 1")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            MigrationRunner(DEFAULT_MIGRATIONS).migrate(contender)
        assert contender.in_transaction is False
    finally:
        blocker.rollback()
        contender.close()
        blocker.close()


def test_repository_retries_native_migration_lock(monkeypatch, tmp_path):
    from quant_monitor.storage import sqlite as sqlite_module

    calls = {"count": 0}
    original = sqlite_module.MigrationRunner.migrate

    def transient_lock(self, connection):
        calls["count"] += 1
        if calls["count"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return original(self, connection)

    monkeypatch.setattr(sqlite_module.MigrationRunner, "migrate", transient_lock)
    repository = SQLiteRepository(tmp_path / "retry-lock.sqlite3")
    repository.initialize()
    try:
        assert repository.initialized is True
        assert calls["count"] == 3
    finally:
        repository.close()


def test_repository_initialization_timeout_is_bounded_for_persistent_lock(
    monkeypatch, tmp_path
):
    """A runtime deadline must also bound the native SQLite migration worker."""

    calls = {"count": 0}

    def persistent_lock(self, connection):
        calls["count"] += 1
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(MigrationRunner, "migrate", persistent_lock)
    repository = SQLiteRepository(
        tmp_path / "bounded-lock.sqlite3",
        initialization_timeout_seconds=0.08,
    )
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="SQLite initialization exceeded"):
        repository.initialize()
    # The exact value depends on filesystem scheduling, but the provider must
    # not fall back to sqlite's historical 5-second busy timeout in bounded
    # startup mode.
    assert time.monotonic() - started < 1.0
    assert calls["count"] >= 1
