"""One-shot market-data collection and persistence service."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from quant_monitor.data_sources import (
    BarFetchResult,
    BarFetchTransport,
    MarketDataProvider,
    MarketDataProviderError,
)
from quant_monitor.domain import (
    Bar,
    CollectionBatch,
    CollectionBatchStatus,
    EventType,
    Metric,
    ObservationQualityStatus,
    Quote,
    ensure_market_time,
    normalized_request_hash,
    normalize_symbol,
    now_market_time,
    observation_from_bar,
)
from quant_monitor.storage import SQLiteRepository
from quant_monitor.trading_calendar import (
    DEFAULT_TRADING_CALENDAR,
    CalendarRangeError,
    TradingCalendar,
)

from .event_bus import EventBus
from .indicators import IndicatorCalculator


@dataclass(frozen=True)
class CollectionIssue:
    component: str
    error_type: str
    message: str


@dataclass(frozen=True)
class CollectionResult:
    quotes: Tuple[Quote, ...]
    bars: Tuple[Bar, ...]
    metrics: Tuple[Metric, ...]
    issues: Tuple[CollectionIssue, ...] = ()


@dataclass(frozen=True)
class BarCollectionResult:
    bars: Tuple[Bar, ...]
    metrics: Tuple[Metric, ...]
    # Exposed for API callers that need to trace an on-demand refresh back to
    # its immutable collection-batch ledger entry.
    batch_id: Optional[str] = None
    transport: Optional[BarFetchTransport] = None


class QuoteSnapshotError(MarketDataProviderError, ValueError):
    """A normalized quote response violated the snapshot contract."""


@dataclass(frozen=True)
class QuoteCollectionResult:
    quotes: Tuple[Quote, ...]
    requested_symbols: Tuple[str, ...]
    received_symbols: Tuple[str, ...]
    stale_symbols: Tuple[str, ...] = ()
    provider_operations: int = 1


class MarketDataCollectionService:
    """Fetch a coherent batch, persist it, then notify in-process consumers."""

    def __init__(
        self,
        provider: MarketDataProvider,
        repository: SQLiteRepository,
        event_bus: EventBus,
        bar_interval: str = "1m",
        bar_limit: int = 200,
        collect_bars: bool = True,
        indicator_calculator: type = IndicatorCalculator,
        clock=now_market_time,
        trading_calendar: Optional[TradingCalendar] = None,
        quote_observer: Optional[Callable[[Sequence[Quote]], None]] = None,
    ) -> None:
        if int(bar_limit) <= 0:
            raise ValueError("bar_limit must be positive")
        self.provider = provider
        self.repository = repository
        self.event_bus = event_bus
        self.bar_interval = str(bar_interval).strip().lower()
        self.bar_limit = int(bar_limit)
        self.collect_bars_enabled = bool(collect_bars)
        self.indicator_calculator = indicator_calculator
        self._clock = clock
        self._quote_observer = quote_observer
        # Share ingestion/publication ordering between background collection
        # and explicit API refreshes using this collector. Provider I/O stays
        # outside this lock; late responses are checked against durable state.
        self._quote_ingestion_lock: Optional[asyncio.Lock] = None
        # Keep ingestion and downstream quality checks on the same calendar.
        # The default remains the explicitly-degraded weekday fallback.
        self.trading_calendar = trading_calendar or DEFAULT_TRADING_CALENDAR

    def _now(self) -> datetime:
        return ensure_market_time(self._clock())

    @staticmethod
    async def _sqlite_call(operation, *args, **kwargs):
        """Run one repository operation off-loop and drain it on cancellation.

        ``asyncio.to_thread`` cannot interrupt a running worker thread.  If a
        collection task were cancelled while SQLite was committing, runtime
        shutdown could close the connection before that worker finished.  By
        retaining the future and draining it before propagating cancellation,
        we preserve the repository's transaction boundary and make shutdown
        safe even when a Windows file lock delays the write.
        """

        pending = asyncio.ensure_future(asyncio.to_thread(operation, *args, **kwargs))
        try:
            # Shield the worker future so cancellation of the collection task
            # does not cancel the underlying thread operation.  The except
            # branch can then drain it before propagating cancellation.
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(pending)
            except BaseException:
                # Preserve the original cancellation.  The operation's own
                # exception is still observable through its repository batch
                # status or the caller's normal error path.
                pass
            raise

    @staticmethod
    def _provider_call_options(options: Mapping[str, Any]) -> Mapping[str, Any]:
        """Keep only documented fetch options when calling an adapter.

        Lineage may retain arbitrary *sanitized* public coordinates for audit,
        but adapters must not receive transport-only metadata such as nested
        HTTP headers.  The common provider contract currently exposes only
        historical bounds and AShareHub's quota reservation knob here.
        """

        allowed = {"start", "end", "reserve_requests"}
        return {key: value for key, value in options.items() if key in allowed}

    def _daily_observation_quality(self, bar: Bar) -> Tuple[ObservationQualityStatus, str]:
        """Classify a freshly received daily bar against the session calendar.

        A provider response is still retained in the legacy view and lineage
        table, but a row dated on a known non-session (or outside a finite
        calendar's coverage) must not silently feed indicators/strategies.
        ``QUARANTINED`` is deliberately used instead of dropping the row so
        an audit can explain what happened without inventing a price.
        """

        if bar.interval != "1d":
            return ObservationQualityStatus.DEGRADED, "interval_not_daily"
        calendar = self._effective_trading_calendar()
        try:
            in_session = bool(calendar.is_session(bar.timestamp.date()))
        except (CalendarRangeError, TypeError, ValueError):
            return ObservationQualityStatus.QUARANTINED, "calendar_coverage_unknown"
        if not in_session:
            return ObservationQualityStatus.QUARANTINED, "off_calendar_session"
        return ObservationQualityStatus.DEGRADED, "calendar_session_unverified"

    def _calendar_lineage(self) -> Mapping[str, Any]:
        calendar = self._effective_trading_calendar()
        metadata = getattr(calendar, "metadata", None)
        payload: Dict[str, Any] = {
            "identifier": getattr(metadata, "identifier", "unknown"),
            "source": getattr(metadata, "source", "unknown"),
            "version": getattr(metadata, "version", "unknown"),
            "verified": False,
        }
        fingerprint = getattr(calendar, "fingerprint", None)
        if isinstance(fingerprint, str) and fingerprint:
            payload["fingerprint"] = fingerprint
        return payload

    def _effective_trading_calendar(self) -> TradingCalendar:
        """Refresh the cache-only calendar view when a provider exposes one.

        AShareHub loads its finite exchange rows lazily (and may refresh them
        after this collector was constructed).  Resolve the optional provider
        view immediately before each quality/lineage decision so ingestion,
        data-quality APIs and backtests converge on the same calendar without
        triggering network I/O.  Providers without this optional hook retain
        the explicitly documented weekday fallback.
        """

        getter = getattr(self.provider, "get_trading_calendar", None)
        if callable(getter):
            try:
                selected = getter()
            except Exception:
                selected = None
            if selected is not None:
                self.trading_calendar = selected
        return self.trading_calendar

    @staticmethod
    def _public_provider_options(
        provider_options: Optional[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        """Return safe, deterministic request coordinates for lineage.

        Provider options are adapter-facing inputs and must never contain
        credentials.  The collection ledger still needs to distinguish two
        otherwise identical requests (for example, different historical
        ``start``/``end`` windows), so retain public options while dropping
        keys that conventionally carry secrets.  This is only used for the
        request hash/metadata; credentials are never forwarded or persisted.
        """

        if not provider_options:
            return {}
        sensitive_parts = (
            "token",
            "api_key",
            "apikey",
            "secret",
            "password",
            "authorization",
            "cookie",
            "credential",
        )

        def sanitize(value: Any, depth: int = 0) -> Any:
            # Provider options are normally scalar dates/flags.  Keep nested
            # public coordinates useful for future adapters while applying
            # the same secret-key policy recursively and bounding malformed
            # input before it reaches the lineage JSON column.
            if depth > 6:
                return "[truncated]"
            if isinstance(value, Mapping):
                nested = {}
                for nested_key, nested_value in value.items():
                    text = str(nested_key).strip()
                    normalized = text.casefold().replace("-", "_")
                    if not text or any(part in normalized for part in sensitive_parts):
                        continue
                    nested[text] = sanitize(nested_value, depth + 1)
                return nested
            if isinstance(value, (list, tuple, set)):
                return [sanitize(item, depth + 1) for item in value]
            return value

        safe = {}
        for raw_key, value in provider_options.items():
            key = str(raw_key).strip()
            normalized = key.casefold().replace("-", "_")
            if not key or any(part in normalized for part in sensitive_parts):
                continue
            # ``None`` means the option was not supplied.  Omitting it makes
            # ``{start: None}`` hash-equivalent to no explicit range.
            if value is not None:
                safe[key] = sanitize(value)
        return safe

    def _new_bar_batch(
        self,
        symbols: Sequence[str],
        provider_options: Optional[Mapping[str, Any]] = None,
    ) -> CollectionBatch:
        started_at = self._now()
        safe_options = self._public_provider_options(provider_options)
        request_coordinates = {
            "bar_interval": self.bar_interval,
            "bar_limit": self.bar_limit,
            "symbols": list(symbols),
            "provider_options": safe_options,
            # Calendar identity changes what constitutes a valid session and
            # therefore belongs in the deterministic batch input hash.
            "calendar": self._calendar_lineage(),
        }
        return CollectionBatch(
            batch_id=uuid.uuid4().hex,
            provider=self.provider.name,
            request_kind="bars",
            input_hash=normalized_request_hash(request_coordinates),
            status=CollectionBatchStatus.RUNNING,
            started_at=started_at,
            metadata={
                "schema": "collection-batch-v1",
                "request": request_coordinates,
            },
        )

    async def collect_quotes(self, symbols: Sequence[str]) -> List[Quote]:
        """Fetch the strict collector view through the shared ingestion path."""

        result = await self.collect_quote_snapshot(symbols)
        return list(result.quotes)

    async def collect_quote_snapshot(
        self, symbols: Sequence[str], *, relaxed: bool = False
    ) -> QuoteCollectionResult:
        """Validate, persist, then publish a provider snapshot.

        Relaxed requests tolerate missing symbols and discard delayed rows,
        not malformed, duplicate, future-dated, or mixed-source payloads.
        Provider-operation count is not a network-request count: an adapter
        may use its cache, refresh auxiliary data, or retry its transport.
        """
        normalized = tuple(dict.fromkeys(normalize_symbol(symbol) for symbol in symbols))
        if not normalized:
            return QuoteCollectionResult((), (), (), provider_operations=0)
        fetcher = self.provider.fetch_quotes
        if relaxed:
            fetcher = getattr(self.provider, "fetch_quotes_relaxed", fetcher)
        response = await fetcher(normalized)
        if not isinstance(response, (list, tuple)) or any(
            not isinstance(item, Quote) for item in response
        ):
            raise QuoteSnapshotError("provider returned an invalid quote payload")
        quotes = list(response)
        requested = set(normalized)
        if any(quote.symbol not in requested for quote in quotes):
            raise QuoteSnapshotError("provider returned a quote for an unrequested symbol")
        source = self.provider.name.strip().casefold()
        if any(
            quote.source.strip().casefold() != source
            and not quote.source.strip().casefold().startswith(source + ":")
            for quote in quotes
        ):
            raise QuoteSnapshotError("provider returned quotes from an unexpected source")
        received_at = self._now()
        if any(quote.timestamp > received_at + timedelta(seconds=5) for quote in quotes):
            raise QuoteSnapshotError("provider returned future-dated quotes")
        # A quote basket is a point-in-time snapshot and therefore must have
        # at most one row per symbol.  Silently choosing the first/last row
        # would make the result depend on provider ordering and could publish
        # a stale value after a newer one.  Reject the whole batch so the
        # collector records an observable issue and preserves the last known
        # good snapshot.
        seen_symbols = set()
        duplicate_symbols = set()
        for quote in quotes:
            if quote.symbol in seen_symbols:
                duplicate_symbols.add(quote.symbol)
            seen_symbols.add(quote.symbol)
        if duplicate_symbols:
            raise QuoteSnapshotError(
                "provider returned duplicate quotes for: {}".format(
                    ", ".join(sorted(duplicate_symbols))
                )
            )
        # Never let a delayed provider response move the durable latest
        # snapshot backwards.  Same-timestamp rows are allowed because a
        # vendor may enrich a quote with a book or volume correction; an older
        # timestamp, however, cannot be distinguished from a stale replay and
        # must fail closed as one coherent basket.
        if self._quote_ingestion_lock is None:
            self._quote_ingestion_lock = asyncio.Lock()
        async with self._quote_ingestion_lock:
            try:
                accepted = await self._sqlite_call(
                    self.repository.upsert_quote_snapshot,
                    quotes,
                    source_prefix=self.provider.name,
                    reject_stale=not relaxed,
                )
            except ValueError:
                raise QuoteSnapshotError("provider returned stale quotes") from None
            if self._quote_observer is not None:
                self._quote_observer(accepted)
            await asyncio.gather(
                *(self.event_bus.publish(EventType.QUOTE, quote) for quote in accepted)
            )
        accepted_symbols = {item.symbol for item in accepted}
        return QuoteCollectionResult(
            quotes=tuple(accepted),
            requested_symbols=normalized,
            received_symbols=tuple(item.symbol for item in quotes),
            stale_symbols=tuple(item.symbol for item in quotes if item.symbol not in accepted_symbols),
        )

    async def collect_bar_batch(
        self,
        symbols: Sequence[str],
        *,
        publish_events: bool = True,
        provider_options: Optional[Mapping[str, Any]] = None,
    ) -> BarCollectionResult:
        """Fetch and durably persist one bounded history batch.

        Runtime bootstrap calls this with one symbol at a time.  A later
        failure or cancellation cannot discard already completed symbols.
        """

        normalized = tuple(dict.fromkeys(normalize_symbol(symbol) for symbol in symbols))
        # Normalize once and use the same safe coordinates for both the
        # provider call and the persisted lineage record.  In particular, a
        # mistakenly supplied token/API key is dropped before it can reach an
        # adapter; credentials belong to provider configuration, never a
        # request-options mapping.
        safe_provider_options = self._public_provider_options(provider_options)
        call_options = self._provider_call_options(safe_provider_options)
        batch = self._new_bar_batch(normalized, safe_provider_options)
        try:
            # Keep all synchronous SQLite work off the event loop.  This is
            # especially important for the first write of a fresh batch,
            # which may wait on a concurrent backup or migration process.
            await self._sqlite_call(self.repository.begin_collection_batch, batch)
            if len(normalized) == 1:
                fetch_result = await self.provider.fetch_bars_with_lineage(
                    normalized[0],
                    interval=self.bar_interval,
                    limit=self.bar_limit,
                    **dict(call_options),
                )
            else:
                if call_options:
                    raise ValueError(
                        "provider_options are supported only for a single-symbol batch"
                    )
                bars = await self.provider.fetch_bars_many(
                    normalized,
                    interval=self.bar_interval,
                    limit=self.bar_limit,
                )
                fetch_result = BarFetchResult(
                    bars=tuple(bars),
                    observations=(),
                    transport=BarFetchTransport.LEGACY_UNKNOWN,
                )
            return await self.persist_bar_fetch_result(
                fetch_result,
                requested_symbols=normalized,
                publish_events=publish_events,
                batch=batch,
            )
        except asyncio.CancelledError:
            # Cancellation is a lifecycle event, not permission to abandon a
            # RUNNING lineage row.  ``to_thread`` itself is cancellable and can
            # leave the SQLite worker writing after the coroutine has unwound;
            # use the same shield-and-drain helper as normal persistence so a
            # shutdown cannot close the repository underneath this terminal
            # transition.  The batch must be durably marked cancelled before
            # propagating the original CancelledError.
            await self._sqlite_call(
                self._record_batch_failure, batch, None, cancelled=True
            )
            raise
        except Exception as exc:
            await asyncio.to_thread(self._record_batch_failure, batch, exc)
            raise

    def _record_batch_failure(
        self,
        batch: CollectionBatch,
        error: Optional[BaseException],
        *,
        cancelled: bool = False,
    ) -> None:
        current = self.repository.get_collection_batch(batch.batch_id)
        if current is None or current.status != CollectionBatchStatus.RUNNING:
            return
        status = (
            CollectionBatchStatus.CANCELLED
            if cancelled
            else CollectionBatchStatus.FAILED
        )
        self.repository.fail_collection_batch(
            CollectionBatch(
                batch_id=batch.batch_id,
                provider=batch.provider,
                request_kind=batch.request_kind,
                input_hash=batch.input_hash,
                status=status,
                started_at=batch.started_at,
                completed_at=self._now(),
                error_type=type(error).__name__ if error is not None else None,
                # Provider exception strings may contain request URLs or other
                # sensitive material. Persist the stable type only.
                error_message=None,
                metadata=batch.metadata,
            )
        )

    async def persist_bar_batch(
        self,
        bars: Sequence[Bar],
        *,
        requested_symbols: Sequence[str],
        publish_events: bool = True,
        batch: Optional[CollectionBatch] = None,
    ) -> BarCollectionResult:
        """Validate and persist an already-fetched provider bar batch.

        The legacy list contract cannot prove whether rows came from an HTTP
        response or a cache, so it updates compatibility projections without
        inventing new v3 observations.
        """

        return await self.persist_bar_fetch_result(
            BarFetchResult(
                bars=tuple(bars),
                observations=(),
                transport=BarFetchTransport.LEGACY_UNKNOWN,
            ),
            requested_symbols=requested_symbols,
            publish_events=publish_events,
            batch=batch,
        )

    async def persist_bar_fetch_result(
        self,
        fetch_result: BarFetchResult,
        *,
        requested_symbols: Sequence[str],
        publish_events: bool = True,
        batch: Optional[CollectionBatch] = None,
    ) -> BarCollectionResult:
        """Persist a typed result without relabelling cache rows as received."""

        normalized = tuple(
            dict.fromkeys(normalize_symbol(symbol) for symbol in requested_symbols)
        )
        bars = fetch_result.bars
        received_bars = fetch_result.observations
        owns_batch = batch is None
        if batch is None:
            batch = self._new_bar_batch(normalized)
        try:
            if owns_batch:
                await self._sqlite_call(self.repository.begin_collection_batch, batch)
            requested = set(normalized)
            if any(bar.symbol not in requested for bar in bars):
                raise ValueError("provider returned bars for an unrequested symbol")
            if any(bar.symbol not in requested for bar in received_bars):
                raise ValueError("provider observed bars for an unrequested symbol")
            if any(bar.interval != self.bar_interval for bar in bars):
                raise ValueError("provider returned bars for an unrequested interval")
            if any(bar.interval != self.bar_interval for bar in received_bars):
                raise ValueError("provider observed bars for an unrequested interval")
            # A provider may return a wider network response than the
            # compatibility view (for example when ``limit`` truncates the
            # current view).  Observations outside that view are therefore
            # valid and are retained for lineage.  When the two collections
            # do overlap, however, they must describe the exact same raw
            # value.  Without this check a provider could persist a fresh
            # immutable observation alongside an older/conflicting ``bars``
            # projection; all cache consumers would then calculate from the
            # projection while the lineage ledger appeared to certify a
            # different value.  Reject the batch before the atomic SQLite
            # commit so the inconsistency is visible as a failed collection,
            # never as partially trusted input.
            projected_by_key = {
                (bar.symbol, bar.interval, bar.timestamp): bar
                for bar in bars
            }
            projection_conflicts = []
            for observed in received_bars:
                projected = projected_by_key.get(
                    (observed.symbol, observed.interval, observed.timestamp)
                )
                if projected is not None and projected != observed:
                    projection_conflicts.append(
                        "{}@{}".format(
                            observed.symbol, observed.timestamp.isoformat()
                        )
                    )
            if projection_conflicts:
                raise ValueError(
                    "provider observation conflicts with bars projection for: {}".format(
                        ", ".join(sorted(set(projection_conflicts)))
                    )
                )
            observation_quality: Dict[Tuple[str, str, datetime], Tuple[ObservationQualityStatus, str]] = {}
            for bar in received_bars:
                observation_quality[(bar.symbol, bar.interval, bar.timestamp)] = (
                    *self._daily_observation_quality(bar),
                )
            quarantined_keys = {
                key
                for key, (status, _reason) in observation_quality.items()
                if status == ObservationQualityStatus.QUARANTINED
            }
            metrics: List[Metric] = []
            grouped = {}
            for bar in bars:
                # A freshly received off-calendar row remains persisted for
                # auditability, but cannot influence indicator state.  Cache
                # rows without a fresh observation keep legacy behavior.
                if (bar.symbol, bar.interval, bar.timestamp) in quarantined_keys:
                    continue
                grouped.setdefault((bar.symbol, bar.interval), []).append(bar)
            for symbol_bars in grouped.values():
                metrics.extend(self.indicator_calculator.metrics_from_bars(symbol_bars))
            persisted_at = self._now()
            observations = tuple(
                observation_from_bar(
                    bar,
                    batch_id=batch.batch_id,
                    received_at=fetch_result.response_received_at,
                    persisted_at=persisted_at,
                    quality_status=observation_quality[
                        (bar.symbol, bar.interval, bar.timestamp)
                    ][0],
                )
                for bar in received_bars
            )
            quarantined_reasons = sorted(
                {
                    reason
                    for status, reason in observation_quality.values()
                    if status == ObservationQualityStatus.QUARANTINED
                }
            )
            completed_metadata = dict(batch.metadata)
            completed_metadata.update(
                {
                    "transport": fetch_result.transport.value,
                    "view_count": len(bars),
                    "observation_count": len(observations),
                    # The adapter supplies a digest of the parsed JSON
                    # response, never the raw body.  Keeping the hash next to
                    # the immutable batch makes retries and provider
                    # revisions comparable without creating a credential
                    # retention path.  Legacy adapters leave it absent and
                    # therefore remain explicitly less auditable.
                    "response_content_hash": fetch_result.response_content_hash,
                    "response_content_hash_scope": (
                        "parsed_json_response"
                        if fetch_result.response_content_hash is not None
                        else "unavailable"
                    ),
                    "calendar": self._calendar_lineage(),
                    "quarantined_observation_count": len(quarantined_keys),
                    "quarantined_observation_reasons": quarantined_reasons,
                }
            )
            completed = CollectionBatch(
                batch_id=batch.batch_id,
                provider=batch.provider,
                request_kind=batch.request_kind,
                input_hash=batch.input_hash,
                status=CollectionBatchStatus.SUCCEEDED,
                started_at=batch.started_at,
                completed_at=persisted_at,
                item_count=len(observations),
                metadata=completed_metadata,
            )
            # Observations, the legacy projection, metrics and the terminal
            # batch state commit together before any EventBus publication.
            await self._sqlite_call(
                self.repository.complete_bar_collection_batch,
                completed,
                observations,
                metrics,
                legacy_bars=bars,
            )
        except BaseException as exc:
            if owns_batch:
                await asyncio.to_thread(self._record_batch_failure, batch, exc)
            raise
        if publish_events:
            publishable_bars = tuple(
                bar
                for bar in bars
                if (bar.symbol, bar.interval, bar.timestamp) not in quarantined_keys
            )
            await asyncio.gather(
                *(self.event_bus.publish(EventType.BAR, bar) for bar in publishable_bars),
                *(self.event_bus.publish(EventType.METRIC, metric) for metric in metrics),
            )
        return BarCollectionResult(
            tuple(bars),
            tuple(metrics),
            batch_id=batch.batch_id,
            transport=fetch_result.transport,
        )

    async def collect_bars(self, symbols: Sequence[str]) -> List[Bar]:
        result = await self.collect_bar_batch(symbols)
        return list(result.bars)

    async def collect(self, symbols: Sequence[str]) -> CollectionResult:
        if not self.repository.initialized:
            # A cold-start migration may take seconds on Windows when a
            # backup/antivirus process owns the SQLite file.  Never let that
            # synchronous setup freeze the ASGI event loop.
            await self._sqlite_call(self.repository.initialize)
        quotes: List[Quote] = []
        bars: List[Bar] = []
        metrics: List[Metric] = []
        issues: List[CollectionIssue] = []
        failures: List[BaseException] = []

        try:
            quotes = await self.collect_quotes(symbols)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures.append(exc)
            issues.append(
                CollectionIssue("quotes", type(exc).__name__, str(exc))
            )

        bars_succeeded = False
        if self.collect_bars_enabled:
            try:
                bar_result = await self.collect_bar_batch(symbols)
                bars = list(bar_result.bars)
                metrics = list(bar_result.metrics)
                bars_succeeded = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures.append(exc)
                issues.append(
                    CollectionIssue("bars", type(exc).__name__, str(exc))
                )

        quote_succeeded = not any(issue.component == "quotes" for issue in issues)
        if not quote_succeeded and (not self.collect_bars_enabled or not bars_succeeded):
            # Preserve the provider's typed, sanitized exception for the common
            # health/error handling path when no enabled feed component worked.
            raise failures[0]

        return CollectionResult(
            tuple(quotes), tuple(bars), tuple(metrics), tuple(issues)
        )


MarketDataCollector = MarketDataCollectionService
