"""One-shot market-data collection and persistence service."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from quant_monitor.data_sources import (
    BarFetchResult,
    BarFetchTransport,
    MarketDataProvider,
)
from quant_monitor.domain import (
    Bar,
    CollectionBatch,
    CollectionBatchStatus,
    EventType,
    Metric,
    Quote,
    ensure_market_time,
    normalized_request_hash,
    normalize_symbol,
    now_market_time,
    observation_from_bar,
)
from quant_monitor.storage import SQLiteRepository

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

    def _now(self) -> datetime:
        return ensure_market_time(self._clock())

    def _new_bar_batch(self, symbols: Sequence[str]) -> CollectionBatch:
        started_at = self._now()
        return CollectionBatch(
            batch_id=uuid.uuid4().hex,
            provider=self.provider.name,
            request_kind="bars",
            input_hash=normalized_request_hash(
                {
                    "bar_interval": self.bar_interval,
                    "bar_limit": self.bar_limit,
                    "symbols": list(symbols),
                }
            ),
            status=CollectionBatchStatus.RUNNING,
            started_at=started_at,
            metadata={"schema": "collection-batch-v1"},
        )

    async def collect_quotes(self, symbols: Sequence[str]) -> List[Quote]:
        normalized = tuple(dict.fromkeys(normalize_symbol(symbol) for symbol in symbols))
        quotes = await self.provider.fetch_quotes(normalized)
        requested = set(normalized)
        if any(quote.symbol not in requested for quote in quotes):
            raise ValueError("provider returned a quote for an unrequested symbol")
        self.repository.upsert_quotes(quotes)
        await asyncio.gather(
            *(self.event_bus.publish(EventType.QUOTE, quote) for quote in quotes)
        )
        return quotes

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
        batch = self._new_bar_batch(normalized)
        self.repository.begin_collection_batch(batch)
        try:
            if len(normalized) == 1:
                fetch_result = await self.provider.fetch_bars_with_lineage(
                    normalized[0],
                    interval=self.bar_interval,
                    limit=self.bar_limit,
                    **dict(provider_options or {}),
                )
            else:
                if provider_options:
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
            self._record_batch_failure(batch, None, cancelled=True)
            raise
        except Exception as exc:
            self._record_batch_failure(batch, exc)
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
            self.repository.begin_collection_batch(batch)
        try:
            requested = set(normalized)
            if any(bar.symbol not in requested for bar in bars):
                raise ValueError("provider returned bars for an unrequested symbol")
            if any(bar.symbol not in requested for bar in received_bars):
                raise ValueError("provider observed bars for an unrequested symbol")
            if any(bar.interval != self.bar_interval for bar in bars):
                raise ValueError("provider returned bars for an unrequested interval")
            if any(bar.interval != self.bar_interval for bar in received_bars):
                raise ValueError("provider observed bars for an unrequested interval")
            metrics: List[Metric] = []
            grouped = {}
            for bar in bars:
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
                )
                for bar in received_bars
            )
            completed_metadata = dict(batch.metadata)
            completed_metadata.update(
                {
                    "transport": fetch_result.transport.value,
                    "view_count": len(bars),
                    "observation_count": len(observations),
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
            self.repository.complete_bar_collection_batch(
                completed, observations, metrics, legacy_bars=bars
            )
        except BaseException as exc:
            if owns_batch:
                self._record_batch_failure(batch, exc)
            raise
        if publish_events:
            await asyncio.gather(
                *(self.event_bus.publish(EventType.BAR, bar) for bar in bars),
                *(self.event_bus.publish(EventType.METRIC, metric) for metric in metrics),
            )
        return BarCollectionResult(tuple(bars), tuple(metrics))

    async def collect_bars(self, symbols: Sequence[str]) -> List[Bar]:
        result = await self.collect_bar_batch(symbols)
        return list(result.bars)

    async def collect(self, symbols: Sequence[str]) -> CollectionResult:
        if not self.repository.initialized:
            self.repository.initialize()
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
