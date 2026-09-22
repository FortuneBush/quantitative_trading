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


V4_RESEARCH_RUNS = Migration(
    version=4,
    name="immutable_research_run_manifests",
    required_tables=("research_runs",),
    required_columns=(
        (
            "research_runs",
            (
                "run_id", "content_hash", "manifest_json", "manifest_json_hash",
                "manifest_schema_version", "strategy_name", "strategy_code_version",
                "dataset_name", "dataset_source", "input_hash", "calendar_version",
                "universe_version", "adjustment_version", "constraint_version",
                "created_at",
            ),
        ),
    ),
    required_indexes=("idx_research_runs_created", "idx_research_runs_strategy_created"),
    table_contracts=(
        TableContract(
            "research_runs",
            (
                _column("run_id", "TEXT", primary_key_ordinal=1),
                _column("content_hash", "TEXT", not_null=True),
                _column("manifest_json", "TEXT", not_null=True),
                _column("manifest_json_hash", "TEXT", not_null=True),
                _column("manifest_schema_version", "TEXT", not_null=True),
                _column("strategy_name", "TEXT", not_null=True),
                _column("strategy_code_version", "TEXT", not_null=True),
                _column("dataset_name", "TEXT", not_null=True),
                _column("dataset_source", "TEXT", not_null=True),
                _column("input_hash", "TEXT", not_null=True),
                _column("calendar_version", "TEXT", not_null=True),
                _column("universe_version", "TEXT", not_null=True),
                _column("adjustment_version", "TEXT", not_null=True),
                _column("constraint_version", "TEXT", not_null=True),
                _column("created_at", "TEXT", not_null=True),
            ),
            required_sql_fragments=(
                "CHECK(length(content_hash) = 71)",
                "CHECK(length(manifest_json_hash) = 71)",
                "CHECK(length(input_hash) = 71)",
                "CHECK(length(manifest_json) > 0)",
                "CHECK(length(created_at) > 0)",
            ),
        ),
    ),
    index_contracts=(
        IndexContract(
            "idx_research_runs_created",
            "research_runs",
            (_index_column("created_at", descending=True), _index_column("run_id")),
        ),
        IndexContract(
            "idx_research_runs_strategy_created",
            "research_runs",
            (
                _index_column("strategy_name"),
                _index_column("created_at", descending=True),
                _index_column("run_id"),
            ),
        ),
    ),
    statements=(
        """
        CREATE TABLE research_runs (
            run_id TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL CHECK(length(content_hash) = 71),
            manifest_json TEXT NOT NULL CHECK(length(manifest_json) > 0),
            manifest_json_hash TEXT NOT NULL CHECK(length(manifest_json_hash) = 71),
            manifest_schema_version TEXT NOT NULL,
            strategy_name TEXT NOT NULL,
            strategy_code_version TEXT NOT NULL,
            dataset_name TEXT NOT NULL,
            dataset_source TEXT NOT NULL,
            input_hash TEXT NOT NULL CHECK(length(input_hash) = 71),
            calendar_version TEXT NOT NULL,
            universe_version TEXT NOT NULL,
            adjustment_version TEXT NOT NULL,
            constraint_version TEXT NOT NULL,
            created_at TEXT NOT NULL CHECK(length(created_at) > 0)
        )
        """,
        """
        CREATE INDEX idx_research_runs_created
        ON research_runs(created_at DESC, run_id)
        """,
        """
        CREATE INDEX idx_research_runs_strategy_created
        ON research_runs(strategy_name, created_at DESC, run_id)
        """,
        """
        CREATE TRIGGER research_runs_immutable_update
        BEFORE UPDATE ON research_runs
        BEGIN
            SELECT RAISE(ABORT, 'research_runs are immutable');
        END
        """,
        """
        CREATE TRIGGER research_runs_immutable_delete
        BEFORE DELETE ON research_runs
        BEGIN
            SELECT RAISE(ABORT, 'research_runs are immutable');
        END
        """,
    ),
)

V5_RESEARCH_RESULTS = Migration(
    version=5,
    name="immutable_research_result_artifacts",
    required_tables=("research_results",),
    required_columns=(
        (
            "research_results",
            ("run_id", "result_hash", "result_json", "result_json_hash", "created_at"),
        ),
    ),
    required_indexes=("idx_research_results_created",),
    table_contracts=(
        TableContract(
            "research_results",
            (
                _column("run_id", "TEXT", primary_key_ordinal=1),
                _column("result_hash", "TEXT", not_null=True),
                _column("result_json", "TEXT", not_null=True),
                _column("result_json_hash", "TEXT", not_null=True),
                _column("created_at", "TEXT", not_null=True),
            ),
            required_sql_fragments=(
                "CHECK(length(result_hash) = 71)",
                "CHECK(length(result_json_hash) = 71)",
                "CHECK(length(result_json) > 0)",
                "CHECK(length(created_at) > 0)",
            ),
        ),
    ),
    index_contracts=(
        IndexContract(
            "idx_research_results_created",
            "research_results",
            (_index_column("created_at", descending=True), _index_column("run_id")),
        ),
    ),
    statements=(
        """
        CREATE TABLE research_results (
            run_id TEXT PRIMARY KEY,
            result_hash TEXT NOT NULL CHECK(length(result_hash) = 71),
            result_json TEXT NOT NULL CHECK(length(result_json) > 0),
            result_json_hash TEXT NOT NULL CHECK(length(result_json_hash) = 71),
            created_at TEXT NOT NULL CHECK(length(created_at) > 0)
        )
        """,
        """
        CREATE INDEX idx_research_results_created
        ON research_results(created_at DESC, run_id)
        """,
        """
        CREATE TRIGGER research_results_immutable_update
        BEFORE UPDATE ON research_results
        BEGIN
            SELECT RAISE(ABORT, 'research_results are immutable');
        END
        """,
        """
        CREATE TRIGGER research_results_immutable_delete
        BEFORE DELETE ON research_results
        BEGIN
            SELECT RAISE(ABORT, 'research_results are immutable');
        END
        """,
    ),
)


V6_COLLECTION_JOBS = Migration(
    version=6,
    name="durable_collection_job_idempotency",
    required_tables=("collector_jobs",),
    required_columns=(
        (
            "collector_jobs",
            (
                "job_id", "idempotency_key_digest", "status", "requested_symbols",
                "created_at", "started_at", "completed_at", "iteration_before",
                "iteration_after", "quotes_count", "bars_count", "metrics_count",
                "signals_count", "error_type",
            ),
        ),
    ),
    required_indexes=("idx_collector_jobs_created", "idx_collector_jobs_idempotency"),
    table_contracts=(
        TableContract(
            "collector_jobs",
            (
                _column("job_id", "TEXT", primary_key_ordinal=1),
                _column("idempotency_key_digest", "TEXT"),
                _column("status", "TEXT", not_null=True),
                _column("requested_symbols", "TEXT", not_null=True),
                _column("created_at", "TEXT", not_null=True),
                _column("started_at", "TEXT"),
                _column("completed_at", "TEXT"),
                _column("iteration_before", "INTEGER"),
                _column("iteration_after", "INTEGER"),
                _column("quotes_count", "INTEGER", not_null=True, default="0"),
                _column("bars_count", "INTEGER", not_null=True, default="0"),
                _column("metrics_count", "INTEGER", not_null=True, default="0"),
                _column("signals_count", "INTEGER", not_null=True, default="0"),
                _column("error_type", "TEXT"),
            ),
            required_sql_fragments=(
                "CHECK(status IN ('queued','running','cancelling','succeeded','failed','cancelled'))",
                "CHECK(idempotency_key_digest IS NULL OR length(idempotency_key_digest) = 64)",
                "CHECK(length(requested_symbols) > 0)",
                "CHECK(quotes_count >= 0)",
                "CHECK(bars_count >= 0)",
                "CHECK(metrics_count >= 0)",
                "CHECK(signals_count >= 0)",
                "CHECK((status IN ('queued','running','cancelling') AND completed_at IS NULL) OR (status IN ('succeeded','failed','cancelled') AND completed_at IS NOT NULL))",
                "CHECK(completed_at IS NULL OR completed_at >= created_at)",
            ),
        ),
    ),
    index_contracts=(
        IndexContract(
            "idx_collector_jobs_created",
            "collector_jobs",
            (_index_column("created_at", descending=True), _index_column("job_id")),
        ),
        IndexContract(
            "idx_collector_jobs_idempotency",
            "collector_jobs",
            (_index_column("idempotency_key_digest"),),
            unique=True,
        ),
    ),
    statements=(
        """
        CREATE TABLE collector_jobs (
            job_id TEXT PRIMARY KEY,
            idempotency_key_digest TEXT UNIQUE CHECK(idempotency_key_digest IS NULL OR length(idempotency_key_digest) = 64),
            status TEXT NOT NULL CHECK(status IN ('queued','running','cancelling','succeeded','failed','cancelled')),
            requested_symbols TEXT NOT NULL CHECK(length(requested_symbols) > 0),
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            iteration_before INTEGER,
            iteration_after INTEGER,
            quotes_count INTEGER NOT NULL DEFAULT 0 CHECK(quotes_count >= 0),
            bars_count INTEGER NOT NULL DEFAULT 0 CHECK(bars_count >= 0),
            metrics_count INTEGER NOT NULL DEFAULT 0 CHECK(metrics_count >= 0),
            signals_count INTEGER NOT NULL DEFAULT 0 CHECK(signals_count >= 0),
            error_type TEXT,
            CHECK((status IN ('queued','running','cancelling') AND completed_at IS NULL) OR (status IN ('succeeded','failed','cancelled') AND completed_at IS NOT NULL)),
            CHECK(completed_at IS NULL OR completed_at >= created_at)
        )
        """,
        """
        CREATE INDEX idx_collector_jobs_created
        ON collector_jobs(created_at DESC, job_id)
        """,
        """
        CREATE UNIQUE INDEX idx_collector_jobs_idempotency
        ON collector_jobs(idempotency_key_digest)
        """,
    ),
)


V7_FUNDAMENTAL_OBSERVATIONS = Migration(
    version=7,
    name="immutable_point_in_time_fundamental_observations",
    required_tables=("fundamental_observations",),
    required_columns=(
        (
            "fundamental_observations",
            (
                "observation_id", "symbol", "metric", "value", "unit", "scope",
                "period_start", "period_end", "available_at", "received_at",
                "source", "revision_id", "metadata", "content_hash",
            ),
        ),
    ),
    required_indexes=("idx_fundamentals_lookup", "idx_fundamentals_source"),
    table_contracts=(
        TableContract(
            "fundamental_observations",
            (
                _column("observation_id", "TEXT", primary_key_ordinal=1),
                _column("symbol", "TEXT", not_null=True),
                _column("metric", "TEXT", not_null=True),
                _column("value", "TEXT"),
                _column("unit", "TEXT", not_null=True),
                _column("scope", "TEXT", not_null=True),
                _column("period_start", "TEXT", not_null=True),
                _column("period_end", "TEXT", not_null=True),
                _column("available_at", "TEXT", not_null=True),
                _column("received_at", "TEXT", not_null=True),
                _column("source", "TEXT", not_null=True),
                _column("revision_id", "TEXT", not_null=True),
                _column("metadata", "TEXT", not_null=True, default="'{}'"),
                _column("content_hash", "TEXT", not_null=True),
            ),
            required_sql_fragments=(
                "CHECK(value IS NULL OR length(value) > 0)",
                "CHECK(scope IN ('consolidated','standalone','unknown'))",
                "CHECK(period_end >= period_start)",
                "CHECK(available_at <= received_at)",
                "CHECK(length(content_hash) = 64)",
            ),
        ),
    ),
    index_contracts=(
        IndexContract(
            "idx_fundamentals_lookup",
            "fundamental_observations",
            (
                _index_column("symbol"),
                _index_column("metric"),
                _index_column("period_end", descending=True),
                _index_column("available_at", descending=True),
            ),
        ),
        IndexContract(
            "idx_fundamentals_source",
            "fundamental_observations",
            (_index_column("source"), _index_column("received_at", descending=True)),
        ),
    ),
    statements=(
        """
        CREATE TABLE fundamental_observations (
            observation_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            metric TEXT NOT NULL,
            value TEXT CHECK(value IS NULL OR length(value) > 0),
            unit TEXT NOT NULL,
            scope TEXT NOT NULL CHECK(scope IN ('consolidated','standalone','unknown')),
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            available_at TEXT NOT NULL,
            received_at TEXT NOT NULL,
            source TEXT NOT NULL,
            revision_id TEXT NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}',
            content_hash TEXT NOT NULL CHECK(length(content_hash) = 64),
            CHECK(period_end >= period_start),
            CHECK(available_at <= received_at)
        )
        """,
        """
        CREATE INDEX idx_fundamentals_lookup
        ON fundamental_observations(symbol, metric, period_end DESC, available_at DESC)
        """,
        """
        CREATE INDEX idx_fundamentals_source
        ON fundamental_observations(source, received_at DESC)
        """,
        """
        CREATE TRIGGER fundamental_observations_immutable_update
        BEFORE UPDATE ON fundamental_observations
        BEGIN
            SELECT RAISE(ABORT, 'fundamental_observations are immutable');
        END
        """,
        """
        CREATE TRIGGER fundamental_observations_immutable_delete
        BEFORE DELETE ON fundamental_observations
        BEGIN
            SELECT RAISE(ABORT, 'fundamental_observations are immutable');
        END
        """,
    ),
)


V8_CORPORATE_ACTIONS_AND_ADJUSTMENT_FACTORS = Migration(
    version=8,
    name="immutable_corporate_actions_and_adjustment_factors",
    required_tables=("corporate_actions", "adjustment_factors"),
    required_columns=(
        (
            "corporate_actions",
            (
                "action_id", "symbol", "action_type", "ex_date",
                "announcement_date", "record_date", "payable_date",
                "cash_amount", "share_ratio", "subscription_price", "source",
                "revision_id", "received_at", "metadata", "content_hash",
            ),
        ),
        (
            "adjustment_factors",
            (
                "factor_id", "symbol", "adjustment", "session_date",
                "benchmark_date", "factor", "factor_version", "source",
                "revision_id", "input_hash", "computed_at", "content_hash",
            ),
        ),
    ),
    required_indexes=(
        "idx_corporate_actions_symbol_ex_date",
        "idx_corporate_actions_source_received",
        "idx_adjustment_factors_lookup",
        "idx_adjustment_factors_source_computed",
    ),
    table_contracts=(
        TableContract(
            "corporate_actions",
            (
                _column("action_id", "TEXT", primary_key_ordinal=1),
                _column("symbol", "TEXT", not_null=True),
                _column("action_type", "TEXT", not_null=True),
                _column("ex_date", "TEXT", not_null=True),
                _column("announcement_date", "TEXT"),
                _column("record_date", "TEXT"),
                _column("payable_date", "TEXT"),
                _column("cash_amount", "TEXT"),
                _column("share_ratio", "TEXT"),
                _column("subscription_price", "TEXT"),
                _column("source", "TEXT", not_null=True),
                _column("revision_id", "TEXT", not_null=True),
                _column("received_at", "TEXT", not_null=True),
                _column("metadata", "TEXT", not_null=True, default="'{}'"),
                _column("content_hash", "TEXT", not_null=True),
            ),
            required_sql_fragments=("CHECK(length(content_hash) = 64)",),
        ),
        TableContract(
            "adjustment_factors",
            (
                _column("factor_id", "TEXT", primary_key_ordinal=1),
                _column("symbol", "TEXT", not_null=True),
                _column("adjustment", "TEXT", not_null=True),
                _column("session_date", "TEXT", not_null=True),
                _column("benchmark_date", "TEXT", not_null=True),
                _column("factor", "TEXT", not_null=True),
                _column("factor_version", "TEXT", not_null=True),
                _column("source", "TEXT", not_null=True),
                _column("revision_id", "TEXT", not_null=True),
                _column("input_hash", "TEXT", not_null=True),
                _column("computed_at", "TEXT", not_null=True),
                _column("content_hash", "TEXT", not_null=True),
            ),
            required_sql_fragments=(
                "CHECK(length(input_hash) = 64)",
                "CHECK(length(content_hash) = 64)",
            ),
        ),
    ),
    index_contracts=(
        IndexContract(
            "idx_corporate_actions_symbol_ex_date",
            "corporate_actions",
            (_index_column("symbol"), _index_column("ex_date", descending=True), _index_column("received_at", descending=True)),
        ),
        IndexContract(
            "idx_corporate_actions_source_received",
            "corporate_actions",
            (_index_column("source"), _index_column("received_at", descending=True)),
        ),
        IndexContract(
            "idx_adjustment_factors_lookup",
            "adjustment_factors",
            (_index_column("symbol"), _index_column("adjustment"), _index_column("session_date", descending=True), _index_column("computed_at", descending=True)),
        ),
        IndexContract(
            "idx_adjustment_factors_source_computed",
            "adjustment_factors",
            (_index_column("source"), _index_column("computed_at", descending=True)),
        ),
    ),
    statements=(
        """
        CREATE TABLE corporate_actions (
            action_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            action_type TEXT NOT NULL CHECK(action_type IN ('cash_dividend','stock_dividend','stock_split','reverse_split','rights_issue')),
            ex_date TEXT NOT NULL,
            announcement_date TEXT,
            record_date TEXT,
            payable_date TEXT,
            cash_amount TEXT,
            share_ratio TEXT,
            subscription_price TEXT,
            source TEXT NOT NULL,
            revision_id TEXT NOT NULL,
            received_at TEXT NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}',
            content_hash TEXT NOT NULL CHECK(length(content_hash) = 64),
            CHECK(announcement_date IS NULL OR announcement_date <= ex_date),
            CHECK(record_date IS NULL OR announcement_date IS NULL OR record_date >= announcement_date),
            CHECK(payable_date IS NULL OR payable_date >= ex_date)
        )
        """,
        """
        CREATE INDEX idx_corporate_actions_symbol_ex_date
        ON corporate_actions(symbol, ex_date DESC, received_at DESC)
        """,
        """
        CREATE INDEX idx_corporate_actions_source_received
        ON corporate_actions(source, received_at DESC)
        """,
        """
        CREATE TRIGGER corporate_actions_immutable_update
        BEFORE UPDATE ON corporate_actions
        BEGIN
            SELECT RAISE(ABORT, 'corporate_actions are immutable');
        END
        """,
        """
        CREATE TRIGGER corporate_actions_immutable_delete
        BEFORE DELETE ON corporate_actions
        BEGIN
            SELECT RAISE(ABORT, 'corporate_actions are immutable');
        END
        """,
        """
        CREATE TABLE adjustment_factors (
            factor_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            adjustment TEXT NOT NULL CHECK(adjustment IN ('raw','qfq','hfq')),
            session_date TEXT NOT NULL,
            benchmark_date TEXT NOT NULL,
            factor TEXT NOT NULL,
            factor_version TEXT NOT NULL,
            source TEXT NOT NULL,
            revision_id TEXT NOT NULL,
            input_hash TEXT NOT NULL CHECK(length(input_hash) = 64),
            computed_at TEXT NOT NULL,
            content_hash TEXT NOT NULL CHECK(length(content_hash) = 64)
        )
        """,
        """
        CREATE INDEX idx_adjustment_factors_lookup
        ON adjustment_factors(symbol, adjustment, session_date DESC, computed_at DESC)
        """,
        """
        CREATE INDEX idx_adjustment_factors_source_computed
        ON adjustment_factors(source, computed_at DESC)
        """,
        """
        CREATE TRIGGER adjustment_factors_immutable_update
        BEFORE UPDATE ON adjustment_factors
        BEGIN
            SELECT RAISE(ABORT, 'adjustment_factors are immutable');
        END
        """,
        """
        CREATE TRIGGER adjustment_factors_immutable_delete
        BEFORE DELETE ON adjustment_factors
        BEGIN
            SELECT RAISE(ABORT, 'adjustment_factors are immutable');
        END
        """,
    ),
)


V9_POINT_IN_TIME_UNIVERSE_SNAPSHOTS = Migration(
    version=9,
    name="immutable_point_in_time_universe_snapshots",
    required_tables=("universe_snapshots",),
    required_columns=(
        (
            "universe_snapshots",
            (
                "snapshot_key", "universe", "session_date", "knowledge_cutoff",
                "version", "content_hash", "snapshot_json", "source",
                "recorded_at", "metadata",
            ),
        ),
    ),
    required_indexes=(
        "idx_universe_snapshots_lookup",
        "idx_universe_snapshots_source_recorded",
    ),
    table_contracts=(
        TableContract(
            "universe_snapshots",
            (
                _column("snapshot_key", "TEXT", primary_key_ordinal=1),
                _column("universe", "TEXT", not_null=True),
                _column("session_date", "TEXT", not_null=True),
                _column("knowledge_cutoff", "TEXT", not_null=True),
                _column("version", "TEXT", not_null=True),
                _column("content_hash", "TEXT", not_null=True),
                _column("snapshot_json", "TEXT", not_null=True),
                _column("source", "TEXT", not_null=True),
                _column("recorded_at", "TEXT", not_null=True),
                _column("metadata", "TEXT", not_null=True, default="'{}'"),
            ),
            required_sql_fragments=(
                "CHECK(length(snapshot_key) = 71)",
                "CHECK(length(content_hash) = 71)",
                "CHECK(length(snapshot_json) > 0)",
                "CHECK(knowledge_cutoff <= recorded_at)",
            ),
        ),
    ),
    index_contracts=(
        IndexContract(
            "idx_universe_snapshots_lookup",
            "universe_snapshots",
            (
                _index_column("universe"),
                _index_column("session_date"),
                _index_column("knowledge_cutoff", descending=True),
                _index_column("version"),
            ),
        ),
        IndexContract(
            "idx_universe_snapshots_source_recorded",
            "universe_snapshots",
            (_index_column("source"), _index_column("recorded_at", descending=True)),
        ),
    ),
    statements=(
        """
        CREATE TABLE universe_snapshots (
            snapshot_key TEXT PRIMARY KEY CHECK(length(snapshot_key) = 71),
            universe TEXT NOT NULL,
            session_date TEXT NOT NULL,
            knowledge_cutoff TEXT NOT NULL,
            version TEXT NOT NULL,
            content_hash TEXT NOT NULL CHECK(length(content_hash) = 71),
            snapshot_json TEXT NOT NULL CHECK(length(snapshot_json) > 0),
            source TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}',
            CHECK(knowledge_cutoff <= recorded_at)
        )
        """,
        """
        CREATE INDEX idx_universe_snapshots_lookup
        ON universe_snapshots(universe, session_date, knowledge_cutoff DESC, version)
        """,
        """
        CREATE INDEX idx_universe_snapshots_source_recorded
        ON universe_snapshots(source, recorded_at DESC)
        """,
        """
        CREATE TRIGGER universe_snapshots_immutable_update
        BEFORE UPDATE ON universe_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'universe_snapshots are immutable');
        END
        """,
        """
        CREATE TRIGGER universe_snapshots_immutable_delete
        BEFORE DELETE ON universe_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'universe_snapshots are immutable');
        END
        """,
    ),
)


DEFAULT_MIGRATIONS = (
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
    "V4_RESEARCH_RUNS",
    "V5_RESEARCH_RESULTS",
    "V6_COLLECTION_JOBS",
    "V7_FUNDAMENTAL_OBSERVATIONS",
    "V8_CORPORATE_ACTIONS_AND_ADJUSTMENT_FACTORS",
    "V9_POINT_IN_TIME_UNIVERSE_SNAPSHOTS",
]
