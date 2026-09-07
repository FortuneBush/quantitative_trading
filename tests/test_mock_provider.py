"""Tests for the deterministic offline market-data provider."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from quant_monitor.data_sources import MockMarketDataProvider
from quant_monitor.domain import parse_market_time


def test_mock_quotes_are_repeatable_for_seed_and_invocation_order():
    async def exercise():
        first = MockMarketDataProvider(seed=42)
        second = MockMarketDataProvider(seed=42)
        symbols = ["600519.SH", "000001.SZ"]
        first_batches = [await first.fetch_quotes(symbols), await first.fetch_quotes(symbols)]
        second_batches = [await second.fetch_quotes(symbols), await second.fetch_quotes(symbols)]
        return symbols, first_batches, second_batches

    symbols, first_batches, second_batches = asyncio.run(exercise())

    assert first_batches == second_batches
    assert first_batches[0] != first_batches[1]
    assert [quote.symbol for quote in first_batches[0]] == symbols
    assert all(quote.source == "mock" for quote in first_batches[0])
    assert all(quote.timestamp.utcoffset() == timedelta(hours=8) for quote in first_batches[0])


def test_mock_bars_are_repeatable_aligned_and_ordered():
    async def exercise():
        end = parse_market_time("2026-08-12T10:03:49+08:00")
        first = MockMarketDataProvider(seed=7)
        second = MockMarketDataProvider(seed=7)
        bars = await first.fetch_bars("600519.SH", interval="5m", end=end, limit=4)
        comparison = await second.fetch_bars(
            "600519.SH", interval="5m", end=end, limit=4
        )
        return bars, comparison

    bars, comparison = asyncio.run(exercise())

    assert bars == comparison
    assert len(bars) == 4
    assert [bar.timestamp for bar in bars] == sorted(bar.timestamp for bar in bars)
    assert all(bar.timestamp.minute % 5 == 0 for bar in bars)
    assert all(bar.high >= max(bar.open, bar.close) for bar in bars)
    assert all(bar.low <= min(bar.open, bar.close) for bar in bars)


def test_mock_provider_validates_symbol_interval_and_limit():
    async def exercise():
        provider = MockMarketDataProvider()
        with pytest.raises(ValueError, match="CODE.EXCHANGE"):
            await provider.fetch_quotes(["600519"])
        with pytest.raises(ValueError, match="unsupported mock interval"):
            await provider.fetch_bars("600519.SH", interval="2m")
        with pytest.raises(ValueError, match="limit must be positive"):
            await provider.fetch_bars("600519.SH", limit=0)

    asyncio.run(exercise())


def test_daily_and_weekly_bars_use_shanghai_calendar_boundaries():
    async def exercise():
        provider = MockMarketDataProvider(seed=7)
        end = parse_market_time("2026-08-12T18:43:21+08:00")  # Wednesday
        daily = await provider.fetch_bars("600519.SH", interval="1d", end=end, limit=2)
        weekly = await provider.fetch_bars("600519.SH", interval="1w", end=end, limit=2)
        return daily, weekly

    daily, weekly = asyncio.run(exercise())

    assert all(bar.timestamp.hour == 15 for bar in daily)
    assert all(bar.timestamp.minute == 30 for bar in daily)
    assert all(bar.timestamp.hour == 0 for bar in weekly)
    assert all(bar.timestamp.minute == 0 for bar in weekly)
    assert all(bar.timestamp.weekday() < 5 for bar in daily)
    assert all(bar.timestamp.weekday() == 0 for bar in weekly)


def test_daily_bars_skip_weekends_and_do_not_publish_before_close():
    async def exercise():
        provider = MockMarketDataProvider(seed=7)
        # Monday morning: the latest completed daily bar is Friday at 15:30.
        end = parse_market_time("2026-08-17T09:30:00+08:00")
        return await provider.fetch_bars(
            "600519.SH", interval="1d", end=end, limit=3
        )

    bars = asyncio.run(exercise())

    assert [bar.timestamp.isoformat() for bar in bars] == [
        "2026-08-12T15:30:00+08:00",
        "2026-08-13T15:30:00+08:00",
        "2026-08-14T15:30:00+08:00",
    ]


def test_mock_catalogue_has_readable_chinese_names():
    instruments = asyncio.run(MockMarketDataProvider().fetch_instruments())

    assert {item.symbol: item.name for item in instruments} == {
        "600519.SH": "贵州茅台",
        "000001.SZ": "平安银行",
        "300750.SZ": "宁德时代",
        "688981.SH": "中芯国际",
    }
