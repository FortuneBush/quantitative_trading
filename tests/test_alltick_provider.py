"""Offline contract tests for the AllTick market-data adapter."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quant_monitor.data_sources import (
    AllTickAuthenticationError,
    AllTickConfigurationError,
    AllTickDataError,
    AllTickMarketDataProvider,
    AllTickPermissionError,
    AllTickRateLimitError,
    AllTickTransportError,
)
from quant_monitor.domain import SHANGHAI_TZ


TEST_TOKEN = "unit-test-token-sentinel"


def _epoch(second: int = 0, *, milliseconds: bool = False) -> str:
    value = (
        datetime(2026, 8, 24, 10, 0, 0, tzinfo=SHANGHAI_TZ)
        + timedelta(seconds=second)
    ).timestamp()
    return str(int(value * 1000 if milliseconds else value))


def _tick(code: str, *, price: str = "123.45", seq: int = 1, second: int = 0):
    return {
        "code": code,
        "seq": str(seq),
        "tick_time": _epoch(second, milliseconds=True),
        "price": price,
        "volume": "300",
        "turnover": "37035.00",
        "trade_direction": 1,
    }


def _bar(timestamp: str, close: str = "10.20"):
    return {
        "timestamp": timestamp,
        "open_price": "10.00",
        "close_price": close,
        "high_price": "10.50",
        "low_price": "9.80",
        "volume": "1000",
        "turnover": "10200.00",
    }


def _depth(code: str, *, seq: int = 1, second: int = 0, one_sided: bool = False):
    return {
        "code": code,
        "seq": str(seq),
        "tick_time": _epoch(second, milliseconds=True),
        "bids": [{"price": "123.40", "volume": "500.00"}],
        "asks": [] if one_sided else [{"price": "123.50", "volume": "700"}],
    }


class QuoteRequester:
    def __init__(self, *, omit=(), ret=200, fail=None):
        self.calls = []
        self.omit = set(omit)
        self.ret = ret
        self.fail = fail

    def __call__(self, method, url, params, body, timeout):
        self.calls.append((method, url, dict(params), body, timeout))
        if self.fail is not None:
            raise self.fail
        query = json.loads(params["query"])
        codes = [item["code"] for item in query["data"]["symbol_list"]]
        return {
            "ret": self.ret,
            "msg": "ok" if self.ret == 200 else "error",
            "trace": query["trace"],
            "data": {
                "tick_list": [
                    _tick(code, seq=index + 1)
                    for index, code in enumerate(codes)
                    if code not in self.omit
                ]
            },
        }


class BarRequester:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, params, body, timeout):
        self.calls.append((method, url, dict(params), body, timeout))
        if url.endswith("/kline"):
            query = json.loads(params["query"])
            code = query["data"]["code"]
            return {
                "ret": 200,
                "msg": "ok",
                "trace": query["trace"],
                "data": {
                    "code": code,
                    "kline_type": query["data"]["kline_type"],
                    # Deliberately unsorted with a duplicate timestamp.
                    "kline_list": [
                        _bar(_epoch(60)),
                        _bar(_epoch(0), close="10.10"),
                        _bar(_epoch(0), close="10.20"),
                    ],
                },
            }
        assert url.endswith("/batch-kline")
        rows = []
        for item in body["data"]["data_list"]:
            rows.append(
                {
                    "code": item["code"],
                    "kline_type": item["kline_type"],
                    "kline_data": [_bar(_epoch(120), close="10.30")],
                }
            )
        return {
            "ret": 200,
            "msg": "ok",
            "trace": body["trace"],
            "data": {"kline_list": rows},
        }


def _provider(requester, **kwargs):
    return AllTickMarketDataProvider(
        TEST_TOKEN,
        symbols=kwargs.pop("symbols", ["600519.SH", "000001.SZ"]),
        requester=requester,
        use_websocket=False,
        use_order_book=kwargs.pop("use_order_book", False),
        bootstrap_daily_reference=kwargs.pop("bootstrap_daily_reference", False),
        fetch_static_info=kwargs.pop("fetch_static_info", False),
        http_min_interval_seconds=0,
        retries=kwargs.pop("retries", 0),
        **kwargs,
    )


def test_alltick_requires_an_environment_token():
    with pytest.raises(AllTickConfigurationError, match="QUANT_MONITOR_ALLTICK_TOKEN"):
        AllTickMarketDataProvider("  ")


def test_alltick_quotes_are_exact_normalized_cached_and_timestamped():
    async def exercise():
        requester = QuoteRequester()
        provider = _provider(requester, quote_cache_seconds=60)
        first = await provider.fetch_quotes(["600519.sh", "000001.SZ", "600519.SH"])
        second = await provider.fetch_quotes(["600519.SH"])
        await provider.close()
        return requester, first, second

    requester, first, second = asyncio.run(exercise())
    assert len(requester.calls) == 1
    assert [item.symbol for item in first] == ["600519.SH", "000001.SZ"]
    assert first[0].last == Decimal("123.45")
    assert first[0].volume == 300
    assert first[0].turnover == Decimal("37035.00")
    assert first[0].timestamp.isoformat() == "2026-08-24T10:00:00+08:00"
    assert first[0].source == "alltick"
    assert first[0].previous_close is None
    assert second[0] == first[0]
    assert requester.calls[0][2]["token"] == TEST_TOKEN


def test_alltick_quotes_chunk_at_plan_limit_and_ignore_extra_rows():
    class ExtraRequester(QuoteRequester):
        def __call__(self, *args):
            payload = super().__call__(*args)
            payload["data"]["tick_list"].append(_tick("300750.SZ"))
            return payload

    async def exercise():
        requester = ExtraRequester()
        provider = _provider(requester, symbols=[], batch_size=2)
        symbols = ["600000.SH", "600001.SH", "600002.SH", "600003.SH", "600004.SH"]
        quotes = await provider.fetch_quotes(symbols)
        await provider.close()
        return requester, quotes

    requester, quotes = asyncio.run(exercise())
    assert len(requester.calls) == 3
    assert [item.symbol for item in quotes] == [
        "600000.SH",
        "600001.SH",
        "600002.SH",
        "600003.SH",
        "600004.SH",
    ]


def test_alltick_concurrent_quote_bootstrap_is_single_flight():
    async def exercise():
        calls = []

        async def requester(method, url, params, body, timeout):
            calls.append(url)
            await asyncio.sleep(0.02)
            query = json.loads(params["query"])
            return {
                "ret": 200,
                "msg": "ok",
                "trace": query["trace"],
                "data": {"tick_list": [_tick("600519.SH")]},
            }

        provider = _provider(requester, quote_cache_seconds=60)
        first, second = await asyncio.gather(
            provider.fetch_quotes(["600519.SH"]),
            provider.fetch_quotes(["600519.SH"]),
        )
        await provider.close()
        return calls, first, second

    calls, first, second = asyncio.run(exercise())
    assert len(calls) == 1
    assert first == second


def test_alltick_strict_and_relaxed_missing_quote_semantics():
    async def exercise(strict):
        requester = QuoteRequester(omit={"601318.SH"})
        provider = _provider(requester)
        try:
            if strict:
                return await provider.fetch_quotes(["600519.SH", "601318.SH"])
            return await provider.fetch_quotes_relaxed(["600519.SH", "601318.SH"])
        finally:
            await provider.close()

    with pytest.raises(AllTickDataError, match="601318.SH"):
        asyncio.run(exercise(True))
    relaxed = asyncio.run(exercise(False))
    assert [item.symbol for item in relaxed] == ["600519.SH"]


def test_alltick_rejects_unsupported_beijing_symbols_before_network():
    requester = QuoteRequester()
    provider = _provider(requester)
    with pytest.raises(ValueError, match=".SH and .SZ"):
        asyncio.run(provider.fetch_quotes(["430047.BJ"]))
    assert requester.calls == []


def test_alltick_tick_sequence_and_time_reject_duplicates_and_regressions():
    async def exercise():
        provider = _provider(QuoteRequester())
        newest = await provider._accept_tick(_tick("600519.SH", price="12", seq=20, second=2))
        duplicate = await provider._accept_tick(_tick("600519.SH", price="11", seq=20, second=2))
        older = await provider._accept_tick(_tick("600519.SH", price="10", seq=19, second=1))
        reset_sequence = await provider._accept_tick(
            _tick("600519.SH", price="13", seq=1, second=3)
        )
        await provider.close()
        return newest, duplicate, older, reset_sequence

    newest, duplicate, older, reset_sequence = asyncio.run(exercise())
    assert newest.last == Decimal("12")
    assert duplicate == newest
    assert older == newest
    assert reset_sequence.last == Decimal("13")


def test_alltick_depth_snapshot_merges_best_bid_ask_and_handles_one_sided_book():
    async def exercise():
        provider = _provider(QuoteRequester())
        await provider._accept_tick(_tick("600519.SH"))
        two_sided = await provider._accept_depth(_depth("600519.SH", seq=10))
        one_sided = await provider._accept_depth(
            _depth("600519.SH", seq=1, second=1, one_sided=True)
        )
        await provider.close()
        return two_sided, one_sided

    two_sided, one_sided = asyncio.run(exercise())
    assert two_sided.bid == Decimal("123.40")
    assert two_sided.ask == Decimal("123.50")
    assert two_sided.bid_size == 500
    assert two_sided.ask_size == 700
    assert one_sided.bid == Decimal("123.40")
    assert one_sided.ask is None
    assert one_sided.ask_size == 0


def test_alltick_expired_or_previous_session_depth_is_removed():
    async def exercise():
        clock = [0.0]
        requester = QuoteRequester()
        provider = _provider(
            requester,
            quote_cache_seconds=10,
            monotonic=lambda: clock[0],
        )
        try:
            await provider._accept_tick(_tick("600519.SH", seq=10))
            current = await provider._accept_depth(
                _depth("600519.SH", seq=10)
            )
            clock[0] = 31.0
            provider._quote_http_at["600519.SH"] = clock[0]
            expired = (await provider.fetch_quotes(["600519.SH"]))[0]
            next_session = await provider._accept_tick(
                _tick("600519.SH", seq=1, second=24 * 60 * 60)
            )
            return requester.calls, current, expired, next_session
        finally:
            await provider.close()

    calls, current, expired, next_session = asyncio.run(exercise())
    assert calls == []
    assert current.bid == Decimal("123.40")
    assert expired.bid is None
    assert expired.ask is None
    assert next_session.bid is None
    assert next_session.ask is None


def test_alltick_http_depth_bootstrap_enriches_cached_quote():
    class DepthRequester(QuoteRequester):
        def __call__(self, method, url, params, body, timeout):
            if url.endswith("/trade-tick"):
                return super().__call__(method, url, params, body, timeout)
            assert url.endswith("/depth-tick")
            self.calls.append((method, url, dict(params), body, timeout))
            query = json.loads(params["query"])
            return {
                "ret": 200,
                "msg": "ok",
                "trace": query["trace"],
                "data": {
                    "tick_list": [
                        _depth(item["code"])
                        for item in query["data"]["symbol_list"]
                    ]
                },
            }

    async def exercise():
        requester = DepthRequester()
        provider = _provider(
            requester,
            symbols=["600519.SH"],
            use_order_book=True,
            quote_cache_seconds=60,
        )
        await provider.fetch_quotes(["600519.SH"])
        assert provider._depth_task is not None
        await provider._depth_task
        quote = (await provider.fetch_quotes(["600519.SH"]))[0]
        await provider.close()
        return requester, quote

    requester, quote = asyncio.run(exercise())
    assert [call[1].rsplit("/", 1)[-1] for call in requester.calls] == [
        "trade-tick",
        "depth-tick",
    ]
    assert quote.bid == Decimal("123.40")
    assert quote.ask == Decimal("123.50")


def test_alltick_depth_604_isolates_denied_symbol():
    class MixedDepthRequester:
        def __init__(self):
            self.calls = []

        def __call__(self, method, url, params, body, timeout):
            assert url.endswith("/depth-tick")
            query = json.loads(params["query"])
            codes = tuple(
                item["code"] for item in query["data"]["symbol_list"]
            )
            self.calls.append(codes)
            if len(codes) > 1 or codes == ("600001.SH",):
                return {"ret": 604, "data": {}}
            return {
                "ret": 200,
                "data": {"tick_list": [_depth(codes[0])]},
            }

    async def exercise():
        requester = MixedDepthRequester()
        provider = _provider(
            requester,
            symbols=["600000.SH", "600001.SH", "600002.SH"],
            use_order_book=True,
            permission_retry_seconds=300,
        )
        try:
            for code in ("600000.SH", "600001.SH", "600002.SH"):
                await provider._accept_tick(_tick(code))
            denied = await provider._fetch_depth_rows(
                ["600000.SH", "600001.SH", "600002.SH"]
            )
            return requester.calls, denied, dict(provider._quotes), provider.realtime_status
        finally:
            await provider.close()

    calls, denied, quotes, status = asyncio.run(exercise())
    assert calls == [
        ("600000.SH", "600001.SH", "600002.SH"),
        ("600000.SH",),
        ("600001.SH",),
        ("600002.SH",),
    ]
    assert set(denied) == {"600001.SH"}
    assert quotes["600000.SH"].bid == Decimal("123.40")
    assert quotes["600001.SH"].bid is None
    assert quotes["600002.SH"].ask == Decimal("123.50")
    assert status["order_book_permission_denied_symbols"] == ["600001.SH"]


def test_alltick_depth_permission_expiry_requests_full_resubscription():
    class RecoveredDepthRequester:
        def __call__(self, method, url, params, body, timeout):
            query = json.loads(params["query"])
            return {
                "ret": 200,
                "data": {
                    "tick_list": [
                        _depth(item["code"])
                        for item in query["data"]["symbol_list"]
                    ]
                },
            }

    async def exercise():
        clock = [20.0]
        provider = _provider(
            RecoveredDepthRequester(),
            symbols=["600000.SH", "600001.SH"],
            use_order_book=True,
            monotonic=lambda: clock[0],
        )
        provider._ws_connected = True
        provider._ws_depth_attempted_symbols = ("600000.SH",)
        provider._ws_depth_last_error = "AllTickPermissionError"
        provider._ws_depth_last_error_code = 604
        provider._ws_depth_suppressed_until = 10.0
        provider._depth_permission_denied["600001.SH"] = 10.0
        try:
            await provider._depth_snapshot_runner()
            return (
                provider._ws_resubscribe_requested,
                provider._ws_depth_suppressed_until,
            )
        finally:
            await provider.close()

    requested, suppressed_until = asyncio.run(exercise())
    assert requested is True
    assert suppressed_until == 0.0


@pytest.mark.parametrize(
    ("interval", "expected_type"),
    [("1m", 1), ("5m", 2), ("15m", 3), ("30m", 4), ("60m", 5), ("1h", 5), ("1d", 8), ("1w", 9)],
)
def test_alltick_kline_interval_mapping(interval, expected_type):
    async def exercise():
        requester = BarRequester()
        provider = _provider(requester)
        bars = await provider.fetch_bars("600519.SH", interval=interval, limit=2)
        await provider.close()
        return requester, bars

    requester, bars = asyncio.run(exercise())
    query = json.loads(requester.calls[0][2]["query"])
    assert query["data"]["kline_type"] == expected_type
    assert query["data"]["kline_timestamp_end"] == 0
    assert len(bars) == 2


def test_alltick_bars_are_ordered_deduplicated_decimal_and_expand_cache():
    async def exercise():
        requester = BarRequester()
        provider = _provider(requester, bars_cache_seconds=60)
        first = await provider.fetch_bars("600519.SH", interval="1m", limit=1)
        expanded = await provider.fetch_bars("600519.SH", interval="1m", limit=3)
        await provider.close()
        return requester, first, expanded

    requester, first, expanded = asyncio.run(exercise())
    assert len(requester.calls) == 2
    assert len(first) == 1
    assert [item.timestamp for item in expanded] == sorted(item.timestamp for item in expanded)
    assert len(expanded) == 2  # duplicate provider timestamps are collapsed
    assert expanded[0].close == Decimal("10.20")
    assert expanded[0].turnover == Decimal("10200.00")


def test_alltick_daily_bars_enrich_cached_quote_reference_fields():
    class CombinedRequester(QuoteRequester, BarRequester):
        def __init__(self):
            QuoteRequester.__init__(self)

        def __call__(self, method, url, params, body, timeout):
            if url.endswith("/trade-tick"):
                return QuoteRequester.__call__(self, method, url, params, body, timeout)
            if url.endswith("/batch-kline"):
                self.calls.append((method, url, dict(params), body, timeout))
                rows = [
                    {
                        "code": item["code"],
                        "kline_type": item["kline_type"],
                        "kline_data": [
                            _bar(_epoch(-86_400), close="10.20"),
                            _bar(_epoch(0), close="10.30"),
                        ],
                    }
                    for item in body["data"]["data_list"]
                ]
                return {
                    "ret": 200,
                    "msg": "ok",
                    "trace": body["trace"],
                    "data": {"kline_list": rows},
                }
            return BarRequester.__call__(self, method, url, params, body, timeout)

    async def exercise():
        requester = CombinedRequester()
        provider = _provider(requester, quote_cache_seconds=60)
        await provider.fetch_quotes(["600519.SH"])
        await provider.fetch_bars("600519.SH", interval="1d", limit=2)
        enriched = await provider.fetch_quotes(["600519.SH"])
        await provider.close()
        return enriched[0]

    quote = asyncio.run(exercise())
    assert quote.previous_close == Decimal("10.20")
    assert quote.open == Decimal("10.00")
    assert quote.high == Decimal("10.50")
    assert quote.low == Decimal("9.80")


def test_alltick_background_daily_batch_bootstrap_enriches_quote():
    class CombinedRequester(QuoteRequester, BarRequester):
        def __init__(self):
            QuoteRequester.__init__(self)

        def __call__(self, method, url, params, body, timeout):
            if url.endswith("/trade-tick"):
                return QuoteRequester.__call__(self, method, url, params, body, timeout)
            if url.endswith("/batch-kline"):
                self.calls.append((method, url, dict(params), body, timeout))
                rows = [
                    {
                        "code": item["code"],
                        "kline_type": item["kline_type"],
                        "kline_data": [
                            _bar(_epoch(-86_400), close="10.20"),
                            _bar(_epoch(0), close="10.30"),
                        ],
                    }
                    for item in body["data"]["data_list"]
                ]
                return {
                    "ret": 200,
                    "msg": "ok",
                    "trace": body["trace"],
                    "data": {"kline_list": rows},
                }
            return BarRequester.__call__(self, method, url, params, body, timeout)

    async def exercise():
        requester = CombinedRequester()
        provider = _provider(
            requester,
            symbols=["600519.SH"],
            quote_cache_seconds=60,
            bootstrap_daily_reference=True,
        )
        await provider.fetch_quotes(["600519.SH"])
        assert provider._reference_task is not None
        await provider._reference_task
        quote = (await provider.fetch_quotes(["600519.SH"]))[0]
        await provider.close()
        return requester, quote

    requester, quote = asyncio.run(exercise())
    assert any(call[1].endswith("/batch-kline") for call in requester.calls)
    assert quote.previous_close == Decimal("10.20")
    assert quote.open == Decimal("10.00")


def test_alltick_multi_symbol_bars_bootstrap_then_use_batch_refresh():
    async def exercise():
        clock = [0.0]
        requester = BarRequester()
        provider = _provider(
            requester,
            bars_cache_seconds=10,
            monotonic=lambda: clock[0],
            batch_size=5,
        )
        first = await provider.fetch_bars_many(
            ["600519.SH", "000001.SZ"], interval="1m", limit=2
        )
        clock[0] = 11.0
        refreshed = await provider.fetch_bars_many(
            ["600519.SH", "000001.SZ"], interval="1m", limit=2
        )
        await provider.close()
        return requester, first, refreshed

    requester, first, refreshed = asyncio.run(exercise())
    assert [call[1].rsplit("/", 1)[-1] for call in requester.calls] == [
        "kline",
        "kline",
        "batch-kline",
    ]
    assert len(first) == 4
    assert len(refreshed) == 4
    assert all(item.source == "alltick" for item in refreshed)


@pytest.mark.parametrize(
    ("ret", "error", "calls"),
    [
        (401, AllTickAuthenticationError, 1),
        (400, AllTickDataError, 1),
        (603, AllTickPermissionError, 1),
        (604, AllTickPermissionError, 1),
        (429, AllTickRateLimitError, 2),
        (605, AllTickRateLimitError, 2),
    ],
)
def test_alltick_error_mapping_and_retry_boundaries(ret, error, calls):
    async def exercise():
        requester = QuoteRequester(ret=ret)
        provider = _provider(requester, retries=1, retry_backoff_seconds=0)
        try:
            await provider.fetch_quotes(["600519.SH"])
        finally:
            await provider.close()
        return requester

    requester = QuoteRequester(ret=ret)
    provider = _provider(requester, retries=1, retry_backoff_seconds=0)
    with pytest.raises(error):
        asyncio.run(provider.fetch_quotes(["600519.SH"]))
    asyncio.run(provider.close())
    assert len(requester.calls) == calls


def test_alltick_code_permission_error_is_temporarily_circuit_broken():
    async def exercise():
        requester = QuoteRequester(ret=604)
        provider = _provider(
            requester,
            permission_retry_seconds=300,
        )
        with pytest.raises(AllTickPermissionError, match="ret=604"):
            await provider.fetch_quotes(["600519.SH"])
        with pytest.raises(AllTickPermissionError, match="temporarily paused"):
            await provider.fetch_quotes(["600519.SH"])
        await provider.close()
        return requester

    requester = asyncio.run(exercise())
    assert len(requester.calls) == 1


def test_alltick_mixed_code_permissions_keep_authorized_quotes():
    class MixedPermissionRequester:
        def __init__(self):
            self.calls = []

        def __call__(self, method, url, params, body, timeout):
            query = json.loads(params["query"])
            codes = [item["code"] for item in query["data"]["symbol_list"]]
            self.calls.append(tuple(codes))
            if len(codes) > 1 or codes == ["600001.SH"]:
                return {"ret": 604, "msg": "denied", "data": {}}
            return {
                "ret": 200,
                "msg": "ok",
                "data": {"tick_list": [_tick(codes[0])]},
            }

    async def exercise():
        requester = MixedPermissionRequester()
        provider = _provider(
            requester,
            symbols=["600000.SH", "600001.SH", "600002.SH"],
            quote_cache_seconds=60,
            permission_retry_seconds=300,
        )
        try:
            first = await provider.fetch_quotes(
                ["600000.SH", "600001.SH", "600002.SH"]
            )
            second = await provider.fetch_quotes(
                ["600000.SH", "600001.SH", "600002.SH"]
            )
            return requester.calls, first, second, provider.realtime_status
        finally:
            await provider.close()

    calls, first, second, status = asyncio.run(exercise())
    assert calls == [
        ("600000.SH", "600001.SH", "600002.SH"),
        ("600000.SH",),
        ("600001.SH",),
        ("600002.SH",),
    ]
    assert [item.symbol for item in first] == ["600000.SH", "600002.SH"]
    assert second == first
    assert status["quote_permission_denied_symbols"] == ["600001.SH"]


def test_alltick_mixed_kline_permissions_keep_authorized_symbols():
    class MixedKlineRequester:
        def __init__(self):
            self.calls = []

        def __call__(self, method, url, params, body, timeout):
            assert url.endswith("/kline")
            query = json.loads(params["query"])
            code = query["data"]["code"]
            self.calls.append(code)
            if code == "600001.SH":
                return {"ret": 604, "msg": "denied", "data": {}}
            return {
                "ret": 200,
                "msg": "ok",
                "data": {
                    "code": code,
                    "kline_list": [_bar(_epoch(0)), _bar(_epoch(60))],
                },
            }

    async def exercise():
        requester = MixedKlineRequester()
        provider = _provider(
            requester,
            symbols=["600000.SH", "600001.SH", "600002.SH"],
            permission_retry_seconds=300,
        )
        try:
            bars = await provider.fetch_bars_many(
                ["600000.SH", "600001.SH", "600002.SH"],
                interval="1m",
                limit=2,
            )
            return requester.calls, bars, provider.realtime_status
        finally:
            await provider.close()

    calls, bars, status = asyncio.run(exercise())
    assert calls == ["600000.SH", "600001.SH", "600002.SH"]
    assert {item.symbol for item in bars} == {"600000.SH", "600002.SH"}
    assert status["bar_permission_denied_symbols"] == ["600001.SH:1m"]


def test_alltick_batch_kline_604_isolates_the_denied_symbol():
    class MixedBatchRequester:
        def __init__(self):
            self.calls = []
            self.deny_batch = False

        def __call__(self, method, url, params, body, timeout):
            if url.endswith("/kline"):
                query = json.loads(params["query"])
                code = query["data"]["code"]
                self.calls.append(("kline", code))
                return {
                    "ret": 200,
                    "data": {
                        "code": code,
                        "kline_list": [_bar(_epoch(0)), _bar(_epoch(60))],
                    },
                }
            codes = tuple(item["code"] for item in body["data"]["data_list"])
            self.calls.append(("batch",) + codes)
            if self.deny_batch and (
                len(codes) > 1 or codes == ("600001.SH",)
            ):
                return {"ret": 604, "data": {}}
            return {
                "ret": 200,
                "data": {
                    "kline_list": [
                        {
                            "code": code,
                            "kline_data": [_bar(_epoch(120), close="10.30")],
                        }
                        for code in codes
                    ]
                },
            }

    async def exercise():
        clock = [0.0]
        requester = MixedBatchRequester()
        provider = _provider(
            requester,
            symbols=["600000.SH", "600001.SH", "600002.SH"],
            bars_cache_seconds=10,
            permission_retry_seconds=300,
            monotonic=lambda: clock[0],
        )
        try:
            await provider.fetch_bars_many(
                ["600000.SH", "600001.SH", "600002.SH"], limit=2
            )
            requester.deny_batch = True
            clock[0] = 20.0
            refreshed = await provider.fetch_bars_many(
                ["600000.SH", "600001.SH", "600002.SH"], limit=2
            )
            return requester.calls, refreshed, provider.realtime_status
        finally:
            await provider.close()

    calls, refreshed, status = asyncio.run(exercise())
    assert calls[-4:] == [
        ("batch", "600000.SH", "600001.SH", "600002.SH"),
        ("batch", "600000.SH"),
        ("batch", "600001.SH"),
        ("batch", "600002.SH"),
    ]
    assert {item.symbol for item in refreshed} == {"600000.SH", "600002.SH"}
    assert status["bar_permission_denied_symbols"] == ["600001.SH:1m"]


def test_alltick_websocket_starts_even_when_http_bootstrap_is_unauthorized():
    async def exercise():
        started = asyncio.Event()
        blocker = asyncio.Event()
        provider = AllTickMarketDataProvider(
            TEST_TOKEN,
            symbols=["600519.SH"],
            requester=QuoteRequester(ret=604),
            use_websocket=True,
            use_order_book=False,
            bootstrap_daily_reference=False,
            fetch_static_info=False,
            http_min_interval_seconds=0,
            retries=0,
        )

        async def fake_runner():
            started.set()
            await blocker.wait()

        provider._websocket_runner = fake_runner
        try:
            with pytest.raises(AllTickPermissionError):
                await provider.fetch_quotes(["600519.SH"])
            await asyncio.wait_for(started.wait(), timeout=1)
            status = provider.realtime_status
            assert status["state"] == "connecting"
            assert status["subscribed_symbols"] == 0
        finally:
            await provider.close()

    asyncio.run(exercise())


def test_alltick_websocket_subscribes_tick_and_depth_on_one_connection():
    async def exercise():
        import websockets

        received = []
        pushed = asyncio.Event()

        async def handler(connection):
            tick_request = json.loads(await connection.recv())
            received.append(tick_request)
            await connection.send(
                json.dumps(
                    {
                        "ret": 200,
                        "msg": "ok",
                        "cmd_id": 22005,
                        "seq_id": tick_request["seq_id"],
                        "data": {},
                    }
                )
            )
            depth_request = json.loads(await connection.recv())
            received.append(depth_request)
            await connection.send(
                json.dumps(
                    {
                        "ret": 200,
                        "msg": "ok",
                        "cmd_id": 22003,
                        "seq_id": depth_request["seq_id"],
                        "data": {},
                    }
                )
            )
            await connection.send(
                json.dumps({"cmd_id": 22998, "data": _tick("600519.SH")})
            )
            await connection.send(
                json.dumps({"cmd_id": 22999, "data": _depth("600519.SH")})
            )
            pushed.set()
            await connection.wait_closed()

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        provider = AllTickMarketDataProvider(
            TEST_TOKEN,
            symbols=["600519.SH"],
            requester=QuoteRequester(),
            websocket_url="ws://127.0.0.1:{}".format(port),
            use_websocket=True,
            use_order_book=True,
            bootstrap_daily_reference=False,
            fetch_static_info=False,
            websocket_subscription_gap_seconds=1.0,
            websocket_reconnect_seconds=0,
            http_min_interval_seconds=0,
            retries=0,
        )
        try:
            await provider.start_realtime()
            await asyncio.wait_for(pushed.wait(), timeout=3)
            for _ in range(50):
                quote = provider._quotes.get("600519.SH")
                if quote is not None and quote.bid is not None:
                    break
                await asyncio.sleep(0.02)
            status = provider.realtime_status
            return received, quote, status
        finally:
            await provider.close()
            server.close()
            await server.wait_closed()

    received, quote, status = asyncio.run(exercise())
    assert [item["cmd_id"] for item in received] == [22004, 22002]
    assert received[1]["data"]["symbol_list"][0]["depth_level"] == 5
    assert quote.last == Decimal("123.45")
    assert quote.bid == Decimal("123.40")
    assert quote.ask == Decimal("123.50")
    assert status["subscribed_symbols"] == 1
    assert status["order_book_subscribed_symbols"] == 1


def test_alltick_depth_permission_error_keeps_tick_websocket_alive():
    async def exercise():
        import websockets

        tick_pushed = asyncio.Event()

        async def handler(connection):
            tick_request = json.loads(await connection.recv())
            await connection.send(
                json.dumps(
                    {
                        "ret": 200,
                        "cmd_id": 22005,
                        "seq_id": tick_request["seq_id"],
                        "data": {},
                    }
                )
            )
            depth_request = json.loads(await connection.recv())
            await connection.send(
                json.dumps(
                    {
                        "ret": 604,
                        "cmd_id": 22003,
                        "seq_id": depth_request["seq_id"],
                        "data": {},
                    }
                )
            )
            await connection.send(
                json.dumps({"cmd_id": 22998, "data": _tick("600519.SH")})
            )
            tick_pushed.set()
            await connection.wait_closed()

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        provider = AllTickMarketDataProvider(
            TEST_TOKEN,
            symbols=["600519.SH"],
            requester=QuoteRequester(),
            websocket_url="ws://127.0.0.1:{}".format(port),
            use_websocket=True,
            use_order_book=True,
            bootstrap_daily_reference=False,
            fetch_static_info=False,
            websocket_subscription_gap_seconds=1.0,
            websocket_reconnect_seconds=0,
            permission_retry_seconds=300,
            http_min_interval_seconds=0,
            retries=0,
        )
        try:
            await provider.start_realtime()
            await asyncio.wait_for(tick_pushed.wait(), timeout=3)
            for _ in range(50):
                if provider._quotes.get("600519.SH") is not None:
                    break
                await asyncio.sleep(0.02)
            return provider._quotes["600519.SH"], provider.realtime_status
        finally:
            await provider.close()
            server.close()
            await server.wait_closed()

    quote, status = asyncio.run(exercise())
    assert quote.last == Decimal("123.45")
    assert status["state"] == "running"
    assert status["subscribed_symbols"] == 1
    assert status["order_book_subscribed_symbols"] == 0
    assert status["order_book_realtime_error_type"] == "AllTickPermissionError"
    assert status["order_book_realtime_error_code"] == 604


def test_alltick_depth_604_recovers_authorized_websocket_subset():
    class DepthIsolationRequester:
        def __init__(self):
            self.calls = []

        def __call__(self, method, url, params, body, timeout):
            assert url.endswith("/depth-tick")
            query = json.loads(params["query"])
            codes = tuple(
                item["code"] for item in query["data"]["symbol_list"]
            )
            self.calls.append(codes)
            if len(codes) > 1 or codes == ("600001.SH",):
                return {"ret": 604, "data": {}}
            return {
                "ret": 200,
                "data": {"tick_list": [_depth(codes[0])]},
            }

    async def exercise():
        import websockets

        requester = DepthIsolationRequester()
        depth_requests = []
        connection_count = 0
        recovered = asyncio.Event()

        async def handler(connection):
            nonlocal connection_count
            connection_count += 1
            current_connection = connection_count
            tick_request = json.loads(await connection.recv())
            await connection.send(
                json.dumps(
                    {
                        "ret": 200,
                        "cmd_id": 22005,
                        "seq_id": tick_request["seq_id"],
                        "data": {},
                    }
                )
            )
            depth_request = json.loads(await connection.recv())
            codes = tuple(
                item["code"]
                for item in depth_request["data"]["symbol_list"]
            )
            depth_requests.append(codes)
            if current_connection == 1:
                await connection.send(
                    json.dumps(
                        {
                            "ret": 604,
                            "cmd_id": 22003,
                            "seq_id": depth_request["seq_id"],
                            "data": {},
                        }
                    )
                )

                async def keep_ticks_flowing():
                    sequence = 1
                    try:
                        while True:
                            await connection.send(
                                json.dumps(
                                    {
                                        "cmd_id": 22998,
                                        "data": _tick(
                                            "600000.SH",
                                            seq=sequence,
                                            second=sequence,
                                        ),
                                    }
                                )
                            )
                            sequence += 1
                            await asyncio.sleep(0.02)
                    except Exception:
                        return

                pusher = asyncio.create_task(keep_ticks_flowing())
                try:
                    await connection.wait_closed()
                finally:
                    pusher.cancel()
                    try:
                        await pusher
                    except asyncio.CancelledError:
                        pass
                return

            await connection.send(
                json.dumps(
                    {
                        "ret": 200,
                        "cmd_id": 22003,
                        "seq_id": depth_request["seq_id"],
                        "data": {},
                    }
                )
            )
            for code in codes:
                await connection.send(
                    json.dumps({"cmd_id": 22999, "data": _depth(code)})
                )
            recovered.set()
            await connection.wait_closed()

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        provider = AllTickMarketDataProvider(
            TEST_TOKEN,
            symbols=["600000.SH", "600001.SH", "600002.SH"],
            requester=requester,
            websocket_url="ws://127.0.0.1:{}".format(port),
            use_websocket=True,
            use_order_book=True,
            bootstrap_daily_reference=False,
            fetch_static_info=False,
            websocket_subscription_gap_seconds=1.0,
            websocket_reconnect_seconds=0,
            permission_retry_seconds=300,
            http_min_interval_seconds=0,
            retries=0,
        )
        try:
            await provider.start_realtime()
            await asyncio.wait_for(recovered.wait(), timeout=8)
            for _ in range(100):
                if provider.realtime_status["order_book_subscribed_symbols"] == 2:
                    break
                await asyncio.sleep(0.02)
            return requester.calls, depth_requests, provider.realtime_status
        finally:
            await provider.close()
            server.close()
            await server.wait_closed()

    http_calls, depth_requests, status = asyncio.run(exercise())
    assert http_calls == [
        ("600000.SH", "600001.SH", "600002.SH"),
        ("600000.SH",),
        ("600001.SH",),
        ("600002.SH",),
    ]
    assert depth_requests == [
        ("600000.SH", "600001.SH", "600002.SH"),
        ("600000.SH", "600002.SH"),
    ]
    assert status["subscribed_symbols"] == 3
    assert status["order_book_subscribed_symbols"] == 2
    assert status["order_book_permission_denied_symbols"] == ["600001.SH"]


def test_alltick_static_info_enriches_configured_instrument_metadata():
    class StaticRequester:
        def __init__(self):
            self.calls = []

        def __call__(self, method, url, params, body, timeout):
            self.calls.append(url)
            query = json.loads(params["query"])
            return {
                "ret": 200,
                "msg": "ok",
                "trace": query["trace"],
                "data": {
                    "static_info_list": [
                        {
                            "symbol": "600519.SH",
                            "name_cn": "贵州茅台",
                            "board": "SHEquity",
                            "currency": "CNY",
                            "lot_size": "100",
                            "eps": "60.10",
                        }
                    ]
                },
            }

    async def exercise():
        requester = StaticRequester()
        provider = _provider(
            requester,
            symbols=["600519.SH"],
            fetch_static_info=True,
        )
        instruments = await provider.fetch_instruments()
        await provider.close()
        return requester, instruments

    requester, instruments = asyncio.run(exercise())
    assert requester.calls[0].endswith("/static_info")
    assert instruments[0].name == "贵州茅台"
    assert instruments[0].metadata["currency"] == "CNY"
    assert instruments[0].metadata["eps"] == "60.10"


def test_alltick_transport_errors_never_leak_token_or_url():
    leaked = "network failed at https://quote.invalid/?token={}".format(TEST_TOKEN)
    requester = QuoteRequester(fail=RuntimeError(leaked))
    provider = _provider(requester)
    with pytest.raises(AllTickTransportError) as captured:
        asyncio.run(provider.fetch_quotes(["600519.SH"]))
    asyncio.run(provider.close())
    assert TEST_TOKEN not in str(captured.value)
    assert "https://" not in str(captured.value)


def test_alltick_configured_instrument_scope_is_explicit_and_close_is_idempotent():
    async def exercise():
        provider = _provider(
            QuoteRequester(),
            symbols=["600519.SH", "000001.SZ"],
            instrument_names={"600519.SH": "贵州茅台"},
        )
        instruments = await provider.fetch_instruments()
        await provider.close()
        await provider.close()
        return provider, instruments

    provider, instruments = asyncio.run(exercise())
    assert provider.universe_scope == "configured_symbols"
    assert [item.symbol for item in instruments] == ["600519.SH", "000001.SZ"]
    assert instruments[0].name == "贵州茅台"
    assert instruments[0].metadata["catalogue_scope"] == "configured_symbols"
