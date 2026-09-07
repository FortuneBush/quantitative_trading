"""Ordered schema definitions for the local SQLite repository."""

from __future__ import annotations

from .runner import (
    ColumnContract,
    IndexColumnContract,
    IndexContract,
    MIGRATION_LEDGER_CONTRACT,
    Migration,
    TableContract,
)


def _column(
    name: str,
    declared_type: str,
    *,
    not_null: bool = False,
    default=None,
    primary_key_ordinal: int = 0,
) -> ColumnContract:
    return ColumnContract(
        name,
        declared_type,
        not_null=not_null,
        default=default,
        primary_key_ordinal=primary_key_ordinal,
    )


def _index_column(name: str, *, descending: bool = False) -> IndexColumnContract:
    return IndexColumnContract(name, descending=descending)


V1_TABLES = (
    "instruments",
    "quotes",
    "bars",
    "metrics",
    "alerts",
    "signals",
)

V1_COLUMNS = (
    (
        "instruments",
        (
            "symbol", "name", "exchange", "security_type", "listed_at",
            "lot_size", "price_tick", "active", "metadata", "updated_at",
        ),
    ),
    (
        "quotes",
        (
            "symbol", "timestamp", "last", "open", "high", "low",
            "previous_close", "bid", "ask", "bid_size", "ask_size",
            "volume", "turnover", "source",
        ),
    ),
    (
        "bars",
        (
            "symbol", "interval", "timestamp", "open", "high", "low",
            "close", "volume", "turnover", "source",
        ),
    ),
    (
        "metrics",
        ("symbol", "name", "timestamp", "value", "window", "metadata"),
    ),
    (
        "alerts",
        (
            "id", "symbol", "timestamp", "level", "title", "message",
            "category", "acknowledged", "metadata",
        ),
    ),
    (
        "signals",
        (
            "id", "strategy", "symbol", "timestamp", "action", "strength",
            "price", "reason", "metadata",
        ),
    ),
)

V1_INDEXES = (
    "idx_quotes_symbol_time",
    "idx_bars_symbol_interval_time",
    "idx_metrics_symbol_name_time",
    "idx_alerts_time",
    "idx_alerts_symbol_time",
    "idx_signals_symbol_time",
    "idx_signals_strategy_time",
)


V1_TABLE_CONTRACTS = (
    TableContract(
        "instruments",
        (
            _column("symbol", "TEXT", primary_key_ordinal=1),
            _column("name", "TEXT", not_null=True),
            _column("exchange", "TEXT", not_null=True),
            _column("security_type", "TEXT", not_null=True),
            _column("listed_at", "TEXT"),
            _column("lot_size", "INTEGER", not_null=True),
            _column("price_tick", "TEXT", not_null=True),
            _column("active", "INTEGER", not_null=True),
            _column("metadata", "TEXT", not_null=True, default="'{}'"),
            _column("updated_at", "TEXT", not_null=True),
        ),
    ),
    TableContract(
        "quotes",
        (
            _column("symbol", "TEXT", not_null=True, primary_key_ordinal=1),
            _column("timestamp", "TEXT", not_null=True, primary_key_ordinal=2),
            _column("last", "TEXT", not_null=True),
            _column("open", "TEXT"),
            _column("high", "TEXT"),
            _column("low", "TEXT"),
            _column("previous_close", "TEXT"),
            _column("bid", "TEXT"),
            _column("ask", "TEXT"),
            _column("bid_size", "INTEGER", not_null=True, default="0"),
            _column("ask_size", "INTEGER", not_null=True, default="0"),
            _column("volume", "INTEGER", not_null=True, default="0"),
            _column("turnover", "TEXT", not_null=True, default="'0'"),
            _column("source", "TEXT", not_null=True),
        ),
    ),
    TableContract(
        "bars",
        (
            _column("symbol", "TEXT", not_null=True, primary_key_ordinal=1),
            _column("interval", "TEXT", not_null=True, primary_key_ordinal=2),
            _column("timestamp", "TEXT", not_null=True, primary_key_ordinal=3),
            _column("open", "TEXT", not_null=True),
            _column("high", "TEXT", not_null=True),
            _column("low", "TEXT", not_null=True),
            _column("close", "TEXT", not_null=True),
            _column("volume", "INTEGER", not_null=True, default="0"),
            _column("turnover", "TEXT", not_null=True, default="'0'"),
            _column("source", "TEXT", not_null=True),
        ),
    ),
    TableContract(
        "metrics",
        (
            _column("symbol", "TEXT", not_null=True, primary_key_ordinal=1),
            _column("name", "TEXT", not_null=True, primary_key_ordinal=2),
            _column("timestamp", "TEXT", not_null=True, primary_key_ordinal=3),
            _column("value", "REAL", not_null=True),
            _column("window", "INTEGER"),
            _column("metadata", "TEXT", not_null=True, default="'{}'"),
        ),
    ),
    TableContract(
        "alerts",
        (
            _column("id", "INTEGER", primary_key_ordinal=1),
            _column("symbol", "TEXT"),
            _column("timestamp", "TEXT", not_null=True),
            _column("level", "TEXT", not_null=True),
            _column("title", "TEXT", not_null=True),
            _column("message", "TEXT", not_null=True),
            _column("category", "TEXT", not_null=True),
            _column("acknowledged", "INTEGER", not_null=True, default="0"),
            _column("metadata", "TEXT", not_null=True, default="'{}'"),
        ),
        required_sql_fragments=("id INTEGER PRIMARY KEY AUTOINCREMENT",),
    ),
    TableContract(
        "signals",
        (
            _column("id", "INTEGER", primary_key_ordinal=1),
            _column("strategy", "TEXT", not_null=True),
            _column("symbol", "TEXT", not_null=True),
            _column("timestamp", "TEXT", not_null=True),
            _column("action", "TEXT", not_null=True),
            _column("strength", "REAL", not_null=True),
            _column("price", "TEXT"),
            _column("reason", "TEXT", not_null=True, default="''"),
            _column("metadata", "TEXT", not_null=True, default="'{}'"),
        ),
        required_sql_fragments=("id INTEGER PRIMARY KEY AUTOINCREMENT",),
    ),
)

V1_INDEX_CONTRACTS = (
    IndexContract(
        "idx_quotes_symbol_time",
        "quotes",
        (_index_column("symbol"), _index_column("timestamp", descending=True)),
    ),
    IndexContract(
        "idx_bars_symbol_interval_time",
        "bars",
        (
            _index_column("symbol"),
            _index_column("interval"),
            _index_column("timestamp", descending=True),
        ),
    ),
    IndexContract(
        "idx_metrics_symbol_name_time",
        "metrics",
        (
            _index_column("symbol"),
            _index_column("name"),
            _index_column("timestamp", descending=True),
        ),
    ),
    IndexContract(
        "idx_alerts_time",
        "alerts",
        (_index_column("timestamp", descending=True),),
    ),
    IndexContract(
        "idx_alerts_symbol_time",
        "alerts",
        (_index_column("symbol"), _index_column("timestamp", descending=True)),
    ),
    IndexContract(
        "idx_signals_symbol_time",
        "signals",
        (_index_column("symbol"), _index_column("timestamp", descending=True)),
    ),
    IndexContract(
        "idx_signals_strategy_time",
        "signals",
        (_index_column("strategy"), _index_column("timestamp", descending=True)),
    ),
)


V1_INITIAL_SCHEMA = Migration(
    version=1,
    name="initial_market_monitor_schema",
    required_tables=V1_TABLES,
    required_columns=V1_COLUMNS,
    required_indexes=V1_INDEXES,
    table_contracts=V1_TABLE_CONTRACTS,
    index_contracts=V1_INDEX_CONTRACTS,
    statements=(
        """
        CREATE TABLE instruments (
            symbol TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            exchange TEXT NOT NULL,
            security_type TEXT NOT NULL,
            listed_at TEXT,
            lot_size INTEGER NOT NULL,
            price_tick TEXT NOT NULL,
            active INTEGER NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE quotes (
            symbol TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            last TEXT NOT NULL,
            open TEXT,
            high TEXT,
            low TEXT,
            previous_close TEXT,
            bid TEXT,
            ask TEXT,
            bid_size INTEGER NOT NULL DEFAULT 0,
            ask_size INTEGER NOT NULL DEFAULT 0,
            volume INTEGER NOT NULL DEFAULT 0,
            turnover TEXT NOT NULL DEFAULT '0',
            source TEXT NOT NULL,
            PRIMARY KEY (symbol, timestamp)
        )
        """,
        "CREATE INDEX idx_quotes_symbol_time ON quotes(symbol, timestamp DESC)",
        """
        CREATE TABLE bars (
            symbol TEXT NOT NULL,
            interval TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            open TEXT NOT NULL,
            high TEXT NOT NULL,
            low TEXT NOT NULL,
            close TEXT NOT NULL,
            volume INTEGER NOT NULL DEFAULT 0,
            turnover TEXT NOT NULL DEFAULT '0',
            source TEXT NOT NULL,
            PRIMARY KEY (symbol, interval, timestamp)
        )
        """,
        """
        CREATE INDEX idx_bars_symbol_interval_time
        ON bars(symbol, interval, timestamp DESC)
        """,
        """
        CREATE TABLE metrics (
            symbol TEXT NOT NULL,
            name TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            value REAL NOT NULL,
            window INTEGER,
            metadata TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (symbol, name, timestamp)
        )
        """,
        """
        CREATE INDEX idx_metrics_symbol_name_time
        ON metrics(symbol, name, timestamp DESC)
        """,
        """
        CREATE TABLE alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            timestamp TEXT NOT NULL,
            level TEXT NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            category TEXT NOT NULL,
            acknowledged INTEGER NOT NULL DEFAULT 0,
            metadata TEXT NOT NULL DEFAULT '{}'
        )
        """,
        "CREATE INDEX idx_alerts_time ON alerts(timestamp DESC)",
        """
        CREATE INDEX idx_alerts_symbol_time
        ON alerts(symbol, timestamp DESC)
        """,
        """
        CREATE TABLE signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            action TEXT NOT NULL,
            strength REAL NOT NULL,
            price TEXT,
            reason TEXT NOT NULL DEFAULT '',
            metadata TEXT NOT NULL DEFAULT '{}'
        )
        """,
        """
        CREATE INDEX idx_signals_symbol_time
        ON signals(symbol, timestamp DESC)
        """,
        """
        CREATE INDEX idx_signals_strategy_time
        ON signals(strategy, timestamp DESC)
        """,
    ),
)


V2_MIGRATION_METADATA = Migration(
    version=2,
    name="migration_metadata_foundation",
    required_tables=("schema_migrations",),
    required_columns=(
        ("schema_migrations", ("version", "checksum", "applied_at")),
    ),
    table_contracts=(MIGRATION_LEDGER_CONTRACT,),
    statements=(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            checksum TEXT NOT NULL CHECK(length(checksum) = 64),
            applied_at TEXT NOT NULL
        )
        """,
    ),
)


V3_COLLECTION_LINEAGE = Migration(
    version=3,
    name="collection_batch_and_bar_observation_lineage",
    required_tables=("collection_batches", "bar_observations"),
    required_columns=(
        (
            "collection_batches",
            (
                "batch_id", "provider", "request_kind", "input_hash", "status",
                "started_at", "completed_at", "item_count", "error_type",
                "error_message", "metadata",
            ),
        ),
        (
            "bar_observations",
            (
                "batch_id", "symbol", "interval", "session_date", "period_start",
                "period_end", "available_at", "received_at", "persisted_at",
                "source", "adjustment", "revision_id", "quality_status", "open",
                "high", "low", "close", "volume", "turnover",
            ),
        ),
    ),
    required_indexes=(
        "idx_collection_batches_status_started",
        "idx_bar_observations_lookup",
        "idx_bar_observations_batch",
    ),
    table_contracts=(
        TableContract(
            "collection_batches",
            (
                _column("batch_id", "TEXT", primary_key_ordinal=1),
                _column("provider", "TEXT", not_null=True),
                _column("request_kind", "TEXT", not_null=True),
                _column("input_hash", "TEXT", not_null=True),
                _column("status", "TEXT", not_null=True),
                _column("started_at", "TEXT", not_null=True),
                _column("completed_at", "TEXT"),
                _column("item_count", "INTEGER", not_null=True, default="0"),
                _column("error_type", "TEXT"),
                _column("error_message", "TEXT"),
                _column("metadata", "TEXT", not_null=True, default="'{}'"),
            ),
            required_sql_fragments=(
                "CHECK(length(input_hash) = 64)",
                "CHECK(status IN ('running','succeeded','failed','cancelled','abandoned'))",
                "CHECK(item_count >= 0)",
                "CHECK((status = 'running' AND completed_at IS NULL AND error_type IS NULL AND error_message IS NULL) OR (status = 'succeeded' AND completed_at IS NOT NULL AND error_type IS NULL AND error_message IS NULL) OR (status = 'cancelled' AND completed_at IS NOT NULL AND error_type IS NULL AND error_message IS NULL) OR (status IN ('failed','abandoned') AND completed_at IS NOT NULL AND error_type IS NOT NULL))",
                "CHECK(completed_at IS NULL OR completed_at >= started_at)",
            ),
        ),
        TableContract(
            "bar_observations",
            (
                _column("batch_id", "TEXT", not_null=True, primary_key_ordinal=1),
                _column("symbol", "TEXT", not_null=True, primary_key_ordinal=2),
                _column("interval", "TEXT", not_null=True, primary_key_ordinal=3),
                _column("session_date", "TEXT", not_null=True, primary_key_ordinal=4),
                _column("period_start", "TEXT", not_null=True),
                _column("period_end", "TEXT", not_null=True, primary_key_ordinal=5),
                _column("available_at", "TEXT"),
                _column("received_at", "TEXT", not_null=True),
                _column("persisted_at", "TEXT", not_null=True),
                _column("source", "TEXT", not_null=True, primary_key_ordinal=6),
                _column("adjustment", "TEXT", not_null=True, primary_key_ordinal=7),
                _column("revision_id", "TEXT", not_null=True, primary_key_ordinal=8),
                _column("quality_status", "TEXT", not_null=True),
                _column("open", "TEXT", not_null=True),
                _column("high", "TEXT", not_null=True),
                _column("low", "TEXT", not_null=True),
                _column("close", "TEXT", not_null=True),
                _column("volume", "INTEGER", not_null=True, default="0"),
                _column("turnover", "TEXT", not_null=True, default="'0'"),
            ),
            required_sql_fragments=(
                "CHECK(period_end > period_start)",
                "CHECK(available_at IS NULL OR available_at >= period_end)",
                "CHECK(available_at IS NULL OR available_at <= received_at)",
                "CHECK(persisted_at >= received_at)",
                "CHECK(adjustment IN ('raw','qfq','hfq'))",
                "CHECK(quality_status IN ('accepted','degraded','quarantined','untrusted_legacy'))",
                "FOREIGN KEY(batch_id) REFERENCES collection_batches(batch_id)",
            ),
        ),
    ),
    index_contracts=(
        IndexContract(
            "idx_collection_batches_status_started",
            "collection_batches",
            (_index_column("status"), _index_column("started_at", descending=True)),
        ),
        IndexContract(
            "idx_bar_observations_lookup",
            "bar_observations",
            (
                _index_column("symbol"),
                _index_column("interval"),
                _index_column("period_end", descending=True),
                _index_column("source"),
                _index_column("adjustment"),
            ),
        ),
        IndexContract(
            "idx_bar_observations_batch",
            "bar_observations",
            (_index_column("batch_id"),),
        ),
    ),
    statements=(
        """
        CREATE TABLE collection_batches (
            batch_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            request_kind TEXT NOT NULL,
            input_hash TEXT NOT NULL CHECK(length(input_hash) = 64),
            status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','cancelled','abandoned')),
            started_at TEXT NOT NULL,
            completed_at TEXT,
            item_count INTEGER NOT NULL DEFAULT 0 CHECK(item_count >= 0),
            error_type TEXT,
            error_message TEXT,
            metadata TEXT NOT NULL DEFAULT '{}',
            CHECK((status = 'running' AND completed_at IS NULL AND error_type IS NULL AND error_message IS NULL) OR (status = 'succeeded' AND completed_at IS NOT NULL AND error_type IS NULL AND error_message IS NULL) OR (status = 'cancelled' AND completed_at IS NOT NULL AND error_type IS NULL AND error_message IS NULL) OR (status IN ('failed','abandoned') AND completed_at IS NOT NULL AND error_type IS NOT NULL)),
            CHECK(completed_at IS NULL OR completed_at >= started_at)
        )
        """,
        """
        CREATE INDEX idx_collection_batches_status_started
        ON collection_batches(status, started_at DESC)
        """,
        """
        CREATE TABLE bar_observations (
            batch_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            interval TEXT NOT NULL,
            session_date TEXT NOT NULL,
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            available_at TEXT,
            received_at TEXT NOT NULL,
            persisted_at TEXT NOT NULL,
            source TEXT NOT NULL,
            adjustment TEXT NOT NULL CHECK(adjustment IN ('raw','qfq','hfq')),
            revision_id TEXT NOT NULL,
            quality_status TEXT NOT NULL CHECK(quality_status IN ('accepted','degraded','quarantined','untrusted_legacy')),
            open TEXT NOT NULL,
            high TEXT NOT NULL,
            low TEXT NOT NULL,
            close TEXT NOT NULL,
            volume INTEGER NOT NULL DEFAULT 0 CHECK(volume >= 0),
            turnover TEXT NOT NULL DEFAULT '0',
            PRIMARY KEY (
                batch_id, symbol, interval, session_date, period_end,
                source, adjustment, revision_id
            ),
            FOREIGN KEY(batch_id) REFERENCES collection_batches(batch_id),
            CHECK(period_end > period_start),
            CHECK(available_at IS NULL OR available_at >= period_end),
            CHECK(available_at IS NULL OR available_at <= received_at),
            CHECK(persisted_at >= received_at)
        )
        """,
        """
        CREATE INDEX idx_bar_observations_lookup
        ON bar_observations(symbol, interval, period_end DESC, source, adjustment)
        """,
        """
        CREATE INDEX idx_bar_observations_batch
        ON bar_observations(batch_id)
        """,
    ),
)


DEFAULT_MIGRATIONS = (V1_INITIAL_SCHEMA, V2_MIGRATION_METADATA, V3_COLLECTION_LINEAGE)
SCHEMA_VERSION = DEFAULT_MIGRATIONS[-1].version


__all__ = [
    "DEFAULT_MIGRATIONS",
    "SCHEMA_VERSION",
    "V1_INITIAL_SCHEMA",
    "V1_COLUMNS",
    "V1_INDEXES",
    "V1_INDEX_CONTRACTS",
    "V1_TABLE_CONTRACTS",
    "V1_TABLES",
    "V2_MIGRATION_METADATA",
    "V3_COLLECTION_LINEAGE",
]
