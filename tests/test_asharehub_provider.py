"""Offline contract tests for the quota-aware AShareHub v2 adapter."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quant_monitor.data_sources import BarFetchTransport
from quant_monitor.data_sources.asharehub import (
    ASHAREHUB_BASE_URL,
    AShareHubAuthenticationError,
    AShareHubBudgetError,
    AShareHubDataError,
    AShareHubMarketDataProvider,
    AShareHubPermissionError,
    AShareHubRateLimitError,
    AShareHubRequestError,
    AShareHubTransportError,
    _HTTPStatusError,
)
from quant_monitor.domain import Bar, SHANGHAI_TZ
from quant_monitor.trading_calendar import DEFAULT_TRADING_CALENDAR, VersionedSessionCalendar


TEST_KEY = "unit-test-key-sentinel"


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value
        self.mono = 1000.0

    def __call__(self) -> datetime:
        return self.value

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)
        self.mono += seconds


def market_open(_):
    return {"is_trading": True}


def market_closed(_):
    return {"is_trading": False}


class Requester:
    def __init__(self, routes=None, failure=None):
        self.routes = dict(routes or {})
        self.failure = failure
        self.calls = []

    def __call__(self, method, url, headers, params, timeout):
        self.calls.append((method, url, dict(headers), dict(params), timeout))
        if self.failure is not None:
            raise self.failure
        path = url.removeprefix(ASHAREHUB_BASE_URL)
        return self.routes.get(path, [])


def quote_row(symbol="600519.SH", *, price="1500.25", trade_time="2026-08-28T10:01:02"):
    return {
        "symbol": symbol,
        "name": "贵州茅台",
        "price": price,
        "open": "1490.00",
        "high": "1502.00",
        "low": "1488.00",
        "pre_close": "1489.99",
        "volume": 123456,
        "amount": "185123456.78",
        "trade_time": trade_time,
        "pct_chg": "0.6886",
    }


def daily_row(symbol="600519.SH", trade_date="20260827"):
    return {
        "symbol": symbol,
        "trade_date": trade_date,
        "open": "1490.00",
        "high": "1502.00",
        "low": "1488.00",
        "close": "1500.25",
        "pre_close": "1489.99",
        "change": "10.26",
        "pct_chg": "0.6886",
        "vol": "1234.56",
        "amount": "185123.456",
    }


def provider(tmp_path, requester, clock=None, **kwargs):
    selected_clock = clock or Clock(datetime(2026, 8, 28, 10, 0, tzinfo=SHANGHAI_TZ))
    return AShareHubMarketDataProvider(
        TEST_KEY,
        symbols=("600519.SH", "000001.SZ"),
        requester=requester,
        budget_path=tmp_path / "budget.json",
        clock=selected_clock,
        monotonic=selected_clock.monotonic,
        market_status_fetcher=kwargs.pop("market_status_fetcher", market_open),
        **kwargs,
    )


def test_remote_base_url_requires_https(tmp_path):
    with pytest.raises(ValueError, match="HTTPS"):
        AShareHubMarketDataProvider(
            TEST_KEY,
            base_url="http://example.com",
            budget_path=tmp_path / "budget.json",
        )

    local = AShareHubMarketDataProvider(
        TEST_KEY,
        base_url="http://127.0.0.1:8765",
        budget_path=tmp_path / "local-budget.json",
    )
    asyncio.run(local.close())


def test_realtime_basket_auth_mapping_timezone_cache_and_status(tmp_path):
    async def exercise():
        clock = Clock(datetime(2026, 8, 28, 10, 0, tzinfo=SHANGHAI_TZ))
        requester = Requester(
            {"/v2/market/realtime": [quote_row(), quote_row("000001.SZ", price="10.23")]}
        )
        target = provider(tmp_path, requester, clock, quote_cache_seconds=300)
        first = await target.fetch_quotes(["600519.sh", "000001.SZ"])
        clock.advance(60)
        second = await target.fetch_quotes(["600519.SH"])
        status = target.realtime_status
        await target.close()
        return requester, first, second, status

    requester, first, second, status = asyncio.run(exercise())
    assert len(requester.calls) == 1
    method, url, headers, params, _ = requester.calls[0]
    assert method == "GET"
    assert url == "https://asharehub.com/v2/market/realtime"
    assert params == {"symbol": "600519.SH,000001.SZ"}
    assert headers["X-API-Key"] == TEST_KEY
    assert TEST_KEY not in url and TEST_KEY not in repr(status)
    assert [item.symbol for item in first] == ["600519.SH", "000001.SZ"]
    assert first[0].timestamp.isoformat() == "2026-08-28T10:01:02+08:00"
    assert first[0].last == Decimal("1500.25")
    assert first[0].bid is None and first[0].ask is None
    assert first[0].volume == 123456
    assert first[0].turnover == Decimal("185123456.78")
    assert second[0] == first[0]
    assert status["mode"] == "http_polling"
    assert status["requests_used_today"] == 1
    assert status["daily_soft_limit"] == 90
    assert status["subscribed_symbols"] == 2


def test_trade_calendar_holiday_avoids_quote_request_and_exposes_source(tmp_path):
    async def exercise():
        clock = Clock(datetime(2026, 10, 5, 10, 0, tzinfo=SHANGHAI_TZ))

        class CalendarRequester(Requester):
            def __call__(self, method, url, headers, params, timeout):
                self.calls.append((method, url, dict(headers), dict(params), timeout))
                if url.endswith("/v2/reference/trade-calendar"):
                    exchange = params["exchange"]
                    return [
                        {
                            "exchange": exchange,
                            "cal_date": "20261005",
                            "is_open": 0,
                            "pretrade_date": "20260930",
                        },
                        {
                            "exchange": exchange,
                            "cal_date": "20260930",
                            "is_open": 1,
                            "pretrade_date": "20260929",
                        },
                    ]
                raise AssertionError("holiday must not request realtime")

        requester = CalendarRequester()
        target = provider(
            tmp_path,
            requester,
            clock,
            trade_calendar_enabled=True,
            trade_calendar_cache_seconds=604800,
        )
        with pytest.raises(AShareHubDataError, match="600519.SH"):
            await target.fetch_quotes(["600519.SH"])
        status = target.realtime_status
        await target.close()
        return requester.calls, status

    calls, status = asyncio.run(exercise())
    assert len(calls) == 2
    assert {call[3]["exchange"] for call in calls} == {"SSE", "SZSE"}
    assert status["trade_calendar_source"] == "asharehub"
    assert status["trade_calendar_error_type"] is None
    assert status["requests_used_today"] == 2


def test_trade_calendar_conflicting_duplicate_rows_fail_closed(tmp_path):
    """Wire ordering must not decide whether a date is open or closed."""

    clock = Clock(datetime(2026, 10, 5, 10, 0, tzinfo=SHANGHAI_TZ))
    requester = Requester(
        {
            "/v2/reference/trade-calendar": [
                {"exchange": "SSE", "cal_date": "20261005", "is_open": 0},
                # Same date, contradictory status: reject the exchange data.
                {"exchange": "SSE", "cal_date": "20261005", "is_open": 1},
            ]
        }
    )
    target = provider(
        tmp_path,
        requester,
        clock,
        trade_calendar_enabled=True,
        trade_calendar_path=tmp_path / "calendar.json",
    )

    async def exercise():
        await target._ensure_trade_calendar()
        status = dict(target.realtime_status)
        await target.close()
        return status

    status = asyncio.run(exercise())
    assert status["trade_calendar_source"] == "weekday_fallback"
    assert status["trade_calendar_error_type"] == "AShareHubDataError"
    assert status["requests_used_today"] == 1


def test_trade_calendar_cache_conflicting_duplicate_rows_is_discarded(tmp_path):
    clock = Clock(datetime(2026, 10, 5, 10, 0, tzinfo=SHANGHAI_TZ))
    calendar_path = tmp_path / "calendar.json"
    calendar_path.write_text(
        json.dumps(
            {
                "version": 1,
                "saved_at": "2026-10-05T09:00:00+08:00",
                "rows": [
                    {"exchange": "SSE", "cal_date": "20261005", "is_open": 0},
                    {"exchange": "SSE", "cal_date": "20261005", "is_open": 1},
                ],
            }
        ),
        encoding="utf-8",
    )
    target = provider(
        tmp_path,
        Requester(failure=RuntimeError("cache should be discarded")),
        clock,
        trade_calendar_enabled=True,
        trade_calendar_path=calendar_path,
    )
    status = dict(target.realtime_status)
    assert status["trade_calendar_source"] == "weekday_fallback"
    assert status["trade_calendar_error_type"] == "TradeCalendarCacheError"
    asyncio.run(target.close())


def test_trade_calendar_cache_is_reused_without_budget_or_network(tmp_path):
    async def exercise():
        clock = Clock(datetime(2026, 10, 5, 10, 0, tzinfo=SHANGHAI_TZ))
        class CalendarRequester(Requester):
            def __call__(self, method, url, headers, params, timeout):
                self.calls.append((method, url, dict(headers), dict(params), timeout))
                exchange = params["exchange"]
                return [{
                    "exchange": exchange,
                    "cal_date": "20261005",
                    "is_open": 0,
                    "pretrade_date": "20260930",
                }]

        first_requester = CalendarRequester()
        target = provider(
            tmp_path,
            first_requester,
            clock,
            trade_calendar_enabled=True,
            trade_calendar_cache_seconds=604800,
            trade_calendar_path=tmp_path / "calendar.json",
        )
        await target._ensure_trade_calendar()
        await target.close()

        second_requester = Requester(failure=RuntimeError("must not call"))
        restored = provider(
            tmp_path,
            second_requester,
            clock,
            trade_calendar_enabled=True,
            trade_calendar_cache_seconds=604800,
            trade_calendar_path=tmp_path / "calendar.json",
        )
        source = restored.realtime_status["trade_calendar_source"]
        await restored._ensure_trade_calendar()
        status = restored.realtime_status
        await restored.close()
        return source, status, first_requester.calls, second_requester.calls

    source, status, first_calls, second_calls = asyncio.run(exercise())
    assert source == "asharehub_cache"
    assert status["trade_calendar_source"] == "asharehub_cache"
    assert len(first_calls) == 2
    assert second_calls == []


def test_loaded_trade_calendar_exposes_immutable_versioned_view_without_network(tmp_path):
    """The finite exchange cache is the same calendar contract research uses."""

    class CalendarRequester(Requester):
        def __call__(self, method, url, headers, params, timeout):
            self.calls.append((method, url, dict(headers), dict(params), timeout))
            exchange = params["exchange"]
            return [
                {"exchange": exchange, "cal_date": "20261002", "is_open": 1},
                {"exchange": exchange, "cal_date": "20261003", "is_open": 1},
                {"exchange": exchange, "cal_date": "20261005", "is_open": 0},
                {"exchange": exchange, "cal_date": "20261006", "is_open": 1},
            ]

    async def exercise():
        target = provider(
            tmp_path,
            CalendarRequester(),
            Clock(datetime(2026, 10, 5, 10, 0, tzinfo=SHANGHAI_TZ)),
            trade_calendar_enabled=True,
        )
        await target._ensure_trade_calendar()
        calendar = target.get_trading_calendar()
        await target.close()
        return calendar

    calendar = asyncio.run(exercise())
    assert isinstance(calendar, VersionedSessionCalendar)
    assert calendar.metadata.identifier == "asharehub_trade_calendar"
    assert calendar.metadata.version == "asharehub-trade-calendar-v1"
    assert calendar.is_session(datetime(2026, 10, 2).date())
    assert calendar.is_session(datetime(2026, 10, 3).date())
    assert not calendar.is_session(datetime(2026, 10, 5).date())
    assert len(calendar.fingerprint) == 64


def test_partial_exchange_calendar_falls_back_for_mixed_research_scope(tmp_path):
    """A mixed SSE/SZSE quality scope cannot use one-sided vendor evidence."""

    target = provider(
        tmp_path,
        Requester(),
        Clock(datetime(2026, 10, 5, 10, 0, tzinfo=SHANGHAI_TZ)),
        trade_calendar_enabled=True,
    )
    target._trade_calendar_loaded_at = target._monotonic()
    target._trade_calendar["SSE"] = {
        datetime(2026, 10, 2).date(): (True, None),
    }
    # SZSE intentionally has no rows: do not promote the SSE-only calendar.
    assert target.get_trading_calendar() is DEFAULT_TRADING_CALENDAR
    asyncio.run(target.close())


def test_expired_trade_calendar_cache_is_reported_and_not_used(tmp_path):
    clock = Clock(datetime(2026, 10, 5, 10, 0, tzinfo=SHANGHAI_TZ))
    calendar_path = tmp_path / "calendar.json"

    async def seed():
        target = provider(
            tmp_path,
            Requester({
                "/v2/reference/trade-calendar": [
                    {"exchange": "SSE", "cal_date": "20261005", "is_open": 0},
                    {"exchange": "SZSE", "cal_date": "20261005", "is_open": 0},
                ]
            }),
            clock,
            trade_calendar_enabled=True,
            trade_calendar_cache_seconds=60,
            trade_calendar_path=calendar_path,
        )
        await target._ensure_trade_calendar()
        await target.close()

    asyncio.run(seed())
    payload = json.loads(calendar_path.read_text(encoding="utf-8"))
    payload["saved_at"] = "2026-10-04T09:00:00+08:00"
    calendar_path.write_text(json.dumps(payload), encoding="utf-8")

    restored = provider(
        tmp_path,
        Requester(failure=RuntimeError("expired cache must not be trusted")),
        clock,
        trade_calendar_enabled=True,
        trade_calendar_cache_seconds=60,
        trade_calendar_path=calendar_path,
    )
    status = dict(restored.realtime_status)
    assert status["trade_calendar_source"] == "weekday_fallback"
    assert status["trade_calendar_error_type"] == "TradeCalendarCacheExpired"
    asyncio.run(restored.close())


def test_trade_calendar_respects_callers_reserved_budget(tmp_path):
    async def exercise():
        clock = Clock(datetime(2026, 10, 5, 10, 0, tzinfo=SHANGHAI_TZ))
        requester = Requester(failure=RuntimeError("calendar must be skipped"))
        target = provider(
            tmp_path,
            requester,
            clock,
            daily_soft_limit=3,
            trade_calendar_enabled=True,
        )
        await target._ensure_trade_calendar(reserve_requests=1)
        status = target.realtime_status
        await target.close()
        return status, requester.calls

    status, calls = asyncio.run(exercise())
    assert calls == []
    assert status["trade_calendar_source"] == "weekday_fallback_budget_reserved"
    assert status["trade_calendar_error_type"] == "TradeCalendarBudgetReserved"


def test_current_market_status_applies_cached_holiday_without_network(tmp_path):
    async def exercise():
        clock = Clock(datetime(2026, 10, 5, 10, 0, tzinfo=SHANGHAI_TZ))

        class CalendarRequester(Requester):
            def __call__(self, method, url, headers, params, timeout):
                self.calls.append((method, url, dict(headers), dict(params), timeout))
                return [{
                    "exchange": params["exchange"],
                    "cal_date": "20261005",
                    "is_open": 0,
                    "pretrade_date": "20260930",
                }]

        requester = CalendarRequester()
        target = provider(
            tmp_path,
            requester,
            clock,
            trade_calendar_enabled=True,
        )
        await target._ensure_trade_calendar()
        status = target.current_market_status()
        await target.close()
        return status

    status = asyncio.run(exercise())
    assert status["is_trading"] is False
    assert status["is_trading_day"] is False
    assert status["label"] == "节假日休市"
    assert status["calendar"] == "asharehub"
    assert status["calendar_source"] == "asharehub"
    assert status["calendar_version"] == "asharehub-trade-calendar-v1"
    assert status["calendar_verified"] is False
    assert status["calendar_exchange"] == "SSE"
    assert status["calendar_lookup"] == "closed"


def test_trade_calendar_makeup_saturday_overrides_weekday_fallback(tmp_path):
    clock = Clock(datetime(2026, 10, 3, 10, 0, tzinfo=SHANGHAI_TZ))  # Saturday
    requester = Requester(
        {
            "/v2/reference/trade-calendar": [
                {"exchange": "SSE", "cal_date": "20261003", "is_open": 1},
                {"exchange": "SZSE", "cal_date": "20261003", "is_open": 1},
            ],
            "/v2/market/realtime": [quote_row(trade_time="2026-10-03T10:00:00")],
        }
    )

    async def exercise():
        target = provider(
            tmp_path,
            requester,
            clock,
            trade_calendar_enabled=True,
            trade_calendar_path=tmp_path / "calendar.json",
        )
        await target._ensure_trade_calendar()
        status = target.current_market_status()
        rows = await target.fetch_quotes(["600519.SH"])
        await target.close()
        return status, rows

    status, rows = asyncio.run(exercise())
    assert status["is_trading_day"] is True
    assert status["is_trading"] is True
    assert status["phase"] == "morning"
    assert status["calendar"] == "asharehub"
    assert status["calendar_source"] == "asharehub"
    assert status["calendar_version"] == "asharehub-trade-calendar-v1"
    assert status["calendar_verified"] is False
    assert status["calendar_exchange"] == "SSE"
    assert status["calendar_lookup"] == "open"
    assert [item.symbol for item in rows] == ["600519.SH"]
    assert sum(1 for call in requester.calls if call[1].endswith("/v2/market/realtime")) == 1


def test_trade_calendar_outside_cached_coverage_is_disclosed_as_unknown(tmp_path):
    """A bounded cache must not look like an exchange-confirmed weekday."""

    clock = Clock(datetime(2026, 10, 5, 10, 0, tzinfo=SHANGHAI_TZ))
    requester = Requester(
        {
            "/v2/reference/trade-calendar": [
                {"exchange": "SSE", "cal_date": "20261001", "is_open": 0},
                {"exchange": "SZSE", "cal_date": "20261001", "is_open": 0},
            ]
        }
    )

    async def exercise():
        target = provider(
            tmp_path,
            requester,
            clock,
            trade_calendar_enabled=True,
            trade_calendar_path=tmp_path / "calendar.json",
        )
        await target._ensure_trade_calendar()
        status = target.current_market_status()
        await target.close()
        return status

    status = asyncio.run(exercise())
    # 2026-10-05 is a weekday, but the fixture does not cover it.  The
    # provider must disclose the unknown lookup instead of claiming the
    # AShareHub snapshot answered the question.
    assert status["calendar"] == "weekday_fallback"
    assert status["calendar_lookup"] == "unknown"
    assert "no row" in status["calendar_notice"]


def test_quotes_strict_and_relaxed_missing_semantics_share_cached_attempt(tmp_path):
    async def exercise():
        requester = Requester({"/v2/market/realtime": [quote_row()]})
        target = provider(tmp_path, requester)
        with pytest.raises(AShareHubDataError, match="000001.SZ"):
            await target.fetch_quotes(["600519.SH", "000001.SZ"])
        relaxed = await target.fetch_quotes_relaxed(["600519.SH", "000001.SZ"])
        await target.close()
        return requester.calls, relaxed

    calls, relaxed = asyncio.run(exercise())
    assert len(calls) == 1
    assert [item.symbol for item in relaxed] == ["600519.SH"]


def test_invalid_realtime_rows_are_not_fabricated(tmp_path):
    async def exercise():
        requester = Requester(
            {
                "/v2/market/realtime": [
                    quote_row(price="0"),
                    quote_row("000001.SZ", trade_time="not-a-time"),
                ]
            }
        )
        target = provider(tmp_path, requester)
        with pytest.raises(AShareHubDataError, match="600519.SH, 000001.SZ"):
            await target.fetch_quotes(["600519.SH", "000001.SZ"])
        status = dict(target.realtime_status)
        await target.close()
        return status

    status = asyncio.run(exercise())
    assert status["last_quote_response_rows"] == 2
    assert status["last_quote_parsed_symbols"] == 0
    assert status["last_quote_dropped_rows"] == 2
    assert status["last_quote_missing_symbols"] == ["600519.SH", "000001.SZ"]


def test_realtime_status_exposes_partial_payload_quality(tmp_path):
    async def exercise():
        requester = Requester(
            {"/v2/market/realtime": [quote_row(), quote_row("000001.SZ", price="bad")]}
        )
        target = provider(tmp_path, requester)
        rows = await target.fetch_quotes_relaxed(["600519.SH", "000001.SZ"])
        status = dict(target.realtime_status)
        await target.close()
        return rows, status

    rows, status = asyncio.run(exercise())
    assert [item.symbol for item in rows] == ["600519.SH"]
    assert status["last_quote_response_rows"] == 2
    assert status["last_quote_parsed_symbols"] == 1
    assert status["last_quote_dropped_rows"] == 1
    assert status["last_quote_missing_symbols"] == ["000001.SZ"]


def test_realtime_payload_quality_counters_classify_invalid_unexpected_and_duplicate_rows(tmp_path):
    async def exercise():
        rows = [
            quote_row(),
            # A second observation for the same symbol is valid but duplicate;
            # the newest timestamp wins deterministically.
            quote_row(price="1501.00", trade_time="2026-08-28T10:01:03"),
            quote_row("000001.SZ", price="bad"),
            quote_row("600000.SH"),
            {"symbol": "not-a-symbol", "price": "10"},
        ]
        target = provider(tmp_path, Requester({"/v2/market/realtime": rows}))
        result = await target.fetch_quotes_relaxed(["600519.SH", "000001.SZ"])
        status = dict(target.realtime_status)
        await target.close()
        return result, status

    result, status = asyncio.run(exercise())
    assert [item.symbol for item in result] == ["600519.SH"]
    assert status["last_quote_response_rows"] == 5
    assert status["last_quote_parsed_symbols"] == 1
    assert status["last_quote_dropped_rows"] == 4
    assert status["last_quote_invalid_rows"] == 2
    assert status["last_quote_unexpected_rows"] == 1
    assert status["last_quote_duplicate_rows"] == 1


def test_realtime_conflicting_same_timestamp_rows_fail_closed(tmp_path):
    """Wire order must not choose between ambiguous same-time revisions."""

    async def exercise():
        rows = [
            quote_row(price="1500.25", trade_time="2026-08-28T10:01:02"),
            quote_row(price="1501.00", trade_time="2026-08-28T10:01:02"),
        ]
        target = provider(tmp_path, Requester({"/v2/market/realtime": rows}))
        with pytest.raises(
            AShareHubDataError,
            match="conflicting duplicate rows.*600519.SH",
        ):
            await target.fetch_quotes(["600519.SH"])
        status = dict(target.realtime_status)
        await target.close()
        return status

    status = asyncio.run(exercise())
    assert status["last_quote_conflict_rows"] == 1
    assert status["last_quote_parsed_symbols"] is None
    assert status["last_quote_missing_symbols"] == []


def test_realtime_older_response_cannot_overwrite_cached_quote(tmp_path):
    """An out-of-order snapshot preserves the latest usable cache value."""

    class SequencedRequester:
        def __init__(self):
            self.calls = []
            self.responses = [
                [quote_row(price="1500.25", trade_time="2026-08-28T10:01:02")],
                [quote_row(price="1499.00", trade_time="2026-08-28T09:59:00")],
            ]

        def __call__(self, method, url, headers, params, timeout):
            self.calls.append((method, url, dict(headers), dict(params), timeout))
            return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]

    async def exercise():
        requester = SequencedRequester()
        target = provider(tmp_path, requester, quote_cache_seconds=0)
        first = await target.fetch_quotes(["600519.SH"])
        with pytest.raises(AShareHubDataError, match="600519.SH"):
            await target.fetch_quotes(["600519.SH"])
        status = dict(target.realtime_status)
        cached = target._quotes["600519.SH"]
        await target.close()
        return first, cached, status, requester.calls

    first, cached, status, calls = asyncio.run(exercise())
    assert first[0].last == Decimal("1500.25")
    assert cached.last == Decimal("1500.25")
    assert cached.timestamp.isoformat() == "2026-08-28T10:01:02+08:00"
    assert status["last_quote_stale_rows"] == 1
    assert status["last_quote_missing_symbols"] == ["600519.SH"]
    assert len(calls) == 2


def test_quote_cache_singleflight_for_concurrent_callers(tmp_path):
    class SlowRequester(Requester):
        def __call__(self, *args):
            import time

            time.sleep(0.03)
            return super().__call__(*args)

    async def exercise():
        requester = SlowRequester({"/v2/market/realtime": [quote_row()]})
        target = provider(tmp_path, requester)
        batches = await asyncio.gather(
            target.fetch_quotes(["600519.SH"]),
            target.fetch_quotes(["600519.SH"]),
        )
        await target.close()
        return requester.calls, batches

    calls, batches = asyncio.run(exercise())
    assert len(calls) == 1
    assert batches[0] == batches[1]


def test_budget_lock_wait_does_not_freeze_asyncio_loop(tmp_path):
    """A contended local quota lock must leave health/heartbeat tasks runnable."""

    async def exercise():
        requester = Requester({"/v2/market/realtime": [quote_row()]})
        target = provider(tmp_path, requester)
        reservation_started = threading.Event()
        released_by_heartbeat = threading.Event()
        release = threading.Event()

        def blocked_reservation(*, reserve_requests=0):
            del reserve_requests
            reservation_started.set()
            # Simulate another process holding the persistent budget lock.
            # The heartbeat task below must be able to release us; a timer is
            # only a safety valve so a regression cannot hang the test suite.
            release.wait(1.0)

        target._consume_budget = blocked_reservation

        async def heartbeat():
            await asyncio.to_thread(reservation_started.wait, 0.5)
            released_by_heartbeat.set()
            release.set()

        heartbeat_task = asyncio.create_task(heartbeat())
        fetch_task = asyncio.create_task(target.fetch_quotes(["600519.SH"]))
        try:
            await asyncio.wait_for(fetch_task, timeout=1.5)
            await asyncio.wait_for(heartbeat_task, timeout=0.5)
            return released_by_heartbeat.is_set()
        finally:
            release.set()
            await asyncio.gather(fetch_task, heartbeat_task, return_exceptions=True)
            await target.close()

    assert asyncio.run(exercise()) is True


def test_closed_market_performs_at_most_one_quote_attempt_even_on_failure(tmp_path):
    async def exercise():
        requester = Requester(failure=RuntimeError("header could contain secret"))
        target = provider(tmp_path, requester, market_status_fetcher=market_closed)
        with pytest.raises(AShareHubTransportError, match="transport failed") as first:
            await target.fetch_quotes(["600519.SH"])
        with pytest.raises(AShareHubDataError, match="600519.SH"):
            await target.fetch_quotes(["600519.SH"])
        await target.close()
        return requester.calls, str(first.value)

    calls, message = asyncio.run(exercise())
    assert len(calls) == 1
    assert TEST_KEY not in message


def test_closed_market_reuses_stale_cache_after_ttl(tmp_path):
    async def exercise():
        clock = Clock(datetime(2026, 8, 28, 15, 30, tzinfo=SHANGHAI_TZ))
        requester = Requester({"/v2/market/realtime": [quote_row()]})
        target = provider(
            tmp_path,
            requester,
            clock,
            quote_cache_seconds=1,
            market_status_fetcher=market_closed,
        )
        first = await target.fetch_quotes(["600519.SH"])
        clock.advance(3600)
        second = await target.fetch_quotes(["600519.SH"])
        await target.close()
        return requester.calls, first, second

    calls, first, second = asyncio.run(exercise())
    assert len(calls) == 1
    assert first == second


def test_realtime_rejects_more_than_official_200_symbol_basket(tmp_path):
    async def exercise():
        requester = Requester()
        target = provider(tmp_path, requester)
        symbols = ["{:06d}.SH".format(index) for index in range(201)]
        with pytest.raises(ValueError, match="at most 200"):
            await target.fetch_quotes(symbols)
        await target.close()
        return requester.calls

    assert asyncio.run(exercise()) == []


def test_daily_bars_units_order_dates_filters_and_cache(tmp_path):
    async def exercise():
        clock = Clock(datetime(2026, 8, 28, 18, 0, tzinfo=SHANGHAI_TZ))
        requester = Requester(
            {
                "/v2/market/daily": [
                    daily_row(trade_date="20260827"),
                    daily_row(trade_date="20260826"),
                ]
            }
        )
        target = provider(tmp_path, requester, clock)
        start = datetime(2026, 8, 20, 9, 30, tzinfo=SHANGHAI_TZ)
        end = datetime(2026, 8, 28, 15, 0, tzinfo=SHANGHAI_TZ)
        first = await target.fetch_bars(
            "600519.sh", interval="1d", start=start, end=end, limit=10
        )
        second = await target.fetch_bars(
            "600519.SH", interval="1d", start=start, end=end, limit=1
        )
        await target.close()
        return requester.calls, first, second

    calls, first, second = asyncio.run(exercise())
    assert len(calls) == 1
    assert calls[0][3] == {
        "symbol": "600519.SH",
        "start_date": "20260820",
        "end_date": "20260828",
    }
    assert [item.timestamp.strftime("%Y%m%d") for item in first] == [
        "20260826",
        "20260827",
    ]
    assert first[0].timestamp.utcoffset() == timedelta(hours=8)
    assert first[0].timestamp.hour == 15
    assert first[0].timestamp.minute == 30
    assert first[0].volume == 123456
    assert first[0].turnover == Decimal("185123456.000")
    assert first[0].source == "asharehub"
    assert second == [first[-1]]


def test_daily_bars_reject_conflicting_duplicate_session_rows(tmp_path):
    """A duplicate session with different OHLCV must fail closed.

    Silently keeping the last duplicate would make the persisted observation
    depend on vendor row ordering and hide a data-quality incident. Identical
    duplicate rows remain harmlessly deduplicated by the adapter.
    """

    async def exercise():
        clock = Clock(datetime(2026, 8, 28, 16, 0, tzinfo=SHANGHAI_TZ))
        duplicate = daily_row(trade_date="20260827")
        conflicting = dict(duplicate, close="1499.99")
        requester = Requester({"/v2/market/daily": [duplicate, conflicting]})
        target = provider(tmp_path, requester, clock)
        with pytest.raises(
            AShareHubDataError,
            match="conflicting duplicate rows.*2026-08-27",
        ):
            await target.fetch_bars_with_lineage(
                "600519.SH",
                interval="1d",
                start=datetime(2026, 8, 27, tzinfo=SHANGHAI_TZ),
                end=datetime(2026, 8, 27, tzinfo=SHANGHAI_TZ),
                limit=10,
            )
        # Failed parsing must not leave a cache entry that can be served as if
        # the ambiguous response had been accepted.
        assert target._bar_cache == {}
        await target.close()

    asyncio.run(exercise())


def _seeded_daily_rows(count, latest_date):
    return [
        Bar(
            symbol="600519.SH",
            interval="1d",
            timestamp=datetime.combine(
                latest_date - timedelta(days=count - index - 1),
                datetime.min.time(),
                tzinfo=SHANGHAI_TZ,
            ),
            open="10",
            high="11",
            low="9",
            close="10.5",
            volume=100,
            source="asharehub",
        )
        for index in range(count)
    ]


def test_seeded_full_window_refreshes_when_latest_expected_daily_is_missing(tmp_path):
    async def exercise():
        clock = Clock(datetime(2026, 8, 28, 16, 0, tzinfo=SHANGHAI_TZ))
        requester = Requester(
            {"/v2/market/daily": [daily_row(trade_date="20260828")]}
        )
        target = provider(tmp_path, requester, clock)
        target.seed_bars(
            _seeded_daily_rows(240, datetime(2026, 8, 27).date())
        )
        first = await target.fetch_bars("600519.SH", interval="1d", limit=240)
        second = await target.fetch_bars("600519.SH", interval="1d", limit=240)
        await target.close()
        return requester.calls, first, second

    calls, first, second = asyncio.run(exercise())
    assert len(calls) == 1
    assert calls[0][3]["start_date"] == "20260827"
    assert first[-1].timestamp.strftime("%Y%m%d") == "20260828"
    assert len(first) == 240
    assert second == first


def test_seeded_stale_window_empty_increment_is_merged_and_cached(tmp_path):
    async def exercise():
        clock = Clock(datetime(2026, 8, 28, 16, 0, tzinfo=SHANGHAI_TZ))
        requester = Requester({"/v2/market/daily": []})
        target = provider(tmp_path, requester, clock)
        target.seed_bars(
            _seeded_daily_rows(240, datetime(2026, 8, 27).date())
        )
        first = await target.fetch_bars("600519.SH", interval="1d", limit=240)
        second = await target.fetch_bars("600519.SH", interval="1d", limit=240)
        await target.close()
        return requester.calls, first, second

    calls, first, second = asyncio.run(exercise())
    assert len(calls) == 1
    assert len(first) == 240
    assert first[-1].timestamp.strftime("%Y%m%d") == "20260827"
    assert second == first


def test_empty_increment_is_refresh_cached_but_not_research_current(tmp_path):
    async def exercise():
        clock = Clock(datetime(2026, 8, 28, 16, 0, tzinfo=SHANGHAI_TZ))
        requester = Requester({"/v2/market/daily": []})
        target = provider(tmp_path, requester, clock)
        target.seed_bars(
            _seeded_daily_rows(240, datetime(2026, 8, 27).date())
        )
        before = target.has_current_bar_data("600519.SH", "1d", 130)
        await target.fetch_bars("600519.SH", interval="1d", limit=240)
        refresh_checked = target.has_current_bar_history("600519.SH", "1d", 240)
        research_current = target.has_current_bar_data("600519.SH", "1d", 130)
        await target.close()
        return before, refresh_checked, research_current, requester.calls

    before, refresh_checked, research_current, calls = asyncio.run(exercise())
    assert before is False
    assert refresh_checked is True
    assert research_current is False
    assert len(calls) == 1


def test_stale_empty_increment_negative_cache_rechecks_after_fifteen_minutes(tmp_path):
    async def exercise():
        clock = Clock(datetime(2026, 8, 28, 16, 0, tzinfo=SHANGHAI_TZ))
        requester = Requester({"/v2/market/daily": []})
        target = provider(tmp_path, requester, clock, bars_cache_seconds=86400)
        target.seed_bars(
            _seeded_daily_rows(240, datetime(2026, 8, 27).date())
        )
        await target.fetch_bars("600519.SH", interval="1d", limit=240)
        await target.fetch_bars("600519.SH", interval="1d", limit=240)
        clock.advance(901)
        requester.routes["/v2/market/daily"] = [
            daily_row(trade_date="20260828")
        ]
        refreshed = await target.fetch_bars(
            "600519.SH", interval="1d", limit=240
        )
        await target.close()
        return requester.calls, refreshed

    calls, refreshed = asyncio.run(exercise())
    assert len(calls) == 2
    assert refreshed[-1].timestamp.strftime("%Y%m%d") == "20260828"


def test_cached_preclose_empty_check_refreshes_again_after_expected_date_advances(
    tmp_path,
):
    async def exercise():
        clock = Clock(datetime(2026, 8, 28, 15, 29, tzinfo=SHANGHAI_TZ))
        requester = Requester({"/v2/market/daily": []})
        target = provider(tmp_path, requester, clock)
        target.seed_bars(
            _seeded_daily_rows(239, datetime(2026, 8, 27).date())
        )
        first = await target.fetch_bars("600519.SH", interval="1d", limit=240)
        clock.advance(61)
        requester.routes["/v2/market/daily"] = [daily_row(trade_date="20260828")]
        second = await target.fetch_bars("600519.SH", interval="1d", limit=240)
        await target.close()
        return requester.calls, first, second

    calls, first, second = asyncio.run(exercise())
    assert len(calls) == 2
    assert first[-1].timestamp.strftime("%Y%m%d") == "20260827"
    assert second[-1].timestamp.strftime("%Y%m%d") == "20260828"


def test_daily_response_does_not_accept_current_partial_bar_before_close(tmp_path):
    """A daily row is unavailable until its conservative 15:30 watermark."""

    async def exercise():
        clock = Clock(datetime(2026, 8, 28, 10, 0, tzinfo=SHANGHAI_TZ))
        requester = Requester(
            {"/v2/market/daily": [daily_row(trade_date="20260828")]}
        )
        target = provider(tmp_path, requester, clock)
        with pytest.raises(AShareHubDataError, match="no usable rows"):
            await target.fetch_bars("600519.SH", interval="1d", limit=1)
        status = dict(target.realtime_status)
        await target.close()
        return status

    status = asyncio.run(exercise())
    assert status["last_bar_future_rows"] == 1


def test_daily_response_accepts_current_bar_at_after_close_watermark(tmp_path):
    async def exercise():
        clock = Clock(datetime(2026, 8, 28, 15, 30, tzinfo=SHANGHAI_TZ))
        requester = Requester(
            {"/v2/market/daily": [daily_row(trade_date="20260828")]}
        )
        target = provider(tmp_path, requester, clock)
        bars = await target.fetch_bars("600519.SH", interval="1d", limit=1)
        status = dict(target.realtime_status)
        await target.close()
        return bars, status

    bars, status = asyncio.run(exercise())
    assert bars[-1].timestamp.isoformat() == "2026-08-28T15:30:00+08:00"
    assert status["last_bar_future_rows"] == 0


@pytest.mark.parametrize(
    ("now_text", "latest_text"),
    [
        ("2026-08-28T15:29:59", "20260827"),
        ("2026-08-28T15:30:00", "20260828"),
        ("2026-08-29T10:00:00", "20260828"),
        ("2026-08-31T10:00:00", "20260828"),
    ],
)
def test_seeded_expected_daily_weekday_fallback_avoids_network_when_covered(
    tmp_path, now_text, latest_text
):
    async def exercise():
        clock = Clock(datetime.fromisoformat(now_text).replace(tzinfo=SHANGHAI_TZ))
        requester = Requester(failure=RuntimeError("must not call"))
        target = provider(tmp_path, requester, clock)
        target.seed_bars(
            _seeded_daily_rows(
                240, datetime.strptime(latest_text, "%Y%m%d").date()
            )
        )
        rows = await target.fetch_bars("600519.SH", interval="1d", limit=240)
        await target.close()
        return rows, requester.calls

    rows, calls = asyncio.run(exercise())
    assert rows[-1].timestamp.strftime("%Y%m%d") == latest_text
    assert calls == []


def test_explicit_historical_end_uses_seeded_range_without_current_refresh(tmp_path):
    async def exercise():
        clock = Clock(datetime(2026, 8, 28, 16, 0, tzinfo=SHANGHAI_TZ))
        requester = Requester(failure=RuntimeError("must not call"))
        target = provider(tmp_path, requester, clock)
        target.seed_bars(
            _seeded_daily_rows(240, datetime(2026, 8, 20).date())
        )
        rows = await target.fetch_bars(
            "600519.SH",
            interval="1d",
            end=datetime(2026, 8, 20, 23, 0, tzinfo=SHANGHAI_TZ),
            limit=240,
        )
        await target.close()
        return rows, requester.calls

    rows, calls = asyncio.run(exercise())
    assert rows[-1].timestamp.strftime("%Y%m%d") == "20260820"
    assert calls == []


def test_daily_only_interval_validation_does_not_spend_budget(tmp_path):
    async def exercise():
        requester = Requester()
        target = provider(tmp_path, requester)
        with pytest.raises(ValueError, match="only provides 1d"):
            await target.fetch_bars("600519.SH", interval="1m")
        with pytest.raises(ValueError, match="limit must be positive"):
            await target.fetch_bars("600519.SH", interval="1d", limit=0)
        await target.close()
        return requester.calls

    assert asyncio.run(exercise()) == []


def test_daily_empty_is_valid_but_nonempty_unusable_is_error(tmp_path):
    async def exercise():
        empty_requester = Requester({"/v2/market/daily": []})
        target = provider(tmp_path / "empty", empty_requester)
        empty = await target.fetch_bars("600519.SH", interval="1d")
        await target.close()

        bad_requester = Requester({"/v2/market/daily": [{"symbol": "600519.SH"}]})
        bad = provider(tmp_path / "bad", bad_requester)
        with pytest.raises(AShareHubDataError, match="no usable rows"):
            await bad.fetch_bars("600519.SH", interval="1d")
        await bad.close()
        return empty

    assert asyncio.run(exercise()) == []


def test_static_stock_catalogue_mapping_and_cache(tmp_path):
    async def exercise():
        requester = Requester(
            {
                "/v2/reference/stocks": [
                    {
                        "symbol": "600519.SH",
                        "name": "贵州茅台",
                        "area": "贵州",
                        "industry": "白酒",
                        "fullname": "贵州茅台酒股份有限公司",
                        "market": "主板",
                        "exchange": "SSE",
                        "curr_type": "CNY",
                        "list_status": "L",
                        "list_date": "20010827",
                        "delist_date": None,
                        "is_hs": "H",
                    },
                    {
                        "symbol": "000001.SZ",
                        "name": "平安银行",
                        "exchange": "SZSE",
                        "list_status": "D",
                        "list_date": "19910403",
                    },
                ]
            }
        )
        target = provider(tmp_path, requester)
        first = await target.fetch_instruments()
        second = await target.fetch_instruments()
        await target.close()
        return requester.calls, first, second

    calls, first, second = asyncio.run(exercise())
    assert len(calls) == 1
    assert first == second
    assert first[0].exchange.value == "SH"
    assert first[0].name == "贵州茅台"
    assert first[0].listed_at.isoformat() == "2001-08-27"
    assert first[0].active is True
    assert first[0].metadata["industry"] == "白酒"
    assert first[1].active is False


def test_seeded_stock_catalogue_avoids_restart_request(tmp_path):
    async def exercise():
        requester = Requester(failure=RuntimeError("must not call"))
        target = provider(tmp_path, requester)
        instrument = AShareHubMarketDataProvider._instrument_from_row(
            {
                "symbol": "600519.SH",
                "name": "贵州茅台",
                "list_status": "L",
            }
        )
        assert instrument is not None
        seeded = target.seed_instruments([instrument])
        rows = await target.fetch_instruments()
        await target.close()
        return seeded, rows, requester.calls

    seeded, rows, calls = asyncio.run(exercise())
    assert seeded == 1
    assert rows[0].symbol == "600519.SH"
    assert calls == []


def test_persistent_soft_budget_and_daily_reset(tmp_path):
    async def exercise():
        clock = Clock(datetime(2026, 8, 28, 10, 0, tzinfo=SHANGHAI_TZ))
        requester = Requester({"/v2/market/realtime": [quote_row()]})
        first = provider(tmp_path, requester, clock, daily_soft_limit=2, quote_cache_seconds=0)
        await first.fetch_quotes(["600519.SH"])
        clock.advance(1)
        await first.fetch_quotes(["600519.SH"])
        with pytest.raises(AShareHubBudgetError, match="soft limit"):
            await first.fetch_quotes(["600519.SH"])
        await first.close()

        restored = provider(tmp_path, requester, clock, daily_soft_limit=2)
        with pytest.raises(AShareHubBudgetError):
            await restored.fetch_bars("600519.SH", interval="1d")
        status_before = restored.realtime_status
        clock.advance(24 * 60 * 60)
        rows = await restored.fetch_quotes(["600519.SH"])
        status_after = restored.realtime_status
        await restored.close()
        return requester.calls, rows, status_before, status_after

    calls, rows, before, after = asyncio.run(exercise())
    assert len(calls) == 3
    assert rows[0].symbol == "600519.SH"
    assert before["requests_used_today"] == 2
    assert before["state"] == "quota_exhausted"
    assert after["requests_used_today"] == 1


def test_budget_consumption_is_serialized_across_provider_instances(tmp_path):
    clock = Clock(datetime(2026, 8, 28, 10, 0, tzinfo=SHANGHAI_TZ))
    first = provider(tmp_path, Requester(), clock, daily_soft_limit=1)
    second = provider(tmp_path, Requester(), clock, daily_soft_limit=1)

    def consume(target):
        try:
            target._consume_budget()
            return "ok"
        except AShareHubBudgetError:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(consume, (first, second)))

    payload = json.loads((tmp_path / "budget.json").read_text(encoding="utf-8"))
    assert sorted(results) == ["blocked", "ok"]
    assert payload["used"] == 1


def test_budget_reserve_is_enforced_inside_cross_process_lock(tmp_path):
    clock = Clock(datetime(2026, 8, 28, 10, 0, tzinfo=SHANGHAI_TZ))
    first = provider(tmp_path, Requester(), clock, daily_soft_limit=3)
    second = provider(tmp_path, Requester(), clock, daily_soft_limit=3)

    first._consume_budget()
    first._consume_budget()
    with pytest.raises(AShareHubBudgetError, match="reserved") as error:
        second._consume_budget(reserve_requests=1)
    assert error.value.reserved is True

    payload = json.loads((tmp_path / "budget.json").read_text(encoding="utf-8"))
    assert payload["used"] == 2


def test_budget_is_scoped_to_a_hashed_credential_identity(tmp_path):
    first = provider(tmp_path, Requester(), daily_soft_limit=2)
    first._consume_budget()

    second = AShareHubMarketDataProvider(
        "different-unit-test-key",
        symbols=("600519.SH",),
        requester=Requester(),
        budget_path=tmp_path / "budget.json",
        daily_soft_limit=2,
        market_status_fetcher=market_open,
    )
    second._consume_budget()

    assert second.realtime_status["requests_used_today"] == 1
    text = (tmp_path / "budget.json").read_text(encoding="utf-8")
    assert "different-unit-test-key" not in text


def test_configured_quote_scope_and_seeded_quotes_preserve_real_age(tmp_path):
    async def exercise():
        clock = Clock(datetime(2026, 8, 28, 10, 0, tzinfo=SHANGHAI_TZ))
        requester = Requester({"/v2/market/realtime": [quote_row(price="1501.00")]})
        target = provider(tmp_path, requester, clock, quote_cache_seconds=300)
        stale = AShareHubMarketDataProvider._quote_from_row(
            quote_row(price="1499.00", trade_time="2026-08-28T09:50:00"),
            ("600519.SH",),
        )
        assert stale is not None
        foreign = stale.__class__(
            symbol=stale.symbol,
            timestamp=stale.timestamp,
            last=stale.last,
            source="alltick",
        )
        seeded = target.seed_quotes([stale, foreign])
        rows = await target.fetch_quotes(["600519.SH"])
        scope = target.quote_universe_scope
        configured = target.configured_symbols
        await target.close()
        return seeded, rows, scope, configured, requester.calls

    seeded, rows, scope, configured, calls = asyncio.run(exercise())
    assert seeded == 1
    assert rows[0].last == Decimal("1501.00")
    assert scope == "configured_symbols"
    assert configured == ("600519.SH", "000001.SZ")
    assert len(calls) == 1


def test_provider_explicitly_supports_daily_bars_only(tmp_path):
    target = provider(tmp_path, Requester())
    assert target.supported_intervals == ("1d",)


def test_closed_market_seeded_quote_is_reused_without_network(tmp_path):
    async def exercise():
        clock = Clock(datetime(2026, 8, 28, 18, 0, tzinfo=SHANGHAI_TZ))
        requester = Requester(failure=RuntimeError("must not call"))
        target = provider(
            tmp_path, requester, clock, market_status_fetcher=market_closed
        )
        stale = AShareHubMarketDataProvider._quote_from_row(
            quote_row(trade_time="2026-08-28T15:00:00"), ("600519.SH",)
        )
        assert stale is not None
        target.seed_quotes([stale])
        rows = await target.fetch_quotes(["600519.SH"])
        await target.close()
        return rows, requester.calls

    rows, calls = asyncio.run(exercise())
    assert rows[0].timestamp.hour == 15
    assert calls == []


def test_seed_quotes_rejects_rows_more_than_five_minutes_in_future(tmp_path):
    async def exercise():
        clock = Clock(datetime(2026, 8, 28, 10, 0, tzinfo=SHANGHAI_TZ))
        requester = Requester({"/v2/market/realtime": [quote_row(price="1501.00")]})
        target = provider(tmp_path, requester, clock)
        future = AShareHubMarketDataProvider._quote_from_row(
            quote_row(price="9999.00", trade_time="2026-08-28T10:05:01"),
            ("600519.SH",),
        )
        assert future is not None
        seeded = target.seed_quotes([future])
        rows = await target.fetch_quotes(["600519.SH"])
        await target.close()
        return seeded, rows, requester.calls

    seeded, rows, calls = asyncio.run(exercise())
    assert seeded == 0
    assert rows[0].last == Decimal("1501.00")
    assert len(calls) == 1


def test_operation_specific_success_timestamps_do_not_mislabel_daily_as_quote(tmp_path):
    async def exercise():
        clock = Clock(datetime(2026, 8, 28, 16, 0, tzinfo=SHANGHAI_TZ))
        requester = Requester({"/v2/market/daily": [daily_row()]})
        target = provider(tmp_path, requester, clock)
        await target.fetch_bars("600519.SH", interval="1d", limit=1)
        status = target.realtime_status
        await target.close()
        return status

    status = asyncio.run(exercise())
    assert status["last_success_at"] == "2026-08-28T16:00:00+08:00"
    assert status["last_quote_success_at"] is None
    assert status["last_bar_success_at"] == "2026-08-28T16:00:00+08:00"
    assert status["last_catalogue_success_at"] is None


@pytest.mark.parametrize(
    "status,error_type",
    [
        (401, AShareHubAuthenticationError),
        (403, AShareHubPermissionError),
        (429, AShareHubRateLimitError),
        (422, AShareHubRequestError),
        (503, AShareHubTransportError),
    ],
)
def test_http_status_classification_is_sanitized(tmp_path, status, error_type):
    async def exercise():
        requester = Requester(failure=_HTTPStatusError(status))
        target = provider(tmp_path, requester)
        with pytest.raises(error_type) as caught:
            await target.fetch_quotes(["600519.SH"])
        snapshot = target.realtime_status
        await target.close()
        return str(caught.value), snapshot

    message, snapshot = asyncio.run(exercise())
    assert TEST_KEY not in message and TEST_KEY not in repr(snapshot)
    assert snapshot["last_http_status"] == status
    assert snapshot["last_error_type"] == error_type.__name__


@pytest.mark.parametrize(
    "status,error_type",
    [
        (401, AShareHubAuthenticationError),
        (429, AShareHubRateLimitError),
    ],
)
def test_auth_and_vendor_quota_circuit_stops_followup_bar_requests(
    tmp_path, status, error_type
):
    async def exercise():
        requester = Requester(failure=_HTTPStatusError(status))
        target = provider(tmp_path, requester)
        with pytest.raises(error_type):
            await target.fetch_quotes(["600519.SH"])
        with pytest.raises(error_type):
            await target.fetch_bars_many(
                ["600519.SH", "000001.SZ"], interval="1d"
            )
        await target.close()
        return requester.calls

    assert len(asyncio.run(exercise())) == 1


def test_vendor_429_circuit_resets_on_next_local_day(tmp_path):
    class DailyRequester:
        def __init__(self):
            self.calls = 0

        def __call__(self, method, url, headers, params, timeout):
            self.calls += 1
            if self.calls == 1:
                raise _HTTPStatusError(429)
            return [quote_row()]

    async def exercise():
        clock = Clock(datetime(2026, 8, 28, 10, 0, tzinfo=SHANGHAI_TZ))
        requester = DailyRequester()
        target = provider(tmp_path, requester, clock)
        with pytest.raises(AShareHubRateLimitError):
            await target.fetch_quotes(["600519.SH"])
        clock.advance(24 * 60 * 60)
        rows = await target.fetch_quotes(["600519.SH"])
        await target.close()
        return requester.calls, rows

    calls, rows = asyncio.run(exercise())
    assert calls == 2
    assert rows[0].symbol == "600519.SH"


def test_vendor_429_status_distinguishes_upstream_from_local_budget(tmp_path):
    async def exercise():
        requester = Requester(failure=_HTTPStatusError(429))
        target = provider(tmp_path, requester, daily_soft_limit=90)
        with pytest.raises(AShareHubRateLimitError):
            await target.fetch_quotes(["600519.SH"])
        snapshot = target.realtime_status
        await target.close()
        return snapshot

    snapshot = asyncio.run(exercise())
    assert snapshot["state"] == "upstream_quota_exhausted"
    assert snapshot["upstream_quota_exhausted"] is True
    assert snapshot["local_budget_exhausted"] is False
    assert snapshot["requests_remaining_soft"] == 89


def test_realtime_permission_circuit_does_not_block_daily_history(tmp_path):
    """A plan may deny realtime while still allowing the daily endpoint."""

    class EndpointPermissions:
        def __init__(self):
            self.calls = []

        def __call__(self, method, url, headers, params, timeout):
            path = url.removeprefix(ASHAREHUB_BASE_URL)
            self.calls.append(path)
            if path == "/v2/market/realtime":
                raise _HTTPStatusError(403)
            if path == "/v2/market/daily":
                return [daily_row()]
            return []

    async def exercise():
        requester = EndpointPermissions()
        target = provider(tmp_path, requester)
        with pytest.raises(AShareHubPermissionError):
            await target.fetch_quotes(["600519.SH"])
        bars = await target.fetch_bars(
            "600519.SH", interval="1d", limit=1
        )
        # The realtime circuit remains open for that operation, avoiding a
        # repeated paid call, while an allowed history endpoint still works.
        with pytest.raises(AShareHubPermissionError):
            await target.fetch_quotes(["600519.SH"])
        status = dict(target.realtime_status)
        await target.close()
        return requester.calls, bars, status

    calls, bars, status = asyncio.run(exercise())
    assert calls == ["/v2/market/realtime", "/v2/market/daily"]
    assert len(bars) == 1
    assert status["permission_circuit_open"] is True


def test_default_zero_retries_and_explicit_retry_each_consume_budget(tmp_path):
    class Flaky:
        def __init__(self):
            self.calls = []

        def __call__(self, method, url, headers, params, timeout):
            self.calls.append(url)
            if len(self.calls) == 1:
                raise RuntimeError("temporary")
            return [quote_row()]

    async def exercise():
        zero = Flaky()
        first = provider(tmp_path / "zero", zero)
        with pytest.raises(AShareHubTransportError):
            await first.fetch_quotes(["600519.SH"])
        zero_status = first.realtime_status
        await first.close()

        retry = Flaky()
        second = provider(
            tmp_path / "retry", retry, retries=1, retry_backoff_seconds=0
        )
        rows = await second.fetch_quotes(["600519.SH"])
        retry_status = second.realtime_status
        await second.close()
        return zero, zero_status, retry, rows, retry_status

    zero, zero_status, retry, rows, retry_status = asyncio.run(exercise())
    assert len(zero.calls) == 1 and zero_status["requests_used_today"] == 1
    assert len(retry.calls) == 2 and retry_status["requests_used_today"] == 2
    assert rows[0].symbol == "600519.SH"


def test_response_must_be_top_level_array(tmp_path):
    async def exercise():
        requester = Requester({"/v2/market/realtime": {"data": [quote_row()]}})
        target = provider(tmp_path, requester)
        with pytest.raises(AShareHubDataError, match="top-level JSON array"):
            await target.fetch_quotes(["600519.SH"])
        await target.close()

    asyncio.run(exercise())


def test_seeded_daily_bars_avoid_network_and_wrong_sources_are_ignored(tmp_path):
    async def exercise():
        requester = Requester()
        target = provider(tmp_path, requester)
        timestamp = datetime(2026, 8, 27, tzinfo=SHANGHAI_TZ)
        valid = Bar(
            symbol="600519.SH",
            interval="1d",
            timestamp=timestamp,
            open="10",
            high="11",
            low="9",
            close="10.5",
            volume=100,
            source="asharehub",
        )
        foreign = Bar(
            symbol="600519.SH",
            interval="1d",
            timestamp=timestamp - timedelta(days=1),
            open="10",
            high="11",
            low="9",
            close="10.5",
            volume=100,
            source="alltick",
        )
        seeded = target.seed_bars([valid, foreign])
        rows = await target.fetch_bars(
            "600519.SH",
            interval="1d",
            start=datetime(2026, 8, 20, tzinfo=SHANGHAI_TZ),
            # Explicit historical coverage ends at the seeded bar.  Current-
            # range requests are tested separately and must refresh.
            end=datetime(2026, 8, 27, tzinfo=SHANGHAI_TZ),
            limit=1,
        )
        await target.close()
        return seeded, rows, requester.calls

    seeded, rows, calls = asyncio.run(exercise())
    assert seeded == 1
    assert rows[0].source == "asharehub"
    assert calls == []


def test_daily_lineage_distinguishes_network_memory_and_sqlite_cache(tmp_path):
    async def exercise():
        clock = Clock(datetime(2026, 8, 28, 16, 0, tzinfo=SHANGHAI_TZ))
        requester = Requester(
            {"/v2/market/daily": [daily_row(trade_date="20260827")]}
        )
        target = provider(tmp_path / "network", requester, clock)
        request = {
            "interval": "1d",
            "start": datetime(2026, 8, 27, tzinfo=SHANGHAI_TZ),
            "end": datetime(2026, 8, 27, tzinfo=SHANGHAI_TZ),
            "limit": 1,
        }
        first = await target.fetch_bars_with_lineage("600519.SH", **request)
        second = await target.fetch_bars_with_lineage("600519.SH", **request)

        seeded_target = provider(tmp_path / "seeded", Requester(), clock)
        seeded_target.seed_bars(first.bars)
        seeded = await seeded_target.fetch_bars_with_lineage(
            "600519.SH", **request
        )
        await target.close()
        await seeded_target.close()
        return first, second, seeded, requester.calls

    first, second, seeded, calls = asyncio.run(exercise())
    assert first.transport == BarFetchTransport.NETWORK
    assert first.observations == first.bars
    assert first.response_received_at is not None
    assert second.transport == BarFetchTransport.MEMORY_CACHE
    assert second.observations == ()
    assert second.response_received_at is None
    assert seeded.transport == BarFetchTransport.SQLITE_CACHE
    assert seeded.observations == ()
    assert seeded.response_received_at is None
    assert len(calls) == 1


def test_close_is_idempotent_and_prevents_new_requests(tmp_path):
    async def exercise():
        requester = Requester()
        target = provider(tmp_path, requester)
        await target.close()
        await target.close()
        with pytest.raises(Exception, match="closed"):
            await target.fetch_instruments()
        return target.realtime_status, requester.calls, target._api_key

    status, calls, key_after_close = asyncio.run(exercise())
    assert status["state"] == "closed"
    assert calls == []
    assert key_after_close == ""


def test_budget_file_contains_no_api_key(tmp_path):
    async def exercise():
        requester = Requester({"/v2/market/realtime": [quote_row()]})
        target = provider(tmp_path, requester)
        await target.fetch_quotes(["600519.SH"])
        await target.close()

    asyncio.run(exercise())
    text = (tmp_path / "budget.json").read_text(encoding="utf-8")
    payload = json.loads(text)
    assert TEST_KEY not in text
    assert "credential_fingerprint" in text
    assert set(payload) == {
        "date",
        "used",
        "last_request_at",
        "credential_fingerprint",
    }
