"""One-cycle integration tests for collection and monitoring."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from quant_monitor.data_sources import MockMarketDataProvider
from quant_monitor.domain import EventType, Quote, Signal, SignalAction, now_market_time
from quant_monitor.services import EventBus, MonitoringService
from quant_monitor.storage import SQLiteRepository
from quant_monitor.strategies import CohortStrategy, MovingAverageCrossStrategy


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


def test_monitor_quote_snapshot_does_not_regress_or_expand_watchlist(tmp_path):
    repository = SQLiteRepository(tmp_path / "snapshot.sqlite3")
    monitor = MonitoringService(
        provider=MockMarketDataProvider(), repository=repository,
        symbols=["600519.SH"], collect_bars=False,
    )
    timestamp = now_market_time()
    latest = Quote(symbol="600519.SH", timestamp=timestamp, last="100", source="mock")
    late = Quote(
        symbol="600519.SH", timestamp=timestamp - timedelta(seconds=1), last="99", source="mock"
    )
    other = Quote(symbol="000001.SZ", timestamp=timestamp, last="10", source="mock")
    monitor.remember_quotes([latest])
    monitor.remember_quotes([late, other])
    assert monitor.current_snapshot()["quotes"] == {latest.symbol: latest}


def test_slow_cycle_does_not_undo_manual_same_timestamp_correction(tmp_path):
    async def exercise():
        timestamp = now_market_time()
        provider = MockMarketDataProvider(clock=lambda: timestamp)
        repository = SQLiteRepository(tmp_path / "correction.sqlite3")
        monitor = MonitoringService(
            provider=provider, repository=repository,
            symbols=["600519.SH"], collect_bars=False,
        )
        evaluating = asyncio.Event()
        release = asyncio.Event()

        async def slow_evaluation(collection):
            evaluating.set()
            await release.wait()
            return ()

        monitor._evaluate_strategies = slow_evaluation
        cycle = asyncio.create_task(monitor.collect_once())
        try:
            await asyncio.wait_for(evaluating.wait(), 2)
            correction = Quote(
                symbol="600519.SH", timestamp=timestamp, last="111", source="mock"
            )

            async def fetch(symbols):
                return [correction]

            provider.fetch_quotes_relaxed = fetch
            await monitor.collector.collect_quote_snapshot(["600519.SH"], relaxed=True)
            release.set()
            await asyncio.wait_for(cycle, 2)
            assert monitor.current_snapshot()["quotes"] == {correction.symbol: correction}
            assert repository.get_latest_quotes() == [correction]
        finally:
            release.set()
            await asyncio.gather(cycle, return_exceptions=True)
            await monitor.stop(close_provider=True)
            repository.close()

    asyncio.run(exercise())


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
            assert snapshot["cycle_stats"] == {
                "total": 1,
                "succeeded": 1,
                "failed": 0,
                "partial": 0,
                "cancelled": 0,
                "last_outcome": "succeeded",
            }

            event_types = {event.type for event in events}
            assert EventType.QUOTE in event_types
            assert EventType.BAR in event_types
            assert EventType.METRIC in event_types
        finally:
            await monitor.stop(close_provider=True)
            repository.close()

    asyncio.run(exercise())


def test_manual_collection_job_is_observable_and_completes(tmp_path):
    async def exercise():
        repository = SQLiteRepository(tmp_path / "jobs.sqlite3")
        monitor = MonitoringService(
            provider=MockMarketDataProvider(seed=88),
            repository=repository,
            symbols=["600519.SH"],
            collect_bars=False,
        )
        try:
            submitted = await monitor.submit_collection_job()
            assert submitted["status"] == "queued"
            for _ in range(100):
                job = monitor.collection_job(submitted["job_id"])
                if job["terminal"]:
                    break
                await asyncio.sleep(0.01)
            assert job["status"] == "succeeded"
            assert job["counts"]["quotes"] == 1
            assert job["iteration_after"] == 1
            listing = monitor.collection_jobs(limit=10)
            assert listing["active"] == 0
            assert listing["items"][0]["job_id"] == submitted["job_id"]
            assert monitor.current_snapshot()["jobs"]["recent"]
        finally:
            await monitor.stop(close_provider=True)
            repository.close()

    asyncio.run(exercise())


def test_manual_collection_job_can_be_cancelled_without_leaking_task(tmp_path):
    class HangingProvider(MockMarketDataProvider):
        def __init__(self):
            super().__init__(seed=89)
            self.entered = asyncio.Event()

        async def fetch_quotes(self, symbols):
            self.entered.set()
            await asyncio.Event().wait()

    async def exercise():
        repository = SQLiteRepository(tmp_path / "cancel-job.sqlite3")
        provider = HangingProvider()
        monitor = MonitoringService(
            provider=provider,
            repository=repository,
            symbols=["600519.SH"],
            collect_bars=False,
        )
        try:
            submitted = await monitor.submit_collection_job()
            await asyncio.wait_for(provider.entered.wait(), timeout=1.0)
            cancelled = await monitor.cancel_collection_job(
                submitted["job_id"], grace_seconds=1.0
            )
            assert cancelled["status"] == "cancelled"
            assert cancelled["terminal"] is True
            assert monitor.collection_jobs()["active"] == 0
        finally:
            await monitor.stop(close_provider=True)
            repository.close()

    asyncio.run(exercise())


def test_quote_refresh_job_can_be_cancelled_and_keeps_quote_operation(tmp_path):
    class HangingProvider(MockMarketDataProvider):
        def __init__(self):
            super().__init__(seed=890)
            self.entered = asyncio.Event()

        async def fetch_quotes(self, symbols):
            self.entered.set()
            await asyncio.Event().wait()

    async def exercise():
        repository = SQLiteRepository(tmp_path / "cancel-quote-refresh.sqlite3")
        provider = HangingProvider()
        monitor = MonitoringService(
            provider=provider,
            repository=repository,
            symbols=["600519.SH"],
            collect_bars=False,
        )
        try:
            submitted = await monitor.submit_quote_refresh_job(["600519.SH"])
            assert submitted["operation"] == "quote_refresh"
            await asyncio.wait_for(provider.entered.wait(), timeout=1.0)
            cancelled = await monitor.cancel_collection_job(
                submitted["job_id"], grace_seconds=1.0
            )
            assert cancelled["operation"] == "quote_refresh"
            assert cancelled["status"] == "cancelled"
            assert cancelled["terminal"] is True
            assert monitor.collection_jobs(operation="quote_refresh")["active"] == 0
        finally:
            await monitor.stop(close_provider=True)
            repository.close()

    asyncio.run(exercise())


def test_cancel_terminal_job_offloads_durable_lookup(tmp_path):
    """The terminal cancellation branch must not call SQLite on the loop."""

    async def exercise():
        repository = SQLiteRepository(tmp_path / "terminal-cancel-job.sqlite3")
        monitor = MonitoringService(
            provider=MockMarketDataProvider(seed=90),
            repository=repository,
            symbols=["600519.SH"],
            collect_bars=False,
        )
        try:
            submitted = await monitor.submit_collection_job()
            job = None
            for _ in range(100):
                job = monitor.collection_job(submitted["job_id"])
                if job["terminal"]:
                    break
                await asyncio.sleep(0.01)
            assert job and job["terminal"]
            original = monitor.collection_job

            def guarded(job_id):
                with pytest.raises(RuntimeError, match="no running event loop"):
                    asyncio.get_running_loop()
                return original(job_id)

            monitor.collection_job = guarded
            cancelled = await monitor.cancel_collection_job(submitted["job_id"])
            assert cancelled["terminal"] is True
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
            persisted = repository.list_signals(strategy=strategy.name)
            assert persisted
            binding = persisted[0].metadata["strategy_binding"]
            assert binding["schema_version"] == "live-strategy-binding-v1"
            assert binding["strategy_name"] == strategy.name
            assert binding["code_identity_status"] == "unavailable"
            assert binding["code_version"] is None
            assert binding["binding_hash"].startswith("sha256:")
        finally:
            await monitor.stop(close_provider=True)
            repository.close()

    asyncio.run(exercise())


def test_live_signal_binding_contains_builtin_code_and_effective_parameters(tmp_path):
    repository = SQLiteRepository(tmp_path / "binding.sqlite3")
    monitor = MonitoringService(
        provider=MockMarketDataProvider(seed=46),
        repository=repository,
        symbols=["600519.SH"],
        collect_bars=False,
    )
    try:
        strategy = MovingAverageCrossStrategy(fast_window=7, slow_window=30)
        binding = monitor._strategy_binding(strategy)
        assert binding["code_identity_status"] == "verified_local_source"
        assert binding["code_version"].startswith("sha256:")
        assert binding["parameters"] == {
            "fast_window": 7,
            "slow_window": 30,
            "interval": "1m",
            "strategy_name": "moving_average_cross",
        }
        assert binding["binding_hash"].startswith("sha256:")
        assert monitor._strategy_binding(strategy) is binding
    finally:
        repository.close()


def test_signal_deduplication_survives_monitor_restart(tmp_path):
    async def exercise():
        repository = SQLiteRepository(tmp_path / "signal-restart.sqlite3")
        first_strategy = RecordingCohortStrategy()
        first = MonitoringService(
            provider=MockMarketDataProvider(seed=45),
            repository=repository,
            symbols=["600519.SH", "000001.SZ"],
            cohort_strategies=[first_strategy],
            bar_interval="1d",
            bar_limit=130,
        )
        try:
            initial = await first.collect_once()
            assert len(initial.signals) == 1
            await first.stop(close_provider=True)

            # A new service has no process-local de-duplication map.  The
            # durable signal business-key lookup must still suppress replay.
            second = MonitoringService(
                provider=MockMarketDataProvider(seed=45),
                repository=repository,
                symbols=["600519.SH", "000001.SZ"],
                cohort_strategies=[RecordingCohortStrategy()],
                bar_interval="1d",
                bar_limit=130,
            )
            try:
                replay = await second.collect_once()
                assert replay.signals == ()
                assert len(repository.list_signals(strategy="recording_cohort")) == 1
            finally:
                await second.stop(close_provider=True)
        finally:
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
            assert snapshot["cycle_stats"]["total"] == 1
            assert snapshot["cycle_stats"]["failed"] == 1
            assert snapshot["cycle_stats"]["last_outcome"] == "failed"
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
            assert snapshot["cycle_stats"]["total"] == 1
            assert snapshot["cycle_stats"]["succeeded"] == 1
            assert snapshot["cycle_stats"]["partial"] == 1
            assert snapshot["cycle_stats"]["last_outcome"] == "partial"
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
            assert monitor.current_snapshot()["cycle_stats"]["cancelled"] == 1
            assert monitor.current_snapshot()["cycle_stats"]["total"] == 1
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
