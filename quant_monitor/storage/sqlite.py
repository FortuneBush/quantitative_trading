"""SQLite persistence for market data, metrics, alerts and strategy signals."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from quant_monitor.domain import (
    Adjustment,
    Alert,
    AlertLevel,
    Bar,
    BarObservation,
    CollectionBatch,
    CollectionBatchStatus,
    Exchange,
    Instrument,
    Metric,
    ObservationQualityStatus,
    Quote,
    SecurityType,
    Signal,
    SignalAction,
    ensure_market_time,
    normalize_symbol,
    parse_market_time,
)
from quant_monitor.storage.migrations import (
    DEFAULT_MIGRATIONS,
    SCHEMA_VERSION as CURRENT_SCHEMA_VERSION,
    MigrationRunner,
)


PathLike = Union[str, Path]


def _iso(value: datetime) -> str:
    return ensure_market_time(value).isoformat(timespec="microseconds")


def _json(value: Any) -> str:
    payload = dict(value) if isinstance(value, Mapping) else (value or {})
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def _load_json(value: Optional[str]) -> Dict[str, Any]:
    if not value:
        return {}
    decoded = json.loads(value)
    return decoded if isinstance(decoded, dict) else {"value": decoded}


def _limit(value: int) -> int:
    normalized = int(value)
    if normalized <= 0:
        raise ValueError("limit must be positive")
    return normalized


class SQLiteRepository:
    """Small thread-safe repository optimized for a single local application.

    Call :meth:`initialize` once before use.  Disk databases use WAL mode, so the
    dashboard can read snapshots while the collector writes a new batch.
    """

    SCHEMA_VERSION = CURRENT_SCHEMA_VERSION

    def __init__(self, path: PathLike) -> None:
        self.path = str(path)
        self._connection: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()

    @property
    def initialized(self) -> bool:
        return self._connection is not None

    def initialize(self) -> None:
        with self._lock:
            if self._connection is not None:
                return
            if self.path != ":memory:":
                Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
            attempts = 3 if self.path != ":memory:" else 1
            for attempt in range(attempts):
                connection = sqlite3.connect(
                    self.path,
                    timeout=5.0,
                    check_same_thread=False,
                )
                connection.row_factory = sqlite3.Row
                try:
                    connection.execute("PRAGMA busy_timeout = 5000")
                    connection.execute("PRAGMA foreign_keys = ON")
                    if self.path != ":memory:":
                        # journal_mode itself takes a schema lock when a fresh
                        # database is initialized concurrently.
                        connection.execute("PRAGMA journal_mode = WAL")
                        connection.execute("PRAGMA synchronous = NORMAL")
                    # Migrations are the sole schema authority. Keeping DDL here
                    # would allow changes outside the checksummed ledger.
                    MigrationRunner(DEFAULT_MIGRATIONS).migrate(connection)
                except sqlite3.OperationalError as exc:
                    connection.close()
                    message = str(exc).casefold()
                    locked = "locked" in message or "busy" in message
                    if not locked or attempt + 1 >= attempts:
                        raise
                    time.sleep(0.05 * (attempt + 1))
                    continue
                except Exception:
                    connection.close()
                    raise
                self._connection = connection
                return

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def __enter__(self) -> "SQLiteRepository":
        self.initialize()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _conn(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("repository is not initialized")
        return self._connection

    def upsert_instruments(self, instruments: Sequence[Instrument]) -> int:
        if not instruments:
            return 0
        from quant_monitor.domain import now_market_time

        updated_at = _iso(now_market_time())
        rows = [
            (
                item.symbol,
                item.name,
                item.exchange.value,
                item.security_type.value,
                item.listed_at.isoformat() if item.listed_at else None,
                item.lot_size,
                str(item.price_tick),
                int(item.active),
                _json(item.metadata),
                updated_at,
            )
            for item in instruments
        ]
        with self._lock, self._conn():
            self._conn().executemany(
                """
                INSERT INTO instruments (
                    symbol, name, exchange, security_type, listed_at, lot_size,
                    price_tick, active, metadata, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    name=excluded.name, exchange=excluded.exchange,
                    security_type=excluded.security_type, listed_at=excluded.listed_at,
                    lot_size=excluded.lot_size, price_tick=excluded.price_tick,
                    active=excluded.active, metadata=excluded.metadata,
                    updated_at=excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def replace_instruments(self, instruments: Sequence[Instrument]) -> int:
        """Atomically replace the provider-neutral security master."""

        if not instruments:
            raise ValueError("replacement instrument catalogue cannot be empty")
        from quant_monitor.domain import now_market_time

        updated_at = _iso(now_market_time())
        rows = [
            (
                item.symbol,
                item.name,
                item.exchange.value,
                item.security_type.value,
                item.listed_at.isoformat() if item.listed_at else None,
                item.lot_size,
                str(item.price_tick),
                int(item.active),
                _json(item.metadata),
                updated_at,
            )
            for item in instruments
        ]
        with self._lock, self._conn():
            connection = self._conn()
            connection.execute("DELETE FROM instruments")
            connection.executemany(
                """
                INSERT INTO instruments (
                    symbol, name, exchange, security_type, listed_at, lot_size,
                    price_tick, active, metadata, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def list_instruments(self, active_only: bool = True) -> List[Instrument]:
        sql = "SELECT * FROM instruments"
        params: Tuple[Any, ...] = ()
        if active_only:
            sql += " WHERE active = ?"
            params = (1,)
        sql += " ORDER BY symbol"
        with self._lock:
            rows = self._conn().execute(sql, params).fetchall()
        return [self._row_to_instrument(row) for row in rows]

    @staticmethod
    def _row_to_instrument(row: sqlite3.Row) -> Instrument:
        return Instrument(
            symbol=row["symbol"],
            name=row["name"],
            exchange=Exchange(row["exchange"]),
            security_type=SecurityType(row["security_type"]),
            listed_at=datetime.fromisoformat(row["listed_at"]).date()
            if row["listed_at"]
            else None,
            lot_size=row["lot_size"],
            price_tick=Decimal(row["price_tick"]),
            active=bool(row["active"]),
            metadata=_load_json(row["metadata"]),
        )

    def get_instrument(self, symbol: str) -> Optional[Instrument]:
        """Resolve one security master row without scanning the catalogue."""

        with self._lock:
            row = self._conn().execute(
                "SELECT * FROM instruments WHERE symbol = ?",
                (normalize_symbol(symbol),),
            ).fetchone()
        return self._row_to_instrument(row) if row is not None else None

    def clear_instruments(self) -> int:
        """Remove the provider-neutral security master before an authoritative replace."""

        with self._lock, self._conn():
            cursor = self._conn().execute("DELETE FROM instruments")
        return max(0, int(cursor.rowcount))

    def upsert_quotes(self, quotes: Sequence[Quote]) -> int:
        if not quotes:
            return 0
        rows = [self._quote_values(quote) for quote in quotes]
        with self._lock, self._conn():
            self._conn().executemany(
                """
                INSERT INTO quotes (
                    symbol, timestamp, last, open, high, low, previous_close,
                    bid, ask, bid_size, ask_size, volume, turnover, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, timestamp) DO UPDATE SET
                    last=excluded.last, open=excluded.open, high=excluded.high,
                    low=excluded.low, previous_close=excluded.previous_close,
                    bid=excluded.bid, ask=excluded.ask,
                    bid_size=excluded.bid_size, ask_size=excluded.ask_size,
                    volume=excluded.volume, turnover=excluded.turnover,
                    source=excluded.source
                """,
                rows,
            )
        return len(rows)

    @staticmethod
    def _quote_values(quote: Quote) -> Tuple[Any, ...]:
        def optional(value: Optional[Decimal]) -> Optional[str]:
            return str(value) if value is not None else None

        return (
            quote.symbol,
            _iso(quote.timestamp),
            str(quote.last),
            optional(quote.open),
            optional(quote.high),
            optional(quote.low),
            optional(quote.previous_close),
            optional(quote.bid),
            optional(quote.ask),
            quote.bid_size,
            quote.ask_size,
            quote.volume,
            str(quote.turnover),
            quote.source,
        )

    @staticmethod
    def _row_to_quote(row: sqlite3.Row) -> Quote:
        return Quote(
            symbol=row["symbol"],
            timestamp=parse_market_time(row["timestamp"]),
            last=Decimal(row["last"]),
            open=Decimal(row["open"]) if row["open"] is not None else None,
            high=Decimal(row["high"]) if row["high"] is not None else None,
            low=Decimal(row["low"]) if row["low"] is not None else None,
            previous_close=Decimal(row["previous_close"])
            if row["previous_close"] is not None
            else None,
            bid=Decimal(row["bid"]) if row["bid"] is not None else None,
            ask=Decimal(row["ask"]) if row["ask"] is not None else None,
            bid_size=row["bid_size"],
            ask_size=row["ask_size"],
            volume=row["volume"],
            turnover=Decimal(row["turnover"]),
            source=row["source"],
        )

    def get_latest_quotes(
        self,
        symbols: Optional[Sequence[str]] = None,
        source_prefix: Optional[str] = None,
    ) -> List[Quote]:
        params: List[Any] = []
        clauses: List[str] = []
        if symbols is not None:
            normalized = [normalize_symbol(symbol) for symbol in symbols]
            if not normalized:
                return []
            placeholders = ",".join("?" for _ in normalized)
            clauses.append("symbol IN ({})".format(placeholders))
            params.extend(normalized)
        if source_prefix is not None:
            normalized_source = str(source_prefix).strip().lower()
            if normalized_source:
                clauses.append("(LOWER(source) = ? OR LOWER(source) LIKE ?)")
                params.extend((normalized_source, normalized_source + ":%"))
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        sql = """
            SELECT q.* FROM quotes AS q
            INNER JOIN (
                SELECT symbol, MAX(timestamp) AS timestamp
                FROM quotes {where}
                GROUP BY symbol
            ) AS latest
            ON q.symbol = latest.symbol AND q.timestamp = latest.timestamp
            ORDER BY q.symbol
        """.format(where=where)
        with self._lock:
            rows = self._conn().execute(sql, params).fetchall()
        return [self._row_to_quote(row) for row in rows]

    def get_quotes(
        self,
        symbol: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 500,
    ) -> List[Quote]:
        clauses = ["symbol = ?"]
        params: List[Any] = [normalize_symbol(symbol)]
        if start is not None:
            clauses.append("timestamp >= ?")
            params.append(_iso(start))
        if end is not None:
            clauses.append("timestamp <= ?")
            params.append(_iso(end))
        params.append(_limit(limit))
        sql = """
            SELECT * FROM (
                SELECT * FROM quotes WHERE {where}
                ORDER BY timestamp DESC LIMIT ?
            ) ORDER BY timestamp ASC
        """.format(where=" AND ".join(clauses))
        with self._lock:
            rows = self._conn().execute(sql, params).fetchall()
        return [self._row_to_quote(row) for row in rows]

    def insert_bars(self, bars: Sequence[Bar]) -> int:
        if not bars:
            return 0
        rows = [
            (
                bar.symbol,
                bar.interval,
                _iso(bar.timestamp),
                str(bar.open),
                str(bar.high),
                str(bar.low),
                str(bar.close),
                bar.volume,
                str(bar.turnover),
                bar.source,
            )
            for bar in bars
        ]
        with self._lock, self._conn():
            self._conn().executemany(
                """
                INSERT INTO bars (
                    symbol, interval, timestamp, open, high, low, close,
                    volume, turnover, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, interval, timestamp) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, volume=excluded.volume,
                    turnover=excluded.turnover, source=excluded.source
                """,
                rows,
            )
        return len(rows)

    def begin_collection_batch(self, batch: CollectionBatch) -> None:
        """Persist a RUNNING batch before the provider network request starts."""

        if batch.status != CollectionBatchStatus.RUNNING:
            raise ValueError("begin_collection_batch requires running status")
        with self._lock, self._conn():
            self._conn().execute(
                """
                INSERT INTO collection_batches (
                    batch_id, provider, request_kind, input_hash, status,
                    started_at, completed_at, item_count, error_type,
                    error_message, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, 0, NULL, NULL, ?)
                """,
                (
                    batch.batch_id,
                    batch.provider,
                    batch.request_kind,
                    batch.input_hash,
                    batch.status.value,
                    _iso(batch.started_at),
                    _json(batch.metadata),
                ),
            )

    def fail_collection_batch(self, batch: CollectionBatch) -> None:
        """Move an existing RUNNING batch to a terminal failure state."""

        if batch.status not in (
            CollectionBatchStatus.FAILED,
            CollectionBatchStatus.CANCELLED,
            CollectionBatchStatus.ABANDONED,
        ):
            raise ValueError("fail_collection_batch requires a non-success terminal status")
        with self._lock, self._conn():
            cursor = self._conn().execute(
                """
                UPDATE collection_batches
                SET status = ?, completed_at = ?, item_count = ?,
                    error_type = ?, error_message = ?, metadata = ?
                WHERE batch_id = ? AND status = 'running'
                """,
                (
                    batch.status.value,
                    _iso(batch.completed_at),
                    batch.item_count,
                    batch.error_type,
                    batch.error_message,
                    _json(batch.metadata),
                    batch.batch_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("collection batch is missing or already terminal")

    @staticmethod
    def _bar_values(bar: Bar) -> Tuple[Any, ...]:
        return (
            bar.symbol,
            bar.interval,
            _iso(bar.timestamp),
            str(bar.open),
            str(bar.high),
            str(bar.low),
            str(bar.close),
            bar.volume,
            str(bar.turnover),
            bar.source,
        )

    @staticmethod
    def _observation_values(item: BarObservation) -> Tuple[Any, ...]:
        return (
            item.batch_id,
            item.symbol,
            item.interval,
            item.session_date.isoformat(),
            _iso(item.period_start),
            _iso(item.period_end),
            _iso(item.available_at) if item.available_at is not None else None,
            _iso(item.received_at),
            _iso(item.persisted_at),
            item.source,
            item.adjustment.value,
            item.revision_id,
            item.quality_status.value,
            str(item.open),
            str(item.high),
            str(item.low),
            str(item.close),
            item.volume,
            str(item.turnover),
        )

    def complete_bar_collection_batch(
        self,
        batch: CollectionBatch,
        observations: Sequence[BarObservation],
        metrics: Sequence[Metric] = (),
        legacy_bars: Optional[Sequence[Bar]] = None,
    ) -> int:
        """Atomically dual-write lineage, legacy projections and terminal state."""

        if batch.status != CollectionBatchStatus.SUCCEEDED:
            raise ValueError("complete_bar_collection_batch requires succeeded status")
        if batch.item_count != len(observations):
            raise ValueError("batch item_count must match observations")
        if any(item.batch_id != batch.batch_id for item in observations):
            raise ValueError("observation batch_id does not match collection batch")
        observation_rows = [self._observation_values(item) for item in observations]
        projected_bars = (
            tuple(legacy_bars)
            if legacy_bars is not None
            else tuple(item.to_legacy_bar() for item in observations)
        )
        # The legacy current view can include older cached rows.  Only rows
        # proven to originate in this response belong in ``observations``.
        bar_rows = [self._bar_values(item) for item in projected_bars]
        metric_rows = [
            (
                metric.symbol,
                metric.name,
                _iso(metric.timestamp),
                metric.value,
                metric.window,
                _json(metric.metadata),
            )
            for metric in metrics
        ]
        with self._lock, self._conn():
            connection = self._conn()
            cursor = connection.execute(
                "SELECT status FROM collection_batches WHERE batch_id = ?",
                (batch.batch_id,),
            )
            row = cursor.fetchone()
            if row is None or row[0] != CollectionBatchStatus.RUNNING.value:
                raise RuntimeError("collection batch is missing or already terminal")
            connection.executemany(
                """
                INSERT INTO bar_observations (
                    batch_id, symbol, interval, session_date, period_start,
                    period_end, available_at, received_at, persisted_at, source,
                    adjustment, revision_id, quality_status, open, high, low,
                    close, volume, turnover
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                observation_rows,
            )
            connection.executemany(
                """
                INSERT INTO bars (
                    symbol, interval, timestamp, open, high, low, close,
                    volume, turnover, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, interval, timestamp) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, volume=excluded.volume,
                    turnover=excluded.turnover, source=excluded.source
                """,
                bar_rows,
            )
            connection.executemany(
                """
                INSERT INTO metrics (symbol, name, timestamp, value, window, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, name, timestamp) DO UPDATE SET
                    value=excluded.value, window=excluded.window,
                    metadata=excluded.metadata
                """,
                metric_rows,
            )
            updated = connection.execute(
                """
                UPDATE collection_batches
                SET status = 'succeeded', completed_at = ?, item_count = ?,
                    error_type = NULL, error_message = NULL, metadata = ?
                WHERE batch_id = ? AND status = 'running'
                """,
                (
                    _iso(batch.completed_at),
                    batch.item_count,
                    _json(batch.metadata),
                    batch.batch_id,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("collection batch could not be completed")
        return len(observations)

    def abandon_running_collection_batches(self, completed_at: datetime) -> int:
        """Mark batches left RUNNING by a previous process as abandoned."""

        timestamp = _iso(completed_at)
        with self._lock, self._conn():
            cursor = self._conn().execute(
                """
                UPDATE collection_batches
                SET status = 'abandoned', completed_at = ?,
                    error_type = 'ProcessInterrupted', error_message = NULL
                WHERE status = 'running'
                """,
                (timestamp,),
            )
        return max(0, int(cursor.rowcount))

    def get_collection_batch(self, batch_id: str) -> Optional[CollectionBatch]:
        with self._lock:
            row = self._conn().execute(
                "SELECT * FROM collection_batches WHERE batch_id = ?",
                (str(batch_id).strip(),),
            ).fetchone()
        if row is None:
            return None
        return CollectionBatch(
            batch_id=row["batch_id"],
            provider=row["provider"],
            request_kind=row["request_kind"],
            input_hash=row["input_hash"],
            status=CollectionBatchStatus(row["status"]),
            started_at=parse_market_time(row["started_at"]),
            completed_at=(
                parse_market_time(row["completed_at"])
                if row["completed_at"] is not None
                else None
            ),
            item_count=row["item_count"],
            error_type=row["error_type"],
            error_message=row["error_message"],
            metadata=_load_json(row["metadata"]),
        )

    def get_bar_observations(
        self,
        symbol: str,
        interval: str = "1d",
        *,
        source: Optional[str] = None,
        adjustment: Optional[Adjustment] = None,
        limit: int = 500,
    ) -> List[BarObservation]:
        clauses = ["symbol = ?", "interval = ?"]
        params: List[Any] = [normalize_symbol(symbol), str(interval).strip().lower()]
        if source is not None:
            clauses.append("source = ?")
            params.append(str(source).strip())
        if adjustment is not None:
            normalized_adjustment = (
                adjustment if isinstance(adjustment, Adjustment) else Adjustment(adjustment)
            )
            clauses.append("adjustment = ?")
            params.append(normalized_adjustment.value)
        params.append(_limit(limit))
        sql = """
            SELECT * FROM (
                SELECT * FROM bar_observations WHERE {where}
                ORDER BY period_end DESC, received_at DESC LIMIT ?
            ) ORDER BY period_end ASC, received_at ASC
        """.format(where=" AND ".join(clauses))
        with self._lock:
            rows = self._conn().execute(sql, params).fetchall()
        return [
            BarObservation(
                batch_id=row["batch_id"],
                symbol=row["symbol"],
                interval=row["interval"],
                session_date=datetime.fromisoformat(row["session_date"]).date(),
                period_start=parse_market_time(row["period_start"]),
                period_end=parse_market_time(row["period_end"]),
                available_at=(
                    parse_market_time(row["available_at"])
                    if row["available_at"] is not None
                    else None
                ),
                received_at=parse_market_time(row["received_at"]),
                persisted_at=parse_market_time(row["persisted_at"]),
                source=row["source"],
                adjustment=Adjustment(row["adjustment"]),
                revision_id=row["revision_id"],
                quality_status=ObservationQualityStatus(row["quality_status"]),
                open=Decimal(row["open"]),
                high=Decimal(row["high"]),
                low=Decimal(row["low"]),
                close=Decimal(row["close"]),
                volume=row["volume"],
                turnover=Decimal(row["turnover"]),
            )
            for row in rows
        ]

    @staticmethod
    def _row_to_bar(row: sqlite3.Row) -> Bar:
        return Bar(
            symbol=row["symbol"],
            interval=row["interval"],
            timestamp=parse_market_time(row["timestamp"]),
            open=Decimal(row["open"]),
            high=Decimal(row["high"]),
            low=Decimal(row["low"]),
            close=Decimal(row["close"]),
            volume=row["volume"],
            turnover=Decimal(row["turnover"]),
            source=row["source"],
        )

    def get_bars(
        self,
        symbol: str,
        interval: str = "1m",
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 500,
        source_prefix: Optional[str] = None,
    ) -> List[Bar]:
        clauses = ["symbol = ?", "interval = ?"]
        params: List[Any] = [normalize_symbol(symbol), str(interval).strip().lower()]
        if start is not None:
            clauses.append("timestamp >= ?")
            params.append(_iso(start))
        if end is not None:
            clauses.append("timestamp <= ?")
            params.append(_iso(end))
        if source_prefix is not None:
            normalized_source = str(source_prefix).strip().lower()
            if normalized_source:
                clauses.append("(LOWER(source) = ? OR LOWER(source) LIKE ?)")
                params.extend((normalized_source, normalized_source + ":%"))
        params.append(_limit(limit))
        sql = """
            SELECT * FROM (
                SELECT * FROM bars WHERE {where}
                ORDER BY timestamp DESC LIMIT ?
            ) ORDER BY timestamp ASC
        """.format(where=" AND ".join(clauses))
        with self._lock:
            rows = self._conn().execute(sql, params).fetchall()
        return [self._row_to_bar(row) for row in rows]

    def insert_metrics(self, metrics: Sequence[Metric]) -> int:
        if not metrics:
            return 0
        rows = [
            (
                metric.symbol,
                metric.name,
                _iso(metric.timestamp),
                metric.value,
                metric.window,
                _json(metric.metadata),
            )
            for metric in metrics
        ]
        with self._lock, self._conn():
            self._conn().executemany(
                """
                INSERT INTO metrics (symbol, name, timestamp, value, window, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, name, timestamp) DO UPDATE SET
                    value=excluded.value, window=excluded.window,
                    metadata=excluded.metadata
                """,
                rows,
            )
        return len(rows)

    def get_metrics(
        self,
        symbol: str,
        name: Optional[str] = None,
        limit: int = 500,
    ) -> List[Metric]:
        clauses = ["symbol = ?"]
        params: List[Any] = [normalize_symbol(symbol)]
        if name is not None:
            clauses.append("name = ?")
            params.append(str(name))
        params.append(_limit(limit))
        sql = """
            SELECT * FROM (
                SELECT * FROM metrics WHERE {where}
                ORDER BY timestamp DESC LIMIT ?
            ) ORDER BY timestamp ASC
        """.format(where=" AND ".join(clauses))
        with self._lock:
            rows = self._conn().execute(sql, params).fetchall()
        return [
            Metric(
                symbol=row["symbol"],
                name=row["name"],
                timestamp=parse_market_time(row["timestamp"]),
                value=row["value"],
                window=row["window"],
                metadata=_load_json(row["metadata"]),
            )
            for row in rows
        ]

    def insert_alert(self, alert: Alert) -> int:
        with self._lock, self._conn():
            cursor = self._conn().execute(
                """
                INSERT INTO alerts (
                    symbol, timestamp, level, title, message, category,
                    acknowledged, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert.symbol,
                    _iso(alert.timestamp),
                    alert.level.value,
                    alert.title,
                    alert.message,
                    alert.category,
                    int(alert.acknowledged),
                    _json(alert.metadata),
                ),
            )
            return int(cursor.lastrowid)

    def insert_alerts(self, alerts: Sequence[Alert]) -> int:
        if not alerts:
            return 0
        rows = [
            (
                alert.symbol,
                _iso(alert.timestamp),
                alert.level.value,
                alert.title,
                alert.message,
                alert.category,
                int(alert.acknowledged),
                _json(alert.metadata),
            )
            for alert in alerts
        ]
        with self._lock, self._conn():
            self._conn().executemany(
                """
                INSERT INTO alerts (
                    symbol, timestamp, level, title, message, category,
                    acknowledged, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def acknowledge_alert(self, alert_id: int, acknowledged: bool = True) -> bool:
        with self._lock, self._conn():
            cursor = self._conn().execute(
                "UPDATE alerts SET acknowledged = ? WHERE id = ?",
                (int(acknowledged), int(alert_id)),
            )
            return cursor.rowcount > 0

    def list_alerts(
        self,
        symbol: Optional[str] = None,
        level: Optional[AlertLevel] = None,
        acknowledged: Optional[bool] = None,
        limit: int = 100,
    ) -> List[Alert]:
        clauses: List[str] = []
        params: List[Any] = []
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(normalize_symbol(symbol))
        if level is not None:
            normalized_level = level if isinstance(level, AlertLevel) else AlertLevel(level)
            clauses.append("level = ?")
            params.append(normalized_level.value)
        if acknowledged is not None:
            clauses.append("acknowledged = ?")
            params.append(int(acknowledged))
        sql = "SELECT * FROM alerts"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY timestamp DESC, id DESC LIMIT ?"
        params.append(_limit(limit))
        with self._lock:
            rows = self._conn().execute(sql, params).fetchall()
        return [
            Alert(
                id=row["id"],
                symbol=row["symbol"],
                timestamp=parse_market_time(row["timestamp"]),
                level=AlertLevel(row["level"]),
                title=row["title"],
                message=row["message"],
                category=row["category"],
                acknowledged=bool(row["acknowledged"]),
                metadata=_load_json(row["metadata"]),
            )
            for row in rows
        ]

    def insert_signal(self, signal: Signal) -> int:
        with self._lock, self._conn():
            cursor = self._conn().execute(
                """
                INSERT INTO signals (
                    strategy, symbol, timestamp, action, strength, price,
                    reason, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal.strategy,
                    signal.symbol,
                    _iso(signal.timestamp),
                    signal.action.value,
                    signal.strength,
                    str(signal.price) if signal.price is not None else None,
                    signal.reason,
                    _json(signal.metadata),
                ),
            )
            return int(cursor.lastrowid)

    def insert_signals(self, signals: Sequence[Signal]) -> int:
        if not signals:
            return 0
        rows = [
            (
                signal.strategy,
                signal.symbol,
                _iso(signal.timestamp),
                signal.action.value,
                signal.strength,
                str(signal.price) if signal.price is not None else None,
                signal.reason,
                _json(signal.metadata),
            )
            for signal in signals
        ]
        with self._lock, self._conn():
            self._conn().executemany(
                """
                INSERT INTO signals (
                    strategy, symbol, timestamp, action, strength, price,
                    reason, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def list_signals(
        self,
        symbol: Optional[str] = None,
        strategy: Optional[str] = None,
        action: Optional[SignalAction] = None,
        limit: int = 100,
    ) -> List[Signal]:
        clauses: List[str] = []
        params: List[Any] = []
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(normalize_symbol(symbol))
        if strategy is not None:
            clauses.append("strategy = ?")
            params.append(str(strategy))
        if action is not None:
            normalized_action = action if isinstance(action, SignalAction) else SignalAction(action)
            clauses.append("action = ?")
            params.append(normalized_action.value)
        sql = "SELECT * FROM signals"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY timestamp DESC, id DESC LIMIT ?"
        params.append(_limit(limit))
        with self._lock:
            rows = self._conn().execute(sql, params).fetchall()
        return [
            Signal(
                id=row["id"],
                strategy=row["strategy"],
                symbol=row["symbol"],
                timestamp=parse_market_time(row["timestamp"]),
                action=SignalAction(row["action"]),
                strength=row["strength"],
                price=Decimal(row["price"]) if row["price"] is not None else None,
                reason=row["reason"],
                metadata=_load_json(row["metadata"]),
            )
            for row in rows
        ]
