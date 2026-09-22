from datetime import timedelta
from decimal import Decimal

from quant_monitor.domain import Quote, parse_market_time
from quant_monitor.serialization import quote_dict


def test_future_quote_is_not_serialized_as_fresh(monkeypatch):
    now = parse_market_time("2026-08-28T10:00:00+08:00")
    monkeypatch.setattr("quant_monitor.serialization.now_market_time", lambda: now)
    quote = Quote(
        symbol="600519.SH",
        timestamp=now + timedelta(seconds=30),
        last=Decimal("1500"),
        previous_close=Decimal("1490"),
        source="asharehub",
    )

    payload = quote_dict(quote, stale_after_seconds=300)

    assert payload["stale"] is True
    assert payload["freshness"] == "future_timestamp"
    assert payload["future_timestamp"] is True
    assert payload["future_by_seconds"] == 30.0
    # Preserve the historical non-negative age contract while exposing the
    # clock anomaly through explicit fields above.
    assert payload["data_age_seconds"] == 0.0
