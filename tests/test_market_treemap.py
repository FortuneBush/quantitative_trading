"""Offline tests for the industry → stock treemap contract."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from quant_monitor.app import create_app
from quant_monitor.config import Settings
from quant_monitor.domain import Exchange, Instrument, Quote, SecurityType, SHANGHAI_TZ
from quant_monitor.market_treemap import build_market_treemap


def _instrument(symbol: str, name: str, industry: str = "银行") -> Instrument:
    return Instrument(
        symbol=symbol,
        name=name,
        exchange=Exchange(symbol.rsplit(".", 1)[1]),
        metadata={"industry": industry, "source": "mock"},
    )


def _quote(symbol: str, last: str, previous: str, turnover: str, stamp: datetime) -> Quote:
    return Quote(
        symbol=symbol,
        timestamp=stamp,
        last=Decimal(last),
        previous_close=Decimal(previous),
        volume=100,
        turnover=Decimal(turnover),
        source="mock",
    )


def test_treemap_groups_industry_and_uses_newest_quote():
    stamp = datetime(2026, 9, 18, 15, tzinfo=SHANGHAI_TZ)
    instruments = [
        _instrument("600000.SH", "浦发银行"),
        _instrument("000001.SZ", "平安银行"),
        _instrument("300750.SZ", "宁德时代", "电力设备"),
    ]
    quotes = [
        _quote("600000.SH", "10", "9", "1000", stamp),
        _quote("600000.SH", "11", "9", "2000", stamp + timedelta(seconds=1)),
        _quote("000001.SZ", "8", "9", "500", stamp),
        _quote("300750.SZ", "50", "50", "300", stamp),
    ]
    payload = build_market_treemap(instruments, quotes, source="mock")
    assert payload["source"] == "mock"
    assert payload["upstream_requests"] == 0
    assert {item["name"] for item in payload["sectors"]} == {"银行", "电力设备"}
    banks = next(item for item in payload["sectors"] if item["name"] == "银行")
    assert [item["symbol"] for item in banks["children"]] == ["600000.SH", "000001.SZ"]
    assert banks["children"][0]["last"] == "11"
    assert banks["children"][0]["size_basis"] == "turnover"
    assert banks["children"][0]["pct_change"] == pytest.approx(22.222222, rel=1e-6)
    assert banks["decliners"] == 1


def test_treemap_keeps_missing_quotes_out_of_tiles_and_reports_coverage():
    stamp = datetime(2026, 9, 18, 15, tzinfo=SHANGHAI_TZ)
    instruments = [
        _instrument("600000.SH", "浦发银行"),
        _instrument("000001.SZ", "平安银行"),
    ]
    payload = build_market_treemap(
        instruments,
        [_quote("600000.SH", "10", "10", "0", stamp)],
    )
    assert payload["universe_count"] == 2
    assert payload["priced_count"] == 1
    assert payload["unpriced_count"] == 1
    assert payload["visible_stock_count"] == 1
    assert payload["items"][0]["children"][0]["size_basis"] == "volume_times_price"


def test_market_treemap_route_is_cache_only(tmp_path):
    settings = Settings(
        app_name="treemap fixture",
        database_path=str(tmp_path / "treemap.sqlite3"),
        data_provider="mock",
        watchlist=["600519.SH", "000001.SZ"],
        poll_interval_seconds=60.0,
        history_bar_count=25,
        mock_seed=20260812,
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/market/treemap")
        assert response.status_code == 200
        body = response.json()
        assert body["upstream_requests"] == 0
        assert body["served_from"] == "sqlite_cache"
        assert body["size_semantics"].startswith("turnover_preferred")
        assert "sectors" in body and "items" in body
        assert body["provider"] == "mock"


def test_empty_cache_reports_explicit_zero_coverage():
    payload = build_market_treemap([], [])
    assert payload["items"] == []
    assert payload["sectors"] == []
    assert payload["count"] == 0
    assert payload["universe_count"] == 0
    assert payload["priced_count"] == 0
    assert payload["unpriced_count"] == 0
    assert payload["visible_stock_count"] == 0
    assert payload["as_of"] is None
    assert payload["upstream_requests"] == 0


def test_treemap_excludes_inactive_and_non_stock_instruments():
    stamp = datetime(2026, 9, 18, 15, tzinfo=SHANGHAI_TZ)
    instruments = [
        _instrument("600000.SH", "甲", "银行"),
        Instrument("600001.SH", "停牌", Exchange.SH, active=False, metadata={"industry": "银行"}),
        Instrument("510000.SH", "ETF", Exchange.SH, SecurityType.ETF, metadata={"industry": "基金"}),
        _instrument("000001.SZ", "无行情", "银行"),
    ]
    payload = build_market_treemap(
        instruments, [_quote("600000.SH", "10", "9", "100", stamp)]
    )
    assert payload["universe_count"] == 2
    assert payload["priced_count"] == 1
    assert payload["unpriced_count"] == 1
    assert [item["symbol"] for item in payload["items"][0]["children"]] == ["600000.SH"]


def test_missing_previous_close_and_zero_size_do_not_become_fake_change():
    stamp = datetime(2026, 9, 18, 15, tzinfo=SHANGHAI_TZ)
    instrument = _instrument("600000.SH", "甲", "银行")
    quote = Quote(
        symbol="600000.SH",
        timestamp=stamp,
        last=Decimal("10"),
        previous_close=None,
        volume=0,
        turnover=Decimal("0"),
        source="mock",
    )
    payload = build_market_treemap([instrument], [quote])
    child = payload["items"][0]["children"][0]
    assert child["pct_change"] is None
    assert child["change_pct"] is None
    assert child["size"] == 1.0
    assert child["size_basis"] == "placeholder"
    assert payload["items"][0]["pct_change"] is None


def test_industry_limit_is_deterministic_and_reports_omitted_groups():
    stamp = datetime(2026, 9, 18, 15, tzinfo=SHANGHAI_TZ)
    instruments = [
        _instrument("600000.SH", "甲", "银行"),
        _instrument("000001.SZ", "乙", "电子"),
    ]
    quotes = [
        _quote("600000.SH", "10", "9", "100", stamp),
        _quote("000001.SZ", "10", "9", "200", stamp),
    ]
    payload = build_market_treemap(instruments, quotes, max_industries=1)
    assert payload["count"] == 1
    assert payload["omitted_industry_count"] == 1
    assert payload["items"][0]["name"] == "电子"

