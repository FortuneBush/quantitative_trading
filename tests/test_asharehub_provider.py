"""Offline contract tests for the quota-aware AShareHub v2 adapter."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
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
        await target.close()

    asyncio.run(exercise())


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
        (403, AShareHubPermissionError),
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
