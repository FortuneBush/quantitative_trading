"""Offline tests for schema-v3 collection and temporal lineage."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest

from quant_monitor.data_sources import (
    BarFetchResult,
    BarFetchTransport,
    MockMarketDataProvider,
)
from quant_monitor.domain import (
    Adjustment,
    Bar,
    BarObservation,
    CollectionBatch,
    CollectionBatchStatus,
    ObservationQualityStatus,
    normalized_request_hash,
    parse_market_time,
)
from quant_monitor.services import EventBus, MarketDataCollectionService
from quant_monitor.storage import SQLiteRepository


T0 = parse_market_time("2026-09-03T15:30:00+08:00")
T1 = parse_market_time("2026-09-04T09:00:00+08:00")
T2 = parse_market_time("2026-09-04T09:00:01+08:00")


def _running(batch_id="batch-1", provider="vendor"):
    return CollectionBatch(
        batch_id=batch_id,
        provider=provider,
        request_kind="bars",
        input_hash=normalized_request_hash({"symbols": ["600000.SH"]}),
        status=CollectionBatchStatus.RUNNING,
        started_at=T1,
    )


def _observation(batch_id="batch-1", source="vendor", adjustment="raw", revision="r1"):
    return BarObservation(
        batch_id=batch_id,
        symbol="600000.SH",
        interval="1d",
        session_date=T0.date(),
        period_start=parse_market_time("2026-09-03T09:30:00+08:00"),
        period_end=parse_market_time("2026-09-03T15:00:00+08:00"),
        available_at=T1,
        received_at=T1,
        persisted_at=T2,
        source=source,
        adjustment=adjustment,
        revision_id=revision,
        quality_status=ObservationQualityStatus.DEGRADED,
        open="10",
        high="11",
        low="9",
        close="10.5",
        volume=100,
        turnover="1050",
    )


def _completed(running, item_count):
    return CollectionBatch(
        batch_id=running.batch_id,
        provider=running.provider,
        request_kind=running.request_kind,
        input_hash=running.input_hash,
        status=CollectionBatchStatus.SUCCEEDED,
        started_at=running.started_at,
        completed_at=T2,
        item_count=item_count,
    )


def test_bar_observation_rejects_future_availability_and_bad_clock_order():
    with pytest.raises(ValueError, match="available_at cannot precede period_end"):
        BarObservation(
            **{
                **_observation().__dict__,
                "available_at": parse_market_time("2026-09-03T14:59:59+08:00"),
            }
        )
    with pytest.raises(ValueError, match="available_at cannot follow received_at"):
        BarObservation(
            **{
                **_observation().__dict__,
                "available_at": T2,
                "received_at": T1,
                "persisted_at": T2,
            }
        )
    with pytest.raises(ValueError, match="persisted_at cannot precede received_at"):
        BarObservation(
            **{
                **_observation().__dict__,
                "received_at": T2,
                "persisted_at": T1,
                "available_at": T1,
            }
        )


def test_repository_keeps_source_adjustment_and_revision_observations_side_by_side(tmp_path):
    repository = SQLiteRepository(tmp_path / "lineage.sqlite3")
    repository.initialize()
    try:
        observations = (
            _observation(),
            _observation(source="second", revision="r1"),
            _observation(adjustment="qfq", revision="r1"),
            _observation(revision="r2"),
        )
        running = _running()
        repository.begin_collection_batch(running)
        assert repository.complete_bar_collection_batch(
            _completed(running, len(observations)), observations
        ) == 4
        stored = repository.get_bar_observations("600000.SH", "1d")
        assert len(stored) == 4
        assert {(item.source, item.adjustment.value, item.revision_id) for item in stored} == {
            ("vendor", "raw", "r1"),
            ("second", "raw", "r1"),
            ("vendor", "qfq", "r1"),
            ("vendor", "raw", "r2"),
        }
        assert repository.get_collection_batch(running.batch_id).status == CollectionBatchStatus.SUCCEEDED
    finally:
        repository.close()


def test_duplicate_observation_identity_is_not_silently_ignored(tmp_path):
    repository = SQLiteRepository(tmp_path / "duplicate-lineage.sqlite3")
    repository.initialize()
    running = _running()
    repository.begin_collection_batch(running)
    duplicate = _observation()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            repository.complete_bar_collection_batch(
                _completed(running, 2), (duplicate, duplicate)
            )
        assert repository.get_collection_batch(running.batch_id).status == CollectionBatchStatus.RUNNING
        assert repository.get_bar_observations("600000.SH") == []
    finally:
        repository.close()


def test_repository_abandons_batches_left_running_by_previous_process(tmp_path):
    repository = SQLiteRepository(tmp_path / "abandoned.sqlite3")
    repository.initialize()
    running = _running()
    repository.begin_collection_batch(running)
    try:
        assert repository.abandon_running_collection_batches(T2) == 1
        stored = repository.get_collection_batch(running.batch_id)
        assert stored.status == CollectionBatchStatus.ABANDONED
        assert stored.error_type == "ProcessInterrupted"
        assert stored.error_message is None
    finally:
        repository.close()


def test_complete_batch_rolls_back_observations_legacy_bars_and_terminal_state(tmp_path):
    repository = SQLiteRepository(tmp_path / "atomic-lineage.sqlite3")
    repository.initialize()
    running = _running()
    repository.begin_collection_batch(running)
    observation = _observation()
    try:
        with repository._lock:
            connection = repository._conn()
            connection.execute(
                """
                CREATE TRIGGER reject_legacy_bar BEFORE INSERT ON bars
                BEGIN SELECT RAISE(ABORT, 'fixture rollback'); END
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="fixture rollback"):
            repository.complete_bar_collection_batch(
                _completed(running, 1), (observation,)
            )
        assert repository.get_collection_batch(running.batch_id).status == CollectionBatchStatus.RUNNING
        assert repository.get_bar_observations("600000.SH", "1d") == []
        assert repository.get_bars("600000.SH", "1d") == []
    finally:
        repository.close()


def test_collection_network_failure_leaves_failed_batch_without_secret_message(tmp_path):
    class BrokenProvider(MockMarketDataProvider):
        async def fetch_bars(self, *args, **kwargs):
            raise RuntimeError("credential-bearing fixture")

    async def exercise():
        repository = SQLiteRepository(tmp_path / "failed-batch.sqlite3")
        repository.initialize()
        service = MarketDataCollectionService(
            BrokenProvider(), repository, EventBus(), bar_interval="1d", bar_limit=1
        )
        try:
            with pytest.raises(RuntimeError, match="credential-bearing"):
                await service.collect_bar_batch(["600000.SH"])
            row = repository._conn().execute(
                "SELECT status, error_type, error_message FROM collection_batches"
            ).fetchone()
            assert tuple(row) == ("failed", "RuntimeError", None)
            assert repository._conn().execute(
                "SELECT COUNT(*) FROM bar_observations"
            ).fetchone()[0] == 0
        finally:
            repository.close()

    asyncio.run(exercise())


def test_typed_network_bar_result_dual_writes_and_events_only_after_commit(tmp_path):
    async def exercise():
        repository = SQLiteRepository(tmp_path / "service-lineage.sqlite3")
        repository.initialize()
        bus = EventBus()
        events = []

        async def observe(event):
            batch = repository._conn().execute(
                "SELECT status FROM collection_batches"
            ).fetchone()
            assert batch[0] == "succeeded"
            events.append(event)

        await bus.subscribe(None, observe)
        service = MarketDataCollectionService(
            MockMarketDataProvider(), repository, bus, bar_interval="1d", bar_limit=1
        )
        bar = Bar(
            symbol="600000.SH",
            interval="1d",
            timestamp=T0,
            open=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10.5"),
            source="fixture",
        )
        try:
            await service.persist_bar_fetch_result(
                BarFetchResult(
                    bars=(bar,),
                    observations=(bar,),
                    transport=BarFetchTransport.NETWORK,
                    response_received_at=T1,
                ),
                requested_symbols=("600000.SH",),
            )
            observation = repository.get_bar_observations("600000.SH", "1d")[0]
            assert observation.available_at == observation.received_at
            assert observation.period_end <= observation.available_at
            assert observation.received_at <= observation.persisted_at
            assert observation.quality_status == ObservationQualityStatus.DEGRADED
            assert repository.get_bars("600000.SH", "1d")[0].timestamp == T0
            assert events
        finally:
            repository.close()

    asyncio.run(exercise())


def test_legacy_bar_list_does_not_invent_observations(tmp_path):
    async def exercise():
        repository = SQLiteRepository(tmp_path / "legacy-list.sqlite3")
        repository.initialize()
        service = MarketDataCollectionService(
            MockMarketDataProvider(), repository, EventBus(), bar_interval="1d"
        )
        bar = Bar(
            symbol="600000.SH",
            interval="1d",
            timestamp=T0,
            open="10",
            high="11",
            low="9",
            close="10.5",
            source="legacy-provider",
        )
        try:
            await service.persist_bar_batch(
                (bar,), requested_symbols=("600000.SH",), publish_events=False
            )
            assert repository.get_bar_observations("600000.SH", "1d") == []
            assert repository.get_bars("600000.SH", "1d") == [bar]
            row = repository._conn().execute(
                "SELECT status, item_count, metadata FROM collection_batches"
            ).fetchone()
            assert row[0] == "succeeded"
            assert row[1] == 0
            assert '"transport":"legacy_unknown"' in row[2]
        finally:
            repository.close()

    asyncio.run(exercise())
