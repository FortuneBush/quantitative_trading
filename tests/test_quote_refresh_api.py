"""Offline regression contracts for explicit, quota-aware quote refreshes."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from quant_monitor.app import create_app
from quant_monitor.config import Settings
from quant_monitor.data_sources import MockMarketDataProvider
from quant_monitor.domain import EventType, Quote, now_market_time


SYMBOLS = ("600519.SH", "688981.SH")
ROUTES = (
    "/api/v1/quotes",
    "/api/v1/quotes/600519.SH",
    "/api/v1/market/quotes",
    "/api/v1/quotes/batch",
)
BASKET_ROUTES = tuple(route for route in ROUTES if not route.endswith("600519.SH"))


def _quote(symbol, timestamp, *, price="100", source="mock"):
    return Quote(
        symbol=symbol,
        timestamp=timestamp,
        last=Decimal(price),
        source=source,
    )


def _request(client, route, *, refresh=True):
    params = {"refresh": str(refresh).lower()}
    if route == "/api/v1/quotes":
        params["symbols"] = ",".join(SYMBOLS)
    return client.get(route, params=params)


def _requested(route):
    return SYMBOLS[:1] if route.endswith("600519.SH") else SYMBOLS


def _items(payload):
    return payload["items"] if "items" in payload else [payload]


def _history(repository):
    return {symbol: repository.get_quotes(symbol) for symbol in SYMBOLS}


@pytest.fixture
def refresh_client(tmp_path):
    settings = Settings(
        database_path=str(tmp_path / "refresh.sqlite3"),
        data_provider="mock",
        poll_interval_seconds=3600,
        history_bar_count=25,
        watchlist=list(SYMBOLS),
    )
    with TestClient(create_app(settings)) as client:
        runtime = client.app.state.runtime
        # Startup uses only the deterministic mock. Stop subsequent polling so
        # event/persistence assertions measure exactly the manual operation.
        client.portal.call(runtime.monitor.stop)
        baseline = now_market_time() + timedelta(seconds=1)
        runtime.monitor.collector._clock = lambda: baseline + timedelta(seconds=2)
        previous = [_quote(symbol, baseline) for symbol in SYMBOLS]
        runtime.repository.upsert_quotes(previous)
        runtime.monitor.remember_quotes(previous)
        events = []

        def observe(event):
            committed = runtime.repository.get_latest_quotes([event.payload.symbol])
            events.append((event.payload, committed == [event.payload]))

        subscription = client.portal.call(runtime.event_bus.subscribe, EventType.QUOTE, observe)
        try:
            yield SimpleNamespace(
                client=client,
                runtime=runtime,
                baseline=baseline,
                previous=previous,
                events=events,
            )
        finally:
            client.portal.call(runtime.event_bus.unsubscribe, subscription)


def _set_response(context, response):
    calls = []

    async def fetch(symbols):
        calls.append(tuple(symbols))
        return response

    context.runtime.provider.fetch_quotes_relaxed = fetch
    return calls


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "scenario",
    ["null", "non_quote", "non_list", "duplicate", "unrequested", "wrong_source", "future"],
)
def test_refresh_rejects_invalid_snapshot_without_writes_or_events(refresh_client, route, scenario):
    context = refresh_client
    first = _quote(SYMBOLS[0], context.baseline + timedelta(seconds=1))
    responses = {
        "null": None,
        "non_quote": [first, {"symbol": SYMBOLS[0]}],
        "non_list": "invalid quote payload",
        "duplicate": [first, first],
        "unrequested": [first, _quote("600036.SH", first.timestamp)],
        # Use the existing timestamp to detect destructive wrong-source upsert.
        "wrong_source": [_quote(SYMBOLS[0], context.baseline, source="mockish")],
        "future": [_quote(SYMBOLS[0], context.baseline + timedelta(seconds=8))],
    }
    before = _history(context.runtime.repository)
    calls = _set_response(context, responses[scenario])

    response = _request(context.client, route)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_unavailable"
    assert response.json()["error"]["details"]["error_type"] == "QuoteSnapshotError"
    assert calls == [tuple(_requested(route))]
    assert _history(context.runtime.repository) == before
    assert context.runtime.repository.get_quotes("600036.SH") == []
    assert context.events == []


@pytest.mark.parametrize("route", ROUTES)
def test_quote_reads_without_refresh_never_invoke_provider(refresh_client, route):
    context = refresh_client

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("cache-only request invoked provider")

    context.runtime.provider.fetch_quotes = fail_if_called
    context.runtime.provider.fetch_quotes_relaxed = fail_if_called
    context.runtime.provider.fetch_instruments = fail_if_called
    response = _request(context.client, route, refresh=False)

    assert response.status_code == 200
    payload = response.json()
    assert payload["served_from"] == "sqlite_cache"
    assert payload["refresh_requested"] is False
    assert payload["provider_operations"] == 0
    assert payload["upstream_requests"] == 0
    assert payload["refresh_result"] is None
    assert context.events == []


@pytest.mark.parametrize("route", BASKET_ROUTES)
def test_partial_refresh_retains_but_discloses_cached_missing_symbols(refresh_client, route):
    context = refresh_client
    fresh = _quote(SYMBOLS[0], context.baseline + timedelta(seconds=1), price="101")
    _set_response(context, [fresh])

    response = _request(context.client, route)

    assert response.status_code == 200
    payload = response.json()
    assert payload["served_from"] == "sqlite_cache"
    assert {item["symbol"]: item["last"] for item in _items(payload)} == {
        SYMBOLS[0]: "101", SYMBOLS[1]: "100"
    }
    assert payload["refresh_result"] == {
        "status": "partial",
        "requested_symbols": list(SYMBOLS),
        "received_symbols": [SYMBOLS[0]],
        "accepted_symbols": [SYMBOLS[0]],
        "missing_symbols": [SYMBOLS[1]],
        "stale_symbols": [],
        "cache_fallback_symbols": [SYMBOLS[1]],
    }
    assert context.events == [(fresh, True)]


@pytest.mark.parametrize("route", ROUTES)
def test_empty_refresh_discloses_cache_fallback_without_publishing(refresh_client, route):
    context = refresh_client
    _set_response(context, [])
    response = _request(context.client, route)

    assert response.status_code == 200
    result = response.json()["refresh_result"]
    assert result == {
        "status": "empty",
        "requested_symbols": list(_requested(route)),
        "received_symbols": [],
        "accepted_symbols": [],
        "missing_symbols": list(_requested(route)),
        "stale_symbols": [],
        "cache_fallback_symbols": list(_requested(route)),
    }
    assert len(_items(response.json())) == len(_requested(route))
    assert context.events == []


@pytest.mark.parametrize("route", ROUTES)
def test_stale_refresh_returns_latest_cache_without_persisting_or_publishing(refresh_client, route):
    context = refresh_client
    requested = _requested(route)
    old = [_quote(symbol, context.baseline - timedelta(seconds=1), price="99") for symbol in requested]
    _set_response(context, old)
    before = _history(context.runtime.repository)

    response = _request(context.client, route)

    assert response.status_code == 200
    payload = response.json()
    assert all(item["last"] == "100" for item in _items(payload))
    assert payload["refresh_result"] == {
        "status": "empty",
        "requested_symbols": list(requested),
        "received_symbols": list(requested),
        "accepted_symbols": [],
        "missing_symbols": [],
        "stale_symbols": list(requested),
        "cache_fallback_symbols": list(requested),
    }
    assert _history(context.runtime.repository) == before
    assert context.events == []


@pytest.mark.parametrize("route", BASKET_ROUTES)
def test_mixed_fresh_and_stale_refresh_accepts_only_fresh_symbols(refresh_client, route):
    context = refresh_client
    fresh = _quote(SYMBOLS[0], context.baseline + timedelta(seconds=1), price="101")
    stale = _quote(SYMBOLS[1], context.baseline - timedelta(seconds=1), price="99")
    _set_response(context, [fresh, stale])
    previous_second_history = context.runtime.repository.get_quotes(SYMBOLS[1])

    response = _request(context.client, route)

    assert response.status_code == 200
    payload = response.json()
    assert {item["symbol"]: item["last"] for item in _items(payload)} == {
        SYMBOLS[0]: "101", SYMBOLS[1]: "100"
    }
    assert payload["refresh_result"] == {
        "status": "partial",
        "requested_symbols": list(SYMBOLS),
        "received_symbols": list(SYMBOLS),
        "accepted_symbols": [SYMBOLS[0]],
        "missing_symbols": [],
        "stale_symbols": [SYMBOLS[1]],
        "cache_fallback_symbols": [SYMBOLS[1]],
    }
    assert context.runtime.repository.get_quotes(SYMBOLS[1]) == previous_second_history
    assert context.events == [(fresh, True)]


@pytest.mark.parametrize("route", ROUTES)
def test_refresh_persists_before_publishing_and_updates_monitor_snapshot(refresh_client, route):
    context = refresh_client
    requested = _requested(route)
    fresh = [_quote(symbol, context.baseline + timedelta(seconds=1), price="101") for symbol in requested]
    calls = _set_response(context, fresh)

    response = _request(context.client, route)

    assert response.status_code == 200
    payload = response.json()
    assert payload["served_from"] == "sqlite_cache"
    assert payload["refresh_requested"] is True
    assert payload["quote_provider_operations"] == 1
    expected_catalogue_calls = int(route in ("/api/v1/market/quotes", "/api/v1/quotes/batch"))
    assert payload["catalogue_provider_operations"] == expected_catalogue_calls
    assert payload["provider_operations"] == 1 + expected_catalogue_calls
    assert payload["upstream_requests"] == 0
    assert payload["refresh_result"]["status"] == "complete"
    assert payload["refresh_result"]["accepted_symbols"] == list(requested)
    assert payload["refresh_result"]["cache_fallback_symbols"] == []
    assert calls == [tuple(requested)]
    assert sorted(context.events, key=lambda item: item[0].symbol) == [(item, True) for item in fresh]
    snapshot = context.runtime.monitor.current_snapshot()["quotes"]
    assert all(snapshot[item.symbol] == item for item in fresh)


@pytest.mark.parametrize("route", ROUTES)
def test_cached_non_mock_provider_does_not_claim_one_http_request(refresh_client, route):
    context = refresh_client
    timestamp = context.baseline + timedelta(seconds=1)

    class CachedProvider(MockMarketDataProvider):
        """An offline provider with no HTTP transport, only a populated cache."""

        operation_calls = 0
        http_calls = 0

        @property
        def name(self):
            return "fixture"

        async def fetch_quotes_relaxed(self, symbols):
            self.operation_calls += 1
            return [_quote(symbol, timestamp, source=self.name) for symbol in symbols]

    provider = CachedProvider()
    context.runtime.provider = provider
    context.runtime.monitor.provider = provider
    context.runtime.monitor.collector.provider = provider

    response = _request(context.client, route)

    assert response.status_code == 200
    payload = response.json()
    assert payload["refresh_result"]["status"] == "complete"
    assert payload["quote_provider_operations"] == provider.operation_calls == 1
    assert provider.http_calls == 0
    # Without request-local transport receipts the API reports unknown, not
    # a fabricated exact HTTP count inferred from a provider method call.
    assert payload["upstream_requests"] is None
    assert payload["upstream_requests_scope"] == "request_local_http_attempts"


def test_manual_refresh_delivers_committed_quote_to_existing_websocket(refresh_client):
    context = refresh_client
    fresh = _quote(SYMBOLS[0], context.baseline + timedelta(seconds=1), price="102")
    _set_response(context, [fresh])
    with context.client.websocket_connect("/ws/market") as websocket:
        assert websocket.receive_json()["type"] == "snapshot"
        response = context.client.get(
            "/api/v1/quotes/600519.SH", params={"refresh": "true"}
        )
        assert response.status_code == 200
        message = websocket.receive_json()
        assert message["type"] == "quote"
        assert message["data"]["symbol"] == fresh.symbol
        assert message["data"]["last"] == "102"
        assert context.runtime.repository.get_latest_quotes([fresh.symbol]) == [fresh]
        assert context.runtime.system_status()["provider"]["quote_freshness"]["newest_timestamp"] == fresh.timestamp.isoformat()


@pytest.mark.parametrize("route", ROUTES)
def test_legacy_refresh_get_advertises_async_job_route(refresh_client, route):
    context = refresh_client
    fresh = [_quote(symbol, context.baseline + timedelta(seconds=1)) for symbol in _requested(route)]
    _set_response(context, fresh)

    response = _request(context.client, route, refresh=True)

    assert response.status_code == 200
    assert response.headers["Deprecation"] == "true"
    assert response.headers["Link"].startswith("</api/v1/quote-refresh/jobs>")
