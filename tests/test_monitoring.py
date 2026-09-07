"""One-cycle integration tests for collection and monitoring."""

from __future__ import annotations

import asyncio

import pytest

from quant_monitor.data_sources import MockMarketDataProvider
from quant_monitor.domain import EventType, Signal, SignalAction
from quant_monitor.services import EventBus, MonitoringService
from quant_monitor.storage import SQLiteRepository
from quant_monitor.strategies import CohortStrategy


class RecordingCohortStrategy(CohortStrategy):
    def __init__(self):
        self.calls = []

    @property
    def name(self):
        return "recording_cohort"

    def evaluate(self, context):
        self.calls.append(context)
        first = next(iter(context.members.values()))
        return (
            Signal(
                strategy=self.name,
                symbol=first.symbol,
                timestamp=first.bars[-1].timestamp,
                action=SignalAction.HOLD,
                strength=0,
                price=first.bars[-1].close,
                reason="test-only cohort signal",
            ),
        )


def test_monitor_collect_once_persists_batch_publishes_events_and_updates_snapshot(tmp_path):
    async def exercise():
        database_path = tmp_path / "cycle.sqlite3"
        repository = SQLiteRepository(database_path)
        provider = MockMarketDataProvider(seed=123)
        bus = EventBus()
        events = []

        async def observe(event):
            events.append(event)

        await bus.subscribe(None, observe)
        monitor = MonitoringService(
            provider=provider,
            repository=repository,
            symbols=["600519.sh", "000001.SZ", "600519.SH"],
            event_bus=bus,
            poll_interval=0.01,
            bar_interval="1m",
            bar_limit=25,
        )
        try:
            result = await monitor.collect_once()

            assert repository.initialized is True
            assert monitor.symbols == ("600519.SH", "000001.SZ")
            assert len(result.collection.quotes) == 2
            assert len(result.collection.bars) == 50
            assert result.collection.metrics
            assert len(repository.get_latest_quotes()) == 2
            assert len(repository.get_bars("600519.SH", "1m")) == 25
            assert repository.get_metrics("600519.SH")

            snapshot = monitor.current_snapshot()
            assert snapshot["iteration"] == 1
            assert snapshot["provider"] == "mock"
            assert snapshot["last_error"] is None
            assert set(snapshot["quotes"]) == {"600519.SH", "000001.SZ"}
            assert snapshot["last_collection_at"] is not None
            assert snapshot["last_collection_duration_ms"] is not None
            assert snapshot["last_collection_duration_ms"] >= 0
            assert snapshot["running"] is False

            event_types = {event.type for event in events}
            assert EventType.QUOTE in event_types
            assert EventType.BAR in event_types
            assert EventType.METRIC in event_types
        finally:
            await monitor.stop(close_provider=True)
            repository.close()

    asyncio.run(exercise())


def test_monitor_evaluates_cross_sectional_strategy_once_per_cycle(tmp_path):
    async def exercise():
        repository = SQLiteRepository(tmp_path / "cohort.sqlite3")
        strategy = RecordingCohortStrategy()
        monitor = MonitoringService(
            provider=MockMarketDataProvider(seed=44),
            repository=repository,
            symbols=["600519.SH", "000001.SZ"],
            cohort_strategies=[strategy],
            bar_interval="1d",
            bar_limit=130,
        )
        try:
            result = await monitor.collect_once()
            assert len(strategy.calls) == 1
            assert set(strategy.calls[0].members) == {"600519.SH", "000001.SZ"}
            assert len(result.signals) == 1
            assert repository.list_signals(strategy=strategy.name)
        finally:
            await monitor.stop(close_provider=True)
            repository.close()

    asyncio.run(exercise())


def test_monitor_collect_once_records_provider_failure_as_alert(tmp_path):
    class BrokenProvider(MockMarketDataProvider):
        async def fetch_quotes(self, symbols):
            raise RuntimeError("fixture feed unavailable")

    async def exercise():
        repository = SQLiteRepository(tmp_path / "failure.sqlite3")
        monitor = MonitoringService(
            provider=BrokenProvider(),
            repository=repository,
            symbols=["600519.SH"],
            collect_bars=False,
        )
        try:
            with pytest.raises(RuntimeError, match="fixture feed unavailable"):
                await monitor.collect_once()

            snapshot = monitor.current_snapshot()
            assert snapshot["iteration"] == 0
            assert "fixture feed unavailable" in snapshot["last_error"]
            alerts = repository.list_alerts()
            assert len(alerts) == 1
            assert alerts[0].category == "collection"
            assert alerts[0].level.value == "critical"
            assert alerts[0].metadata["provider"] == "mock"
        finally:
            repository.close()

    asyncio.run(exercise())


def test_monitor_keeps_bars_when_quote_component_fails(tmp_path):
    class QuoteBrokenProvider(MockMarketDataProvider):
        async def fetch_quotes(self, symbols):
            raise RuntimeError("fixture quote feed unavailable")

    async def exercise():
        repository = SQLiteRepository(tmp_path / "partial.sqlite3")
        monitor = MonitoringService(
            provider=QuoteBrokenProvider(seed=321),
            repository=repository,
            symbols=["600519.SH"],
            bar_interval="1m",
            bar_limit=25,
        )
        try:
            result = await monitor.collect_once()
            assert result.collection.quotes == ()
            assert len(result.collection.bars) == 25
            assert result.collection.issues[0].component == "quotes"
            assert repository.get_bars("600519.SH", "1m")

            snapshot = monitor.current_snapshot()
            assert snapshot["iteration"] == 1
            assert "fixture quote feed unavailable" in snapshot["last_error"]
            alerts = repository.list_alerts()
            assert len(alerts) == 1
            assert alerts[0].level.value == "warning"

            await monitor.collect_once()
            assert len(repository.list_alerts()) == 1
        finally:
            repository.close()

    asyncio.run(exercise())


def test_monitor_collect_once_serializes_concurrent_cycles(tmp_path):
    class BlockingProvider(MockMarketDataProvider):
        def __init__(self):
            super().__init__(seed=99)
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = 0
            self.active_calls = 0
            self.maximum_active_calls = 0

        async def fetch_quotes(self, symbols):
            self.calls += 1
            self.active_calls += 1
            self.maximum_active_calls = max(
                self.maximum_active_calls, self.active_calls
            )
            self.entered.set()
            try:
                await self.release.wait()
                return await super().fetch_quotes(symbols)
            finally:
                self.active_calls -= 1

    async def exercise():
        repository = SQLiteRepository(tmp_path / "single-flight.sqlite3")
        provider = BlockingProvider()
        monitor = MonitoringService(
            provider=provider,
            repository=repository,
            symbols=["600519.SH"],
            collect_bars=False,
        )
        try:
            first = asyncio.create_task(monitor.collect_once())
            await asyncio.wait_for(provider.entered.wait(), timeout=1.0)
            second = asyncio.create_task(monitor.collect_once())
            await asyncio.sleep(0)

            assert provider.calls == 1
            assert second.done() is False

            provider.release.set()
            await asyncio.wait_for(
                asyncio.gather(first, second), timeout=1.0
            )

            assert provider.calls == 2
            assert provider.maximum_active_calls == 1
            assert monitor.current_snapshot()["iteration"] == 2
        finally:
            provider.release.set()
            await monitor.stop(close_provider=True)
            repository.close()

    asyncio.run(exercise())


def test_monitor_stop_cancels_stuck_cycle_after_grace_period(tmp_path):
    class HangingProvider(MockMarketDataProvider):
        def __init__(self):
            super().__init__(seed=100)
            self.entered = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.closed = False

        async def fetch_quotes(self, symbols):
            self.entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

        async def close(self):
            self.closed = True

    async def exercise():
        repository = SQLiteRepository(tmp_path / "bounded-stop.sqlite3")
        provider = HangingProvider()
        monitor = MonitoringService(
            provider=provider,
            repository=repository,
            symbols=["600519.SH"],
            collect_bars=False,
        )
        try:
            await monitor.start()
            await asyncio.wait_for(provider.entered.wait(), timeout=1.0)
            started = asyncio.get_running_loop().time()

            await asyncio.wait_for(
                monitor.stop(close_provider=True, grace_seconds=0.01),
                timeout=1.0,
            )

            elapsed = asyncio.get_running_loop().time() - started
            assert elapsed < 0.5
            assert provider.cancelled.is_set()
            assert provider.closed is True
            assert monitor.running is False
            assert monitor.current_snapshot()["iteration"] == 0
        finally:
            await monitor.stop(close_provider=True, grace_seconds=0)
            repository.close()

    asyncio.run(exercise())


def test_monitor_stop_allows_in_flight_cycle_to_finish_within_grace(tmp_path):
    class ReleasableProvider(MockMarketDataProvider):
        def __init__(self):
            super().__init__(seed=101)
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.cancelled = False

        async def fetch_quotes(self, symbols):
            self.entered.set()
            try:
                await self.release.wait()
                return await super().fetch_quotes(symbols)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    async def exercise():
        repository = SQLiteRepository(tmp_path / "graceful-stop.sqlite3")
        provider = ReleasableProvider()
        monitor = MonitoringService(
            provider=provider,
            repository=repository,
            symbols=["600519.SH"],
            collect_bars=False,
        )
        try:
            await monitor.start()
            await asyncio.wait_for(provider.entered.wait(), timeout=1.0)
            stopping = asyncio.create_task(
                monitor.stop(close_provider=True, grace_seconds=0.5)
            )
            await asyncio.sleep(0)
            provider.release.set()
            await asyncio.wait_for(stopping, timeout=1.0)

            assert provider.cancelled is False
            assert monitor.running is False
            assert monitor.current_snapshot()["iteration"] == 1
        finally:
            provider.release.set()
            await monitor.stop(close_provider=True, grace_seconds=0)
            repository.close()

    asyncio.run(exercise())
