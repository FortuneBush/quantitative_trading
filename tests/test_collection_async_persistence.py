"""The collection layer must not run blocking SQLite work on the event loop."""

from __future__ import annotations

import asyncio
import threading

from quant_monitor.data_sources import MockMarketDataProvider
from quant_monitor.domain import CollectionBatchStatus
from quant_monitor.services import EventBus, MarketDataCollectionService
from quant_monitor.services import MonitoringService
from quant_monitor.storage import SQLiteRepository


def test_quote_persistence_does_not_block_event_loop(tmp_path):
    """A slow SQLite transaction leaves an async heartbeat schedulable."""

    class BlockingRepository(SQLiteRepository):
        def __init__(self, path):
            super().__init__(path)
            self.entered = threading.Event()
            self.release = threading.Event()

        def upsert_quotes(self, quotes):
            self.entered.set()
            # Simulate an antivirus/backup process holding the SQLite lock.
            # The collection coroutine must be cancellable and the ASGI loop
            # must continue servicing unrelated requests during this wait.
            if not self.release.wait(timeout=2.0):
                raise TimeoutError("fixture SQLite write was not released")
            return super().upsert_quotes(quotes)

    async def exercise():
        repository = BlockingRepository(tmp_path / "async-persistence.sqlite3")
        provider = MockMarketDataProvider(seed=123)
        collector = MarketDataCollectionService(
            provider=provider,
            repository=repository,
            event_bus=EventBus(),
            collect_bars=False,
        )
        await asyncio.to_thread(repository.initialize)
        ticks = 0
        heartbeat_done = asyncio.Event()

        async def heartbeat():
            nonlocal ticks
            while not heartbeat_done.is_set():
                ticks += 1
                await asyncio.sleep(0)

        heartbeat_task = asyncio.create_task(heartbeat())
        collection_task = asyncio.create_task(collector.collect_quotes(["600519.SH"]))
        try:
            await asyncio.wait_for(asyncio.to_thread(repository.entered.wait), 1.0)
            await asyncio.sleep(0.02)
            assert ticks > 0
            repository.release.set()
            quotes = await asyncio.wait_for(collection_task, 1.0)
            assert len(quotes) == 1
        finally:
            repository.release.set()
            heartbeat_done.set()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            if not collection_task.done():
                collection_task.cancel()
                await asyncio.gather(collection_task, return_exceptions=True)
            repository.close()

    asyncio.run(exercise())


def test_manual_job_persistence_does_not_block_event_loop(tmp_path):
    """Durable job admission remains schedulable while SQLite is locked."""

    class BlockingRepository(SQLiteRepository):
        def __init__(self, path):
            super().__init__(path)
            self.entered = threading.Event()
            self.release = threading.Event()

        def upsert_collection_job(self, **kwargs):
            if kwargs.get("status") == "queued":
                self.entered.set()
                if not self.release.wait(timeout=2.0):
                    raise TimeoutError("fixture SQLite job write was not released")
            return super().upsert_collection_job(**kwargs)

    async def exercise():
        repository = BlockingRepository(tmp_path / "job-async-persistence.sqlite3")
        monitor = MonitoringService(
            provider=MockMarketDataProvider(seed=789),
            repository=repository,
            symbols=["600519.SH"],
            collect_bars=False,
        )
        await asyncio.to_thread(repository.initialize)
        ticks = 0
        heartbeat_done = asyncio.Event()

        async def heartbeat():
            nonlocal ticks
            while not heartbeat_done.is_set():
                ticks += 1
                await asyncio.sleep(0)

        heartbeat_task = asyncio.create_task(heartbeat())
        submit_task = asyncio.create_task(monitor.submit_collection_job())
        try:
            await asyncio.wait_for(asyncio.to_thread(repository.entered.wait), 1.0)
            await asyncio.sleep(0.02)
            assert ticks > 0
            repository.release.set()
            submitted = await asyncio.wait_for(submit_task, 1.0)
            assert submitted["status"] == "queued"
            # Let the mock cycle finish before closing its repository.
            for _ in range(100):
                if monitor.collection_job(submitted["job_id"])["terminal"]:
                    break
                await asyncio.sleep(0.01)
        finally:
            repository.release.set()
            heartbeat_done.set()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            if not submit_task.done():
                submit_task.cancel()
                await asyncio.gather(submit_task, return_exceptions=True)
            await monitor.stop(close_provider=True)
            repository.close()

    asyncio.run(exercise())


def test_cancelled_manual_job_admission_drains_sqlite_worker(tmp_path):
    """Cancelling admission waits for the worker before the DB can close."""

    class BlockingRepository(SQLiteRepository):
        def __init__(self, path):
            super().__init__(path)
            self.entered = threading.Event()
            self.release = threading.Event()

        def upsert_collection_job(self, **kwargs):
            if kwargs.get("status") == "queued":
                self.entered.set()
                if not self.release.wait(timeout=2.0):
                    raise TimeoutError("fixture SQLite job write was not released")
            return super().upsert_collection_job(**kwargs)

    async def exercise():
        repository = BlockingRepository(tmp_path / "cancel-job-admission.sqlite3")
        monitor = MonitoringService(
            provider=MockMarketDataProvider(seed=790),
            repository=repository,
            symbols=["600519.SH"],
            collect_bars=False,
        )
        await asyncio.to_thread(repository.initialize)
        submit_task = asyncio.create_task(monitor.submit_collection_job())
        await asyncio.wait_for(asyncio.to_thread(repository.entered.wait), 1.0)
        submit_task.cancel()
        await asyncio.sleep(0.02)
        # The cancellation-safe helper keeps the coroutine alive while the
        # worker still owns the repository transaction.
        assert submit_task.done() is False
        repository.release.set()
        try:
            await submit_task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("job admission cancellation was swallowed")
        assert monitor.collection_jobs()["active"] == 0
        await monitor.stop(close_provider=True)
        repository.close()

    asyncio.run(exercise())


def test_monitor_start_restore_does_not_block_event_loop(tmp_path):
    """Direct MonitoringService users get the same non-blocking startup path."""

    class BlockingRepository(SQLiteRepository):
        def __init__(self, path):
            super().__init__(path)
            self.entered = threading.Event()
            self.release = threading.Event()

        def list_collection_job_records(self, limit=100):
            self.entered.set()
            if not self.release.wait(timeout=2.0):
                raise TimeoutError("fixture SQLite restore was not released")
            return super().list_collection_job_records(limit)

    async def exercise():
        repository = BlockingRepository(tmp_path / "start-restore.sqlite3")
        monitor = MonitoringService(
            provider=MockMarketDataProvider(seed=791),
            repository=repository,
            symbols=["600519.SH"],
            collect_bars=False,
            poll_interval=60.0,
        )
        ticks = 0
        heartbeat_done = asyncio.Event()

        async def heartbeat():
            nonlocal ticks
            while not heartbeat_done.is_set():
                ticks += 1
                await asyncio.sleep(0)

        heartbeat_task = asyncio.create_task(heartbeat())
        start_task = asyncio.create_task(monitor.start(collect_immediately=False))
        try:
            await asyncio.wait_for(asyncio.to_thread(repository.entered.wait), 1.0)
            await asyncio.sleep(0.02)
            assert ticks > 0
            repository.release.set()
            await asyncio.wait_for(start_task, 1.0)
            assert monitor.running
        finally:
            repository.release.set()
            heartbeat_done.set()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            if not start_task.done():
                start_task.cancel()
                await asyncio.gather(start_task, return_exceptions=True)
            await monitor.stop(close_provider=True)
            repository.close()

    asyncio.run(exercise())


def test_cancelled_collection_drains_sqlite_worker_before_task_finishes(tmp_path):
    """Runtime shutdown cannot close SQLite under an in-flight worker."""

    class BlockingRepository(SQLiteRepository):
        def __init__(self, path):
            super().__init__(path)
            self.entered = threading.Event()
            self.release = threading.Event()

        def upsert_quotes(self, quotes):
            self.entered.set()
            if not self.release.wait(timeout=2.0):
                raise TimeoutError("fixture SQLite write was not released")
            return super().upsert_quotes(quotes)

    async def exercise():
        repository = BlockingRepository(tmp_path / "cancel-persistence.sqlite3")
        await asyncio.to_thread(repository.initialize)
        collector = MarketDataCollectionService(
            provider=MockMarketDataProvider(seed=456),
            repository=repository,
            event_bus=EventBus(),
            collect_bars=False,
        )
        task = asyncio.create_task(collector.collect_quotes(["600519.SH"]))
        await asyncio.wait_for(asyncio.to_thread(repository.entered.wait), 1.0)
        task.cancel()
        # Cancellation is intentionally held until the worker transaction has
        # drained; this prevents Runtime.stop() from closing the connection
        # underneath a still-running executor thread.
        await asyncio.sleep(0.02)
        assert task.done() is False
        repository.release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("collection cancellation was swallowed")
        assert repository.get_latest_quotes()
        repository.close()

    asyncio.run(exercise())


def test_cancelled_bar_batch_is_terminal_before_task_finishes(tmp_path):
    """A cancelled provider request cannot leave a RUNNING lineage row."""

    class SlowProvider(MockMarketDataProvider):
        def __init__(self):
            super().__init__(seed=457)
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def fetch_bars_with_lineage(self, symbol, **kwargs):
            self.started.set()
            await self.release.wait()
            return await super().fetch_bars_with_lineage(symbol, **kwargs)

    class BlockingRepository(SQLiteRepository):
        def __init__(self, path):
            super().__init__(path)
            self.cancel_entered = threading.Event()
            self.cancel_release = threading.Event()

        def fail_collection_batch(self, batch):
            if batch.status == CollectionBatchStatus.CANCELLED:
                self.cancel_entered.set()
                if not self.cancel_release.wait(timeout=2.0):
                    raise TimeoutError("fixture cancelled-batch write was not released")
            return super().fail_collection_batch(batch)

    async def exercise():
        repository = BlockingRepository(tmp_path / "cancelled-bar-batch.sqlite3")
        provider = SlowProvider()
        await asyncio.to_thread(repository.initialize)
        collector = MarketDataCollectionService(
            provider=provider,
            repository=repository,
            event_bus=EventBus(),
            bar_interval="1d",
            bar_limit=2,
        )
        task = asyncio.create_task(collector.collect_bar_batch(["600519.SH"]))
        await asyncio.wait_for(provider.started.wait(), 1.0)
        task.cancel()
        await asyncio.wait_for(asyncio.to_thread(repository.cancel_entered.wait), 1.0)
        # The cancellation handler is deliberately draining the SQLite worker.
        assert task.done() is False
        assert repository.query_collection_batches(limit=10)[0][0].status == CollectionBatchStatus.RUNNING
        repository.cancel_release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("collection cancellation was swallowed")
        stored = repository.query_collection_batches(limit=10)[0][0]
        assert stored.status == CollectionBatchStatus.CANCELLED
        assert stored.error_type is None
        repository.close()

    asyncio.run(exercise())
