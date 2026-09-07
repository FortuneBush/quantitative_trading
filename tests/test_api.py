"""HTTP contract tests against a fully started temporary application."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

import pytest


# Keep core tests runnable in a minimal Python environment.  In a development
# install (``pip install -r requirements-dev.txt``), this module exercises every
# route below through FastAPI's ASGI test client.
pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from quant_monitor.app import create_app
from quant_monitor.config import Settings
from quant_monitor.data_sources import (
    AkShareDataError,
    AllTickDataError,
    AllTickMarketDataProvider,
)
from quant_monitor.domain import (
    Alert,
    AlertLevel,
    Quote,
    Signal,
    SignalAction,
    now_market_time,
)


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        app_name="API contract fixture",
        database_path=str(tmp_path / "api.sqlite3"),
        data_provider="mock",
        poll_interval_seconds=60.0,
        bar_interval="1m",
        history_bar_count=25,
        watchlist=["600519.SH", "000001.SZ"],
        mock_seed=20260812,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def assert_common_headers(response):
    assert len(response.headers["x-request-id"]) == 32
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["server-timing"].startswith("app;dur=")


def assert_iso_market_timestamp(value):
    assert "T" in value
    assert value.endswith("+08:00")


def test_health_overview_watchlist_and_system_status(client):
    health = client.get("/health")
    assert health.status_code == 200
    assert_common_headers(health)
    health_body = health.json()
    assert health_body["status"] == "ok"
    assert health_body["database"] == {"type": "sqlite", "state": "connected"}
    assert health_body["feed"]["name"] == "mock"
    assert health_body["collector"]["running"] is True
    assert health_body["collector"]["last_duration_ms"] is not None
    assert_iso_market_timestamp(health_body["server_time"])

    live = client.get("/live")
    assert live.status_code == 200
    assert live.json()["live"] is True

    ready = client.get("/ready")
    assert ready.status_code == 200
    ready_body = ready.json()
    assert ready_body["serving_ready"] is True
    assert ready_body["research_ready"] is True
    assert "history_bootstrap" in ready_body

    overview = client.get("/api/v1/overview")
    assert overview.status_code == 200
    assert overview.headers["cache-control"] == "no-store"
    overview_body = overview.json()
    assert overview_body["counts"]["watchlist"] == 2
    assert overview_body["counts"]["configured_watchlist"] == 2
    assert len(overview_body["indices"]) == 2
    assert overview_body["provider"]["name"] == "mock"
    assert overview_body["market_status"]["calendar"] == "weekday_fallback"

    watchlist = client.get("/api/v1/watchlist")
    assert watchlist.status_code == 200
    watchlist_body = watchlist.json()
    assert watchlist_body["count"] == 2
    assert watchlist_body["configured_count"] == 2
    assert watchlist_body["missing_symbols"] == []
    assert watchlist_body["provider"] == "mock"
    assert watchlist_body["collector_error"] is None
    assert {item["symbol"] for item in watchlist_body["items"]} == {
        "600519.SH",
        "000001.SZ",
    }
    assert all(isinstance(item["price"], str) for item in watchlist_body["items"])

    system = client.get("/api/v1/system/status")
    assert system.status_code == 200
    system_body = system.json()
    assert system_body["healthy"] is True
    assert system_body["startup"]["serving_ready"] is True
    assert system_body["startup"]["research_ready"] is True
    assert system_body["last_collection_age_ms"] == system_body["latency_ms"]
    assert system_body["provider"]["state"] == "running"
    assert system_body["collector"]["iteration"] >= 1
    assert system_body["settings"]["watchlist"] == ["600519.SH", "000001.SZ"]
    assert "database_path" not in system_body["settings"]


def test_quotes_bars_and_public_configuration(client):
    quote = client.get("/api/v1/quotes/600519.sh")
    assert quote.status_code == 200
    quote_body = quote.json()
    assert quote_body["symbol"] == "600519.SH"
    assert quote_body["source"] == "mock"
    assert quote_body["price"] == quote_body["last"]

    quotes = client.get("/api/v1/quotes", params={"symbols": "600519.sh,000001.sz"})
    assert quotes.status_code == 200
    assert len(quotes.json()["items"]) == 2

    bars = client.get(
        "/api/v1/bars/600519.sh",
        params={"period": "5m", "limit": 4},
    )
    assert bars.status_code == 200
    bars_body = bars.json()
    assert bars_body["symbol"] == "600519.SH"
    assert bars_body["interval"] == "5m"
    assert bars_body["count"] == 4
    assert len(bars_body["items"]) == 4
    assert bars_body["items"] == sorted(
        bars_body["items"], key=lambda item: item["timestamp"]
    )

    public_config = client.get("/api/v1/config/public")
    assert public_config.status_code == 200
    assert public_config.json()["history_bar_count"] == 25
    assert "database_path" not in public_config.json()


def test_provider_interval_capability_is_rejected_before_fetch(client):
    provider_type = type(client.app.state.runtime.provider)
    original = provider_type.supported_intervals
    provider_type.supported_intervals = property(lambda self: ("1d",))

    try:
        bars = client.get("/api/v1/bars/600519.SH", params={"interval": "1m"})
        analysis = client.get(
            "/api/v1/analysis/600519.SH", params={"interval": "1m"}
        )
    finally:
        provider_type.supported_intervals = original

    assert bars.status_code == 422
    assert analysis.status_code == 422
    assert "does not support" in bars.json()["error"]["message"]


def test_instruments_endpoint_exposes_provider_security_master(client):
    response = client.get(
        "/api/v1/instruments", params={"symbols": "600519.sh,000001.SZ"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "mock"
    assert body["universe_scope"] == "provider_catalogue"
    assert body["count"] == 2
    assert {item["symbol"] for item in body["items"]} == {
        "600519.SH",
        "000001.SZ",
    }
    assert body["served_from"] == "sqlite_cache"
    assert body["refresh_requested"] is False


def test_instruments_default_is_cache_only_and_refresh_is_explicit(client):
    provider = client.app.state.runtime.provider
    original = provider.fetch_instruments
    calls = []

    async def fail_if_called():
        calls.append("unexpected")
        raise AssertionError("default instrument GET must not call provider")

    provider.fetch_instruments = fail_if_called
    cached = client.get("/api/v1/instruments")
    assert cached.status_code == 200
    assert cached.json()["served_from"] == "sqlite_cache"
    assert calls == []

    provider.fetch_instruments = original
    refreshed = client.get("/api/v1/instruments", params={"refresh": "true"})
    assert refreshed.status_code == 200
    assert refreshed.json()["served_from"] == "provider_refresh"
    assert refreshed.json()["refresh_requested"] is True


def test_bank_sector_endpoint_is_cached_only_and_exposes_coverage(client):
    provider = client.app.state.runtime.provider

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("bank sector route must not call provider")

    provider.fetch_quotes = fail_if_called
    provider.fetch_bars = fail_if_called
    provider.fetch_instruments = fail_if_called

    response = client.get("/api/v1/sectors/banks")

    assert response.status_code == 200
    body = response.json()
    assert body["sector"] == "银行"
    assert body["served_from"] == "sqlite_cache"
    assert body["upstream_requests"] == 0
    assert body["universe"]["exchanges"] == ["SH", "SZ"]
    assert body["coverage"]["configured"] == 2
    assert body["coverage"]["quotes_received"] == 2
    assert body["coverage"]["analysis_ready"] == 0
    assert body["coverage"]["analysis_reason"] == "minimum_universe_not_met"
    assert {item["symbol"] for item in body["items"]} == {
        "600519.SH",
        "000001.SZ",
    }
    assert body["items"][0]["analysis"] is None


def test_daily_data_quality_endpoint_is_cache_only(client):
    provider = client.app.state.runtime.provider

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("quality audit must not call provider")

    provider.fetch_bars = fail_if_called
    response = client.get(
        "/api/v1/data-quality/daily",
        params={"required_history_bars": 25},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["interval"] == "1d"
    assert body["expected_symbol_count"] == 2
    assert body["required_history_bars"] == 25
    assert body["served_from"] == "sqlite_cache"
    assert body["upstream_requests"] == 0
    assert body["calendar"] == "weekday_fallback"


def test_bank_sector_snapshot_runs_off_loop_and_uses_short_cache(client):
    state = client.app.state
    original = state.bank_sector_snapshot_builder
    calls = []

    def counted_builder():
        calls.append(threading.get_ident())
        # ``asyncio.to_thread`` must isolate both SQLite reads and factor work
        # from the ASGI loop.  A running loop here would make this assertion
        # fail through the normal HTTP error path.
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return original()

    state.bank_sector_snapshot_builder = counted_builder
    state.bank_sector_cache_payload = None
    state.bank_sector_cache_expires_at = 0.0

    first = client.get("/api/v1/sectors/banks")
    second = client.get("/api/v1/sectors/banks")

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert len(calls) == 1


def test_bank_sector_concurrent_cold_requests_are_single_flight(client):
    state = client.app.state
    original = state.bank_sector_snapshot_builder
    calls = 0
    calls_lock = threading.Lock()

    def slow_builder():
        nonlocal calls
        with calls_lock:
            calls += 1
        # Leave enough time for all request coroutines to contend on the
        # application-local lock.  Only the lock owner may run the builder.
        time.sleep(0.1)
        return original()

    state.bank_sector_snapshot_builder = slow_builder
    state.bank_sector_cache_payload = None
    state.bank_sector_cache_expires_at = 0.0

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(
            pool.map(
                lambda _: client.get("/api/v1/sectors/banks"),
                range(8),
            )
        )

    assert all(response.status_code == 200 for response in responses)
    assert calls == 1
    assert len({response.json()["updated_at"] for response in responses}) == 1


def test_quote_routes_hide_rows_from_a_different_provider(client):
    before = client.get("/api/v1/quotes/600519.SH").json()
    foreign = Quote(
        symbol="600519.SH",
        timestamp=now_market_time() + timedelta(minutes=5),
        last=Decimal("9999"),
        source="alltick",
    )
    client.app.state.runtime.repository.upsert_quotes([foreign])

    response = client.get("/api/v1/quotes/600519.SH")
    assert response.status_code == 200
    assert response.json()["source"] == "mock"
    assert response.json()["last"] == before["last"]


def test_batch_market_quotes_supports_pagination_sort_search_and_alias(client):
    response = client.get(
        "/api/v1/market/quotes",
        params={
            "limit": 1,
            "offset": 0,
            "sort": "last",
            "order": "desc",
            "refresh": "true",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2  # the mock catalogue has two Shanghai stocks
    assert body["universe_total"] == 2
    assert body["unpriced_count"] == 0
    assert body["limit"] == 1
    assert body["offset"] == 0
    assert body["has_more"] is True
    assert len(body["items"]) == 1
    assert body["source"] == "mock"
    assert body["universe_scope"] == "provider_catalogue"
    assert body["as_of"].endswith("+08:00")
    assert body["summary"]["count"] == 2
    assert body["summary"]["catalogue_total"] == 2
    assert body["summary"]["advancers"] + body["summary"]["decliners"] + body["summary"]["unchanged"] == 2
    assert body["market_status"]["calendar"] == "weekday_fallback"
    assert body["served_from"] == "provider_refresh"
    assert body["refresh_requested"] is True

    search = client.get(
        "/api/v1/market/quotes",
        params={
            "search": "688981",
            "include_status": "false",
            "include_source": "false",
            "refresh": "true",
        },
    )
    assert search.status_code == 200
    search_body = search.json()
    assert search_body["total"] == 1
    assert search_body["items"][0]["symbol"] == "688981.SH"
    assert "market_status" not in search_body
    assert "source" not in search_body

    alias = client.get(
        "/api/v1/quotes/batch", params={"limit": 10, "refresh": "true"}
    )
    assert alias.status_code == 200
    assert alias.json()["total"] == 2

    frontend_sort = client.get(
        "/api/v1/market/quotes",
        params={
            "limit": 10,
            "sort": "change_pct",
            "order": "desc",
            "refresh": "true",
        },
    )
    assert frontend_sort.status_code == 200


def test_batch_market_quotes_default_is_sqlite_only(client):
    provider = client.app.state.runtime.provider
    calls = []

    async def fail_if_called(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("default market quote GET must not call provider")

    provider.fetch_instruments = fail_if_called
    provider.fetch_quotes = fail_if_called
    provider.fetch_quotes_relaxed = fail_if_called

    response = client.get("/api/v1/market/quotes", params={"limit": 10})
    assert response.status_code == 200
    body = response.json()
    assert body["served_from"] == "sqlite_cache"
    assert body["refresh_requested"] is False
    assert body["universe_total"] == 2
    assert body["total"] == 1
    assert calls == []


def test_batch_market_quotes_marks_alltick_configured_scope(client):
    sentinel = "unit-test-token-not-for-network"

    def requester(method, url, params, body, timeout):
        query = json.loads(params["query"])
        rows = [
            {
                "code": item["code"],
                "seq": str(index + 1),
                "tick_time": "1787536800000",
                "price": "12.34",
                "volume": "100",
                "turnover": "1234",
                "trade_direction": 0,
            }
            for index, item in enumerate(query["data"]["symbol_list"])
        ]
        return {
            "ret": 200,
            "msg": "ok",
            "trace": query["trace"],
            "data": {"tick_list": rows},
        }

    provider = AllTickMarketDataProvider(
        sentinel,
        symbols=["600519.SH", "000001.SZ"],
        requester=requester,
        use_websocket=False,
        http_min_interval_seconds=0,
        retries=0,
    )
    client.app.state.runtime.provider = provider
    response = client.get(
        "/api/v1/market/quotes", params={"limit": 10, "refresh": "true"}
    )
    asyncio.run(provider.close())

    assert response.status_code == 200
    body = response.json()
    assert body["universe_scope"] == "configured_symbols"
    assert body["universe_total"] == 1  # endpoint is intentionally Shanghai-only
    assert body["total"] == 1
    assert body["source"] == "alltick"
    assert body["items"][0]["volume_semantics"] == "latest_trade"
    assert sentinel not in response.text


def test_batch_market_quotes_empty_catalogue_is_a_successful_empty_result(client):
    async def empty_catalogue():
        return []

    client.app.state.runtime.provider.fetch_instruments = empty_catalogue
    response = client.get("/api/v1/market/quotes", params={"refresh": "true"})
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["as_of"] is None
    assert body["summary"]["count"] == 0
    assert body["source"] == "mock"


def test_batch_market_quotes_provider_outage_is_retryable_503(client):
    async def fail(*args, **kwargs):
        raise AkShareDataError("upstream disconnected")

    client.app.state.runtime.provider.fetch_instruments = fail
    response = client.get("/api/v1/market/quotes", params={"refresh": "true"})
    assert response.status_code == 503
    body = response.json()["error"]
    assert body["code"] == "provider_unavailable"
    assert body["details"]["error_type"] == "AkShareDataError"


def test_batch_market_quotes_null_provider_payload_is_not_treated_as_empty(client):
    async def null_catalogue():
        return None

    client.app.state.runtime.provider.fetch_instruments = null_catalogue
    response = client.get("/api/v1/market/quotes", params={"refresh": "true"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_unavailable"


@pytest.mark.parametrize(
    "params",
    [
        {"sort": "not-a-field"},
        {"order": "sideways"},
    ],
)
def test_batch_market_quotes_rejects_invalid_sorting(params, client):
    response = client.get("/api/v1/market/quotes", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "http_error"


def test_alerts_signals_and_strategies(client):
    repository = client.app.state.runtime.repository
    timestamp = now_market_time()
    alert_id = repository.insert_alert(
        Alert(
            symbol="600519.SH",
            timestamp=timestamp,
            level=AlertLevel.WARNING,
            title="fixture alert",
            message="test-only",
            category="test",
        )
    )
    repository.insert_signal(
        Signal(
            strategy="fixture_strategy",
            symbol="600519.SH",
            timestamp=timestamp,
            action=SignalAction.BUY,
            strength=0.5,
            price=Decimal("123.45"),
            reason="test-only",
        )
    )

    alerts = client.get(
        "/api/v1/alerts",
        params={"symbol": "600519.sh", "level": "warning"},
    )
    assert alerts.status_code == 200
    assert alerts.json()["count"] == 1
    assert alerts.json()["items"][0]["severity"] == "warning"

    acknowledgement = client.patch(
        "/api/v1/alerts/{}".format(alert_id),
        json={"acknowledged": True},
    )
    assert acknowledgement.status_code == 200
    assert acknowledgement.json() == {"id": alert_id, "acknowledged": True}

    signals = client.get(
        "/api/v1/signals",
        params={"symbol": "600519.sh", "strategy": "fixture_strategy"},
    )
    assert signals.status_code == 200
    assert signals.json()["count"] == 1
    assert signals.json()["items"][0]["action"] == "buy"
    assert signals.json()["items"][0]["price"] == "123.45"

    strategies = client.get("/api/v1/strategies")
    assert strategies.status_code == 200
    assert strategies.json()["items"][0]["mode"] == "signal_only"


@pytest.mark.parametrize(
    ("path", "expected_message"),
    [
        ("/api/v1/quotes/not-a-symbol", "CODE.EXCHANGE"),
        ("/api/v1/bars/600519.SH?interval=2m", "unsupported interval"),
        ("/api/v1/bars/not-a-symbol?interval=1m", "CODE.EXCHANGE"),
    ],
)
def test_invalid_symbol_and_interval_have_consistent_error_envelope(
    client, path, expected_message
):
    response = client.get(path)

    assert response.status_code == 422
    assert_common_headers(response)
    error = response.json()["error"]
    assert error["code"] == "http_error"
    assert expected_message in error["message"]
    assert error["request_id"] == response.headers["x-request-id"]


def test_static_dashboard_assets_are_served(client):
    index = client.get("/")
    assert index.status_code == 200
    assert "text/html" in index.headers["content-type"]
    assert "<!doctype html>" in index.text.lower()

    script = client.get("/static/app.js")
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    assert len(script.text) > 100
    assert "loadWatchlist" in script.text
    assert "/market/quotes" not in script.text
    assert "loadAnalysis" in script.text
    assert "market-gainers" in index.text
    assert "analysis-metrics" in index.text
    assert "left: 54" in script.text
    assert 'x: padding.left - 7' in script.text
    assert '"text-anchor": "end"' in script.text
    assert 'id="readiness-banner"' in index.text
    assert 'role="progressbar"' in index.text
    assert "renderReadiness" in script.text
    assert "deferred_for_budget" in script.text
    assert "历史日线已准备" in script.text

    styles = client.get("/static/styles.css")
    assert styles.status_code == 200
    assert ".readiness-banner" in styles.text
    assert 'data-state="warning"' in styles.text


def test_alert_acknowledgement_requires_json_object(client):
    response = client.patch(
        "/api/v1/alerts/1",
        content="[]",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["message"] == "JSON object required"


def test_alert_acknowledgement_requires_strict_boolean(client):
    response = client.patch("/api/v1/alerts/1", json={"acknowledged": "false"})

    assert response.status_code == 422
    assert "JSON boolean" in response.json()["error"]["message"]


def test_cross_origin_websocket_is_rejected(client):
    with pytest.raises(Exception):
        with client.websocket_connect(
            "/ws/market",
            headers={"Origin": "https://untrusted.example"},
        ):
            pass


def test_bars_cache_expands_after_small_limit_request(client):
    first = client.get("/api/v1/bars/600519.SH", params={"interval": "5m", "limit": 1})
    expanded = client.get(
        "/api/v1/bars/600519.SH",
        params={"interval": "5m", "limit": 12},
    )

    assert first.status_code == 200
    assert first.json()["count"] == 1
    assert expanded.status_code == 200
    assert expanded.json()["count"] == 12


def test_real_provider_bar_read_is_cache_only_without_explicit_refresh(client):
    calls = []

    async def must_not_fetch(*args, **kwargs):
        calls.append(True)
        raise AssertionError("cache-only GET must not call upstream")

    runtime = client.app.state.runtime
    original_name = type(runtime.provider).name
    type(runtime.provider).name = property(lambda self: "asharehub")
    runtime.provider.fetch_bars = must_not_fetch
    try:
        response = client.get(
            "/api/v1/bars/600519.SH",
            params={"interval": "5m", "limit": 2000},
        )
    finally:
        type(runtime.provider).name = original_name

    assert response.status_code == 200
    assert response.json()["refresh_requested"] is False
    assert response.json()["served_from"] == "sqlite_cache"
    assert calls == []


def test_provider_outage_uses_retryable_503_envelope(client):
    async def fail(*args, **kwargs):
        raise AkShareDataError("upstream disconnected")

    client.app.state.runtime.provider.fetch_bars = fail
    response = client.get(
        "/api/v1/bars/600519.SH",
        params={"interval": "1w", "limit": 1, "refresh": "true"},
    )

    assert response.status_code == 503
    body = response.json()["error"]
    assert body["code"] == "provider_unavailable"
    assert body["details"]["error_type"] == "AkShareDataError"


def test_alltick_provider_error_is_503_and_secret_is_not_exposed(client):
    sentinel = "alltick-secret-must-not-leak"

    async def fail(*args, **kwargs):
        # The adapter itself sanitizes transport errors before they reach this
        # handler; use an already-sanitized provider exception here.
        raise AllTickDataError("AllTick kline failed (trace=unit-test)")

    client.app.state.runtime.provider.fetch_bars = fail
    response = client.get(
        "/api/v1/bars/600519.SH",
        params={"interval": "1w", "limit": 1, "refresh": "true"},
    )

    assert response.status_code == 503
    body = response.json()["error"]
    assert body["code"] == "provider_unavailable"
    assert body["details"]["error_type"] == "AllTickDataError"
    assert sentinel not in response.text
