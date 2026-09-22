"""HTTP contract tests for the cache-only market treemap endpoint."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from quant_monitor.app import create_app
from quant_monitor.config import Settings


@pytest.fixture
def treemap_client(tmp_path):
    settings = Settings(
        app_name="Treemap contract fixture",
        database_path=str(tmp_path / "treemap.sqlite3"),
        data_provider="mock",
        poll_interval_seconds=3600.0,
        watchlist=["600519.SH", "000001.SZ"],
        mock_seed=20260812,
    )
    with TestClient(create_app(settings)) as client:
        yield client


def test_treemap_is_cache_only_and_exposes_industry_and_stock_hierarchy(treemap_client, monkeypatch):
    runtime = treemap_client.app.state.runtime

    async def fail_provider_io(*args, **kwargs):
        raise AssertionError("treemap must not perform provider I/O")

    # If this route accidentally starts using provider refreshes, fail loudly;
    # endpoint calls must remain safe under the AShareHub daily quota.
    for method in ("fetch_quotes", "fetch_bars", "fetch_instruments"):
        monkeypatch.setattr(runtime.provider, method, fail_provider_io)

    response = treemap_client.get("/api/v1/market/treemap")
    assert response.status_code == 200
    payload = response.json()
    assert payload["served_from"] == "sqlite_cache"
    assert payload["upstream_requests"] == 0
    assert isinstance(payload["items"], list)
    assert payload["sectors"] == payload["items"]
    assert payload["count"] == len(payload["items"])
    assert payload["size_semantics"]
    assert payload["change_semantics"] == "turnover_weighted_pct_change"
    assert "market_status" in payload
    assert payload["provider"] == "mock"
    assert payload["universe_scope"] == "cached_catalogue"

    for sector in payload["items"]:
        assert sector["id"].startswith("industry:")
        assert sector["name"]
        assert sector["children"] == sector["items"]
        assert sector["value"] == sector["size"]
        assert sector["priced_count"] == len(sector["children"])
        for item in sector["children"]:
            assert item["symbol"] == item["id"]
            assert item["name"]
            assert item["size"] == item["value"]
            assert item["change_pct"] == item["pct_change"]
            assert item["quote_status"] == "available"


def test_treemap_alias_and_bounded_query_parameters(treemap_client):
    primary = treemap_client.get(
        "/api/v1/market/treemap",
        params={"max_industries": 1, "max_stocks_per_industry": 1},
    )
    alias = treemap_client.get(
        "/api/v1/sectors/treemap",
        params={"max_industries": 1, "max_stocks_per_industry": 1},
    )
    assert primary.status_code == alias.status_code == 200
    assert alias.json() == primary.json()
    assert primary.json()["count"] <= 1
    assert all(len(group["children"]) <= 1 for group in primary.json()["items"])

    too_large = treemap_client.get(
        "/api/v1/market/treemap", params={"max_industries": 2001}
    )
    assert too_large.status_code == 422


def test_treemap_preserves_empty_cache_as_a_valid_response(treemap_client):
    # Remove the cached domain rows without touching the provider. This models
    # first boot before catalogue/quote bootstrap and must not become an error.
    repository = treemap_client.app.state.runtime.repository
    repository._connection.execute("DELETE FROM quotes")
    repository._connection.execute("DELETE FROM instruments")
    repository._connection.commit()
    treemap_client.app.state.market_treemap_cache_payload = None

    response = treemap_client.get("/api/v1/market/treemap")
    assert response.status_code == 200
    payload = response.json()
    assert payload["items"] == []
    assert payload["sectors"] == []
    assert payload["universe_count"] == 0
    assert payload["priced_count"] == 0
    assert payload["unpriced_count"] == 0
    assert payload["as_of"] is None
    assert payload["upstream_requests"] == 0
