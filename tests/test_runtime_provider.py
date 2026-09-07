"""Runtime provider selection tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
import threading

from quant_monitor.config import Settings
import pytest

from quant_monitor.data_sources import (
    AShareHubBudgetError,
    AShareHubConfigurationError,
    AShareHubMarketDataProvider,
    AkShareMarketDataProvider,
    AllTickConfigurationError,
    AllTickMarketDataProvider,
    BarFetchResult,
    BarFetchTransport,
    MockMarketDataProvider,
)
from quant_monitor.data_sources.asharehub import (
    AShareHubAuthenticationError,
    AShareHubRateLimitError,
)
from quant_monitor.domain import Bar, Exchange, Instrument, SHANGHAI_TZ
from quant_monitor.domain import (
    CollectionBatch,
    CollectionBatchStatus,
    normalized_request_hash,
    now_market_time,
)
from quant_monitor.runtime import ApplicationRuntime
from quant_monitor.universes import BANK_STOCK_SYMBOLS


def test_runtime_selects_asharehub_without_network_on_construction(tmp_path):
    api_key = "unit-test-asharehub-key"
    runtime = ApplicationRuntime(
        Settings(
            data_provider="asharehub",
            asharehub_api_key=api_key,
            asharehub_budget_path=str(tmp_path / "budget.json"),
            database_path=str(tmp_path / "asharehub.sqlite3"),
            watchlist=["600519.SH"],
        )
    )
    try:
        assert isinstance(runtime.provider, AShareHubMarketDataProvider)
        assert runtime.provider.name == "asharehub"
        assert "AShareHub" in runtime.data_notice()
        status = runtime.system_status()
        assert status["provider"]["mode"] == "http_polling"
        assert status["provider"]["quota"]["soft_limit"] == 90
        assert status["provider"]["capabilities"]["intraday_bars"] is False
        assert api_key not in json.dumps(status)
    finally:
        runtime.repository.close()


def test_runtime_status_distinguishes_upstream_429_from_local_budget(tmp_path):
    runtime = ApplicationRuntime(
        Settings(
            data_provider="asharehub",
            asharehub_api_key="unit-test-key",
            asharehub_budget_path=str(tmp_path / "budget.json"),
            database_path=str(tmp_path / "status.sqlite3"),
            watchlist=["600519.SH"],
        )
    )
    today = runtime.provider._market_now().date().isoformat()
    runtime.provider._budget_used = 1
    runtime.provider._fatal_error = AShareHubRateLimitError(
        "upstream limit", status_code=429, operation="realtime"
    )
    runtime.provider._fatal_error_date = today

    status = runtime.system_status()

    assert status["provider"]["quota"]["upstream_exhausted"] is True
    assert status["provider"]["quota"]["local_exhausted"] is False
    assert status["provider"]["quota"]["exhausted"] is True
    assert "upstream_quota_exhausted" in status["provider"]["issues"]
    asyncio.run(runtime.provider.close())


def test_runtime_marks_interrupted_collection_batches_abandoned(tmp_path):
    async def exercise():
        runtime = ApplicationRuntime(
            Settings(
                data_provider="mock",
                database_path=str(tmp_path / "interrupted-batch.sqlite3"),
                watchlist=["600519.SH"],
                bar_interval="1d",
                history_bar_count=5,
                poll_interval_seconds=3600,
            )
        )
        runtime.repository.initialize()
        started_at = now_market_time()
        batch = CollectionBatch(
            batch_id="interrupted-before-restart",
            provider="mock",
            request_kind="bars",
            input_hash=normalized_request_hash({"symbols": ["600519.SH"]}),
            status=CollectionBatchStatus.RUNNING,
            started_at=started_at,
        )
        runtime.repository.begin_collection_batch(batch)
        try:
            await runtime.start()
            recovered = runtime.repository.get_collection_batch(batch.batch_id)
            assert recovered is not None
            assert recovered.status == CollectionBatchStatus.ABANDONED
            assert recovered.error_type == "ProcessInterrupted"
            assert recovered.completed_at >= started_at
        finally:
            await runtime.stop()

    asyncio.run(exercise())


def test_runtime_asharehub_requires_api_key(tmp_path):
    with pytest.raises(AShareHubConfigurationError, match="QUANT_MONITOR_ASHAREHUB_API_KEY"):
        ApplicationRuntime(
            Settings(
                data_provider="asharehub",
                asharehub_api_key="",
                asharehub_budget_path=str(tmp_path / "budget.json"),
                database_path=str(tmp_path / "missing-key.sqlite3"),
                watchlist=["600519.SH"],
            )
        )


def test_runtime_asharehub_initial_auth_failure_spends_only_one_request(tmp_path):
    async def exercise():
        runtime = ApplicationRuntime(
            Settings(
                data_provider="asharehub",
                asharehub_api_key="rejected-test-key",
                asharehub_budget_path=str(tmp_path / "budget.json"),
                database_path=str(tmp_path / "auth-failure.sqlite3"),
                watchlist=["600519.SH", "000001.SZ"],
                poll_interval_seconds=3600,
            )
        )
        calls = []

        def reject(method, url, headers, params, timeout):
            calls.append((method, url))
            raise AShareHubAuthenticationError(
                "AShareHub API key authentication failed",
                status_code=401,
                operation="realtime",
            )

        runtime.provider._requester = reject
        await runtime.start()
        deadline = asyncio.get_running_loop().time() + 2.0
        while not calls and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0)
        while (
            runtime.monitor.current_snapshot()["last_error"] is None
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0)
        status = runtime.system_status()
        await runtime.stop()
        return calls, status

    calls, status = asyncio.run(exercise())

    assert len(calls) == 1
    assert status["provider"]["quota"]["used"] == 1
    assert "no_quote_data" in status["provider"]["issues"]
    assert status["collector"]["last_error"] is not None


def test_runtime_asharehub_bank_start_does_not_wait_for_initial_http(tmp_path):
    async def exercise():
        runtime = ApplicationRuntime(
            Settings(
                data_provider="asharehub",
                asharehub_api_key="slow-unit-test-key",
                asharehub_budget_path=str(tmp_path / "budget.json"),
                database_path=str(tmp_path / "nonblocking.sqlite3"),
                watchlist=list(BANK_STOCK_SYMBOLS),
                universe_mode="watchlist",
                poll_interval_seconds=3600,
            )
        )
        entered = threading.Event()
        release = threading.Event()

        def slow_request(method, url, headers, params, timeout):
            entered.set()
            release.wait(timeout=2.0)
            raise AShareHubAuthenticationError(
                "fixture request released",
                status_code=401,
                operation="realtime",
            )

        runtime.provider._requester = slow_request
        try:
            await asyncio.wait_for(runtime.start(), timeout=0.5)
            assert runtime.monitor.running is True
            startup = runtime.system_status()["startup"]
            assert startup["ready"] is True
            assert startup["ready_at"] is not None
            assert startup["collection_mode"] == "background"
            history = startup["history_bootstrap"]
            assert history["state"] in {"scheduled", "running"}
            assert history["ready"] == 0
            assert history["total"] == 42
            assert history["remaining"] == 42
            assert history["attempted"] <= 1
            assert history["failed"] == {}
            assert history["deferred_for_budget"] is False
            assert await asyncio.to_thread(entered.wait, 1.0)
        finally:
            release.set()
            await runtime.stop()

    asyncio.run(exercise())


def test_runtime_asharehub_history_persists_each_symbol_before_next_request(tmp_path):
    async def exercise():
        runtime = ApplicationRuntime(
            Settings(
                data_provider="asharehub",
                asharehub_api_key="incremental-unit-test-key",
                asharehub_budget_path=str(tmp_path / "budget.json"),
                database_path=str(tmp_path / "incremental.sqlite3"),
                watchlist=["600519.SH", "600036.SH"],
                universe_mode="watchlist",
                history_bar_count=130,
                poll_interval_seconds=3600,
                asharehub_history_refresh_seconds=3600,
            )
        )
        second_started = asyncio.Event()
        release_second = asyncio.Event()
        calls = []

        async def fetch_bar(
            symbol,
            interval="1d",
            start=None,
            end=None,
            limit=200,
            reserve_requests=0,
        ):
            calls.append(symbol)
            if symbol == "600036.SH":
                second_started.set()
                await release_second.wait()
            latest = datetime(2026, 8, 28, tzinfo=SHANGHAI_TZ)
            bars = [
                Bar(
                    symbol=symbol,
                    interval="1d",
                    timestamp=latest - timedelta(days=limit - index - 1),
                    open="10",
                    high="11",
                    low="9",
                    close="10.5",
                    volume=100,
                    source="asharehub",
                )
                for index in range(limit)
            ]
            return BarFetchResult(
                bars=tuple(bars),
                observations=tuple(bars),
                transport=BarFetchTransport.NETWORK,
                response_received_at=now_market_time(),
            )

        runtime.provider.fetch_bars_with_lineage = fetch_bar
        runtime.provider.has_current_bar_history = lambda *args, **kwargs: False
        runtime.provider.has_current_bar_data = lambda symbol, **kwargs: (
            symbol == "600519.SH" and bool(
                runtime.repository.get_bars(
                    symbol, "1d", limit=130, source_prefix="asharehub"
                )
            )
        )
        # Keep quote collection entirely local to isolate history ordering.
        runtime.provider.fetch_quotes = lambda symbols: asyncio.sleep(0, result=[])
        try:
            await runtime.start()
            await asyncio.wait_for(second_started.wait(), timeout=2.0)
            persisted = runtime.repository.get_bars(
                "600519.SH", "1d", limit=130, source_prefix="asharehub"
            )
            persisted_metrics = runtime.repository.get_metrics(
                "600519.SH", limit=20
            )
            status = runtime.system_status()["startup"]["history_bootstrap"]
            assert len(persisted) == 130
            assert persisted_metrics
            assert calls[:2] == ["600519.SH", "600036.SH"]
            assert status["ready"] == 1
            assert status["current_symbol"] == "600036.SH"
        finally:
            release_second.set()
            await runtime.stop()

    asyncio.run(exercise())


def test_runtime_asharehub_history_respects_reserved_request_budget(tmp_path):
    async def exercise():
        runtime = ApplicationRuntime(
            Settings(
                data_provider="asharehub",
                asharehub_api_key="reserve-unit-test-key",
                asharehub_budget_path=str(tmp_path / "budget.json"),
                database_path=str(tmp_path / "reserve.sqlite3"),
                watchlist=["600519.SH"],
                universe_mode="watchlist",
                poll_interval_seconds=3600,
                asharehub_daily_soft_limit=11,
                asharehub_history_reserve_requests=10,
                asharehub_history_refresh_seconds=3600,
            )
        )
        calls = []

        async def must_not_fetch(*args, **kwargs):
            calls.append(True)
            return []

        runtime.provider.fetch_bars_with_lineage = must_not_fetch
        runtime.provider.fetch_quotes = lambda symbols: asyncio.sleep(0, result=[])
        runtime.provider.has_current_bar_history = lambda *args, **kwargs: False
        runtime.provider.has_current_bar_data = lambda *args, **kwargs: False
        runtime.provider._budget_used = 1
        try:
            await runtime.start()
            deadline = asyncio.get_running_loop().time() + 1.0
            while (
                not runtime.system_status()["startup"]["history_bootstrap"][
                    "deferred_for_budget"
                ]
                and asyncio.get_running_loop().time() < deadline
            ):
                await asyncio.sleep(0)
            history = runtime.system_status()["startup"]["history_bootstrap"]
            assert history["state"] == "deferred_budget"
            assert history["attempted"] == 0
            assert calls == []
        finally:
            await runtime.stop()

    asyncio.run(exercise())


def test_runtime_does_not_refetch_fresh_strategy_ready_partial_history(tmp_path):
    async def exercise():
        runtime = ApplicationRuntime(
            Settings(
                data_provider="asharehub",
                asharehub_api_key="ready-history-unit-test-key",
                asharehub_budget_path=str(tmp_path / "budget.json"),
                database_path=str(tmp_path / "ready-history.sqlite3"),
                watchlist=["600519.SH"],
                universe_mode="watchlist",
                history_bar_count=240,
                bank_strategy_min_history=130,
                poll_interval_seconds=3600,
                asharehub_history_refresh_seconds=3600,
            )
        )
        calls = []

        async def must_not_fetch(*args, **kwargs):
            calls.append((args, kwargs))
            return []

        runtime.provider.fetch_bars_with_lineage = must_not_fetch
        runtime.provider.fetch_quotes = lambda symbols: asyncio.sleep(0, result=[])
        runtime.provider.has_current_bar_data = lambda *args, **kwargs: True
        runtime.provider.has_current_bar_history = lambda *args, **kwargs: False
        try:
            await runtime.start()
            await asyncio.sleep(0)
            assert runtime.system_status()["startup"]["research_ready"] is True
            assert calls == []
        finally:
            await runtime.stop()

    asyncio.run(exercise())


def test_runtime_marks_atomic_reserve_race_as_deferred_not_fatal(tmp_path):
    async def exercise():
        runtime = ApplicationRuntime(
            Settings(
                data_provider="asharehub",
                asharehub_api_key="reserve-race-unit-test-key",
                asharehub_budget_path=str(tmp_path / "budget.json"),
                database_path=str(tmp_path / "reserve-race.sqlite3"),
                watchlist=["600519.SH"],
                universe_mode="watchlist",
                poll_interval_seconds=3600,
                asharehub_history_refresh_seconds=3600,
            )
        )

        async def reserve_race(*args, **kwargs):
            raise AShareHubBudgetError("reserved locally", reserved=True)

        runtime.provider.fetch_bars_with_lineage = reserve_race
        runtime.provider.fetch_quotes = lambda symbols: asyncio.sleep(0, result=[])
        runtime.provider.has_current_bar_data = lambda *args, **kwargs: False
        try:
            await runtime.start()
            deadline = asyncio.get_running_loop().time() + 1.0
            while (
                not runtime.system_status()["startup"]["history_bootstrap"][
                    "deferred_for_budget"
                ]
                and asyncio.get_running_loop().time() < deadline
            ):
                await asyncio.sleep(0)
            history = runtime.system_status()["startup"]["history_bootstrap"]
            assert history["state"] == "deferred_budget"
            assert history["deferred_for_budget"] is True
            assert history["attempted"] == 1
        finally:
            await runtime.stop()

    asyncio.run(exercise())


def test_runtime_asharehub_instruments_fall_back_to_persisted_catalogue(tmp_path):
    async def exercise():
        runtime = ApplicationRuntime(
            Settings(
                data_provider="asharehub",
                asharehub_api_key="unit-test-key",
                asharehub_budget_path=str(tmp_path / "budget.json"),
                database_path=str(tmp_path / "catalogue.sqlite3"),
                watchlist=["600519.SH"],
            )
        )
        runtime.repository.initialize()
        expected = Instrument(
            "600519.SH",
            "贵州茅台",
            Exchange.SH,
            metadata={"source": "asharehub"},
        )
        runtime.repository.upsert_instruments([expected])

        async def fail():
            raise AShareHubAuthenticationError("authentication failed")

        runtime.provider.fetch_instruments = fail
        items = await runtime.instruments()
        runtime.repository.close()
        return items

    items = asyncio.run(exercise())
    assert [item.symbol for item in items] == ["600519.SH"]


def test_runtime_does_not_seed_incomplete_catalogue_with_enough_banks(tmp_path):
    async def exercise():
        runtime = ApplicationRuntime(
            Settings(
                data_provider="asharehub",
                asharehub_api_key="unit-test-key",
                asharehub_budget_path=str(tmp_path / "budget.json"),
                database_path=str(tmp_path / "incomplete-catalogue.sqlite3"),
                universe_mode="bank",
                poll_interval_seconds=3600,
            )
        )
        runtime.repository.initialize()
        partial_banks = [
            Instrument(
                symbol,
                "银行{}".format(index),
                Exchange(symbol.rsplit(".", 1)[1]),
                metadata={"source": "asharehub", "industry": "银行"},
            )
            # Meeting the bank-count threshold alone must not make a tiny
            # whole-market catalogue look authoritative for seven days.
            for index, symbol in enumerate(BANK_STOCK_SYMBOLS[:40])
        ]
        runtime.repository.upsert_instruments(
            partial_banks
            + [
                Instrument(
                    "600519.SH",
                    "贵州茅台",
                    Exchange.SH,
                    metadata={"source": "asharehub", "industry": "白酒"},
                )
            ]
        )
        seeded = []
        refreshed = asyncio.Event()

        def seed(items):
            seeded.extend(items)
            return len(items)

        async def fetch_instruments():
            refreshed.set()
            return []

        runtime.provider.seed_instruments = seed
        runtime.provider.fetch_instruments = fetch_instruments
        runtime.provider.fetch_quotes = lambda symbols: asyncio.sleep(0, result=[])
        runtime.provider.has_current_bar_history = lambda *args, **kwargs: True
        runtime.provider.has_current_bar_data = lambda *args, **kwargs: True
        try:
            await runtime.start()
            await asyncio.wait_for(refreshed.wait(), timeout=1.0)
            assert seeded == []
            assert runtime.universe_source == "static_bank_fallback"
            assert runtime._catalogue_refresh_task is not None
        finally:
            await runtime.stop()

    asyncio.run(exercise())


def test_background_catalogue_rejects_partial_result_without_clearing_cache(tmp_path):
    async def exercise():
        runtime = ApplicationRuntime(
            Settings(
                data_provider="asharehub",
                asharehub_api_key="unit-test-key",
                asharehub_budget_path=str(tmp_path / "budget.json"),
                database_path=str(tmp_path / "partial-catalogue.sqlite3"),
                universe_mode="bank",
            )
        )
        runtime.repository.initialize()
        original = Instrument(
            "600519.SH",
            "贵州茅台",
            Exchange.SH,
            metadata={"source": "asharehub", "industry": "白酒"},
        )
        runtime.repository.upsert_instruments([original])
        partial = [
            Instrument(
                symbol,
                name,
                Exchange(symbol.rsplit(".", 1)[1]),
                metadata={"source": "asharehub", "industry": "银行"},
            )
            for symbol, name in zip(
                BANK_STOCK_SYMBOLS[:15], ("银行{}".format(i) for i in range(15))
            )
        ]
        runtime.provider.fetch_instruments = lambda: asyncio.sleep(0, result=partial)
        try:
            await runtime._refresh_bank_catalogue()
            assert runtime.repository.list_instruments() == [original]
        finally:
            runtime.repository.close()

    asyncio.run(exercise())


def test_runtime_selects_alltick_without_network_on_construction(tmp_path):
    token = "unit-test-token"
    runtime = ApplicationRuntime(
        Settings(
            data_provider="alltick",
            alltick_token=token,
            database_path=str(tmp_path / "alltick.sqlite3"),
            watchlist=["600519.SH"],
        )
    )
    try:
        assert isinstance(runtime.provider, AllTickMarketDataProvider)
        assert runtime.provider.name == "alltick"
        assert runtime.provider.universe_scope == "configured_symbols"
        assert "AllTick" in runtime.data_notice()
        status = runtime.system_status()
        assert status["provider"]["realtime"]["enabled"] is True
        assert token not in json.dumps(status)
    finally:
        runtime.repository.close()


def test_runtime_alltick_requires_token(tmp_path):
    with pytest.raises(AllTickConfigurationError, match="QUANT_MONITOR_ALLTICK_TOKEN"):
        ApplicationRuntime(
            Settings(
                data_provider="alltick",
                alltick_token="",
                database_path=str(tmp_path / "missing-token.sqlite3"),
                watchlist=["600519.SH"],
            )
        )


def test_runtime_health_reports_optional_alltick_capability_failures(tmp_path):
    class RunningTask:
        @staticmethod
        def done():
            return False

    runtime = ApplicationRuntime(
        Settings(
            data_provider="alltick",
            alltick_token="unit-test-token",
            database_path=str(tmp_path / "alltick-degraded.sqlite3"),
            watchlist=["600519.SH"],
        )
    )
    try:
        runtime.monitor._task = RunningTask()
        runtime.provider._ws_task = RunningTask()
        runtime.provider._ws_connected = True
        runtime.provider._ws_tick_subscribed = 1
        runtime.provider._ws_depth_last_error = "AllTickPermissionError"
        runtime.provider._ws_depth_last_error_code = 604

        status = runtime.system_status()
        assert status["status"] == "degraded"
        assert status["healthy"] is False
        assert status["provider"]["state"] == "degraded"
        assert "order_book_unavailable" in status["provider"]["issues"]
        assert status["provider"]["realtime"]["subscribed_symbols"] == 1
    finally:
        runtime.monitor._task = None
        runtime.provider._ws_task = None
        runtime.repository.close()


def test_runtime_selects_akshare_provider_without_network_on_construction(tmp_path):
    async def exercise():
        runtime = ApplicationRuntime(
            Settings(
                data_provider="akshare",
                database_path=str(tmp_path / "akshare.sqlite3"),
                watchlist=["600519.SH"],
            )
        )
        try:
            assert isinstance(runtime.provider, AkShareMarketDataProvider)
            assert runtime.provider.name == "akshare"
        finally:
            runtime.repository.close()

    asyncio.run(exercise())


def test_runtime_keeps_mock_provider_available_for_offline_runs(tmp_path):
    async def exercise():
        runtime = ApplicationRuntime(
            Settings(
                data_provider="mock",
                database_path=str(tmp_path / "mock.sqlite3"),
                watchlist=["600519.SH"],
            )
        )
        try:
            assert isinstance(runtime.provider, MockMarketDataProvider)
            assert runtime.provider.name == "mock"
        finally:
            runtime.repository.close()

    asyncio.run(exercise())
