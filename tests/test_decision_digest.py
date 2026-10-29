"""Offline contract tests for the cache-only decision dashboard digest."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from quant_monitor.app import create_app
from quant_monitor.config import Settings
from quant_monitor.domain import (
    Alert,
    AlertLevel,
    Quote,
    SHANGHAI_TZ,
    Signal,
    SignalAction,
)
from quant_monitor.services.decision_digest import (
    build_decision_digest,
    render_decision_digest_markdown,
)


REFERENCE = datetime(2026, 10, 27, 15, 0, tzinfo=SHANGHAI_TZ)


def quote(symbol: str, timestamp: datetime, last: str, previous: Optional[str]) -> Quote:
    return Quote(
        symbol=symbol,
        timestamp=timestamp,
        last=Decimal(last),
        previous_close=Decimal(previous) if previous is not None else None,
        source="fixture",
    )


def signal(symbol: str, timestamp: datetime, action: SignalAction, strength: float, *, ident: int) -> Signal:
    return Signal(
        id=ident,
        strategy="fixture_strategy",
        symbol=symbol,
        timestamp=timestamp,
        action=action,
        strength=strength,
        price=Decimal("10.00"),
        reason="fixture",
    )


def alert(symbol: str, timestamp: datetime, level: AlertLevel, *, ident: int, acknowledged: bool = False) -> Alert:
    return Alert(
        id=ident,
        symbol=symbol,
        timestamp=timestamp,
        level=level,
        title="fixture alert",
        message="check the cached evidence",
        acknowledged=acknowledged,
    )


def test_digest_is_deterministic_and_keeps_latest_signal_per_symbol():
    quotes = [
        quote("600001.SH", REFERENCE - timedelta(minutes=1), "11", "10"),
        quote("000001.SZ", REFERENCE - timedelta(hours=1), "9", "10"),
        quote("000002.SZ", REFERENCE + timedelta(seconds=10), "8", None),
    ]
    signals = [
        signal("600001.SH", REFERENCE - timedelta(minutes=2), SignalAction.SELL, -0.5, ident=1),
        signal("600001.SH", REFERENCE - timedelta(seconds=30), SignalAction.BUY, 0.8, ident=2),
        signal("000001.SZ", REFERENCE - timedelta(minutes=3), SignalAction.SELL, -0.7, ident=3),
    ]
    alerts = [
        alert("600001.SH", REFERENCE - timedelta(minutes=1), AlertLevel.WARNING, ident=2),
        alert("000001.SZ", REFERENCE - timedelta(minutes=2), AlertLevel.INFO, ident=1, acknowledged=True),
    ]
    kwargs = dict(
        configured_symbols=("000003.SZ", "000002.SZ", "600001.SH", "000001.SZ"),
        instrument_names={"600001.SH": "Alpha", "000001.SZ": "Beta"},
        provider="fixture",
        generated_at=REFERENCE,
        as_of=REFERENCE,
        stale_after_seconds=300,
        data_readiness={"ready": True},
        research_readiness={"ready": False, "reason": "fixture_unverified"},
    )

    first = build_decision_digest(quotes, signals, alerts, **kwargs)
    second = build_decision_digest(
        list(reversed(quotes)),
        list(reversed(signals)),
        list(reversed(alerts)),
        **kwargs,
    )

    assert first == second
    assert first["schema_version"] == "decision_digest_v1"
    assert [row["symbol"] for row in first["items"]] == [
        "000001.SZ",
        "000002.SZ",
        "000003.SZ",
        "600001.SH",
    ]
    assert first["summary"] == {
        "configured_symbols": 4,
        "quoted_symbols": 3,
        "missing_quotes": 1,
        "advancers": 1,
        "decliners": 1,
        "unchanged": 0,
        "unknown_change": 2,
        "signal_counts": {"buy": 1, "hold": 0, "sell": 1, "unknown": 0},
        "alert_counts": {"info": 1, "warning": 1, "critical": 0, "unknown": 0},
        "warning_count": 5,
    }
    assert first["data_quality"]["freshness_counts"] == {
        "fresh": 1,
        "stale": 1,
        "future_timestamp": 1,
        "unknown": 1,
        "untracked": 0,
    }
    assert first["data_quality"]["missing_symbols"] == ["000003.SZ"]
    assert "stale_quotes_present" in first["warnings"]
    assert "future_quote_timestamp_present" in first["warnings"]
    assert "configured_symbols_missing_quotes" in first["warnings"]
    assert "unacknowledged_alerts_present" in first["warnings"]
    assert "research_history_not_ready" in first["warnings"]
    assert first["items"][1]["pct_change"] is None
    assert first["items"][1]["price"] == "8"
    assert first["items"][3]["signal"]["action"] == "buy"
    assert first["research_assurance"]["research_only"] is True
    assert first["research_assurance"]["orders_supported"] is False
    assert first["research_only"] is True
    assert first["orders_supported"] is False


def test_digest_has_explicit_empty_cache_and_markdown_notice():
    payload = build_decision_digest(
        (),
        configured_symbols=(),
        provider="mock",
        generated_at=REFERENCE,
        as_of=REFERENCE,
        research_readiness=None,
    )

    assert payload["as_of"] is None
    assert payload["items"] == []
    assert payload["summary"]["missing_quotes"] == 0
    assert payload["data_quality"]["ready"] is False
    assert "empty_dashboard_cache" in payload["warnings"]
    assert "no_persisted_signals" in payload["warnings"]
    assert payload["research_assurance"] == {
        "ready": False,
        "reason": "research_evidence_is_not_certified",
        "research_only": True,
        "orders_supported": False,
        "decision_ready": False,
        "decision_ready_reason": "dashboard_aggregation_not_certified",
    }

    markdown = render_decision_digest_markdown(payload)
    assert "# 量化研究决策日报" in markdown
    assert "暂无本地缓存行情。" in markdown
    assert "仅研究信号，不支持下单" in markdown
    assert "empty_dashboard_cache" in markdown


def test_digest_limits_rows_without_losing_explicit_unknown_values():
    payload = build_decision_digest(
        [
            quote("000001.SZ", REFERENCE, "10", "10"),
            quote("000002.SZ", REFERENCE, "11", "10"),
            quote("000003.SZ", REFERENCE, "9", "10"),
        ],
        configured_symbols=("000001.SZ", "000002.SZ", "000003.SZ"),
        generated_at=REFERENCE,
        as_of=REFERENCE,
        stale_after_seconds=60,
        limit=1,
    )

    assert len(payload["items"]) == 1
    assert len(payload["alerts"]) == 0
    assert len(payload["rankings"]["top_gainers"]) == 3
    assert payload["summary"]["unknown_change"] == 0
    assert payload["summary"]["signal_counts"] == {
        "buy": 0,
        "hold": 0,
        "sell": 0,
        "unknown": 0,
    }


def test_equal_timestamp_quote_replays_use_a_canonical_tie_breaker():
    first = build_decision_digest(
        [
            quote("600001.SH", REFERENCE, "10", "10"),
            quote("600001.SH", REFERENCE, "11", "10"),
        ],
        configured_symbols=("600001.SH",),
        generated_at=REFERENCE,
        as_of=REFERENCE,
    )
    second = build_decision_digest(
        [
            quote("600001.SH", REFERENCE, "11", "10"),
            quote("600001.SH", REFERENCE, "10", "10"),
        ],
        configured_symbols=("600001.SH",),
        generated_at=REFERENCE,
        as_of=REFERENCE,
    )

    assert first["items"] == second["items"]
    assert first["items"][0]["price"] == "11"


@pytest.fixture
def digest_client(tmp_path):
    settings = Settings(
        app_name="Decision digest contract fixture",
        database_path=str(tmp_path / "decision-digest.sqlite3"),
        data_provider="mock",
        poll_interval_seconds=3600.0,
        history_bar_count=25,
        watchlist=["600519.SH", "000001.SZ"],
        mock_seed=20260812,
    )
    with TestClient(create_app(settings)) as client:
        yield client


def test_daily_dashboard_and_alias_are_cache_only(digest_client, monkeypatch):
    runtime = digest_client.app.state.runtime

    async def fail_provider_io(*args, **kwargs):
        raise AssertionError("decision dashboard must not perform provider I/O")

    for method in ("fetch_quotes", "fetch_bars", "fetch_instruments"):
        monkeypatch.setattr(runtime.provider, method, fail_provider_io)

    primary = digest_client.get("/api/v1/dashboard/daily", params={"limit": 1})
    alias = digest_client.get("/api/v1/decision-digest", params={"limit": 1})
    assert primary.status_code == alias.status_code == 200
    first = primary.json()
    second = alias.json()
    assert first["schema_version"] == "decision_digest_v1"
    assert first["provider"] == "mock"
    assert len(first["items"]) <= 1
    assert first["research_only"] is True
    assert first["orders_supported"] is False
    assert first["research_assurance"]["research_only"] is True
    assert first["research_assurance"]["orders_supported"] is False
    assert first["summary"] == second["summary"]
    stable_item_keys = ("symbol", "name", "price", "pct_change", "timestamp", "freshness", "signal")
    assert [
        tuple(item.get(key) for key in stable_item_keys) for item in first["items"]
    ] == [
        tuple(item.get(key) for key in stable_item_keys) for item in second["items"]
    ]
    assert "api_key" not in primary.text.lower()


def test_daily_dashboard_supports_bounded_markdown_and_rejects_unknown_format(digest_client):
    markdown = digest_client.get(
        "/api/v1/dashboard/daily", params={"format": "markdown", "limit": 1}
    )
    assert markdown.status_code == 200
    assert "text/markdown" in markdown.headers["content-type"]
    assert markdown.text.startswith("# 量化研究决策日报")
    assert "仅研究信号，不支持下单" in markdown.text

    invalid = digest_client.get("/api/v1/dashboard/daily", params={"format": "html"})
    assert invalid.status_code == 422
    assert digest_client.get("/api/v1/dashboard/daily", params={"limit": 0}).status_code == 422


def test_daily_dashboard_exposes_missing_cache_without_fabricating_prices(digest_client, monkeypatch):
    runtime = digest_client.app.state.runtime
    runtime.repository._connection.execute("DELETE FROM quotes")
    runtime.repository._connection.commit()

    async def fail_provider_io(*args, **kwargs):
        raise AssertionError("empty dashboard must remain cache-only")

    monkeypatch.setattr(runtime.provider, "fetch_quotes", fail_provider_io)
    payload = digest_client.get("/api/v1/dashboard/daily").json()
    assert payload["as_of"] is None
    assert payload["summary"]["quoted_symbols"] == 0
    assert payload["summary"]["missing_quotes"] == 2
    assert payload["data_quality"]["missing_symbols"] == ["000001.SZ", "600519.SH"]
    assert all(item["price"] is None for item in payload["items"])
    assert "configured_symbols_missing_quotes" in payload["warnings"]
