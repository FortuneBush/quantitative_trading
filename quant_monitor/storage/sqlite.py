"""SQLite persistence for market data, metrics, alerts and strategy signals."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from quant_monitor.domain import (
    Adjustment,
    AdjustmentFactor,
    Alert,
    AlertLevel,
    Bar,
    BarObservation,
    CollectionBatch,
    CollectionBatchStatus,
    CorporateAction,
    CorporateActionType,
    Exchange,
    FundamentalObservation,
    Instrument,
    Metric,
    ObservationQualityStatus,
    Quote,
    SecurityType,
    Signal,
    SignalAction,
    ensure_market_time,
    fundamentals_as_of,
    normalize_symbol,
    now_market_time,
    parse_market_time,
)
from quant_monitor.research import (
    UniverseSnapshot,
    ResearchResultRecord,
    ResearchRunManifest,
    ResearchRunRecord,
    hash_canonical,
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


def _offset(value: int) -> int:
    """Validate a pagination offset without silently coercing negatives."""

    normalized = int(value)
    if normalized < 0:
        raise ValueError("offset must be non-negative")
    return normalized


_RESEARCH_SORT_COLUMNS = frozenset(
    {"created_at", "run_id", "strategy_name", "dataset_name", "dataset_source"}
)


def _research_tags(manifest_json: str) -> frozenset[str]:
    """Read optional user labels from an immutable manifest without trust.

    Tags deliberately live in the already-hashed ``assumptions.tags`` tree,
    so adding the catalog filter does not require a schema migration or alter
    the identity of existing manifests.  Malformed/absent labels simply mean
    that the run has no searchable tags; the immutable record itself remains
    protected by ``ResearchRunRecord`` validation.
    """

    try:
        payload = json.loads(manifest_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return frozenset()
    assumptions = payload.get("assumptions") if isinstance(payload, dict) else None
    raw = assumptions.get("tags") if isinstance(assumptions, dict) else None
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset()
    result = set()
    for item in raw:
        if isinstance(item, str) and item.strip():
            result.add(item.strip().casefold())
    return frozenset(result)


def _research_tag_filter(value: Optional[Sequence[str]]) -> frozenset[str]:
    if value is None:
        return frozenset()
    result = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("tags must contain non-empty strings")
        normalized = item.strip().casefold()
        if len(normalized) > 64:
            raise ValueError("research tag is too long")
        result.add(normalized)
    return frozenset(result)


class SQLiteRepository:
    """Small thread-safe repository optimized for a single local application.

    Call :meth:`initialize` once before use.  Disk databases use WAL mode, so the
    dashboard can read snapshots while the collector writes a new batch.  The
    optional ``initialization_timeout_seconds`` bounds connection/migration lock
    waits for lifecycle callers; ordinary repositories retain the historical
    SQLite timeout/retry behavior when it is omitted.
    """

    SCHEMA_VERSION = CURRENT_SCHEMA_VERSION

    def __init__(
        self,
        path: PathLike,
        *,
        initialization_timeout_seconds: Optional[float] = None,
    ) -> None:
        self.path = str(path)
        self._connection: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()
        if initialization_timeout_seconds is not None:
            initialization_timeout_seconds = float(initialization_timeout_seconds)
            if initialization_timeout_seconds <= 0:
                raise ValueError("initialization_timeout_seconds must be positive")
        self.initialization_timeout_seconds = initialization_timeout_seconds

    @property
    def initialized(self) -> bool:
        return self._connection is not None

    def initialize(self) -> None:
        with self._lock:
            if self._connection is not None:
                return
            started = time.monotonic()
            deadline = (
                started + self.initialization_timeout_seconds
                if self.initialization_timeout_seconds is not None
                else None
            )
            if self.path != ":memory:":
                Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
            attempts = 3 if self.path != ":memory:" else 1
            for attempt in range(attempts):
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError(
                        "SQLite initialization exceeded {:.3f} seconds".format(
                            self.initialization_timeout_seconds
                        )
                    )
                connect_timeout = 5.0 if remaining is None else max(0.001, min(5.0, remaining))
                connection = sqlite3.connect(
                    self.path,
                    timeout=connect_timeout,
                    check_same_thread=False,
                )
                connection.row_factory = sqlite3.Row
                try:
                    busy_timeout_ms = (
                        5000
                        if remaining is None
                        else max(1, min(5000, int(connect_timeout * 1000)))
                    )
                    connection.execute(
                        "PRAGMA busy_timeout = {}".format(busy_timeout_ms)
                    )
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
                    if not locked:
                        raise
                    if attempt + 1 >= attempts:
                        if deadline is not None:
                            raise TimeoutError(
                                "SQLite initialization exceeded {:.3f} seconds".format(
                                    self.initialization_timeout_seconds
                                )
                            ) from exc
                        raise
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        raise TimeoutError(
                            "SQLite initialization exceeded {:.3f} seconds".format(
                                self.initialization_timeout_seconds
                            )
                        ) from exc
                    delay = 0.05 * (attempt + 1)
                    time.sleep(delay if remaining is None else min(delay, remaining))
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

    def upsert_quote_snapshot(
        self,
        quotes: Sequence[Quote],
        *,
        source_prefix: str,
        reject_stale: bool = True,
    ) -> List[Quote]:
        """Validate and persist one provider snapshot without a check/write race.

        A snapshot contains at most one row per symbol. Older rows either
        reject the entire batch or are excluded in relaxed mode; corrections
        at the same timestamp remain valid. The shared reentrant lock keeps
        other writers on this repository instance outside the freshness
        check and write. It is not a cross-process single-writer guarantee.
        Historical ingestion may continue using :meth:`upsert_quotes`.
        """

        if not isinstance(source_prefix, str) or not source_prefix.strip():
            raise ValueError("source_prefix must be a non-empty string")
        source = source_prefix.strip().lower()
        if not isinstance(reject_stale, bool):
            raise ValueError("reject_stale must be a boolean")
        try:
            batch = list(quotes)
        except TypeError:
            raise ValueError("snapshot must contain Quote instances") from None
        seen = set()
        for quote in batch:
            if not isinstance(quote, Quote):
                raise ValueError("snapshot must contain Quote instances")
            if quote.symbol in seen:
                raise ValueError("snapshot contains duplicate quote symbols")
            seen.add(quote.symbol)
            quote_source = quote.source.strip().lower()
            if quote_source != source and not quote_source.startswith(source + ":"):
                raise ValueError("snapshot contains quotes from an unexpected source")
        if not batch:
            return []
        # Match the provider boundary literally. LIKE would treat '_' and '%'
        # in a source name as wildcards and could mistake another provider's
        # newer quote for this provider's latest snapshot.
        placeholders = ",".join("?" for _ in batch)
        sql = """
            SELECT symbol, MAX(timestamp) AS timestamp
            FROM quotes
            WHERE symbol IN ({}) AND (
                LOWER(TRIM(source)) = ? OR
                SUBSTR(LOWER(TRIM(source)), 1, ?) = ?
            )
            GROUP BY symbol
        """.format(placeholders)
        params = [quote.symbol for quote in batch]
        params.extend((source, len(source) + 1, source + ":"))
        with self._lock:
            latest = {
                row["symbol"]: parse_market_time(row["timestamp"])
                for row in self._conn().execute(sql, params).fetchall()
            }
            stale = {
                quote.symbol
                for quote in batch
                if quote.symbol in latest and quote.timestamp < latest[quote.symbol]
            }
            if stale and reject_stale:
                raise ValueError(
                    "provider returned stale quotes for: {}".format(
                        ", ".join(sorted(stale))
                    )
                )
            accepted = [quote for quote in batch if quote.symbol not in stale]
            self.upsert_quotes(accepted)
            return accepted

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

    def upsert_collection_job(
        self,
        *,
        job_id: str,
        idempotency_key_digest: Optional[str],
        status: str,
        requested_symbols: Sequence[str],
        created_at: datetime,
        started_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
        iteration_before: Optional[int] = None,
        iteration_after: Optional[int] = None,
        quotes_count: int = 0,
        bars_count: int = 0,
        metrics_count: int = 0,
        signals_count: int = 0,
        error_type: Optional[str] = None,
    ) -> None:
        """Persist one explicit collector job without retaining its raw key.

        The idempotency digest and lifecycle snapshot are durable so a retry
        after a process restart can resolve to the original job.  This table
        is intentionally separate from ``collection_batches``: a job is an
        API operation while batches describe provider attempts and may be
        split into quote/bar requests.
        """

        normalized_job_id = str(job_id).strip()
        digest = (
            None
            if idempotency_key_digest is None
            else str(idempotency_key_digest).strip().casefold()
        )
        state = str(status).strip().lower()
        symbols = tuple(str(item).strip() for item in requested_symbols if str(item).strip())
        if not normalized_job_id or not symbols:
            raise ValueError("job_id and requested_symbols are required")
        if digest is not None and (len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest.casefold())):
            raise ValueError("idempotency_key_digest must be a SHA-256 hex digest")
        with self._lock, self._conn():
            self._conn().execute(
                """
                INSERT INTO collector_jobs (
                    job_id, idempotency_key_digest, status, requested_symbols,
                    created_at, started_at, completed_at, iteration_before,
                    iteration_after, quotes_count, bars_count, metrics_count,
                    signals_count, error_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status=excluded.status,
                    requested_symbols=collector_jobs.requested_symbols,
                    created_at=collector_jobs.created_at,
                    started_at=excluded.started_at,
                    completed_at=excluded.completed_at,
                    iteration_before=excluded.iteration_before,
                    iteration_after=excluded.iteration_after,
                    quotes_count=excluded.quotes_count,
                    bars_count=excluded.bars_count,
                    metrics_count=excluded.metrics_count,
                    signals_count=excluded.signals_count,
                    error_type=excluded.error_type,
                    idempotency_key_digest=COALESCE(collector_jobs.idempotency_key_digest, excluded.idempotency_key_digest)
                """,
                (
                    normalized_job_id,
                    digest,
                    state,
                    json.dumps(list(symbols), ensure_ascii=False, separators=(",", ":")),
                    _iso(created_at),
                    _iso(started_at) if started_at is not None else None,
                    _iso(completed_at) if completed_at is not None else None,
                    iteration_before,
                    iteration_after,
                    int(quotes_count),
                    int(bars_count),
                    int(metrics_count),
                    int(signals_count),
                    None if error_type is None else str(error_type),
                ),
            )

    @staticmethod
    def _row_to_collection_job(row: sqlite3.Row) -> Dict[str, Any]:
        try:
            symbols = json.loads(row["requested_symbols"])
        except (TypeError, ValueError, json.JSONDecodeError):
            symbols = []
        if not isinstance(symbols, list):
            symbols = []
        return {
            "job_id": str(row["job_id"]),
            "idempotency_key_digest": row["idempotency_key_digest"],
            "status": str(row["status"]),
            "requested_symbols": tuple(str(item) for item in symbols),
            "created_at": parse_market_time(row["created_at"]),
            "started_at": parse_market_time(row["started_at"]) if row["started_at"] else None,
            "completed_at": parse_market_time(row["completed_at"]) if row["completed_at"] else None,
            "iteration_before": row["iteration_before"],
            "iteration_after": row["iteration_after"],
            "quotes_count": int(row["quotes_count"] or 0),
            "bars_count": int(row["bars_count"] or 0),
            "metrics_count": int(row["metrics_count"] or 0),
            "signals_count": int(row["signals_count"] or 0),
            "error_type": row["error_type"],
        }

    def get_collection_job_record(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn().execute(
                "SELECT * FROM collector_jobs WHERE job_id = ?",
                (str(job_id).strip(),),
            ).fetchone()
        return self._row_to_collection_job(row) if row is not None else None

    def get_collection_job_by_idempotency(
        self, idempotency_key_digest: str
    ) -> Optional[Dict[str, Any]]:
        digest = str(idempotency_key_digest).strip()
        with self._lock:
            row = self._conn().execute(
                "SELECT * FROM collector_jobs WHERE idempotency_key_digest = ?",
                (digest,),
            ).fetchone()
        return self._row_to_collection_job(row) if row is not None else None

    def list_collection_job_records(self, limit: int = 100) -> List[Dict[str, Any]]:
        bounded = max(1, min(int(limit), 1000))
        with self._lock:
            rows = self._conn().execute(
                "SELECT * FROM collector_jobs ORDER BY created_at DESC, job_id DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        # Return oldest-to-newest so the in-memory bounded history preserves
        # the same ordering as the API's previous process-local registry.
        return [self._row_to_collection_job(row) for row in reversed(rows)]

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

    @staticmethod
    def _row_to_collection_batch(row: sqlite3.Row) -> CollectionBatch:
        """Convert a lineage row while keeping the domain model authoritative."""

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

    def get_collection_batch(self, batch_id: str) -> Optional[CollectionBatch]:
        with self._lock:
            row = self._conn().execute(
                "SELECT * FROM collection_batches WHERE batch_id = ?",
                (str(batch_id).strip(),),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_collection_batch(row)

    @staticmethod
    def _row_to_research_run(row: sqlite3.Row) -> ResearchRunRecord:
        """Convert and integrity-check an immutable manifest row."""

        return ResearchRunRecord(
            run_id=row["run_id"],
            content_hash=row["content_hash"],
            manifest_json=row["manifest_json"],
            manifest_json_hash=row["manifest_json_hash"],
            manifest_schema_version=row["manifest_schema_version"],
            strategy_name=row["strategy_name"],
            strategy_code_version=row["strategy_code_version"],
            dataset_name=row["dataset_name"],
            dataset_source=row["dataset_source"],
            input_hash=row["input_hash"],
            calendar_version=row["calendar_version"],
            universe_version=row["universe_version"],
            adjustment_version=row["adjustment_version"],
            constraint_version=row["constraint_version"],
            created_at=parse_market_time(row["created_at"]),
        )

    def create_research_run(
        self,
        manifest: ResearchRunManifest,
        *,
        created_at: Optional[datetime] = None,
    ) -> ResearchRunRecord:
        """Insert one immutable, safe research manifest.

        There is deliberately no upsert or update operation.  Reusing a
        ``run_id`` raises ``sqlite3.IntegrityError`` so a completed run cannot
        be silently replaced by a later result.
        """

        if not isinstance(manifest, ResearchRunManifest):
            raise TypeError("manifest must be a ResearchRunManifest")
        from quant_monitor.domain import now_market_time

        record = ResearchRunRecord.from_manifest(
            manifest,
            created_at=created_at if created_at is not None else now_market_time(),
        )
        with self._lock, self._conn():
            self._conn().execute(
                """
                INSERT INTO research_runs (
                    run_id, content_hash, manifest_json, manifest_json_hash,
                    manifest_schema_version, strategy_name, strategy_code_version,
                    dataset_name, dataset_source, input_hash, calendar_version,
                    universe_version, adjustment_version, constraint_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.run_id,
                    record.content_hash,
                    record.manifest_json,
                    record.manifest_json_hash,
                    record.manifest_schema_version,
                    record.strategy_name,
                    record.strategy_code_version,
                    record.dataset_name,
                    record.dataset_source,
                    record.input_hash,
                    record.calendar_version,
                    record.universe_version,
                    record.adjustment_version,
                    record.constraint_version,
                    _iso(record.created_at),
                ),
            )
        return record

    def get_research_run(self, run_id: str) -> Optional[ResearchRunRecord]:
        normalized_run_id = str(run_id).strip()
        if not normalized_run_id:
            raise ValueError("run_id cannot be empty")
        with self._lock:
            row = self._conn().execute(
                "SELECT * FROM research_runs WHERE run_id = ?",
                (normalized_run_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_research_run(row)

    def query_research_runs(
        self,
        *,
        strategy_name: Optional[str] = None,
        dataset_name: Optional[str] = None,
        created_from: Optional[datetime] = None,
        created_to: Optional[datetime] = None,
        tags: Optional[Sequence[str]] = None,
        sort_by: str = "created_at",
        sort_order: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[ResearchRunRecord], int]:
        """Return a deterministic, bounded page of immutable research runs.

        ``tags`` is an AND filter over optional ``assumptions.tags`` labels.
        Sorting is restricted to known columns so callers cannot inject SQL;
        ``run_id`` is always a deterministic tie-breaker.
        """

        page_limit = _limit(limit)
        page_offset = _offset(offset)
        normalized_tags = _research_tag_filter(tags)
        normalized_sort = str(sort_by).strip().lower()
        if normalized_sort not in _RESEARCH_SORT_COLUMNS:
            raise ValueError("unsupported research sort field")
        normalized_order = str(sort_order).strip().lower()
        if normalized_order not in {"asc", "desc"}:
            raise ValueError("sort_order must be asc or desc")
        clauses: List[str] = []
        params: List[Any] = []
        if strategy_name is not None:
            normalized_strategy = str(strategy_name).strip()
            if not normalized_strategy:
                raise ValueError("strategy_name cannot be empty")
            clauses.append("strategy_name = ?")
            params.append(normalized_strategy)
        if dataset_name is not None:
            normalized_dataset = str(dataset_name).strip()
            if not normalized_dataset:
                raise ValueError("dataset_name cannot be empty")
            clauses.append("dataset_name = ?")
            params.append(normalized_dataset)
        if created_from is not None:
            clauses.append("created_at >= ?")
            params.append(_iso(created_from))
        if created_to is not None:
            clauses.append("created_at <= ?")
            params.append(_iso(created_to))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        direction = "ASC" if normalized_order == "asc" else "DESC"
        # A tag filter is stored inside the immutable manifest JSON (rather
        # than a schema column), so filter after the SQL predicate while still
        # applying a stable order and bounded page to the resulting set.
        order_sql = "ORDER BY {} {}, run_id {}".format(
            normalized_sort, direction, direction
        )
        with self._lock:
            connection = self._conn()
            if normalized_tags:
                rows = connection.execute(
                    "SELECT * FROM research_runs{} {}".format(where, order_sql),
                    params,
                ).fetchall()
                rows = [
                    row
                    for row in rows
                    if normalized_tags.issubset(_research_tags(row["manifest_json"]))
                ]
                total = len(rows)
                rows = rows[page_offset : page_offset + page_limit]
            else:
                total = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM research_runs" + where,
                        params,
                    ).fetchone()[0]
                )
                rows = connection.execute(
                    "SELECT * FROM research_runs{} {} LIMIT ? OFFSET ?".format(
                        where, order_sql
                    ),
                    params + [page_limit, page_offset],
                ).fetchall()
        return [self._row_to_research_run(row) for row in rows], total

    def list_research_runs(
        self,
        *,
        strategy_name: Optional[str] = None,
        dataset_name: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
        sort_by: str = "created_at",
        sort_order: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ) -> List[ResearchRunRecord]:
        """Convenience read-only list API for callers that do not need totals."""

        runs, _ = self.query_research_runs(
            strategy_name=strategy_name,
            dataset_name=dataset_name,
            tags=tags,
            sort_by=sort_by,
            sort_order=sort_order,
            limit=limit,
            offset=offset,
        )
        return runs

    def research_result_presence(self, run_ids: Sequence[str]) -> set[str]:
        """Return run IDs that have an immutable result artifact.

        This is a compact, read-only projection used by the dashboard list
        endpoint.  Keeping the lookup set-based avoids fetching potentially
        large result JSON blobs (and avoids one SQLite query per run).
        """

        normalized = tuple(dict.fromkeys(str(item).strip() for item in run_ids if str(item).strip()))
        if not normalized:
            return set()
        placeholders = ",".join("?" for _ in normalized)
        with self._lock:
            rows = self._conn().execute(
                "SELECT run_id FROM research_results WHERE run_id IN ({})".format(placeholders),
                normalized,
            ).fetchall()
        return {str(row["run_id"]) for row in rows}

    @staticmethod
    def _row_to_research_result(row: sqlite3.Row) -> ResearchResultRecord:
        return ResearchResultRecord(
            run_id=row["run_id"],
            result_hash=row["result_hash"],
            result_json=row["result_json"],
            result_json_hash=row["result_json_hash"],
            created_at=parse_market_time(row["created_at"]),
        )

    def create_research_result(
        self,
        run_id: str,
        result_json: str,
        *,
        created_at: Optional[datetime] = None,
    ) -> ResearchResultRecord:
        """Persist one immutable backtest result bound to an existing run."""

        normalized_run_id = str(run_id).strip()
        if not normalized_run_id:
            raise ValueError("run_id cannot be empty")
        if self.get_research_run(normalized_run_id) is None:
            raise ValueError("research run not found")
        from quant_monitor.domain import now_market_time

        record = ResearchResultRecord.from_json(
            normalized_run_id,
            result_json,
            created_at=created_at if created_at is not None else now_market_time(),
        )
        with self._lock, self._conn():
            self._conn().execute(
                """
                INSERT INTO research_results (
                    run_id, result_hash, result_json, result_json_hash, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    record.run_id,
                    record.result_hash,
                    record.result_json,
                    record.result_json_hash,
                    _iso(record.created_at),
                ),
            )
        return record

    def get_research_result(self, run_id: str) -> Optional[ResearchResultRecord]:
        normalized_run_id = str(run_id).strip()
        if not normalized_run_id:
            raise ValueError("run_id cannot be empty")
        with self._lock:
            row = self._conn().execute(
                "SELECT * FROM research_results WHERE run_id = ?",
                (normalized_run_id,),
            ).fetchone()
        return self._row_to_research_result(row) if row is not None else None

    def query_research_results(
        self,
        *,
        run_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[ResearchResultRecord], int]:
        page_limit = _limit(limit)
        page_offset = _offset(offset)
        clauses: List[str] = []
        params: List[Any] = []
        if run_id is not None:
            normalized_run_id = str(run_id).strip()
            if not normalized_run_id:
                raise ValueError("run_id cannot be empty")
            clauses.append("run_id = ?")
            params.append(normalized_run_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            connection = self._conn()
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM research_results" + where, params
                ).fetchone()[0]
            )
            rows = connection.execute(
                "SELECT * FROM research_results{} ORDER BY created_at DESC, run_id DESC "
                "LIMIT ? OFFSET ?".format(where),
                params + [page_limit, page_offset],
            ).fetchall()
        return [self._row_to_research_result(row) for row in rows], total

    def query_collection_batches(
        self,
        *,
        provider: Optional[str] = None,
        status: Optional[CollectionBatchStatus] = None,
        request_kind: Optional[str] = None,
        started_from: Optional[datetime] = None,
        started_to: Optional[datetime] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[CollectionBatch], int]:
        """Return a bounded, deterministic page of collection batches.

        The count and page are intentionally queried from the same repository
        connection while holding the repository lock.  This gives API clients
        a useful snapshot for pagination without exposing the SQLite cursor or
        allowing arbitrary SQL fragments into the query.
        """

        page_limit = _limit(limit)
        page_offset = _offset(offset)
        clauses: List[str] = []
        params: List[Any] = []
        if provider is not None:
            normalized_provider = str(provider).strip().lower()
            if not normalized_provider:
                raise ValueError("provider cannot be empty")
            clauses.append("LOWER(provider) = ?")
            params.append(normalized_provider)
        if status is not None:
            normalized_status = (
                status
                if isinstance(status, CollectionBatchStatus)
                else CollectionBatchStatus(status)
            )
            clauses.append("status = ?")
            params.append(normalized_status.value)
        if request_kind is not None:
            normalized_kind = str(request_kind).strip().lower()
            if not normalized_kind:
                raise ValueError("request_kind cannot be empty")
            clauses.append("LOWER(request_kind) = ?")
            params.append(normalized_kind)
        if started_from is not None:
            clauses.append("started_at >= ?")
            params.append(_iso(started_from))
        if started_to is not None:
            clauses.append("started_at <= ?")
            params.append(_iso(started_to))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            connection = self._conn()
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM collection_batches" + where,
                    params,
                ).fetchone()[0]
            )
            rows = connection.execute(
                "SELECT * FROM collection_batches{} "
                "ORDER BY started_at DESC, batch_id DESC LIMIT ? OFFSET ?".format(where),
                params + [page_limit, page_offset],
            ).fetchall()
        return [self._row_to_collection_batch(row) for row in rows], total

    def count_bar_observations(self, batch_id: str) -> int:
        """Return the number of immutable observations attached to a batch."""

        normalized_batch_id = str(batch_id).strip()
        if not normalized_batch_id:
            raise ValueError("batch_id cannot be empty")
        with self._lock:
            row = self._conn().execute(
                "SELECT COUNT(*) FROM bar_observations WHERE batch_id = ?",
                (normalized_batch_id,),
            ).fetchone()
        return int(row[0])

    @staticmethod
    def _row_to_bar_observation(row: sqlite3.Row) -> BarObservation:
        return BarObservation(
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

    def query_bar_observations(
        self,
        *,
        symbol: Optional[str] = None,
        interval: Optional[str] = None,
        batch_id: Optional[str] = None,
        source: Optional[str] = None,
        adjustment: Optional[Adjustment] = None,
        quality_status: Optional[ObservationQualityStatus] = None,
        period_from: Optional[datetime] = None,
        period_to: Optional[datetime] = None,
        limit: int = 500,
        offset: int = 0,
    ) -> Tuple[List[BarObservation], int]:
        """Query immutable bar observations with safe, indexed filters."""

        page_limit = _limit(limit)
        page_offset = _offset(offset)
        clauses: List[str] = []
        params: List[Any] = []
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(normalize_symbol(symbol))
        if interval is not None:
            normalized_interval = str(interval).strip().lower()
            if not normalized_interval:
                raise ValueError("interval cannot be empty")
            clauses.append("interval = ?")
            params.append(normalized_interval)
        if batch_id is not None:
            normalized_batch_id = str(batch_id).strip()
            if not normalized_batch_id:
                raise ValueError("batch_id cannot be empty")
            clauses.append("batch_id = ?")
            params.append(normalized_batch_id)
        if source is not None:
            normalized_source = str(source).strip().lower()
            if not normalized_source:
                raise ValueError("source cannot be empty")
            clauses.append("LOWER(source) = ?")
            params.append(normalized_source)
        if adjustment is not None:
            normalized_adjustment = (
                adjustment
                if isinstance(adjustment, Adjustment)
                else Adjustment(adjustment)
            )
            clauses.append("adjustment = ?")
            params.append(normalized_adjustment.value)
        if quality_status is not None:
            normalized_quality = (
                quality_status
                if isinstance(quality_status, ObservationQualityStatus)
                else ObservationQualityStatus(quality_status)
            )
            clauses.append("quality_status = ?")
            params.append(normalized_quality.value)
        if period_from is not None:
            clauses.append("period_end >= ?")
            params.append(_iso(period_from))
        if period_to is not None:
            clauses.append("period_end <= ?")
            params.append(_iso(period_to))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            connection = self._conn()
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM bar_observations" + where,
                    params,
                ).fetchone()[0]
            )
            rows = connection.execute(
                "SELECT * FROM bar_observations{} "
                "ORDER BY period_end DESC, received_at DESC, symbol ASC, "
                "revision_id ASC LIMIT ? OFFSET ?".format(where),
                params + [page_limit, page_offset],
            ).fetchall()
        return [self._row_to_bar_observation(row) for row in rows], total

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
        return [self._row_to_bar_observation(row) for row in rows]

    @staticmethod
    def _fundamental_values(item: FundamentalObservation) -> Tuple[Any, ...]:
        """Return the append-only storage representation of one fact."""

        return (
            item.observation_id,
            item.symbol,
            item.metric,
            None if item.value is None else str(item.value),
            item.unit,
            item.scope,
            item.period_start.isoformat(),
            item.period_end.isoformat(),
            _iso(item.available_at),
            _iso(item.received_at),
            item.source,
            item.revision_id,
            _json(item.to_dict()["metadata"]),
            item.content_hash,
        )

    def insert_fundamental_observations(
        self, observations: Sequence[FundamentalObservation]
    ) -> int:
        """Append immutable point-in-time fundamental observations.

        Re-delivery of an identical ``observation_id``/content hash is
        idempotent.  Reusing an ID for different economic content fails closed;
        existing rows are never updated or deleted.
        """

        rows = tuple(observations)
        if not rows:
            return 0
        if any(not isinstance(item, FundamentalObservation) for item in rows):
            raise TypeError(
                "observations must contain FundamentalObservation objects"
            )
        values = [self._fundamental_values(item) for item in rows]
        with self._lock, self._conn():
            connection = self._conn()
            inserted = 0
            seen: Dict[str, str] = {}
            for item, row in zip(rows, values):
                prior_batch_hash = seen.get(item.observation_id)
                if prior_batch_hash is not None:
                    if prior_batch_hash != item.content_hash:
                        raise ValueError(
                            "conflicting fundamental observation_id {}".format(
                                item.observation_id
                            )
                        )
                    continue
                seen[item.observation_id] = item.content_hash
                existing = connection.execute(
                    "SELECT content_hash FROM fundamental_observations "
                    "WHERE observation_id = ?",
                    (item.observation_id,),
                ).fetchone()
                if existing is not None:
                    if str(existing[0]) != item.content_hash:
                        raise ValueError(
                            "conflicting fundamental observation_id {}".format(
                                item.observation_id
                            )
                        )
                    continue
                connection.execute(
                    """
                    INSERT INTO fundamental_observations (
                        observation_id, symbol, metric, value, unit, scope,
                        period_start, period_end, available_at, received_at,
                        source, revision_id, metadata, content_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    row,
                )
                inserted += 1
        return inserted

    @staticmethod
    def _row_to_fundamental(row: sqlite3.Row) -> FundamentalObservation:
        return FundamentalObservation(
            observation_id=row["observation_id"],
            symbol=row["symbol"],
            metric=row["metric"],
            value=Decimal(row["value"]) if row["value"] is not None else None,
            unit=row["unit"],
            scope=row["scope"],
            period_start=date.fromisoformat(row["period_start"]),
            period_end=date.fromisoformat(row["period_end"]),
            available_at=parse_market_time(row["available_at"]),
            received_at=parse_market_time(row["received_at"]),
            source=row["source"],
            revision_id=row["revision_id"],
            metadata=_load_json(row["metadata"]),
        )

    def query_fundamentals_as_of(
        self,
        *,
        decision_at: datetime,
        symbol: Optional[str] = None,
        metric: Optional[str] = None,
        period_start: Optional[date] = None,
        period_end: Optional[date] = None,
        unit: Optional[str] = None,
        scope: Optional[str] = None,
        source: Optional[str] = None,
        symbols: Optional[Sequence[str]] = None,
        metrics: Optional[Sequence[str]] = None,
    ) -> Tuple[List[FundamentalObservation], int]:
        """Resolve immutable facts as visible at ``decision_at``.

        The SQL predicate is only a bounded candidate read.  Final period and
        revision selection is delegated to the provider-independent domain
        selector, which fails closed on incomparable dimensions and conflicts.
        The second return value is the candidate row count before selection and
        is useful for coverage diagnostics.
        """

        cutoff = ensure_market_time(decision_at)
        if symbol is not None and symbols is not None:
            raise ValueError("use symbol or symbols, not both")
        if metric is not None and metrics is not None:
            raise ValueError("use metric or metrics, not both")
        if isinstance(symbols, (str, bytes)) or isinstance(metrics, (str, bytes)):
            raise TypeError("symbols and metrics must be sequences, not strings")
        symbol_set = None if symbols is None else tuple(dict.fromkeys(
            normalize_symbol(item) for item in symbols
        ))
        metric_set = None if metrics is None else tuple(dict.fromkeys(
            str(item).strip().lower() for item in metrics
        ))
        if (symbol_set is not None and len(symbol_set) > 500) or (
            metric_set is not None and len(metric_set) > 100
        ):
            raise ValueError("fundamental selection exceeds query bounds")
        for name in metric_set or ():
            fundamentals_as_of((), decision_at=cutoff, metric=name)
        if period_start is not None and (
            not isinstance(period_start, date) or isinstance(period_start, datetime)
        ):
            raise TypeError("period_start must be a date")
        if period_end is not None and (
            not isinstance(period_end, date) or isinstance(period_end, datetime)
        ):
            raise TypeError("period_end must be a date")
        if period_start is not None and period_end is not None and period_start > period_end:
            raise ValueError("period_start cannot follow period_end")
        clauses = ["available_at <= ?"]
        params: List[Any] = [_iso(cutoff)]
        # Scope the candidate read *before* revision resolution. Ambiguities
        # in unrelated securities/metrics must not poison a selected cohort.
        # An explicit empty sequence means no members, never all members.
        for column, values in (("symbol", symbol_set), ("metric", metric_set)):
            if values is not None:
                clauses.append(
                    column + " IN (" + ",".join("?" for _ in values) + ")"
                    if values else "0 = 1"
                )
                params.extend(values)
        normalized_symbol = normalize_symbol(symbol) if symbol is not None else None
        normalized_metric = str(metric).strip().lower() if metric is not None else None
        normalized_unit = str(unit).strip().lower() if unit is not None else None
        normalized_scope = str(scope).strip().lower() if scope is not None else None
        normalized_source = str(source).strip() if source is not None else None
        if normalized_symbol is not None:
            clauses.append("symbol = ?")
            params.append(normalized_symbol)
        if normalized_metric is not None:
            clauses.append("metric = ?")
            params.append(normalized_metric)
        if period_start is not None:
            clauses.append("period_start = ?")
            params.append(period_start.isoformat())
        if period_end is not None:
            clauses.append("period_end = ?")
            params.append(period_end.isoformat())
        if normalized_unit is not None:
            clauses.append("unit = ?")
            params.append(normalized_unit)
        if normalized_scope is not None:
            clauses.append("scope = ?")
            params.append(normalized_scope)
        if normalized_source is not None:
            clauses.append("source = ?")
            params.append(normalized_source)
        where = " AND ".join(clauses)
        with self._lock:
            connection = self._conn()
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM fundamental_observations WHERE " + where,
                    params,
                ).fetchone()[0]
            )
            rows = connection.execute(
                "SELECT * FROM fundamental_observations WHERE " + where +
                " ORDER BY symbol ASC, metric ASC, period_end DESC, "
                "available_at DESC, revision_id ASC, observation_id ASC",
                params,
            ).fetchall()
        observations = [self._row_to_fundamental(row) for row in rows]
        selected = fundamentals_as_of(
            observations,
            decision_at=cutoff,
            symbol=normalized_symbol,
            metric=normalized_metric,
            period_start=period_start,
            period_end=period_end,
            unit=normalized_unit,
            scope=normalized_scope,
            source=normalized_source,
        )
        return list(selected), total

    @staticmethod
    def _corporate_action_values(item: CorporateAction) -> Tuple[Any, ...]:
        return (
            item.action_id,
            item.symbol,
            item.action_type.value,
            item.ex_date.isoformat(),
            item.announcement_date.isoformat() if item.announcement_date else None,
            item.record_date.isoformat() if item.record_date else None,
            item.payable_date.isoformat() if item.payable_date else None,
            str(item.cash_amount) if item.cash_amount is not None else None,
            str(item.share_ratio) if item.share_ratio is not None else None,
            str(item.subscription_price) if item.subscription_price is not None else None,
            item.source,
            item.revision_id,
            _iso(item.received_at),
            _json(item.metadata),
            item.content_hash,
        )

    def insert_corporate_actions(self, actions: Sequence[CorporateAction]) -> int:
        """Append immutable company actions with idempotent redelivery.

        An ``action_id`` is a provider/revision identity.  Reusing it with a
        different content hash fails closed instead of silently changing the
        return model.  Revised economic terms must therefore use a new
        provider revision/action identity.
        """

        rows = tuple(actions)
        if not rows:
            return 0
        if any(not isinstance(item, CorporateAction) for item in rows):
            raise TypeError("actions must contain CorporateAction objects")
        values = [self._corporate_action_values(item) for item in rows]
        with self._lock, self._conn():
            connection = self._conn()
            inserted = 0
            seen: Dict[str, str] = {}
            for item, row in zip(rows, values):
                prior = seen.get(item.action_id)
                if prior is not None:
                    if prior != item.content_hash:
                        raise ValueError(
                            "conflicting corporate action_id {}".format(item.action_id)
                        )
                    continue
                seen[item.action_id] = item.content_hash
                existing = connection.execute(
                    "SELECT content_hash FROM corporate_actions WHERE action_id = ?",
                    (item.action_id,),
                ).fetchone()
                if existing is not None:
                    if str(existing[0]) != item.content_hash:
                        raise ValueError(
                            "conflicting corporate action_id {}".format(item.action_id)
                        )
                    continue
                connection.execute(
                    """
                    INSERT INTO corporate_actions (
                        action_id, symbol, action_type, ex_date,
                        announcement_date, record_date, payable_date,
                        cash_amount, share_ratio, subscription_price, source,
                        revision_id, received_at, metadata, content_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    row,
                )
                inserted += 1
        return inserted

    @staticmethod
    def _row_to_corporate_action(row: sqlite3.Row) -> CorporateAction:
        return CorporateAction(
            action_id=row["action_id"],
            symbol=row["symbol"],
            action_type=CorporateActionType(row["action_type"]),
            ex_date=date.fromisoformat(row["ex_date"]),
            announcement_date=(
                date.fromisoformat(row["announcement_date"])
                if row["announcement_date"] else None
            ),
            record_date=(
                date.fromisoformat(row["record_date"])
                if row["record_date"] else None
            ),
            payable_date=(
                date.fromisoformat(row["payable_date"])
                if row["payable_date"] else None
            ),
            cash_amount=(Decimal(row["cash_amount"]) if row["cash_amount"] is not None else None),
            share_ratio=(Decimal(row["share_ratio"]) if row["share_ratio"] is not None else None),
            subscription_price=(
                Decimal(row["subscription_price"])
                if row["subscription_price"] is not None else None
            ),
            source=row["source"],
            revision_id=row["revision_id"],
            received_at=parse_market_time(row["received_at"]),
            metadata=_load_json(row["metadata"]),
        )

    def query_corporate_actions(
        self,
        *,
        as_of: Optional[datetime] = None,
        symbol: Optional[str] = None,
        action_type: Optional[CorporateActionType] = None,
        ex_from: Optional[date] = None,
        ex_to: Optional[date] = None,
        source: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> Tuple[List[CorporateAction], int]:
        """Read company actions visible by receipt time and ex-date filters."""

        if ex_from is not None and (not isinstance(ex_from, date) or isinstance(ex_from, datetime)):
            raise TypeError("ex_from must be a date")
        if ex_to is not None and (not isinstance(ex_to, date) or isinstance(ex_to, datetime)):
            raise TypeError("ex_to must be a date")
        if ex_from is not None and ex_to is not None and ex_from > ex_to:
            raise ValueError("ex_from cannot follow ex_to")
        clauses: List[str] = []
        params: List[Any] = []
        if as_of is not None:
            clauses.append("received_at <= ?")
            params.append(_iso(ensure_market_time(as_of)))
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(normalize_symbol(symbol))
        if action_type is not None:
            parsed = action_type if isinstance(action_type, CorporateActionType) else CorporateActionType(action_type)
            clauses.append("action_type = ?")
            params.append(parsed.value)
        if ex_from is not None:
            clauses.append("ex_date >= ?")
            params.append(ex_from.isoformat())
        if ex_to is not None:
            clauses.append("ex_date <= ?")
            params.append(ex_to.isoformat())
        if source is not None:
            clauses.append("source = ?")
            params.append(str(source).strip())
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        bounded_limit = _limit(limit)
        bounded_offset = _offset(offset)
        with self._lock:
            connection = self._conn()
            total = int(connection.execute("SELECT COUNT(*) FROM corporate_actions" + where, params).fetchone()[0])
            rows = connection.execute(
                "SELECT * FROM corporate_actions" + where
                + " ORDER BY ex_date ASC, received_at ASC, action_id ASC LIMIT ? OFFSET ?",
                params + [bounded_limit, bounded_offset],
            ).fetchall()
        return [self._row_to_corporate_action(row) for row in rows], total

    @staticmethod
    def _adjustment_factor_values(item: AdjustmentFactor) -> Tuple[Any, ...]:
        return (
            item.factor_id,
            item.symbol,
            item.adjustment.value,
            item.session_date.isoformat(),
            item.benchmark_date.isoformat(),
            str(item.factor),
            item.factor_version,
            item.source,
            item.revision_id,
            item.input_hash,
            _iso(item.computed_at),
            item.content_hash,
        )

    def insert_adjustment_factors(self, factors: Sequence[AdjustmentFactor]) -> int:
        """Append immutable adjustment factors with idempotent redelivery."""

        rows = tuple(factors)
        if not rows:
            return 0
        if any(not isinstance(item, AdjustmentFactor) for item in rows):
            raise TypeError("factors must contain AdjustmentFactor objects")
        values = [self._adjustment_factor_values(item) for item in rows]
        with self._lock, self._conn():
            connection = self._conn()
            inserted = 0
            seen: Dict[str, str] = {}
            for item, row in zip(rows, values):
                prior = seen.get(item.factor_id)
                if prior is not None:
                    if prior != item.content_hash:
                        raise ValueError(
                            "conflicting adjustment factor_id {}".format(item.factor_id)
                        )
                    continue
                seen[item.factor_id] = item.content_hash
                existing = connection.execute(
                    "SELECT content_hash FROM adjustment_factors WHERE factor_id = ?",
                    (item.factor_id,),
                ).fetchone()
                if existing is not None:
                    if str(existing[0]) != item.content_hash:
                        raise ValueError(
                            "conflicting adjustment factor_id {}".format(item.factor_id)
                        )
                    continue
                connection.execute(
                    """
                    INSERT INTO adjustment_factors (
                        factor_id, symbol, adjustment, session_date,
                        benchmark_date, factor, factor_version, source,
                        revision_id, input_hash, computed_at, content_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    row,
                )
                inserted += 1
        return inserted

    @staticmethod
    def _row_to_adjustment_factor(row: sqlite3.Row) -> AdjustmentFactor:
        return AdjustmentFactor(
            factor_id=row["factor_id"],
            symbol=row["symbol"],
            adjustment=Adjustment(row["adjustment"]),
            session_date=date.fromisoformat(row["session_date"]),
            benchmark_date=date.fromisoformat(row["benchmark_date"]),
            factor=Decimal(row["factor"]),
            factor_version=row["factor_version"],
            source=row["source"],
            revision_id=row["revision_id"],
            input_hash=row["input_hash"],
            computed_at=parse_market_time(row["computed_at"]),
        )

    def query_adjustment_factors(
        self,
        *,
        as_of: Optional[datetime] = None,
        symbol: Optional[str] = None,
        adjustment: Optional[Adjustment] = None,
        session_from: Optional[date] = None,
        session_to: Optional[date] = None,
        factor_version: Optional[str] = None,
        limit: int = 500,
        offset: int = 0,
    ) -> Tuple[List[AdjustmentFactor], int]:
        """Read versioned factors without applying them to raw bars."""

        if session_from is not None and (not isinstance(session_from, date) or isinstance(session_from, datetime)):
            raise TypeError("session_from must be a date")
        if session_to is not None and (not isinstance(session_to, date) or isinstance(session_to, datetime)):
            raise TypeError("session_to must be a date")
        if session_from is not None and session_to is not None and session_from > session_to:
            raise ValueError("session_from cannot follow session_to")
        clauses: List[str] = []
        params: List[Any] = []
        if as_of is not None:
            clauses.append("computed_at <= ?")
            params.append(_iso(ensure_market_time(as_of)))
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(normalize_symbol(symbol))
        if adjustment is not None:
            parsed = adjustment if isinstance(adjustment, Adjustment) else Adjustment(adjustment)
            clauses.append("adjustment = ?")
            params.append(parsed.value)
        if session_from is not None:
            clauses.append("session_date >= ?")
            params.append(session_from.isoformat())
        if session_to is not None:
            clauses.append("session_date <= ?")
            params.append(session_to.isoformat())
        if factor_version is not None:
            clauses.append("factor_version = ?")
            params.append(str(factor_version).strip())
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        bounded_limit = _limit(limit)
        bounded_offset = _offset(offset)
        with self._lock:
            connection = self._conn()
            total = int(connection.execute("SELECT COUNT(*) FROM adjustment_factors" + where, params).fetchone()[0])
            rows = connection.execute(
                "SELECT * FROM adjustment_factors" + where
                + " ORDER BY session_date ASC, computed_at ASC, factor_id ASC LIMIT ? OFFSET ?",
                params + [bounded_limit, bounded_offset],
            ).fetchall()
        return [self._row_to_adjustment_factor(row) for row in rows], total

    @staticmethod
    def _universe_snapshot_key(snapshot: UniverseSnapshot) -> str:
        """Return the immutable identity for one PIT snapshot.

        The key deliberately excludes the members/content hash.  Reusing the
        same universe/session/cutoff/version with different members is a
        conflict, not a new row that may overwrite historical research input.
        """

        if not isinstance(snapshot, UniverseSnapshot):
            raise TypeError("snapshot must be a UniverseSnapshot")
        return hash_canonical(
            {
                "universe": snapshot.universe,
                "session_date": snapshot.session_date.isoformat(),
                "knowledge_cutoff": snapshot.knowledge_cutoff.isoformat(),
                "version": snapshot.version,
            },
            namespace="point-in-time-universe-snapshot-key-v1",
        )

    @staticmethod
    def _universe_snapshot_values(
        snapshot: UniverseSnapshot,
        *,
        source: str,
        recorded_at: datetime,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Tuple[Any, ...]:
        payload = snapshot.to_dict()
        return (
            SQLiteRepository._universe_snapshot_key(snapshot),
            snapshot.universe,
            snapshot.session_date.isoformat(),
            _iso(snapshot.knowledge_cutoff),
            snapshot.version,
            snapshot.content_hash,
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            str(source).strip() or "unknown",
            _iso(recorded_at),
            _json(metadata),
        )

    def insert_universe_snapshots(
        self,
        snapshots: Sequence[UniverseSnapshot],
        *,
        source: str = "unknown",
        recorded_at: Optional[datetime] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> int:
        """Append immutable PIT universe snapshots.

        Snapshot identity is the universe/session/knowledge-cutoff/version
        tuple.  Re-delivery with the same content hash is idempotent; a
        different hash for that identity fails closed.  This method never
        derives a snapshot from ``instruments`` or a live provider catalogue.
        Callers must construct and validate :class:`UniverseSnapshot` first.
        """

        rows = tuple(snapshots)
        if not rows:
            return 0
        if any(not isinstance(item, UniverseSnapshot) for item in rows):
            raise TypeError("snapshots must contain UniverseSnapshot objects")
        source_text = str(source).strip() if source is not None else ""
        if not source_text or "\x00" in source_text:
            raise ValueError("source cannot be empty or contain NUL")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        received = now_market_time() if recorded_at is None else ensure_market_time(recorded_at)
        values = [
            self._universe_snapshot_values(
                item,
                source=source_text,
                recorded_at=received,
                metadata=metadata,
            )
            for item in rows
        ]
        with self._lock, self._conn():
            connection = self._conn()
            inserted = 0
            seen: Dict[str, str] = {}
            for item, row in zip(rows, values):
                key = self._universe_snapshot_key(item)
                prior = seen.get(key)
                if prior is not None:
                    if prior != item.content_hash:
                        raise ValueError("conflicting universe snapshot {}".format(key))
                    continue
                seen[key] = item.content_hash
                existing = connection.execute(
                    "SELECT content_hash FROM universe_snapshots WHERE snapshot_key = ?",
                    (key,),
                ).fetchone()
                if existing is not None:
                    if str(existing[0]) != item.content_hash:
                        raise ValueError("conflicting universe snapshot {}".format(key))
                    continue
                connection.execute(
                    """
                    INSERT INTO universe_snapshots (
                        snapshot_key, universe, session_date, knowledge_cutoff,
                        version, content_hash, snapshot_json, source,
                        recorded_at, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    row,
                )
                inserted += 1
        return inserted

    def query_universe_snapshots_as_of(
        self,
        *,
        as_of: datetime,
        universe: Optional[str] = None,
        session_date: Optional[date] = None,
        version: Optional[str] = None,
        source: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> Tuple[List[UniverseSnapshot], int]:
        """Read only snapshots whose knowledge cutoff is visible at ``as_of``.

        The query is intentionally independent from the current instruments
        table.  If a stored payload or hash is corrupted, or its identity key
        no longer matches the payload, the query raises instead of returning a
        plausible but unauditable universe.
        """

        cutoff = ensure_market_time(as_of)
        if session_date is not None and (
            not isinstance(session_date, date) or isinstance(session_date, datetime)
        ):
            raise TypeError("session_date must be a date")
        # Both the knowledge cutoff and the local receipt time must be no
        # later than the decision time. Otherwise a snapshot imported today
        # could retroactively become evidence for an earlier backtest merely
        # because it describes an older historical session.
        clauses = ["knowledge_cutoff <= ?", "recorded_at <= ?"]
        params: List[Any] = [_iso(cutoff), _iso(cutoff)]
        normalized_universe = str(universe).strip() if universe is not None else None
        normalized_version = str(version).strip() if version is not None else None
        normalized_source = str(source).strip() if source is not None else None
        if normalized_universe:
            clauses.append("universe = ?")
            params.append(normalized_universe)
        if session_date is not None:
            clauses.append("session_date = ?")
            params.append(session_date.isoformat())
        if normalized_version:
            clauses.append("version = ?")
            params.append(normalized_version)
        if normalized_source:
            clauses.append("source = ?")
            params.append(normalized_source)
        where = " AND ".join(clauses)
        bounded_limit = _limit(limit)
        bounded_offset = _offset(offset)
        with self._lock:
            connection = self._conn()
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM universe_snapshots WHERE " + where,
                    params,
                ).fetchone()[0]
            )
            rows = connection.execute(
                "SELECT * FROM universe_snapshots WHERE " + where
                + " ORDER BY session_date ASC, knowledge_cutoff ASC, version ASC, "
                "snapshot_key ASC LIMIT ? OFFSET ?",
                params + [bounded_limit, bounded_offset],
            ).fetchall()
        snapshots: List[UniverseSnapshot] = []
        for row in rows:
            try:
                snapshot = UniverseSnapshot.from_dict(json.loads(row["snapshot_json"]))
            except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
                raise ValueError("invalid persisted universe snapshot") from exc
            if snapshot.content_hash != row["content_hash"]:
                raise ValueError("persisted universe snapshot content hash mismatch")
            if self._universe_snapshot_key(snapshot) != row["snapshot_key"]:
                raise ValueError("persisted universe snapshot identity mismatch")
            snapshots.append(snapshot)
        return snapshots, total

    def query_universe_snapshot_records_as_of(
        self,
        **kwargs: Any,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Return PIT snapshots together with persisted receipt provenance.

        ``query_universe_snapshots_as_of`` remains the convenient domain-only
        API.  This companion is used by HTTP/audit callers that must show the
        source, local receipt time and ingestion metadata without putting
        those persistence concerns into the immutable research contract.
        """

        snapshots, total = self.query_universe_snapshots_as_of(**kwargs)
        if not snapshots:
            return [], total
        keys = [self._universe_snapshot_key(item) for item in snapshots]
        placeholders = ",".join("?" for _ in keys)
        with self._lock:
            rows = self._conn().execute(
                "SELECT snapshot_key, source, recorded_at, metadata "
                "FROM universe_snapshots WHERE snapshot_key IN ({})".format(placeholders),
                keys,
            ).fetchall()
        by_key = {
            row["snapshot_key"]: row
            for row in rows
        }
        records: List[Dict[str, Any]] = []
        for snapshot in snapshots:
            key = self._universe_snapshot_key(snapshot)
            row = by_key.get(key)
            if row is None:
                raise ValueError("persisted universe snapshot provenance is missing")
            records.append(
                {
                    "snapshot": snapshot,
                    "snapshot_key": key,
                    "source": row["source"],
                    "recorded_at": parse_market_time(row["recorded_at"]),
                    "metadata": _load_json(row["metadata"]),
                }
            )
        return records, total

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
        """Return the first page of alerts (legacy convenience API)."""

        items, _ = self.query_alerts(
            symbol=symbol,
            level=level,
            acknowledged=acknowledged,
            limit=limit,
            offset=0,
        )
        return items

    def query_alerts(
        self,
        symbol: Optional[str] = None,
        level: Optional[AlertLevel] = None,
        acknowledged: Optional[bool] = None,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[Alert], int]:
        """Return a deterministic alert page and the matching total count.

        Keeping the count and page query under the repository lock gives API
        consumers a coherent pagination snapshot while preserving the old
        ``list_alerts`` method for internal callers.
        """

        page_limit = _limit(limit)
        page_offset = _offset(offset)
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
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            connection = self._conn()
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM alerts" + where, params
                ).fetchone()[0]
            )
            rows = connection.execute(
                "SELECT * FROM alerts{} ORDER BY timestamp DESC, id DESC "
                "LIMIT ? OFFSET ?".format(where),
                params + [page_limit, page_offset],
            ).fetchall()
        items = [
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
        return items, total

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

    def signal_exists(
        self,
        *,
        strategy: str,
        symbol: str,
        action: SignalAction,
        timestamp: datetime,
    ) -> bool:
        """Check the durable signal business key without loading its payload.

        Monitoring uses this read before publishing a newly evaluated signal.
        It closes the restart/replay gap left by the process-local cache while
        retaining the append-only signal history for audit purposes.
        """

        normalized_strategy = str(strategy).strip()
        if not normalized_strategy:
            raise ValueError("strategy cannot be empty")
        normalized_symbol = normalize_symbol(symbol)
        normalized_action = (
            action if isinstance(action, SignalAction) else SignalAction(action)
        )
        with self._lock:
            row = self._conn().execute(
                """
                SELECT 1 FROM signals
                WHERE strategy = ? AND symbol = ? AND action = ? AND timestamp = ?
                LIMIT 1
                """,
                (
                    normalized_strategy,
                    normalized_symbol,
                    normalized_action.value,
                    _iso(timestamp),
                ),
            ).fetchone()
        return row is not None

    def list_signals(
        self,
        symbol: Optional[str] = None,
        strategy: Optional[str] = None,
        action: Optional[SignalAction] = None,
        limit: int = 100,
    ) -> List[Signal]:
        """Return the first page of signals (legacy convenience API)."""

        items, _ = self.query_signals(
            symbol=symbol,
            strategy=strategy,
            action=action,
            limit=limit,
            offset=0,
        )
        return items

    def query_signals(
        self,
        symbol: Optional[str] = None,
        strategy: Optional[str] = None,
        action: Optional[SignalAction] = None,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[Signal], int]:
        """Return a deterministic signal page and matching total count."""

        page_limit = _limit(limit)
        page_offset = _offset(offset)
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
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            connection = self._conn()
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM signals" + where, params
                ).fetchone()[0]
            )
            rows = connection.execute(
                "SELECT * FROM signals{} ORDER BY timestamp DESC, id DESC "
                "LIMIT ? OFFSET ?".format(where),
                params + [page_limit, page_offset],
            ).fetchall()
        items = [
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
        return items, total
