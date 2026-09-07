"""Long-running orchestration service for the local monitoring platform."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
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
)

from .collection import CollectionIssue, CollectionResult, MarketDataCollectionService
from .event_bus import EventBus


@dataclass(frozen=True)
class MonitoringCycleResult:
    collection: CollectionResult
    signals: Tuple[Signal, ...]


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
        self._latest_quotes: Dict[str, Quote] = {}
        self._latest_metrics: Dict[Tuple[str, str], Metric] = {}
        self._latest_signals: Dict[Tuple[str, str], Signal] = {}
        self._emitted_signal_times: Dict[Tuple[str, str, str], datetime] = {}
        self._last_issue_signature: Optional[Tuple[Tuple[str, str, str], ...]] = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _get_cycle_lock(self) -> asyncio.Lock:
        """Return the loop-bound lock that serializes complete collection cycles."""

        if self._cycle_lock is None:
            self._cycle_lock = asyncio.Lock()
        return self._cycle_lock

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

    async def start(self, collect_immediately: bool = True) -> None:
        """Start background polling.  Calling twice is harmless."""

        if self.running:
            return
        if not self.repository.initialized:
            self.repository.initialize()
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
            try:
                collection = await self.collector.collect(self.symbols)
                signals = await self._evaluate_strategies(collection)
                issue_message = self._issue_message(collection.issues)
                with self._state_lock:
                    self._iteration += 1
                    self._last_collection_at = now_market_time()
                    self._last_collection_duration_ms = round(
                        (time.monotonic() - started) * 1000.0,
                        3,
                    )
                    self._last_error = issue_message
                    for quote in collection.quotes:
                        self._latest_quotes[quote.symbol] = quote
                    for metric in collection.metrics:
                        self._latest_metrics[(metric.symbol, metric.name)] = metric
                    for signal in signals:
                        self._latest_signals[(signal.strategy, signal.symbol)] = signal
                await self._publish_collection_issues(collection.issues)
                return MonitoringCycleResult(collection, tuple(signals))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                with self._state_lock:
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
                        key = (signal.strategy, signal.symbol, signal.action.value)
                        if self._emitted_signal_times.get(key) == signal.timestamp:
                            continue
                        self._emitted_signal_times[key] = signal.timestamp
                        emitted.append(signal)
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
            cohort_context = CohortStrategyContext(
                timestamp=max(item.timestamp for item in contexts.values()),
                members=contexts,
                cohort="a_share_banks",
                metadata={
                    "price_basis": "raw_unadjusted",
                    "expected_bar_date": expected_latest_daily_date().isoformat(),
                    "calendar": "weekday_fallback",
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
                        key = (signal.strategy, signal.symbol, signal.action.value)
                        if self._emitted_signal_times.get(key) == signal.timestamp:
                            continue
                        self._emitted_signal_times[key] = signal.timestamp
                        emitted.append(signal)
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
            return {
                "running": self.running,
                "provider": self.provider.name,
                "symbols": self.symbols,
                "iteration": self._iteration,
                "started_at": self._started_at,
                "last_collection_at": self._last_collection_at,
                "last_collection_duration_ms": self._last_collection_duration_ms,
                "last_error": self._last_error,
                "quotes": dict(self._latest_quotes),
                "metrics": tuple(self._latest_metrics.values()),
                "signals": tuple(self._latest_signals.values()),
            }
