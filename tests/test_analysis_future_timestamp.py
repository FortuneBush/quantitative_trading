"""Future-dated analysis inputs must be visible and non-fresh."""

from datetime import timedelta
from decimal import Decimal

from quant_monitor.domain import Bar, parse_market_time
from quant_monitor.services.analysis import QuantitativeAnalysisService


def test_future_quote_is_explicitly_degraded():
    now = parse_market_time("2026-09-15T10:00:00+08:00")
    future = now + timedelta(minutes=5)
    quote = __import__("quant_monitor.domain", fromlist=["Quote"]).Quote(
        symbol="600519.SH", timestamp=future, last=Decimal("100"), source="fixture"
    )
    snapshot = QuantitativeAnalysisService().analyze(
        "600519.SH", [], quote, now=now
    )
    assert snapshot.stale is True
    assert snapshot.data_quality["status"] == "future_timestamp"
    assert snapshot.data_quality["future_timestamp"] is True
    assert snapshot.data_quality["future_quote"] is True


def test_small_provider_clock_skew_is_not_classified_as_future_timestamp():
    now = parse_market_time("2026-09-15T10:00:00+08:00")
    quote = __import__("quant_monitor.domain", fromlist=["Quote"]).Quote(
        symbol="600519.SH",
        timestamp=now + timedelta(seconds=2),
        last=Decimal("100"),
        source="fixture",
    )

    snapshot = QuantitativeAnalysisService().analyze(
        "600519.SH", [], quote, now=now
    )

    assert snapshot.stale is False
    assert snapshot.data_quality["future_timestamp"] is False
