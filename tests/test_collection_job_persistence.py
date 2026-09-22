"""Offline restart guarantees for durable collector-job idempotency."""

from __future__ import annotations

import asyncio

from quant_monitor.data_sources import MockMarketDataProvider
from quant_monitor.services import MonitoringService
from quant_monitor.storage import SQLiteRepository


def test_idempotency_key_replays_terminal_job_after_repository_restart(tmp_path):
    async def exercise():
        path = tmp_path / "restart.sqlite3"
        key = "restart-safe-refresh"
        first_repo = SQLiteRepository(path)
        first_monitor = MonitoringService(
            provider=MockMarketDataProvider(seed=101),
            repository=first_repo,
            symbols=["600519.SH"],
            collect_bars=False,
        )
        try:
            first = await first_monitor.submit_collection_job(key)
            for _ in range(100):
                terminal = first_monitor.collection_job(first["job_id"])
                if terminal["terminal"]:
                    break
                await asyncio.sleep(0.01)
            assert terminal["status"] == "succeeded"
        finally:
            await first_monitor.stop(close_provider=True)
            first_repo.close()

        second_repo = SQLiteRepository(path)
        second_monitor = MonitoringService(
            provider=MockMarketDataProvider(seed=202),
            repository=second_repo,
            symbols=["600519.SH"],
            collect_bars=False,
        )
        try:
            replay = await second_monitor.submit_collection_job(key)
            assert replay["job_id"] == first["job_id"]
            assert replay["status"] == "succeeded"
            assert replay["idempotency_replayed"] is True
        finally:
            await second_monitor.stop(close_provider=True)
            second_repo.close()

    asyncio.run(exercise())


def test_interrupted_job_is_terminalized_without_provider_replay(tmp_path):
    async def exercise():
        path = tmp_path / "interrupted.sqlite3"
        repository = SQLiteRepository(path)
        repository.initialize()
        try:
            from quant_monitor.domain import now_market_time

            created = now_market_time()
            repository.upsert_collection_job(
                job_id="interrupted-job",
                idempotency_key_digest=("a" * 64),
                status="running",
                requested_symbols=["600519.SH"],
                created_at=created,
                started_at=created,
            )
        finally:
            repository.close()

        reopened = SQLiteRepository(path)
        monitor = MonitoringService(
            provider=MockMarketDataProvider(seed=303),
            repository=reopened,
            symbols=["600519.SH"],
            collect_bars=False,
        )
        try:
            await monitor.start(collect_immediately=False)
            assert monitor.collection_job("interrupted-job")["status"] == "failed"
            recovered = reopened.get_collection_job_record("interrupted-job")
            assert recovered["status"] == "failed"
            assert recovered["error_type"] == "ProcessRestarted"
            assert recovered["completed_at"] is not None
        finally:
            await monitor.stop(close_provider=True)
            reopened.close()

    asyncio.run(exercise())


def test_cancel_hydrates_durable_job_when_it_is_first_request_after_restart(tmp_path):
    """Cancel must not require a preceding GET/start call to restore the ledger."""

    async def exercise():
        path = tmp_path / "cancel-after-restart.sqlite3"
        repository = SQLiteRepository(path)
        repository.initialize()
        try:
            from quant_monitor.domain import now_market_time

            created = now_market_time()
            repository.upsert_collection_job(
                job_id="restart-cancel-job",
                idempotency_key_digest=("b" * 64),
                status="running",
                requested_symbols=["600519.SH"],
                created_at=created,
                started_at=created,
            )
        finally:
            repository.close()

        reopened = SQLiteRepository(path)
        monitor = MonitoringService(
            provider=MockMarketDataProvider(seed=404),
            repository=reopened,
            symbols=["600519.SH"],
            collect_bars=False,
        )
        try:
            # Do not call start() or collection_job() first: cancellation is
            # intentionally the first lifecycle request after the restart.
            cancelled = await monitor.cancel_collection_job("restart-cancel-job")
            assert cancelled is not None
            assert cancelled["status"] == "failed"
            assert cancelled["error_type"] == "ProcessRestarted"
            assert cancelled["terminal"] is True
            assert monitor.collection_job("restart-cancel-job")["status"] == "failed"
        finally:
            await monitor.stop(close_provider=True)
            reopened.close()

    asyncio.run(exercise())
