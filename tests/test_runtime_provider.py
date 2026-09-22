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
from quant_monitor.domain import Bar, Exchange, Instrument, Quote, SHANGHAI_TZ
from quant_monitor.domain import (
    CollectionBatch,
    CollectionBatchStatus,
    normalized_request_hash,
    now_market_time,
)
from quant_monitor.runtime import (
    ApplicationRuntime,
    build_calendar_readiness,
    build_data_readiness,
)
from quant_monitor.universes import BANK_STOCK_SYMBOLS
from quant_monitor.trading_calendar import VersionedSessionCalendar


@pytest.mark.parametrize(
    ("provider", "enabled", "source", "error", "expected_ready", "reason"),
    [
        ("mock", False, "weekday_fallback", None, True, "not_required"),
        ("asharehub", True, "asharehub_cache", None, True, "exchange_calendar_loaded"),
        ("asharehub", True, "asharehub_partial", "TradeCalendarPartialData", False, "exchange_calendar_unavailable"),
        ("asharehub", True, "weekday_fallback", "TradeCalendarCacheExpired", False, "exchange_calendar_unavailable"),
        ("asharehub", False, "weekday_fallback", None, False, "calendar_disabled"),
    ],
)
def test_calendar_readiness_is_cache_only_and_fail_closed(
    provider, enabled, source, error, expected_ready, reason
):
    result = build_calendar_readiness(
        provider_name=provider,
        calendar_enabled=enabled,
        calendar_source=source,
        calendar_error_type=error,
        configured_symbols=2,
    )
    assert result["ready"] is expected_ready
    assert result["reason"] == reason


@pytest.mark.parametrize(
    "market_is_trading, state, received, expected, reason",
    [
        (False, "stale", 1, False, "market_closed_cached_close_allowed"),
        (False, "stale", 2, True, "market_closed_cached_close_allowed"),
        (False, "future_timestamp", 2, False, "market_closed_cached_close_allowed"),
        (True, "untracked", 2, True, "fresh_quotes_required"),
        (True, "fresh", 2, True, "fresh_quotes_required"),
    ],
)
def test_data_readiness_uses_one_strict_full_coverage_rule(
    market_is_trading, state, received, expected, reason
):
    readiness = build_data_readiness(
        configured_symbols=2,
        received_symbols=received,
        freshness_state=state,
        market_is_trading=market_is_trading,
    )
    assert readiness["ready"] is expected
    assert readiness["reason"] == reason


def test_runtime_builds_each_configured_profile_through_allowlisted_registry(tmp_path):
    for profile, expected_attribute, expected_type in (
        ("moving_average_cross", "strategies", "moving_average_cross"),
        ("bank_daily_multifactor_v1", "cohort_strategies", "bank_daily_multifactor_v1"),
    ):
        runtime = ApplicationRuntime(
            Settings(
                data_provider="mock",
                strategy_profile=profile,
                database_path=str(tmp_path / (profile + ".sqlite3")),
                watchlist=["600519.SH"],
                bank_strategy_min_history=121,
                bank_strategy_min_universe=2,
            )
        )
        try:
            strategies = getattr(runtime.monitor, expected_attribute)
            assert len(strategies) == 1
            assert strategies[0].name == expected_type
        finally:
            runtime.repository.close()


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
        assert status["calendar_readiness"]["ready"] is False
        assert status["calendar_readiness"]["reason"] == "calendar_disabled"
        assert api_key not in json.dumps(status)
        assert status["database"] == {"type": "sqlite", "state": "disconnected"}
    finally:
        runtime.repository.close()


def test_runtime_calendar_view_is_shared_with_collector_and_has_no_network_side_effect(tmp_path):
    runtime = ApplicationRuntime(
        Settings(
            data_provider="asharehub",
            asharehub_api_key="unit-test-asharehub-key",
            asharehub_trade_calendar_enabled=True,
            asharehub_budget_path=str(tmp_path / "budget.json"),
            database_path=str(tmp_path / "calendar-runtime.sqlite3"),
            watchlist=["600519.SH", "000001.SZ"],
        )
    )
    try:
        provider = runtime.provider
        now = provider._monotonic()
        provider._trade_calendar_loaded_at = now
        provider._trade_calendar["SSE"] = {
            datetime(2026, 10, 2).date(): (True, None),
            datetime(2026, 10, 5).date(): (False, datetime(2026, 10, 2).date()),
            datetime(2026, 10, 6).date(): (True, datetime(2026, 10, 5).date()),
        }
        provider._trade_calendar["SZSE"] = dict(provider._trade_calendar["SSE"])
        provider._trade_calendar_source = "asharehub_cache"
        provider._trade_calendar_error_type = None
        calendar = runtime.current_trading_calendar()
        assert isinstance(calendar, VersionedSessionCalendar)
        assert runtime.monitor.collector.trading_calendar is calendar
        assert calendar.metadata.identifier == "asharehub_trade_calendar"
        assert runtime.system_status()["calendar_readiness"]["ready"] is True
    finally:
        runtime.repository.close()


def test_runtime_status_redacts_collection_error_text(tmp_path):
    """Public health diagnostics expose type, never provider exception text."""

    secret = "api-token-in-provider-response"
    runtime = ApplicationRuntime(
        Settings(
            data_provider="mock",
            database_path=str(tmp_path / "redaction.sqlite3"),
            watchlist=["600519.SH"],
        )
    )
    try:
        runtime.monitor._last_error = (
            "MockMarketDataError: upstream response contained {}".format(secret)
        )
        status = runtime.system_status()
        collector = status["collector"]
        assert collector["last_error_type"] == "MockMarketDataError"
        assert collector["last_error"] == "collection failed (MockMarketDataError)"
        assert secret not in json.dumps(status)
    finally:
        runtime.repository.close()


def test_runtime_status_exposes_quote_freshness_without_provider_io(tmp_path):
    runtime = ApplicationRuntime(
        Settings(
            data_provider="asharehub",
            asharehub_api_key="unit-test-key",
            asharehub_budget_path=str(tmp_path / "budget.json"),
            database_path=str(tmp_path / "freshness.sqlite3"),
            watchlist=["600519.SH", "600036.SH", "601318.SH"],
        )
    )
    try:
        runtime.quote_stale_after_seconds = 30.0
        now = now_market_time()
        runtime.monitor._latest_quotes = {
            "600519.SH": Quote(
                symbol="600519.SH",
                timestamp=now - timedelta(seconds=90),
                last="100",
                source="asharehub",
            ),
            "600036.SH": Quote(
                symbol="600036.SH",
                timestamp=now + timedelta(seconds=30),
                last="10",
                source="asharehub",
            ),
        }
        freshness = runtime.system_status()["provider"]["quote_freshness"]
        assert freshness["configured_symbols"] == 3
        assert freshness["received_symbols"] == 2
        assert freshness["stale_symbols"] == 1
        assert freshness["missing_symbols"] == 1
        assert freshness["future_timestamp_symbols"] == 1
        assert freshness["state"] == "future_timestamp"
        assert freshness["threshold_seconds"] == 30.0
        assert freshness["newest_timestamp"].endswith("+08:00")
    finally:
        runtime.repository.close()


def test_runtime_status_exposes_asharehub_quote_response_quality(tmp_path):
    """Response-shape diagnostics are explicit and aggregate in system status."""

    runtime = ApplicationRuntime(
        Settings(
            data_provider="asharehub",
            asharehub_api_key="unit-test-key",
            asharehub_budget_path=str(tmp_path / "budget.json"),
            database_path=str(tmp_path / "quality.sqlite3"),
            watchlist=["600519.SH", "600036.SH", "601318.SH"],
        )
    )
    try:
        provider = runtime.provider
        provider._last_quote_response_rows = 2
        provider._last_quote_parsed_symbols = 1
        provider._last_quote_dropped_rows = 1
        provider._last_quote_invalid_rows = 1
        provider._last_quote_unexpected_rows = 0
        provider._last_quote_duplicate_rows = 0
        provider._last_quote_conflict_rows = 0
        provider._last_quote_missing_symbols = ("600036.SH", "601318.SH")
        quality = runtime.system_status()["provider"]["quote_response_quality"]
        assert quality["response_rows"] == 2
        assert quality["parsed_symbols"] == 1
        assert quality["dropped_rows"] == 1
        assert quality["invalid_rows"] == 1
        assert quality["unexpected_rows"] == 0
        assert quality["duplicate_rows"] == 0
        assert quality["conflict_rows"] == 0
        assert quality["missing_symbols"] == ["600036.SH", "601318.SH"]
        assert quality["missing_count"] == 2
        assert quality["requested_symbols"] == 3
        assert quality["coverage_ratio"] == pytest.approx(1 / 3, rel=1e-6)
    finally:
        runtime.repository.close()


@pytest.mark.parametrize("provider_name", ["asharehub", "alltick", "akshare"])
@pytest.mark.parametrize(
    "is_trading, offset_seconds, expected_issue",
    [
        (False, 30, "quote_timestamp_future"),
        (True, 30, "quote_timestamp_future"),
        (False, 5, None),  # The documented clock-skew tolerance is inclusive.
        (False, -3600, None),  # Prior-close snapshots are normal after hours.
        (True, -3600, "quote_data_stale"),
    ],
)
def test_runtime_future_quotes_degrade_health_even_outside_market_hours(
    tmp_path, monkeypatch, provider_name, is_trading, offset_seconds, expected_issue
):
    """Quote integrity is independent from exchange opening hours; no I/O."""

    now = datetime(2026, 8, 29, 0, 7, tzinfo=SHANGHAI_TZ)
    monkeypatch.setattr("quant_monitor.runtime.now_market_time", lambda: now)
    runtime = ApplicationRuntime(
        Settings(
            data_provider="mock",
            database_path=str(tmp_path / "quote-health.sqlite3"),
            watchlist=["600519.SH"],
        )
    )

    class StatusOnlyProvider:
        name = provider_name

        def current_market_status(self):
            return {"is_trading": is_trading}

    runtime.provider = StatusOnlyProvider()
    runtime.quote_stale_after_seconds = 30.0
    quote = Quote(
        symbol="600519.SH",
        timestamp=now + timedelta(seconds=offset_seconds),
        last="100",
        source=provider_name,
    )
    monkeypatch.setattr(
        runtime.monitor,
        "current_snapshot",
        lambda: {"running": True, "quotes": {quote.symbol: quote}},
    )
    try:
        runtime.repository.initialize()
        status = runtime.system_status()
        issues = status["provider"]["issues"]
        assert issues == ([expected_issue] if expected_issue else [])
        assert status["healthy"] is (expected_issue is None)
        assert status["status"] == ("degraded" if expected_issue else "ok")
        assert status["provider"]["state"] == (
            "degraded" if expected_issue else "running"
        )
        if expected_issue == "quote_timestamp_future":
            assert status["provider"]["quote_freshness"]["state"] == "future_timestamp"
            assert status["provider"]["quote_freshness"]["fresh_symbols"] == 0
    finally:
        runtime.repository.close()


def test_runtime_status_exposes_data_readiness_for_open_market_without_quotes(
    tmp_path,
):
    """A live process with an empty feed must not look data-ready to scrapers."""

    runtime = ApplicationRuntime(
        Settings(
            data_provider="mock",
            database_path=str(tmp_path / "data-readiness.sqlite3"),
            watchlist=["600519.SH"],
        )
    )

    class OpenMarketProvider:
        name = "mock"

        def current_market_status(self):
            return {"is_trading": True}

    runtime.provider = OpenMarketProvider()
    runtime.monitor.current_snapshot = lambda: {
        "running": True,
        "quotes": {},
    }
    try:
        runtime.repository.initialize()
        readiness = runtime.system_status()["data_readiness"]
        assert readiness["ready"] is False
        assert readiness["reason"] == "fresh_quotes_required"
        assert readiness["configured_symbols"] == 1
        assert readiness["received_symbols"] == 0
        assert readiness["market_is_trading"] is True
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
            assert startup["phase"] == "ready"
            assert startup["started_at"] is not None
            assert startup["duration_ms"] is not None
            assert startup["duration_ms"] >= 0
            assert startup["error_type"] is None
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
            assert runtime.system_status()["startup"]["research_ready_reason"] == "minimum_history_coverage_met"
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


def test_runtime_akshare_catalogue_refresh_does_not_block_startup(tmp_path):
    async def exercise():
        runtime = ApplicationRuntime(
            Settings(
                data_provider="akshare",
                database_path=str(tmp_path / "akshare-start.sqlite3"),
                watchlist=["600519.SH"],
                poll_interval_seconds=3600,
            )
        )
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_instruments():
            entered.set()
            await release.wait()
            return []

        async def no_quotes(symbols):
            return []

        async def no_bars(symbols, interval="1d", limit=200):
            return []

        runtime.provider.fetch_instruments = slow_instruments
        runtime.provider.fetch_quotes = no_quotes
        runtime.provider.fetch_bars_many = no_bars
        try:
            await asyncio.wait_for(runtime.start(), timeout=0.5)
            assert runtime.system_status()["startup"]["phase"] == "ready"
            await asyncio.wait_for(entered.wait(), timeout=0.5)
            assert runtime._instrument_task is not None
            assert not runtime._instrument_task.done()
        finally:
            release.set()
            await runtime.stop()

    asyncio.run(exercise())


def test_runtime_startup_failure_reports_phase_without_exception_message(
    tmp_path, caplog
):
    async def exercise():
        runtime = ApplicationRuntime(
            Settings(
                data_provider="mock",
                database_path=str(tmp_path / "startup-failure.sqlite3"),
                watchlist=["600519.SH"],
            )
        )

        def fail_initialize():
            raise RuntimeError("synthetic-sensitive-value")

        runtime.repository.initialize = fail_initialize
        with pytest.raises(RuntimeError, match="synthetic-sensitive-value"):
            await runtime.start()
        assert runtime._startup_phase == "failed"
        assert runtime._startup_error_type == "RuntimeError"
        assert runtime._startup_duration_ms is not None

    caplog.set_level("INFO")
    asyncio.run(exercise())
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "phase=database_initialize" in messages
    assert "error_type=RuntimeError" in messages
    assert "synthetic-sensitive-value" not in messages


def test_runtime_database_initialization_timeout_is_bounded(tmp_path, caplog):
    """A stale SQLite lock must fail startup with an actionable phase error."""

    async def exercise():
        runtime = ApplicationRuntime(
            Settings(
                data_provider="mock",
                database_path=str(tmp_path / "startup-timeout.sqlite3"),
                watchlist=["600519.SH"],
                startup_database_timeout_seconds=0.05,
            )
        )
        entered = threading.Event()
        release = threading.Event()

        def blocked_initialize():
            entered.set()
            release.wait(1.0)

        runtime.repository.initialize = blocked_initialize
        task = asyncio.create_task(runtime._initialize_repository_with_progress())
        try:
            assert await asyncio.to_thread(entered.wait, 1.0)
            with pytest.raises(RuntimeError, match="timed out"):
                await task
        finally:
            release.set()
            # ``to_thread`` cannot interrupt an already-running worker. Give
            # it a chance to finish before the test closes the repository.
            await asyncio.sleep(0.1)
            runtime.repository.close()

    caplog.set_level("INFO")
    asyncio.run(exercise())
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "phase=database_initialize status=timeout" in messages


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


def test_runtime_revokes_readiness_when_collector_stops(tmp_path):
    async def exercise():
        runtime = ApplicationRuntime(
            Settings(
                data_provider="mock",
                database_path=str(tmp_path / "stopped-collector.sqlite3"),
                watchlist=["600519.SH"],
                poll_interval_seconds=3600,
            )
        )
        try:
            await runtime.start()
            assert runtime.system_status()["startup"]["serving_ready"] is True
            await runtime.monitor.stop()
            status = runtime.system_status()
            assert status["startup"]["serving_ready"] is False
            assert status["startup"]["ready"] is False
            assert status["healthy"] is False
            assert status["status"] == "degraded"
            assert status["provider"]["state"] == "error"
            assert "collector_stopped" in status["provider"]["issues"]
        finally:
            await runtime.stop()
        status = runtime.system_status()
        assert status["startup"]["phase"] == "stopped"
        assert status["startup"]["serving_ready"] is False
        assert status["status"] == "stopped"
        assert status["healthy"] is False
        assert status["database"]["state"] == "disconnected"

    asyncio.run(exercise())


def test_runtime_revokes_readiness_before_shutdown_awaits(tmp_path):
    async def exercise():
        runtime = ApplicationRuntime(
            Settings(
                data_provider="mock",
                database_path=str(tmp_path / "stopping.sqlite3"),
                watchlist=["600519.SH"],
                poll_interval_seconds=3600,
            )
        )
        entered = asyncio.Event()
        release = asyncio.Event()
        close_provider = runtime.provider.close

        async def slow_close():
            entered.set()
            await release.wait()
            await close_provider()

        await runtime.start()
        runtime.provider.close = slow_close
        stopping = asyncio.create_task(runtime.stop())
        try:
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            status = runtime.system_status()
            assert status["startup"]["phase"] == "stopping"
            assert status["startup"]["serving_ready"] is False
            assert status["status"] == "stopping"
            assert status["healthy"] is False
        finally:
            release.set()
            await stopping

    asyncio.run(exercise())


def test_runtime_revokes_readiness_when_database_is_disconnected(tmp_path):
    async def exercise():
        runtime = ApplicationRuntime(
            Settings(
                data_provider="mock",
                database_path=str(tmp_path / "disconnected.sqlite3"),
                watchlist=["600519.SH"],
                poll_interval_seconds=3600,
            )
        )
        try:
            await runtime.start()
            runtime.repository.close()
            status = runtime.system_status()
            assert status["collector"]["running"] is True
            assert status["startup"]["serving_ready"] is False
            assert status["healthy"] is False
            assert status["status"] == "degraded"
            assert "database_disconnected" in status["provider"]["issues"]
        finally:
            await runtime.stop()

    asyncio.run(exercise())


def test_runtime_late_startup_failure_stops_activated_collector(tmp_path):
    async def exercise():
        runtime = ApplicationRuntime(
            Settings(
                data_provider="mock",
                database_path=str(tmp_path / "activation-failure.sqlite3"),
                watchlist=["600519.SH"],
                poll_interval_seconds=3600,
            )
        )
        complete_phase = runtime._complete_startup_phase

        def fail_activation(phase, started, **details):
            if phase == "service_activation":
                assert runtime.monitor.running is True
                raise RuntimeError("synthetic activation failure")
            complete_phase(phase, started, **details)

        runtime._complete_startup_phase = fail_activation
        with pytest.raises(RuntimeError, match="synthetic activation failure"):
            await runtime.start()
        assert runtime.monitor.running is False
        assert runtime.monitor._task is None
        assert runtime.subscription is None
        assert runtime.repository.initialized is False
        status = runtime.system_status()
        assert status["startup"]["phase"] == "failed"
        assert status["startup"]["serving_ready"] is False
        assert status["startup"]["ready_at"] is None
        assert status["startup"]["error_type"] == "RuntimeError"
        assert status["status"] == "error"
        assert status["healthy"] is False

    asyncio.run(exercise())
