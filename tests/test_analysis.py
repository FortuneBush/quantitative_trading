"""Offline tests for the explainable quantitative analysis layer and API."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from quant_monitor.app import create_app
from quant_monitor.config import Settings
from quant_monitor.data_sources import AkShareDataError
from quant_monitor.domain import Bar, Quote, parse_market_time
from quant_monitor.services import QuantitativeAnalysisService
from quant_monitor.trading_calendar import VersionedSessionCalendar


def _bars(count: int, *, descending: bool = False):
    # Keep the synthetic history before the quote/as-of times used below;
    # analysis now correctly rejects future-dated bars instead of treating
    # them as fresh research evidence.
    start = parse_market_time("2026-05-24T09:30:00+08:00")
    values = []
    for index in range(count):
        # A deterministic rising series makes trend and momentum assertions
        # independent of the mock provider's clock.
        close = Decimal("100") + Decimal(index) * Decimal("0.5")
        values.append(
            Bar(
                symbol="600519.SH",
                interval="1d",
                timestamp=start + timedelta(days=index),
                open=close - Decimal("0.2"),
                high=close + Decimal("0.3"),
                low=close - Decimal("0.4"),
                close=close,
                volume=100_000 + index * 1_000,
                turnover=close * Decimal(100_000 + index * 1_000),
                source="mock",
            )
        )
    return list(reversed(values)) if descending else values


def test_analysis_snapshot_is_explainable_and_sorted():
    service = QuantitativeAnalysisService()
    quote = Quote(
        symbol="600519.SH",
        timestamp=parse_market_time("2026-08-12T15:00:00+08:00"),
        last=Decimal("140"),
        previous_close=Decimal("139"),
        source="mock",
    )
    snapshot = service.analyze(
        "600519.SH",
        _bars(80, descending=True),
        quote,
        now=parse_market_time("2026-08-12T15:01:00+08:00"),
    )

    assert snapshot.symbol == "600519.SH"
    assert snapshot.bars_used == 80
    assert snapshot.price == Decimal("140")
    assert snapshot.change_pct is not None
    assert snapshot.indicators["sma_5"] is not None
    assert snapshot.indicators["sma_20"] is not None
    assert snapshot.indicators["rsi_14"] == 100.0
    assert snapshot.trend["label"] == "bullish"
    assert snapshot.momentum["label"] in {"positive", "strong_positive"}
    assert snapshot.risk["label"] in {"low", "medium"}
    assert snapshot.recommendation["signal_only"] is True
    assert snapshot.data_quality["status"] == "ok"


def test_analysis_returns_null_for_unready_indicators():
    snapshot = QuantitativeAnalysisService().analyze("600519.SH", _bars(5))

    assert snapshot.data_quality["status"] == "insufficient"
    assert snapshot.indicators["sma_5"] is not None
    assert snapshot.indicators["sma_20"] is None
    assert snapshot.indicators["rsi_14"] is None
    assert snapshot.indicators["volatility_20"] is None
    assert snapshot.trend["score"] is None
    assert snapshot.momentum["score"] is None
    assert snapshot.risk["score"] is None
    assert snapshot.recommendation["action"] == "insufficient_data"
    assert "sma_20" in snapshot.data_quality["missing_indicators"]


def test_flat_series_is_neutral_instead_of_bullish_by_tie_breaking():
    flat = _bars(80)
    # Replace closes while preserving valid OHLC relationships.
    flat = [
        Bar(
            symbol=item.symbol,
            interval=item.interval,
            timestamp=item.timestamp,
            open=Decimal("100"),
            high=Decimal("100.10"),
            low=Decimal("99.90"),
            close=Decimal("100"),
            volume=item.volume,
            turnover=Decimal("10000000"),
            source=item.source,
        )
        for item in flat
    ]
    snapshot = QuantitativeAnalysisService().analyze("600519.SH", flat)
    assert snapshot.trend["label"] == "neutral"
    assert snapshot.trend["score"] == 0.0


def test_zero_prior_close_does_not_create_fake_volatility():
    bars = _bars(25)
    first = bars[0]
    bars[0] = Bar(
        symbol=first.symbol,
        interval=first.interval,
        timestamp=first.timestamp,
        open=Decimal("0"),
        high=Decimal("0"),
        low=Decimal("0"),
        close=Decimal("0"),
        volume=first.volume,
        turnover=Decimal("0"),
        source=first.source,
    )
    snapshot = QuantitativeAnalysisService().analyze("600519.SH", bars)
    assert snapshot.indicators["volatility_20"] is None
    assert snapshot.indicators["annualized_volatility_20"] is None
    assert snapshot.data_quality["status"] == "insufficient"


def test_empty_analysis_preserves_requested_interval_and_quality():
    snapshot = QuantitativeAnalysisService().analyze(
        "600519.SH", [], interval="1d"
    )
    assert snapshot.interval == "1d"
    assert snapshot.price is None
    assert snapshot.data_quality["status"] == "insufficient"
    assert snapshot.trend["score"] is None


def test_analysis_rejects_quote_for_another_symbol():
    quote = Quote(
        symbol="600036.SH",
        timestamp=parse_market_time("2026-08-12T10:00:00+08:00"),
        last=Decimal("10"),
        source="mock",
    )
    with pytest.raises(ValueError, match="quote must match"):
        QuantitativeAnalysisService().analyze("600519.SH", [], quote)


def test_daily_analysis_uses_current_quote_for_freshness():
    quote_time = parse_market_time("2026-08-12T15:00:00+08:00")
    quote = Quote(
        symbol="600519.SH",
        timestamp=quote_time,
        last=Decimal("140"),
        previous_close=Decimal("139"),
        source="mock",
    )
    snapshot = QuantitativeAnalysisService(stale_after_seconds=60).analyze(
        "600519.SH",
        _bars(80),
        quote,
        now=quote_time + timedelta(seconds=30),
    )
    assert snapshot.stale is False

    historical = QuantitativeAnalysisService(stale_after_seconds=60).analyze(
        "600519.SH",
        _bars(80),
        now=parse_market_time("2026-11-03T10:00:00+08:00"),
    )
    assert historical.stale is True


def test_fresh_quote_does_not_hide_stale_daily_history():
    quote_time = parse_market_time("2026-11-03T10:00:00+08:00")
    quote = Quote(
        symbol="600519.SH",
        timestamp=quote_time,
        last=Decimal("140"),
        previous_close=Decimal("139"),
        source="mock",
    )

    snapshot = QuantitativeAnalysisService(stale_after_seconds=60).analyze(
        "600519.SH",
        _bars(80),
        quote,
        now=quote_time + timedelta(seconds=30),
    )

    assert snapshot.stale is True
    assert snapshot.data_quality["status"] == "stale"
    assert snapshot.data_quality["quote_stale"] is False
    assert snapshot.data_quality["bar_stale"] is True


def test_daily_analysis_uses_injected_calendar_and_discloses_its_identity():
    # This is a synthetic weekday closure, not an exchange holiday claim.
    calendar = VersionedSessionCalendar.from_weekday_rules(
        date(2026, 5, 1), date(2026, 8, 31),
        holidays=(date(2026, 8, 12),), source="offline-fixture", version="1",
    )
    bars = _bars(80)
    bars[-1] = replace(
        bars[-1], timestamp=parse_market_time("2026-08-11T15:00:00+08:00")
    )
    now = parse_market_time("2026-08-12T16:00:00+08:00")

    snapshot = QuantitativeAnalysisService(calendar=calendar).analyze(
        "600519.SH", bars, now=now
    )
    fallback = QuantitativeAnalysisService().analyze("600519.SH", bars, now=now)

    assert snapshot.data_quality["status"] == "ok"
    assert snapshot.stale is False
    assert snapshot.data_quality["expected_bar_date"] == "2026-08-11"
    assert snapshot.data_quality["calendar_source"] == "offline-fixture"
    assert snapshot.data_quality["calendar_fingerprint"] == calendar.fingerprint
    assert snapshot.data_quality["calendar_verified"] is False
    assert snapshot.data_quality["calendar_out_of_coverage"] is False
    assert fallback.data_quality["bar_stale"] is True
    assert fallback.data_quality["calendar"] == "weekday_fallback"
    assert fallback.data_quality["calendar_verified"] is False


def test_analysis_calendar_coverage_failure_does_not_fall_back_or_recommend():
    calendar = VersionedSessionCalendar.from_weekday_rules(
        date(2026, 5, 1), date(2026, 8, 14),
        source="offline-fixture", version="1",
    )
    service = QuantitativeAnalysisService(calendar=calendar)
    now = parse_market_time("2026-08-17T16:00:00+08:00")

    snapshot = service.analyze("600519.SH", _bars(80), now=now)

    assert snapshot.stale is True
    assert snapshot.data_quality["status"] == "calendar_out_of_coverage"
    assert snapshot.data_quality["expected_bar_date"] is None
    assert snapshot.data_quality["calendar_out_of_coverage"] is True
    assert snapshot.data_quality["calendar_fingerprint"] == calendar.fingerprint
    assert snapshot.recommendation["action"] == "insufficient_data"
    assert snapshot.recommendation["label"] == "交易日历覆盖不足"

    # Intraday age checks do not depend on a session-date snapshot.
    intraday = replace(_bars(1)[0], interval="1m", timestamp=now)
    snapshot = service.analyze("600519.SH", [intraday], now=now)
    assert snapshot.stale is False
    assert "calendar_out_of_coverage" not in snapshot.data_quality


def test_analysis_rejects_non_calendar_injection():
    with pytest.raises(TypeError, match="calendar must implement TradingCalendar"):
        QuantitativeAnalysisService(calendar=object())


def test_analysis_api_supports_single_and_batch_mock_requests(tmp_path):
    settings = Settings(
        database_path=str(tmp_path / "analysis.sqlite3"),
        data_provider="mock",
        poll_interval_seconds=60.0,
        watchlist=["600519.SH", "600036.SH"],
        history_bar_count=20,
    )
    with TestClient(create_app(settings)) as client:
        single = client.get(
            "/api/v1/analysis/600519.SH",
            params={"interval": "1d", "limit": 30},
        )
        assert single.status_code == 200
        body = single.json()
        assert body["symbol"] == "600519.SH"
        assert body["interval"] == "1d"
        assert body["bars_used"] == 30
        assert body["source"] == "mock"
        assert "trend" in body and "risk" in body
        assert body["recommendation"]["signal_only"] is True
        assert body["data_provenance"]["authoritative"] is False
        assert body["data_quality"]["observation_authority"] == "legacy_bars_projection"
        assert body["data_quality"]["decision_ready"] is False
        assert body["data_quality"]["decision_ready_reason"] == (
            "immutable_observation_lineage_incomplete"
        )
        assert body["research_action"] == "insufficient_data"
        assert body["recommendation"]["action"] == "insufficient_data"
        assert body["recommendation"]["model_action"] is not None

        batch = client.get(
            "/api/v1/analysis",
            params={"symbols": "600519.SH,600036.SH", "interval": "1d", "limit": 5},
        )
        assert batch.status_code == 200
        batch_body = batch.json()
        assert batch_body["count"] == 2
        assert {item["symbol"] for item in batch_body["items"]} == {
            "600519.SH",
            "600036.SH",
        }
        assert all(item["data_quality"]["status"] == "insufficient" for item in batch_body["items"])


def test_analysis_uses_cached_partial_history_when_provider_is_temporarily_down(tmp_path):
    settings = Settings(
        database_path=str(tmp_path / "analysis-outage.sqlite3"),
        data_provider="mock",
        poll_interval_seconds=60.0,
        bar_interval="1m",
        watchlist=["600519.SH"],
        history_bar_count=20,
    )
    with TestClient(create_app(settings)) as client:
        async def fail(*args, **kwargs):
            raise AkShareDataError("temporary upstream outage")

        client.app.state.runtime.provider.fetch_bars = fail
        response = client.get(
            "/api/v1/analysis/600519.SH",
            params={"interval": "1m", "limit": 240},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["bars_used"] == 20
        assert body["data_quality"]["status"] == "insufficient"


def test_analysis_fetches_current_quote_for_symbol_outside_watchlist(tmp_path):
    settings = Settings(
        database_path=str(tmp_path / "analysis-non-watchlist.sqlite3"),
        data_provider="mock",
        poll_interval_seconds=60.0,
        watchlist=["600519.SH"],
        history_bar_count=20,
    )
    with TestClient(create_app(settings)) as client:
        response = client.get(
            "/api/v1/analysis/688981.SH",
            params={"interval": "1d", "limit": 10},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["symbol"] == "688981.SH"
        assert body["price"] is not None
        assert body["source"] == "mock"
