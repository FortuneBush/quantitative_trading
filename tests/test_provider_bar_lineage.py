"""Offline tests for provider bar-fetch lineage without any paid API calls."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from quant_monitor.data_sources.asharehub import AShareHubMarketDataProvider
from quant_monitor.data_sources.base import (
    BarFetchResult,
    BarFetchTransport,
    MarketDataProvider,
)
from quant_monitor.domain import Bar, SHANGHAI_TZ


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value
        self.monotonic_value = 100.0

    def __call__(self) -> datetime:
        return self.value

    def monotonic(self) -> float:
        return self.monotonic_value


def _bar(day: int, *, close: str = "10.5") -> Bar:
    return Bar(
        symbol="600519.SH",
        interval="1d",
        timestamp=datetime(2026, 8, day, 15, 30, tzinfo=SHANGHAI_TZ),
        open="10",
        high="11",
        low="9",
        close=close,
        volume=100,
        source="asharehub",
    )


def _daily_row(day: int, *, close: str = "10.5"):
    return {
        "symbol": "600519.SH",
        "trade_date": "202608{:02d}".format(day),
        "open": "10",
        "high": "11",
        "low": "9",
        "close": close,
        "vol": "1",
        "amount": "2",
    }


def _provider(tmp_path, requester, clock):
    return AShareHubMarketDataProvider(
        "offline-test-key",
        symbols=("600519.SH",),
        requester=requester,
        budget_path=tmp_path / "budget.json",
        clock=clock,
        monotonic=clock.monotonic,
        market_status_fetcher=lambda _: {"is_trading": False},
    )


def test_asharehub_lineage_separates_network_observations_from_combined_view(
    tmp_path,
):
    async def exercise():
        clock = _Clock(datetime(2026, 8, 27, 18, 0, tzinfo=SHANGHAI_TZ))
        calls = []

        def requester(method, url, headers, params, timeout):
            calls.append((method, url, dict(params)))
            # Model the response completing three seconds after the call began.
            clock.value += timedelta(seconds=3)
            return [_daily_row(27)]

        target = _provider(tmp_path, requester, clock)
        target.seed_bars([_bar(26)])
        kwargs = {
            "interval": "1d",
            "start": datetime(2026, 8, 20, tzinfo=SHANGHAI_TZ),
            "end": datetime(2026, 8, 27, 23, 0, tzinfo=SHANGHAI_TZ),
            "limit": 2,
        }
        network = await target.fetch_bars_with_lineage("600519.SH", **kwargs)
        cached = await target.fetch_bars_with_lineage("600519.SH", **kwargs)
        legacy = await target.fetch_bars("600519.SH", **kwargs)
        await target.close()
        return calls, network, cached, legacy

    calls, network, cached, legacy = asyncio.run(exercise())
    assert len(calls) == 1
    assert network.transport is BarFetchTransport.NETWORK
    assert [item.timestamp.day for item in network.current_view] == [26, 27]
    assert [item.timestamp.day for item in network.observations] == [27]
    assert network.response_received_at == datetime(
        2026, 8, 27, 18, 0, 3, tzinfo=SHANGHAI_TZ
    )
    assert cached.transport is BarFetchTransport.MEMORY_CACHE
    assert cached.observations == ()
    assert cached.response_received_at is None
    assert legacy == list(network.current_view)


def test_asharehub_sqlite_seed_is_not_claimed_as_a_new_observation(tmp_path):
    async def exercise():
        clock = _Clock(datetime(2026, 8, 27, 18, 0, tzinfo=SHANGHAI_TZ))

        def requester(*_):
            raise AssertionError("SQLite cache hit must not access the network")

        target = _provider(tmp_path, requester, clock)
        target.seed_bars([_bar(26), _bar(27)])
        result = await target.fetch_bars_with_lineage(
            "600519.SH",
            interval="1d",
            start=datetime(2026, 8, 20, tzinfo=SHANGHAI_TZ),
            end=datetime(2026, 8, 27, 23, 0, tzinfo=SHANGHAI_TZ),
            limit=2,
        )
        await target.close()
        return result

    result = asyncio.run(exercise())
    assert result.transport is BarFetchTransport.SQLITE_CACHE
    assert len(result.bars) == 2
    assert result.observations == ()
    assert result.response_received_at is None


def test_asharehub_lineage_preserves_duplicate_rows_from_one_network_response(
    tmp_path,
):
    async def exercise():
        clock = _Clock(datetime(2026, 8, 27, 18, 0, tzinfo=SHANGHAI_TZ))

        def requester(*_):
            return [_daily_row(27, close="10.25"), _daily_row(27, close="10.5")]

        target = _provider(tmp_path, requester, clock)
        result = await target.fetch_bars_with_lineage(
            "600519.SH",
            interval="1d",
            start=datetime(2026, 8, 20, tzinfo=SHANGHAI_TZ),
            end=datetime(2026, 8, 27, 23, 0, tzinfo=SHANGHAI_TZ),
            limit=2,
        )
        await target.close()
        return result

    result = asyncio.run(exercise())
    assert result.transport is BarFetchTransport.NETWORK
    assert [item.close for item in result.observations] == [
        _bar(27, close="10.25").close,
        _bar(27, close="10.5").close,
    ]
    # Compatibility view still keeps the last row for one logical timestamp.
    assert [item.close for item in result.bars] == [_bar(27, close="10.5").close]


class _LegacyProvider(MarketDataProvider):
    @property
    def name(self):
        return "legacy"

    async def fetch_quotes(self, symbols):
        return []

    async def fetch_bars(self, symbol, interval="1m", start=None, end=None, limit=200):
        return [_bar(27)]


def test_base_lineage_hook_fails_closed_for_legacy_provider():
    result = asyncio.run(
        _LegacyProvider().fetch_bars_with_lineage(
            "600519.SH", interval="1d", limit=1
        )
    )
    assert result.bars == (_bar(27),)
    assert result.observations == ()
    assert result.transport is BarFetchTransport.LEGACY_UNKNOWN
    assert result.response_received_at is None


def test_bar_fetch_result_rejects_fabricated_cache_receipt_or_observations():
    received_at = datetime(2026, 8, 27, 18, 0, tzinfo=SHANGHAI_TZ)
    with pytest.raises(ValueError, match="only a network"):
        BarFetchResult(
            bars=(_bar(27),),
            observations=(_bar(27),),
            transport=BarFetchTransport.SQLITE_CACHE,
        )
    with pytest.raises(ValueError, match="only a network"):
        BarFetchResult(
            bars=(_bar(27),),
            observations=(),
            transport=BarFetchTransport.MEMORY_CACHE,
            response_received_at=received_at,
        )
    with pytest.raises(ValueError, match="requires response_received_at"):
        BarFetchResult(
            bars=(_bar(27),),
            observations=(_bar(27),),
            transport=BarFetchTransport.NETWORK,
        )
