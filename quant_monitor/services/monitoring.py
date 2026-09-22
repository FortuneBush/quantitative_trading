"""Long-running orchestration service for the local monitoring platform."""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from quant_monitor.data_sources import MarketDataProvider
from quant_monitor.domain import (
    Alert,
    AlertLevel,
    Bar,
    EventType,
    Metric,
    Quote,
    Signal,
    now_market_time,
    normalize_symbol,
)
from quant_monitor.storage import SQLiteRepository
from quant_monitor.market import expected_latest_daily_date
from quant_monitor.strategies import (
    CohortStrategy,
    CohortStrategyContext,
    Strategy,
    StrategyContext,
    strategy_parameter_schema,
)
from quant_monitor.research import strategy_code_identity
from quant_monitor.research import BANK_METRIC_SPECS, build_bank_fundamental_factors
from quant_monitor.research.manifest import hash_canonical
from quant_monitor.trading_calendar import TradingCalendar

from .collection import CollectionIssue, CollectionResult, MarketDataCollectionService
from .event_bus import EventBus


@dataclass(frozen=True)
class MonitoringCycleResult:
    collection: CollectionResult
    signals: Tuple[Signal, ...]


@dataclass(frozen=True)
class CollectionJobSnapshot:
    """Lifecycle snapshot for an explicitly requested one-shot collection.

    Jobs are durable API-operation snapshots. The collection-batch ledger
    remains the source of truth for provider attempts, while this record keeps
    retries and lifecycle state observable across process restarts.
    """

    job_id: str
    status: str
    requested_symbols: Tuple[str, ...]
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    iteration_before: Optional[int] = None
    iteration_after: Optional[int] = None
    quotes_count: int = 0
    bars_count: int = 0
    metrics_count: int = 0
    signals_count: int = 0
    error_type: Optional[str] = None

    @property
    def terminal(self) -> bool:
        return self.status in {"succeeded", "failed", "cancelled"}

    @property
    def operation(self) -> str:
        """Return the durable operation kind encoded by the job id.

        ``collector_jobs`` predates the quote-refresh API and deliberately has
        no free-form JSON metadata column.  Quote refresh jobs therefore use a
        reserved ``qr_`` id prefix.  Deriving the kind from the persisted id
        keeps restart recovery unambiguous without adding another schema
        migration (the collector-job ledger already exists in the current
        schema) and
        avoids treating a quote-only job as a full strategy cycle after a
        restart).  Legacy ids remain ``monitor_cycle``.
        """

        return "quote_refresh" if self.job_id.startswith("qr_") else "monitor_cycle"


class MonitoringService:
    """Manage polling, persistence, metrics, strategies, alerts and snapshots."""

    def __init__(
        self,
        provider: MarketDataProvider,
        repository: SQLiteRepository,
        symbols: Sequence[str],
        event_bus: Optional[EventBus] = None,
        strategies: Optional[Sequence[Strategy]] = None,
        cohort_strategies: Optional[Sequence[CohortStrategy]] = None,
        poll_interval: float = 3.0,
        bar_interval: str = "1m",
        bar_limit: int = 200,
        collect_bars: bool = True,
        trading_calendar: Optional[TradingCalendar] = None,
    ) -> None:
        normalized = tuple(dict.fromkeys(normalize_symbol(symbol) for symbol in symbols))
        if not normalized:
            raise ValueError("monitoring requires at least one symbol")
        if float(poll_interval) <= 0:
            raise ValueError("poll_interval must be positive")
        self.provider = provider
        self.repository = repository
        self.symbols = normalized
        self.event_bus = event_bus or EventBus()
        self.strategies = tuple(strategies or ())
        self.cohort_strategies = tuple(cohort_strategies or ())
        strategy_names = [
            strategy.name
            for strategy in self.strategies + self.cohort_strategies
        ]
        if len(strategy_names) != len(set(strategy_names)):
            raise ValueError("strategy names must be unique")
        self.poll_interval = float(poll_interval)
        self.collector = MarketDataCollectionService(
            provider=provider,
            repository=repository,
            event_bus=self.event_bus,
            bar_interval=bar_interval,
            bar_limit=bar_limit,
            collect_bars=collect_bars,
            trading_calendar=trading_calendar,
            quote_observer=self.remember_quotes,
        )
        self._task: Optional["asyncio.Task[None]"] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._cycle_lock: Optional[asyncio.Lock] = None
        self._state_lock = threading.RLock()
        self._iteration = 0
        self._started_at: Optional[datetime] = None
        self._last_collection_at: Optional[datetime] = None
        self._last_collection_duration_ms: Optional[float] = None
        self._last_error: Optional[str] = None
        # Process-lifetime cycle counters make the collector observable as a
        # service rather than only exposing its latest outcome.  They are
        # intentionally in-memory: durable collection-batch lineage remains
        # the source of truth for historical/audit reporting.
        self._collection_cycles_total = 0
        self._collection_cycles_succeeded = 0
        self._collection_cycles_failed = 0
        self._collection_cycles_partial = 0
        self._collection_cycles_cancelled = 0
        self._last_collection_outcome: Optional[str] = None
        self._latest_quotes: Dict[str, Quote] = {}
        self._latest_metrics: Dict[Tuple[str, str], Metric] = {}
        self._latest_signals: Dict[Tuple[str, str], Signal] = {}
        self._emitted_signal_times: Dict[Tuple[str, str, str], datetime] = {}
        # Bind every live signal to the effective strategy parameters and the
        # content-addressed implementation observed when this monitor starts.
        # The binding is cached per strategy name so a polling cycle does not
        # repeatedly hash the source tree; it is also copied into Signal
        # metadata before persistence/event publication for audit clients.
        self._strategy_bindings: Dict[str, Mapping[str, Any]] = {}
        self._last_issue_signature: Optional[Tuple[Tuple[str, str, str], ...]] = None
        self._collection_jobs: Dict[str, CollectionJobSnapshot] = {}
        self._collection_job_tasks: Dict[str, "asyncio.Task[None]"] = {}
        # Idempotency keys are deliberately kept as one-way digests.  A client
        # may safely retry a request after a network timeout without exposing
        # its key in snapshots or logs.  The digest and job lifecycle are also
        # persisted in SQLite so retries remain safe after a process restart.
        self._collection_job_idempotency: Dict[str, str] = {}
        self._collection_job_digests: Dict[str, str] = {}
        self._collection_jobs_loaded = False
        self._collection_job_limit = 100
        # Serialise manual-job admission across awaits.  The state lock is a
        # thread lock and cannot protect the idempotency lookup once that
        # synchronous SQLite operation is moved to a worker thread.
        self._collection_job_submit_lock: Optional[asyncio.Lock] = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _get_cycle_lock(self) -> asyncio.Lock:
        """Return the loop-bound lock that serializes complete collection cycles."""

        if self._cycle_lock is None:
            self._cycle_lock = asyncio.Lock()
        return self._cycle_lock

    def _get_collection_job_submit_lock(self) -> asyncio.Lock:
        """Return the loop-bound lock that serializes job admission."""

        if self._collection_job_submit_lock is None:
            self._collection_job_submit_lock = asyncio.Lock()
        return self._collection_job_submit_lock

    @staticmethod
    async def _sqlite_call(operation, *args, **kwargs):
        """Run a repository operation off-loop and drain it on cancellation.

        SQLiteRepository intentionally exposes a synchronous, lock-protected
        API.  A manual collector request can arrive while startup is
        restoring a large job ledger or while an antivirus process holds the
        database file, so calling it directly from an async lifecycle path
        would stall health checks and WebSocket heartbeats.  Shielding the
        worker future also guarantees that runtime shutdown never closes the
        connection while the worker is still committing.
        """

        pending = asyncio.ensure_future(asyncio.to_thread(operation, *args, **kwargs))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(pending)
            except BaseException:
                # Preserve the original cancellation; the operation's own
                # failure remains visible through its normal lifecycle path.
                pass
            raise

    def configure_symbols(self, symbols: Sequence[str]) -> Tuple[str, ...]:
        """Replace the monitored universe before the background task starts."""

        if self.running:
            raise RuntimeError("cannot reconfigure symbols while monitoring is running")
        normalized = tuple(
            dict.fromkeys(normalize_symbol(symbol) for symbol in symbols)
        )
        if not normalized:
            raise ValueError("monitoring requires at least one symbol")
        self.symbols = normalized
        return normalized

    def remember_quotes(self, quotes: Sequence[Quote]) -> None:
        """Update status from committed quotes without rolling time backwards.

        Manual refreshes publish through the same runtime event handler as
        polling. A slow strategy evaluation must not later overwrite those
        newer events with the earlier cycle's quotes.
        """

        with self._state_lock:
            for quote in quotes:
                if quote.symbol not in self.symbols:
                    continue
                previous = self._latest_quotes.get(quote.symbol)
                if previous is None or quote.timestamp >= previous.timestamp:
                    self._latest_quotes[quote.symbol] = quote

    def _job_payload(
        self, job: CollectionJobSnapshot, *, idempotency_replayed: bool = False
    ) -> Dict[str, Any]:
        """Return a JSON-friendly copy while keeping the domain record immutable."""

        return {
            "job_id": job.job_id,
            "operation": job.operation,
            "status": job.status,
            "terminal": job.terminal,
            "requested_symbols": list(job.requested_symbols),
            "created_at": job.created_at,
            "started_at": job.started_at,
            "completed_at": job.completed_at,
            "iteration_before": job.iteration_before,
            "iteration_after": job.iteration_after,
            "counts": {
                "quotes": job.quotes_count,
                "bars": job.bars_count,
                "metrics": job.metrics_count,
                "signals": job.signals_count,
            },
            "error_type": job.error_type,
            "idempotency_replayed": bool(idempotency_replayed),
        }

    @staticmethod
    def _snapshot_from_record(record: Mapping[str, Any]) -> CollectionJobSnapshot:
        """Convert a durable collector_jobs row into the API domain snapshot."""

        return CollectionJobSnapshot(
            job_id=str(record["job_id"]),
            status=str(record["status"]),
            requested_symbols=tuple(record.get("requested_symbols") or ()),
            created_at=record["created_at"],
            started_at=record.get("started_at"),
            completed_at=record.get("completed_at"),
            iteration_before=record.get("iteration_before"),
            iteration_after=record.get("iteration_after"),
            quotes_count=int(record.get("quotes_count") or 0),
            bars_count=int(record.get("bars_count") or 0),
            metrics_count=int(record.get("metrics_count") or 0),
            signals_count=int(record.get("signals_count") or 0),
            error_type=record.get("error_type"),
        )

    def _persist_collection_job(
        self, job: CollectionJobSnapshot, idempotency_key_digest: Optional[str] = None
    ) -> None:
        digest = idempotency_key_digest
        if digest is None:
            digest = self._collection_job_digests.get(job.job_id)
        self.repository.upsert_collection_job(
            job_id=job.job_id,
            idempotency_key_digest=digest,
            status=job.status,
            requested_symbols=job.requested_symbols,
            created_at=job.created_at,
            started_at=job.started_at,
            completed_at=job.completed_at,
            iteration_before=job.iteration_before,
            iteration_after=job.iteration_after,
            quotes_count=job.quotes_count,
            bars_count=job.bars_count,
            metrics_count=job.metrics_count,
            signals_count=job.signals_count,
            error_type=job.error_type,
        )

    def restore_collection_jobs(self) -> int:
        """Load durable manual jobs and close work interrupted by a restart.

        A provider request is never replayed automatically.  Any queued,
        running or cancelling job from a previous process is terminalized as
        ``failed`` with a typed ``ProcessRestarted`` reason, while successful
        and explicitly cancelled jobs remain available for idempotent retries.
        """

        if self._collection_jobs_loaded:
            return len(self._collection_jobs)
        if not self.repository.initialized:
            self.repository.initialize()
        records = self.repository.list_collection_job_records(1000)
        now = now_market_time()
        restored = []
        interrupted = []
        with self._state_lock:
            for record in records:
                job = self._snapshot_from_record(record)
                digest = record.get("idempotency_key_digest")
                if digest:
                    self._collection_job_idempotency[str(digest)] = job.job_id
                    self._collection_job_digests[job.job_id] = str(digest)
                if not job.terminal:
                    job = CollectionJobSnapshot(
                        job_id=job.job_id,
                        status="failed",
                        requested_symbols=job.requested_symbols,
                        created_at=job.created_at,
                        started_at=job.started_at,
                        completed_at=now,
                        iteration_before=job.iteration_before,
                        iteration_after=job.iteration_after,
                        quotes_count=job.quotes_count,
                        bars_count=job.bars_count,
                        metrics_count=job.metrics_count,
                        signals_count=job.signals_count,
                        error_type="ProcessRestarted",
                    )
                    interrupted.append(job)
                self._collection_jobs[job.job_id] = job
                restored.append(job)
            self._collection_jobs_loaded = True
            self._trim_collection_jobs_locked()
        for job in interrupted:
            # Persist only rows whose lifecycle changed during recovery.  The
            # upsert is harmless for terminal rows but avoiding it keeps startup
            # read-mostly and prevents needless SQLite writes.
            self._persist_collection_job(job)
        return len(restored)

    def collection_jobs(
        self, limit: int = 20, operation: Optional[str] = None
    ) -> Dict[str, Any]:
        """List recent explicit collection jobs without provider I/O."""

        self.restore_collection_jobs()
        bounded = max(1, min(int(limit), self._collection_job_limit))
        selected_operation = None if operation is None else str(operation).strip().lower()
        if selected_operation not in {None, "monitor_cycle", "quote_refresh"}:
            raise ValueError("unsupported collection job operation")
        with self._state_lock:
            all_jobs = [
                item
                for item in self._collection_jobs.values()
                if selected_operation is None or item.operation == selected_operation
            ]
            jobs = all_jobs[-bounded:]
            jobs.reverse()
            active = sum(not item.terminal for item in all_jobs)
            statuses = {
                "queued": 0,
                "running": 0,
                "cancelling": 0,
                "succeeded": 0,
                "failed": 0,
                "cancelled": 0,
                "unknown": 0,
            }
            for item in all_jobs:
                statuses[item.status if item.status in statuses else "unknown"] += 1
        return {
            "items": [self._job_payload(item) for item in jobs],
            "count": len(jobs),
            "active": active,
            "total": sum(statuses.values()),
            "by_status": statuses,
            "limit": bounded,
        }

    def collection_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        self.restore_collection_jobs()
        with self._state_lock:
            job = self._collection_jobs.get(str(job_id).strip())
        if job is None:
            record = self.repository.get_collection_job_record(str(job_id).strip())
            if record is not None:
                job = self._snapshot_from_record(record)
                with self._state_lock:
                    self._collection_jobs[job.job_id] = job
                    digest = record.get("idempotency_key_digest")
                    if digest:
                        self._collection_job_digests[job.job_id] = str(digest)
                        self._collection_job_idempotency[str(digest)] = job.job_id
        return self._job_payload(job) if job is not None else None

    async def submit_collection_job(
        self,
        idempotency_key: Optional[str] = None,
        *,
        requested_symbols: Optional[Sequence[str]] = None,
        quote_only: bool = False,
    ) -> Dict[str, Any]:
        """Schedule one explicit full monitoring cycle and return immediately.

        Only one manual job may be active at a time.  Normal background
        polling is still serialized by ``_cycle_lock``; this guard prevents a
        user repeatedly clicking refresh from building an unbounded queue.

        ``idempotency_key`` follows the usual HTTP retry contract: when a
        non-empty key is supplied, retries return the original job snapshot
        (including after it reaches a terminal state) instead of starting a
        second provider cycle.  The digest and lifecycle snapshot are also
        persisted, so the same guarantee survives a process restart; an
        interrupted non-terminal job is marked ``ProcessRestarted`` and is
        never replayed automatically.
        """

        selected_symbols = tuple(
            dict.fromkeys(
                normalize_symbol(symbol)
                for symbol in (requested_symbols if requested_symbols is not None else self.symbols)
            )
        )
        if not selected_symbols:
            raise ValueError("at least one symbol is required")

        key_digest: Optional[str] = None
        if idempotency_key is not None:
            key = str(idempotency_key).strip()
            if not key:
                raise ValueError("Idempotency-Key must not be empty")
            if len(key) > 255:
                raise ValueError("Idempotency-Key must be 255 characters or fewer")
            # Keep the historical collector-job digest stable while isolating
            # the new quote-refresh endpoint's idempotency namespace.  Reusing
            # one HTTP key across two endpoints must not return the wrong job
            # kind or accidentally suppress a requested refresh.
            digest_input = (
                "quote-refresh:" + key if quote_only else key
            )
            key_digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()

        # The lock spans the awaited restore/lookup/commit sequence.  Without
        # it, two concurrent requests with the same key could both miss the
        # durable idempotency row after the SQLite calls moved off-loop.
        async with self._get_collection_job_submit_lock():
            await self._sqlite_call(self.restore_collection_jobs)

            if key_digest is not None:
                with self._state_lock:
                    existing_id = self._collection_job_idempotency.get(key_digest)
                    existing = (
                        self._collection_jobs.get(existing_id)
                        if existing_id is not None
                        else None
                    )
                if existing is not None:
                    return self._job_payload(existing, idempotency_replayed=True)
                # A bounded in-memory history may have evicted the terminal
                # job; the durable index is still authoritative.  This lookup
                # is synchronous in SQLiteRepository, so keep it off-loop.
                if existing_id is None:
                    record = await self._sqlite_call(
                        self.repository.get_collection_job_by_idempotency,
                        key_digest,
                    )
                    if record is not None:
                        existing = self._snapshot_from_record(record)
                        with self._state_lock:
                            self._collection_jobs[existing.job_id] = existing
                            self._collection_job_idempotency[key_digest] = existing.job_id
                            self._collection_job_digests[existing.job_id] = key_digest
                        return self._job_payload(existing, idempotency_replayed=True)

            with self._state_lock:
                if any(not item.terminal for item in self._collection_jobs.values()):
                    raise RuntimeError("a collection job is already running")
                now = now_market_time()
                job = CollectionJobSnapshot(
                    job_id=("qr_" if quote_only else "") + uuid.uuid4().hex,
                    status="queued",
                    requested_symbols=selected_symbols,
                    created_at=now,
                    iteration_before=self._iteration,
                )
                self._collection_jobs[job.job_id] = job
                if key_digest is not None:
                    self._collection_job_idempotency[key_digest] = job.job_id
                    self._collection_job_digests[job.job_id] = key_digest
                self._trim_collection_jobs_locked()

            try:
                await self._sqlite_call(
                    self._persist_collection_job, job, key_digest
                )
            except BaseException:
                # Do not leave an in-memory queued job that was never durably
                # admitted; callers can safely retry after a transient DB
                # failure or cancellation.
                with self._state_lock:
                    self._collection_jobs.pop(job.job_id, None)
                    self._collection_job_digests.pop(job.job_id, None)
                    if key_digest is not None:
                        self._collection_job_idempotency.pop(key_digest, None)
                raise

            task = asyncio.create_task(
                self._run_collection_job(job.job_id),
                name="collection-job-{}".format(job.job_id[:8]),
            )
            self._collection_job_tasks[job.job_id] = task
            return self._job_payload(job)

    def _trim_collection_jobs_locked(self) -> None:
        terminal_ids = [
            key for key, item in self._collection_jobs.items() if item.terminal
        ]
        while len(self._collection_jobs) > self._collection_job_limit and terminal_ids:
            oldest = terminal_ids.pop(0)
            self._collection_jobs.pop(oldest, None)
            for digest, job_id in tuple(self._collection_job_idempotency.items()):
                if job_id == oldest:
                    self._collection_job_idempotency.pop(digest, None)
            self._collection_job_digests.pop(oldest, None)

    def _updated_collection_job(
        self, job_id: str, changes: Mapping[str, Any]
    ) -> Optional[CollectionJobSnapshot]:
        """Build and publish an immutable in-memory job snapshot.

        Keeping this construction separate lets async lifecycle callers commit
        the durable row through ``_sqlite_call`` without holding the state
        lock across an await.
        """

        with self._state_lock:
            current = self._collection_jobs.get(job_id)
            if current is None:
                return None
            updated = CollectionJobSnapshot(
                job_id=current.job_id,
                status=changes.get("status", current.status),
                requested_symbols=current.requested_symbols,
                created_at=current.created_at,
                started_at=changes.get("started_at", current.started_at),
                completed_at=changes.get("completed_at", current.completed_at),
                iteration_before=changes.get("iteration_before", current.iteration_before),
                iteration_after=changes.get("iteration_after", current.iteration_after),
                quotes_count=changes.get("quotes_count", current.quotes_count),
                bars_count=changes.get("bars_count", current.bars_count),
                metrics_count=changes.get("metrics_count", current.metrics_count),
                signals_count=changes.get("signals_count", current.signals_count),
                error_type=changes.get("error_type", current.error_type),
            )
            self._collection_jobs[job_id] = updated
        return updated

    def _update_collection_job(self, job_id: str, **changes: Any) -> None:
        """Synchronous compatibility wrapper for non-async recovery callers."""

        updated = self._updated_collection_job(job_id, changes)
        if updated is not None:
            self._persist_collection_job(updated)

    async def _update_collection_job_async(self, job_id: str, **changes: Any) -> None:
        """Update memory immediately, then persist without blocking the loop."""

        updated = self._updated_collection_job(job_id, changes)
        if updated is not None:
            await self._sqlite_call(self._persist_collection_job, updated)

    async def _run_collection_job(self, job_id: str) -> None:
        started = now_market_time()
        try:
            await self._update_collection_job_async(
                job_id, status="running", started_at=started
            )
            with self._state_lock:
                job_snapshot = self._collection_jobs.get(job_id)
            if job_snapshot is None:
                raise RuntimeError("collection job disappeared before execution")
            if job_snapshot.operation == "quote_refresh":
                # Quote refreshes intentionally do not run bars, indicators or
                # strategies.  This is the asynchronous replacement for the
                # compatibility GET refresh=true path and keeps its side
                # effects observable under the same durable job lifecycle.
                quote_result = await self.collector.collect_quote_snapshot(
                    job_snapshot.requested_symbols, relaxed=True
                )
                result = None
            else:
                quote_result = None
                result = await self.collect_once()
        except asyncio.CancelledError:
            # This also covers cancellation while the initial ``running``
            # transition is waiting for SQLite.  Keep the durable lifecycle
            # terminal whenever the worker can still drain its commit.
            await self._update_collection_job_async(
                job_id,
                status="cancelled",
                completed_at=now_market_time(),
                iteration_after=self._iteration,
            )
            raise
        except Exception as exc:
            # Keep provider messages out of the job API; the lineage ledger and
            # alert stream carry the detailed local diagnostic where allowed.
            await self._update_collection_job_async(
                job_id,
                status="failed",
                completed_at=now_market_time(),
                iteration_after=self._iteration,
                error_type=type(exc).__name__,
            )
        else:
            if quote_result is not None:
                await self._update_collection_job_async(
                    job_id,
                    status="succeeded",
                    completed_at=now_market_time(),
                    iteration_after=self._iteration,
                    quotes_count=len(quote_result.quotes),
                    bars_count=0,
                    metrics_count=0,
                    signals_count=0,
                )
                return
            await self._update_collection_job_async(
                job_id,
                status="succeeded",
                completed_at=now_market_time(),
                iteration_after=self._iteration,
                quotes_count=len(result.collection.quotes),
                bars_count=len(result.collection.bars),
                metrics_count=len(result.collection.metrics),
                signals_count=len(result.signals),
            )
        finally:
            self._collection_job_tasks.pop(job_id, None)

    async def submit_quote_refresh_job(
        self,
        symbols: Sequence[str],
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Queue an asynchronous, quote-only provider refresh.

        The operation shares the durable collector-job ledger, admission limit,
        idempotency namespace, cancellation and restart recovery guarantees of
        the full monitoring-cycle jobs, while deliberately avoiding bars,
        indicators and strategy evaluation.  ``symbols`` is normalized before
        admission so retries and lineage use a deterministic request scope.
        """

        normalized = tuple(dict.fromkeys(normalize_symbol(item) for item in symbols))
        if not normalized:
            raise ValueError("at least one symbol is required")
        return await self.submit_collection_job(
            idempotency_key,
            requested_symbols=normalized,
            quote_only=True,
        )

    async def cancel_collection_job(
        self, job_id: str, grace_seconds: float = 2.0
    ) -> Optional[Dict[str, Any]]:
        """Request cancellation and wait briefly for provider cleanup."""

        key = str(job_id).strip()
        # Cancellation is a first-class lifecycle operation and must work
        # immediately after a process restart as well.  The HTTP cancel route
        # can be the first request for a job, so do not rely on a preceding
        # GET (or on ``start()``) having hydrated the durable ledger into the
        # in-memory cache.  Keep the recovery scan off the event loop just as
        # the submit/start paths do.
        await self._sqlite_call(self.restore_collection_jobs)
        with self._state_lock:
            job = self._collection_jobs.get(key)
            task = self._collection_job_tasks.get(key)
        if job is None:
            return None
        if job.terminal or task is None:
            # ``collection_job`` may restore a durable row from SQLite.  This
            # branch is reached by the async HTTP cancellation endpoint too,
            # so keep the synchronous repository lookup off the event loop.
            return await self._sqlite_call(self.collection_job, key)
        await self._update_collection_job_async(key, status="cancelling")
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=max(0.0, float(grace_seconds)))
        except asyncio.TimeoutError:
            # The task remains observable as cancelling; its own finally block
            # will remove the task once a provider honors cancellation.
            return await self._sqlite_call(self.collection_job, key)
        except asyncio.CancelledError:
            pass
        return await self._sqlite_call(self.collection_job, key)

    async def start(self, collect_immediately: bool = True) -> None:
        """Start background polling.  Calling twice is harmless."""

        if self.running:
            return
        if not self.repository.initialized:
            await self._sqlite_call(self.repository.initialize)
        # Restore before scheduling the polling loop so direct users of
        # MonitoringService (outside ApplicationRuntime) receive the same
        # restart guarantees as the HTTP application.  The repository API is
        # synchronous; keep cold-start scans and ProcessRestarted updates off
        # the event loop.  ApplicationRuntime may already have hydrated this
        # cache, in which case this is a cheap no-op in the worker thread.
        await self._sqlite_call(self.restore_collection_jobs)
        self._stop_event = asyncio.Event()
        with self._state_lock:
            self._started_at = now_market_time()
        self._task = asyncio.create_task(
            self._run(collect_immediately=collect_immediately), name="quant-monitor"
        )

    async def stop(
        self,
        close_provider: bool = False,
        grace_seconds: float = 2.0,
    ) -> None:
        """Stop polling, cancelling an in-flight cycle after a bounded grace period."""

        grace = float(grace_seconds)
        if grace < 0:
            raise ValueError("grace_seconds cannot be negative")

        # Manual jobs share the provider and repository with the polling task.
        # Cancel them before the runtime closes either resource; otherwise a
        # process shutdown could leave a request writing into a closed DB.
        with self._state_lock:
            manual_tasks = tuple(self._collection_job_tasks.values())
        if manual_tasks:
            for manual_task in manual_tasks:
                if not manual_task.done():
                    manual_task.cancel()
            await asyncio.gather(*manual_tasks, return_exceptions=True)

        task = self._task
        if task is not None:
            if self._stop_event is not None:
                self._stop_event.set()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=grace)
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            except asyncio.CancelledError:
                if not task.cancelled():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise
            finally:
                if self._task is task:
                    self._task = None
                    self._stop_event = None
        if close_provider:
            await self.provider.close()

    async def _run(self, collect_immediately: bool = True) -> None:
        if not collect_immediately and self._stop_event is not None:
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.poll_interval
                )
            except asyncio.TimeoutError:
                pass
        while self._stop_event is not None and not self._stop_event.is_set():
            try:
                await self.collect_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # collect_once already records and publishes the actionable error.
                pass
            if self._stop_event is None or self._stop_event.is_set():
                break
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass

    async def collect_once(self) -> MonitoringCycleResult:
        """Run one complete monitoring cycle, useful for APIs and tests."""

        async with self._get_cycle_lock():
            started = time.monotonic()
            with self._state_lock:
                self._collection_cycles_total += 1
                self._last_collection_outcome = "running"
            try:
                collection = await self.collector.collect(self.symbols)
                # Quote state is updated at commit time by the collector.
                # Reapplying it here (after bars/strategy work) could undo a
                # concurrent same-timestamp quote correction.
                signals = await self._evaluate_strategies(collection)
                issue_message = self._issue_message(collection.issues)
                with self._state_lock:
                    self._iteration += 1
                    self._collection_cycles_succeeded += 1
                    if collection.issues:
                        self._collection_cycles_partial += 1
                    self._last_collection_outcome = (
                        "partial" if collection.issues else "succeeded"
                    )
                    self._last_collection_at = now_market_time()
                    self._last_collection_duration_ms = round(
                        (time.monotonic() - started) * 1000.0,
                        3,
                    )
                    self._last_error = issue_message
                    for metric in collection.metrics:
                        self._latest_metrics[(metric.symbol, metric.name)] = metric
                    for signal in signals:
                        self._latest_signals[(signal.strategy, signal.symbol)] = signal
                await self._publish_collection_issues(collection.issues)
                return MonitoringCycleResult(collection, tuple(signals))
            except asyncio.CancelledError:
                with self._state_lock:
                    self._collection_cycles_cancelled += 1
                    self._last_collection_outcome = "cancelled"
                raise
            except Exception as exc:
                with self._state_lock:
                    self._collection_cycles_failed += 1
                    self._last_collection_outcome = "failed"
                    self._last_collection_duration_ms = round(
                        (time.monotonic() - started) * 1000.0,
                        3,
                    )
                await self._record_error(exc)
                raise

    async def evaluate_cached_collection(
        self,
        collection: CollectionResult,
    ) -> Tuple[Signal, ...]:
        """Evaluate a durable externally-collected snapshot once.

        The AShareHub history worker persists one symbol at a time and invokes
        this only after a coherent cross-section is assembled from SQLite.  It
        deliberately does no network I/O and shares the cycle lock with normal
        polling so strategy state and signal de-duplication stay serialized.
        """

        async with self._get_cycle_lock():
            signals = await self._evaluate_strategies(collection)
            with self._state_lock:
                for metric in collection.metrics:
                    self._latest_metrics[(metric.symbol, metric.name)] = metric
                for signal in signals:
                    self._latest_signals[(signal.strategy, signal.symbol)] = signal
            return tuple(signals)

    @staticmethod
    def _issue_message(issues: Sequence[CollectionIssue]) -> Optional[str]:
        if not issues:
            return None
        return "; ".join(
            "{} {}: {}".format(issue.component, issue.error_type, issue.message)
            for issue in issues
        )

    async def _publish_collection_issues(
        self, issues: Sequence[CollectionIssue]
    ) -> None:
        signature = tuple(
            (issue.component, issue.error_type, issue.message) for issue in issues
        )
        if not signature:
            self._last_issue_signature = None
            return
        if signature == self._last_issue_signature:
            return
        self._last_issue_signature = signature
        for issue in issues:
            alert = Alert(
                symbol=None,
                timestamp=now_market_time(),
                level=AlertLevel.WARNING,
                title="Partial market-data collection",
                message="{}: {}".format(issue.component, issue.message),
                category="collection",
                metadata={
                    "provider": self.provider.name,
                    "component": issue.component,
                    "error_type": issue.error_type,
                },
            )
            self.repository.insert_alert(alert)
            await self.event_bus.publish(EventType.ALERT, alert)

    def _signal_already_emitted(self, signal: Signal) -> bool:
        """Apply in-memory and durable checks for one signal business key."""

        key = (signal.strategy, signal.symbol, signal.action.value)
        if self._emitted_signal_times.get(key) == signal.timestamp:
            return True
        # A fresh service instance has an empty in-memory map.  Consult the
        # append-only SQLite history before publishing so replaying the same
        # latest snapshot after a restart does not create a duplicate row.
        if self.repository.signal_exists(
            strategy=signal.strategy,
            symbol=signal.symbol,
            action=signal.action,
            timestamp=signal.timestamp,
        ):
            self._emitted_signal_times[key] = signal.timestamp
            return True
        self._emitted_signal_times[key] = signal.timestamp
        return False

    def _strategy_binding(self, strategy: object) -> Mapping[str, Any]:
        """Return a deterministic implementation/configuration binding.

        A persisted signal historically carried only ``strategy`` (a human
        label).  That is insufficient when thresholds are changed in-place or
        a deployment runs a different source revision.  Built-ins expose a
        content-addressed code identity; custom strategies remain explicitly
        ``unavailable`` rather than receiving a misleading fallback version.
        """

        name = str(getattr(strategy, "name", "")).strip()
        if not name:
            raise ValueError("strategy name cannot be empty")
        cached = self._strategy_bindings.get(name)
        if cached is not None:
            return cached
        parameters = {
            str(item["name"]): item.get("value")
            for item in strategy_parameter_schema(strategy)
            if isinstance(item, Mapping) and isinstance(item.get("name"), str)
        }
        descriptor: Dict[str, Any] = {
            "schema_version": "live-strategy-binding-v1",
            "strategy_name": name,
            "parameters": parameters,
        }
        try:
            identity = dict(strategy_code_identity(name))
        except (FileNotFoundError, OSError, ValueError):
            identity = {"status": "unavailable"}
        if identity.get("status") == "unavailable":
            descriptor["code_identity_status"] = "unavailable"
            descriptor["code_version"] = None
        else:
            descriptor["code_identity_status"] = "verified_local_source"
            descriptor["code_version"] = identity.get("version")
        descriptor["binding_hash"] = hash_canonical(
            descriptor, namespace="live-strategy-binding-v1"
        )
        frozen = dict(descriptor)
        self._strategy_bindings[name] = frozen
        return frozen

    def _bind_signal(self, strategy: object, signal: Signal) -> Signal:
        """Attach the strategy binding without mutating the strategy output."""

        metadata = dict(signal.metadata)
        # A strategy is untrusted at this boundary: it may return arbitrary
        # metadata, including a forged code version or parameter digest.  The
        # monitoring service is the authority that knows the effective
        # implementation, so always replace (rather than ``setdefault``) the
        # binding before persistence and event publication.
        metadata["strategy_binding"] = dict(self._strategy_binding(strategy))
        return replace(signal, metadata=metadata)

    async def _evaluate_strategies(self, collection: CollectionResult) -> List[Signal]:
        if not self.strategies and not self.cohort_strategies:
            return []
        bars_by_symbol: Dict[str, List[Bar]] = {symbol: [] for symbol in self.symbols}
        metrics_by_symbol: Dict[str, List[Metric]] = {symbol: [] for symbol in self.symbols}
        quotes_by_symbol = {quote.symbol: quote for quote in collection.quotes}
        for bar in collection.bars:
            bars_by_symbol.setdefault(bar.symbol, []).append(bar)
        for metric in collection.metrics:
            metrics_by_symbol.setdefault(metric.symbol, []).append(metric)

        emitted: List[Signal] = []
        contexts: Dict[str, StrategyContext] = {}
        for symbol in self.symbols:
            quote = quotes_by_symbol.get(symbol)
            bars = tuple(bars_by_symbol.get(symbol, ()))
            metrics = tuple(metrics_by_symbol.get(symbol, ()))
            timestamp = quote.timestamp if quote is not None else (
                bars[-1].timestamp if bars else now_market_time()
            )
            context = StrategyContext(symbol, timestamp, quote, bars, metrics)
            contexts[symbol] = context
            for strategy in self.strategies:
                try:
                    candidates = strategy.evaluate(context)
                    for signal in candidates:
                        if signal.strategy != strategy.name or signal.symbol != symbol:
                            raise ValueError(
                                "strategy {} returned a mismatched signal".format(strategy.name)
                            )
                        bound_signal = self._bind_signal(strategy, signal)
                        if self._signal_already_emitted(bound_signal):
                            continue
                        emitted.append(bound_signal)
                except Exception as exc:
                    alert = Alert(
                        symbol=symbol,
                        timestamp=now_market_time(),
                        level=AlertLevel.WARNING,
                        title="Strategy evaluation failed",
                        message="{}: {}".format(strategy.name, exc),
                        category="strategy",
                        metadata={"strategy": strategy.name, "error_type": type(exc).__name__},
                    )
                    self.repository.insert_alert(alert)
                    await self.event_bus.publish(EventType.ALERT, alert)

        if self.cohort_strategies and contexts:
            cohort_timestamp = max(item.timestamp for item in contexts.values())
            fundamental_factor_report = None
            try:
                observations, _candidate_count = await asyncio.to_thread(
                    self.repository.query_fundamentals_as_of,
                    decision_at=cohort_timestamp,
                    symbols=tuple(contexts),
                    metrics=tuple(spec.name for spec in BANK_METRIC_SPECS),
                )
                fundamental_factor_report = build_bank_fundamental_factors(
                    observations,
                    decision_at=cohort_timestamp,
                    symbols=tuple(contexts),
                ).to_dict()
            except (TypeError, ValueError) as exc:
                # An ambiguous financial revision must not stop price-only
                # monitoring; the strategy receives an explicit unavailable
                # report and records a price-only fallback reason.
                fundamental_factor_report = {
                    "selection": "bank_fundamental_factors_as_of_v1",
                    "decision_at": cohort_timestamp.isoformat(),
                    "quality": {"status": "invalid", "complete_count": 0},
                    "items": [],
                    "warnings": ["fundamental_selection_ambiguous"],
                    "error_type": type(exc).__name__,
                }
            cohort_context = CohortStrategyContext(
                timestamp=cohort_timestamp,
                members=contexts,
                cohort="a_share_banks",
                metadata={
                    "price_basis": "raw_unadjusted",
                    "expected_bar_date": expected_latest_daily_date().isoformat(),
                    "calendar": "weekday_fallback",
                    # Mock is an explicit offline fixture.  For live
                    # providers, collection observations are currently
                    # degraded/unverified and must not produce user-facing
                    # bank BUY/SELL signals until accepted lineage exists.
                    "strategy_decision_ready": self.provider.name == "mock",
                    "fundamental_factor_report": fundamental_factor_report,
                },
            )
            for strategy in self.cohort_strategies:
                try:
                    candidates = strategy.evaluate(cohort_context)
                    for signal in candidates:
                        if (
                            signal.strategy != strategy.name
                            or signal.symbol not in contexts
                        ):
                            raise ValueError(
                                "cohort strategy {} returned a mismatched signal".format(
                                    strategy.name
                                )
                            )
                        bound_signal = self._bind_signal(strategy, signal)
                        if self._signal_already_emitted(bound_signal):
                            continue
                        emitted.append(bound_signal)
                except Exception as exc:
                    alert = Alert(
                        symbol=None,
                        timestamp=now_market_time(),
                        level=AlertLevel.WARNING,
                        title="Cohort strategy evaluation failed",
                        message="{}: {}".format(strategy.name, exc),
                        category="strategy",
                        metadata={
                            "strategy": strategy.name,
                            "evaluation_scope": "cross_sectional",
                            "error_type": type(exc).__name__,
                        },
                    )
                    self.repository.insert_alert(alert)
                    await self.event_bus.publish(EventType.ALERT, alert)
        self.repository.insert_signals(emitted)
        await asyncio.gather(
            *(self.event_bus.publish(EventType.SIGNAL, signal) for signal in emitted)
        )
        return emitted

    async def _record_error(self, exc: Exception) -> None:
        message = "{}: {}".format(type(exc).__name__, exc)
        with self._state_lock:
            self._last_error = message
        signature = (("fatal", type(exc).__name__, str(exc)),)
        if signature == self._last_issue_signature:
            return
        self._last_issue_signature = signature
        if not self.repository.initialized:
            self.repository.initialize()
        alert = Alert(
            symbol=None,
            timestamp=now_market_time(),
            level=AlertLevel.CRITICAL,
            title="Market-data collection failed",
            message=message,
            category="collection",
            metadata={"provider": self.provider.name, "error_type": type(exc).__name__},
        )
        self.repository.insert_alert(alert)
        await self.event_bus.publish(EventType.ALERT, alert)
        await self.event_bus.publish(
            EventType.ERROR,
            exc,
            component="monitoring",
            provider=self.provider.name,
        )

    def current_snapshot(self) -> Mapping[str, Any]:
        """Return a stable copy suitable for adapting into an API response."""

        with self._state_lock:
            recent_jobs = list(self._collection_jobs.values())[-5:]
            active_jobs = sum(not item.terminal for item in self._collection_jobs.values())
            # Keep lifecycle counters in the snapshot so operators can alert
            # on a growing queue or a failed/restarted job without scraping
            # and interpreting the bounded ``recent`` payload.  Unknown
            # statuses are retained under ``unknown`` rather than silently
            # discarded, making future lifecycle additions observable.
            status_counts = {
                "queued": 0,
                "running": 0,
                "cancelling": 0,
                "succeeded": 0,
                "failed": 0,
                "cancelled": 0,
                "unknown": 0,
            }
            for item in self._collection_jobs.values():
                status_counts[item.status if item.status in status_counts else "unknown"] += 1
            return {
                "running": self.running,
                "provider": self.provider.name,
                "symbols": self.symbols,
                "iteration": self._iteration,
                "started_at": self._started_at,
                "last_collection_at": self._last_collection_at,
                "last_collection_duration_ms": self._last_collection_duration_ms,
                "last_error": self._last_error,
                "cycle_stats": {
                    "total": self._collection_cycles_total,
                    "succeeded": self._collection_cycles_succeeded,
                    "failed": self._collection_cycles_failed,
                    "partial": self._collection_cycles_partial,
                    "cancelled": self._collection_cycles_cancelled,
                    "last_outcome": self._last_collection_outcome,
                },
                "quotes": dict(self._latest_quotes),
                "metrics": tuple(self._latest_metrics.values()),
                "signals": tuple(self._latest_signals.values()),
                "jobs": {
                    "active": active_jobs,
                    "total": len(self._collection_jobs),
                    "by_status": status_counts,
                    "recent": [self._job_payload(item) for item in reversed(recent_jobs)],
                },
            }
