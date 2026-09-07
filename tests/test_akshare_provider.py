"""Offline contract tests for the AKShare market-data adapter."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pandas as pd
import pytest

from quant_monitor.data_sources import AkShareDataError, AkShareMarketDataProvider
from quant_monitor.domain import parse_market_time


def _spot_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "代码": "600519",
                "名称": "贵州茅台",
                "最新价": 1499.87,
                "今开": 1490.0,
                "最高": 1501.23,
                "最低": 1488.88,
                "昨收": 1489.99,
                "成交量": 123456,
                "成交额": 185123456.78,
            },
            {
                "代码": 600036,
                "名称": "招商银行",
                "最新价": 40.12,
                "今开": 40.0,
                "最高": 40.5,
                "最低": 39.8,
                "昨收": 39.95,
                "成交量": 100,
                "成交额": 4012.0,
            },
            # No usable price: the adapter must not turn this into a fake zero
            # quote or silently reuse a stale database value.
            {
                "代码": "601318",
                "名称": "中国平安",
                "最新价": "--",
                "今开": "--",
                "最高": "--",
                "最低": "--",
                "昨收": 50.0,
                "成交量": "--",
                "成交额": "--",
            },
        ]
    )


def test_akshare_quotes_are_normalized_and_cached():
    async def exercise():
        calls = []
        received_at = parse_market_time("2026-08-22T10:01:02+08:00")

        def fetch_spot():
            calls.append(True)
            return _spot_frame()

        provider = AkShareMarketDataProvider(
            spot_fetcher=fetch_spot,
            quote_cache_seconds=60,
            clock=lambda: received_at,
        )
        first = await provider.fetch_quotes(["600519.sh", "600036.SH"])
        second = await provider.fetch_quotes(["600036.SH"])
        return provider, calls, first, second, received_at

    provider, calls, first, second, received_at = asyncio.run(exercise())
    try:
        assert len(calls) == 1
        assert [item.symbol for item in first] == ["600519.SH", "600036.SH"]
        assert first[0].last == Decimal("1499.87")
        assert first[0].previous_close == Decimal("1489.99")
        assert first[0].volume == 123456
        assert first[0].turnover == Decimal("185123456.78")
        assert first[0].bid is None and first[0].ask is None
        assert first[0].source == "akshare"
        assert first[0].timestamp == received_at
        assert second[0].symbol == "600036.SH"
    finally:
        asyncio.run(provider.close())


def test_akshare_strict_mode_rejects_missing_or_unpriced_symbols():
    async def exercise():
        provider = AkShareMarketDataProvider(
            spot_fetcher=lambda: _spot_frame(),
            quote_cache_seconds=0,
        )
        with pytest.raises(AkShareDataError, match="601318.SH"):
            await provider.fetch_quotes(["600519.SH", "601318.SH"])
        await provider.close()

    asyncio.run(exercise())


def test_akshare_relaxed_quotes_skip_unpriced_rows_for_universe_batches():
    async def exercise():
        provider = AkShareMarketDataProvider(
            spot_fetcher=lambda: _spot_frame(),
            quote_cache_seconds=60,
        )
        quotes = await provider.fetch_quotes_relaxed(["600519.SH", "601318.SH"])
        await provider.close()
        return quotes

    quotes = asyncio.run(exercise())
    assert [item.symbol for item in quotes] == ["600519.SH"]


def test_akshare_retries_transient_snapshot_failures():
    async def exercise():
        attempts = []

        def fetch_spot():
            attempts.append(True)
            if len(attempts) == 1:
                raise ConnectionError("temporary upstream failure")
            return _spot_frame()

        provider = AkShareMarketDataProvider(
            spot_fetcher=fetch_spot,
            retries=1,
            retry_backoff_seconds=0,
        )
        quotes = await provider.fetch_quotes(["600519.SH"])
        await provider.close()
        return attempts, quotes

    attempts, quotes = asyncio.run(exercise())
    assert len(attempts) == 2
    assert quotes[0].last == Decimal("1499.87")


def test_akshare_uses_documented_sina_snapshot_as_fallback():
    async def exercise():
        def primary():
            raise ConnectionError("Eastmoney endpoint unavailable")

        fallback = pd.DataFrame(
            [
                {
                    "代码": "sh600519",
                    "名称": "贵州茅台",
                    "最新价": 1500.0,
                    "买入": 1499.9,
                    "卖出": 1500.1,
                    "昨收": 1490.0,
                    "今开": 1495.0,
                    "最高": 1502.0,
                    "最低": 1493.0,
                    "成交量": 1000,
                    "成交额": 2000000,
                    "时间戳": "10:01:02",
                },
                {"代码": "sz000001", "名称": "平安银行", "最新价": 10},
            ]
        )
        provider = AkShareMarketDataProvider(
            spot_fetcher=primary,
            fallback_spot_fetcher=lambda: fallback,
            retries=0,
            use_spot_fallback=True,
            clock=lambda: parse_market_time("2026-08-22T10:02:00+08:00"),
        )
        quotes = await provider.fetch_quotes(["600519.SH"])
        await provider.close()
        return quotes

    quotes = asyncio.run(exercise())
    assert len(quotes) == 1
    assert quotes[0].source == "akshare:sina"
    assert quotes[0].bid == Decimal("1499.9")
    assert quotes[0].ask == Decimal("1500.1")
    assert quotes[0].timestamp.isoformat() == "2026-08-22T10:02:00+08:00"


def test_sina_numeric_codes_do_not_leak_non_shares_into_shanghai_catalogue():
    async def exercise():
        fallback = pd.DataFrame(
            [
                {"代码": "600519", "名称": "贵州茅台", "最新价": 1500.0},
                {"代码": "000001", "名称": "平安银行", "最新价": 10.0},
                {"代码": "920000", "名称": "北交所样本", "最新价": 13.0},
            ]
        )
        provider = AkShareMarketDataProvider(
            spot_fetcher=lambda: (_ for _ in ()).throw(ConnectionError("primary")),
            fallback_spot_fetcher=lambda: fallback,
            retries=0,
            use_spot_fallback=True,
        )
        quotes = await provider.fetch_quotes(["600519.SH"])
        instruments = await provider.fetch_instruments()
        await provider.close()
        return quotes, instruments

    quotes, instruments = asyncio.run(exercise())
    assert [quote.symbol for quote in quotes] == ["600519.SH"]
    assert [instrument.symbol for instrument in instruments] == ["600519.SH"]


def test_akshare_minute_bars_are_ordered_limited_and_cached():
    async def exercise():
        calls = []

        def fetch_minute(**kwargs):
            calls.append(kwargs)
            assert kwargs["symbol"] == "600519"
            assert kwargs["period"] == "5"
            return pd.DataFrame(
                [
                    {"时间": "2026-08-22 10:00:00", "开盘": 10, "最高": 11, "最低": 9, "收盘": 10.5, "成交量": 10, "成交额": 100},
                    {"时间": "2026-08-22 10:05:00", "开盘": 10.5, "最高": 12, "最低": 10, "收盘": 11.5, "成交量": 20, "成交额": 220},
                    {"时间": "2026-08-22 10:10:00", "开盘": 11.5, "最高": 13, "最低": 11, "收盘": 12.5, "成交量": 30, "成交额": 360},
                ]
            )

        provider = AkShareMarketDataProvider(
            minute_fetcher=fetch_minute,
            bars_cache_seconds=60,
            clock=lambda: parse_market_time("2026-08-22T10:15:00+08:00"),
        )
        first = await provider.fetch_bars("600519.SH", interval="5m", limit=2)
        second = await provider.fetch_bars("600519.SH", interval="5m", limit=1)
        await provider.close()
        return calls, first, second

    calls, first, second = asyncio.run(exercise())
    assert len(calls) == 1
    assert [bar.timestamp.minute for bar in first] == [5, 10]
    assert first[0].close == Decimal("11.5")
    assert first[1].volume == 30
    assert second[0].timestamp.minute == 10
    assert all(bar.source == "akshare" for bar in first)


def test_akshare_daily_bars_map_period_and_timezone():
    async def exercise():
        calls = []

        def fetch_history(**kwargs):
            calls.append(kwargs)
            return pd.DataFrame(
                [
                    {"日期": "2026-08-20", "开盘": 10, "最高": 11, "最低": 9, "收盘": 10.5, "成交量": 10, "成交额": 100},
                    {"日期": "2026-08-21", "开盘": 10.5, "最高": 12, "最低": 10, "收盘": 11.5, "成交量": 20, "成交额": 220},
                ]
            )

        provider = AkShareMarketDataProvider(history_fetcher=fetch_history)
        bars = await provider.fetch_bars("600519.SH", interval="1d", limit=10)
        await provider.close()
        return calls, bars

    calls, bars = asyncio.run(exercise())
    assert calls[0]["period"] == "daily"
    assert [bar.timestamp.isoformat() for bar in bars] == [
        "2026-08-20T00:00:00+08:00",
        "2026-08-21T00:00:00+08:00",
    ]
