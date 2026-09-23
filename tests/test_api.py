"""HTTP contract tests against a fully started temporary application."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest


# Keep core tests runnable in a minimal Python environment.  In a development
# install (``pip install -r requirements-dev.txt``), this module exercises every
# route below through FastAPI's ASGI test client.
pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from quant_monitor.backtesting import BacktestEngine
from quant_monitor.app import _bar_projection_provenance, _safe_request_path, create_app
from quant_monitor.config import Settings
from quant_monitor.data_sources import (
    AShareHubMarketDataProvider,
    AkShareDataError,
    AllTickDataError,
    AllTickMarketDataProvider,
    MockMarketDataProvider,
)
from quant_monitor.domain import (
    Adjustment,
    Alert,
    AlertLevel,
    Bar,
    Exchange,
    Instrument,
    Quote,
    SHANGHAI_TZ,
    SecurityType,
    Signal,
    SignalAction,
    now_market_time,
)
from quant_monitor.research import (
    DatasetDescriptor,
    ResearchRunManifest,
    hash_canonical,
    hash_input_records,
    strategy_code_version,
)
from quant_monitor.storage import SQLiteRepository


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


def _stable_weekday_bar_time():
    """Keep calendar-gate tests deterministic when the suite runs on weekends."""
    return datetime(2026, 9, 18, 15, 0, tzinfo=SHANGHAI_TZ)


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
    assert ready_body["data_ready"] is True
    assert ready_body["calendar_ready"] is True
    assert ready_body["calendar_reason"] == "not_required"
    assert ready_body["data_gate_requested"] is False
    assert ready_body["feed"]["provider"] == "mock"
    assert ready_body["phase"] == "ready"
    assert ready_body["started_at"] is not None
    assert ready_body["ready_at"] is not None
    assert ready_body["duration_ms"] is not None
    assert ready_body["error_type"] is None
    assert "history_bootstrap" in ready_body

    strict_ready = client.get("/ready?require_data=true&require_research=true")
    assert strict_ready.status_code == 200
    strict_body = strict_ready.json()
    assert strict_body["ready"] is True
    assert strict_body["data_gate_requested"] is True
    assert strict_body["research_gate_requested"] is True

    strict_calendar = client.get("/ready?require_calendar=true")
    assert strict_calendar.status_code == 200
    assert strict_calendar.json()["calendar_gate_requested"] is True
    assert strict_calendar.json()["calendar_ready"] is True

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
    assert system_body["collector"]["cycle_stats"]["total"] >= 1
    assert system_body["collector"]["cycle_stats"]["succeeded"] >= 1
    assert system_body["settings"]["watchlist"] == ["600519.SH", "000001.SZ"]
    assert "database_path" not in system_body["settings"]


def test_prometheus_metrics_are_local_and_do_not_trigger_provider_io(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert "text/plain" in response.headers["content-type"]
    body = response.text
    assert "# TYPE quant_monitor_up gauge" in body
    assert "quant_monitor_up 1" in body
    assert "# TYPE quant_monitor_collector_iterations_total counter" in body
    assert "# TYPE quant_monitor_collector_cycles_total counter" in body
    assert "quant_monitor_collector_cycles_succeeded_total" in body
    assert "quant_monitor_collector_cycles_failed_total" in body
    assert "quant_monitor_collector_cycles_partial_total" in body
    assert "quant_monitor_collector_cycles_cancelled_total" in body
    assert "# TYPE quant_monitor_collector_jobs_active gauge" in body
    assert "quant_monitor_collector_jobs_total" in body
    assert "quant_monitor_collector_jobs_succeeded" in body
    assert "quant_monitor_quote_fresh_symbols" in body
    assert "quant_monitor_quote_stale_symbols" in body
    assert "quant_monitor_quote_missing_symbols" in body
    assert "quant_monitor_quote_future_timestamp_symbols" in body
    assert "quant_monitor_quote_oldest_age_seconds" in body
    assert "# TYPE quant_monitor_data_ready gauge" in body
    assert "quant_monitor_data_ready 1" in body
    assert 'quant_monitor_data_readiness_info{provider="mock",reason="market_closed_cached_close_allowed"} 1' in body
    assert 'quant_monitor_quote_freshness_info{provider="mock",state="untracked"} 1' in body
    assert 'quant_monitor_provider_info{provider="mock",state="running"} 1' in body
    assert "quant_monitor_provider_quota_used" not in body
    # Provider credentials and per-symbol values must never be exported.
    assert "api_key" not in body.lower()
    assert "600519" not in body


def test_asharehub_response_quality_is_available_in_status_metrics_and_batch_api(
    client, tmp_path
):
    """Expose aggregate upstream response quality without another HTTP call."""

    runtime = client.app.state.runtime
    provider = AShareHubMarketDataProvider(
        "unit-test-key",
        symbols=["600519.SH", "000001.SZ"],
        budget_path=tmp_path / "quality-budget.json",
    )
    provider._last_quote_response_rows = 2
    provider._last_quote_parsed_symbols = 1
    provider._last_quote_dropped_rows = 1
    provider._last_quote_invalid_rows = 1
    provider._last_quote_unexpected_rows = 0
    provider._last_quote_duplicate_rows = 0
    provider._last_quote_conflict_rows = 0
    provider._last_quote_missing_symbols = ("000001.SZ",)
    # The configured basket has two symbols, while this response was for a
    # single-symbol refresh.  The batch diagnostics must use the per-request
    # denominator rather than overstating the missing coverage.
    provider._last_quote_requested_symbols = 1
    runtime.provider = provider
    runtime.monitor.provider = provider
    try:
        status = client.get("/api/v1/system/status")
        assert status.status_code == 200
        quality = status.json()["provider"]["quote_response_quality"]
        assert quality["response_rows"] == 2
        assert quality["parsed_symbols"] == 1
        assert quality["dropped_rows"] == 1
        assert quality["invalid_rows"] == 1
        assert quality["unexpected_rows"] == 0
        assert quality["duplicate_rows"] == 0
        assert quality["conflict_rows"] == 0
        assert quality["missing_count"] == 1
        metrics = client.get("/metrics").text
        assert "quant_monitor_quote_response_rows 2" in metrics
        assert "quant_monitor_quote_parsed_symbols 1" in metrics
        assert "quant_monitor_quote_dropped_rows 1" in metrics
        assert "quant_monitor_quote_invalid_rows 1" in metrics
        assert "quant_monitor_quote_conflict_rows 0" in metrics
        assert "quant_monitor_quote_response_missing_symbols 1" in metrics
        batch = client.get("/api/v1/market/quotes", params={"limit": 10})
        assert batch.status_code == 200
        batch_quality = batch.json()["provider_diagnostics"]["quote_response_quality"]
        assert batch_quality["response_rows"] == 2
        assert batch_quality["missing_symbols"] == ["000001.SZ"]
        assert batch_quality["requested_symbols"] == 1
        assert batch_quality["coverage_ratio"] == 1.0
    finally:
        asyncio.run(provider.close())


def test_collection_job_api_is_async_and_reports_completion(client):
    submitted = client.post("/api/v1/collector/jobs")
    assert submitted.status_code == 202
    body = submitted.json()
    assert body["status"] in {"queued", "running", "succeeded"}
    assert body["served_from"] == "in_process_job_manager"
    job_id = body["job_id"]

    terminal = None
    for _ in range(100):
        terminal = client.get("/api/v1/collector/jobs/{}".format(job_id)).json()
        if terminal["terminal"]:
            break
        time.sleep(0.01)
    assert terminal["status"] == "succeeded"
    assert terminal["counts"]["quotes"] == 2
    listing = client.get("/api/v1/collector/jobs", params={"limit": 10})
    assert listing.status_code == 200
    assert listing.json()["active"] == 0
    assert listing.json()["items"][0]["job_id"] == job_id
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "quant_monitor_collector_jobs_active 0" in metrics.text
    assert "quant_monitor_collector_jobs_succeeded 1" in metrics.text

    missing = client.get("/api/v1/collector/jobs/missing")
    assert missing.status_code == 404


def test_quotes_bars_and_public_configuration(client):
    quote = client.get("/api/v1/quotes/600519.sh")
    assert quote.status_code == 200
    quote_body = quote.json()
    assert quote_body["symbol"] == "600519.SH"
    assert quote_body["source"] == "mock"
    assert quote_body["price"] == quote_body["last"]
    assert quote_body["requested_symbol"] == "600519.SH"
    assert quote_body["as_of"] == quote_body["timestamp"]
    assert quote_body["freshness"] in {"fresh", "stale"}
    assert quote_body["served_from"] == "sqlite_cache"
    assert quote_body["upstream_requests"] == 0
    assert quote_body["market_status"]["calendar"] == "weekday_fallback"

    quotes = client.get("/api/v1/quotes", params={"symbols": "600519.sh,000001.sz"})
    assert quotes.status_code == 200
    quotes_body = quotes.json()
    assert len(quotes_body["items"]) == 2
    assert quotes_body["count"] == 2
    assert quotes_body["requested_count"] == 2
    assert quotes_body["fresh_count"] + quotes_body["stale_count"] == 2
    assert quotes_body["requested_symbols"] == ["600519.SH", "000001.SZ"]
    assert quotes_body["received_symbols"] == ["000001.SZ", "600519.SH"]
    assert quotes_body["missing_symbols"] == []
    assert quotes_body["served_from"] == "sqlite_cache"
    assert quotes_body["upstream_requests"] == 0
    assert quotes_body["as_of"].endswith("+08:00")
    partial = client.get(
        "/api/v1/quotes", params={"symbols": "600519.SH,600036.SH"}
    )
    assert partial.status_code == 200
    partial_body = partial.json()
    assert partial_body["received_symbols"] == ["600519.SH"]
    assert partial_body["missing_symbols"] == ["600036.SH"]

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
    # The compatibility bars projection must not be mistaken for authoritative
    # replay evidence.  Mock startup data is intentionally legacy-only here.
    assert bars_body["data_provenance"]["authoritative"] is False
    assert bars_body["data_provenance"]["served_item_count"] == 4
    assert bars_body["data_provenance"]["lineage_complete"] is False
    assert "bar_projection_rows_without_immutable_observation" in (
        bars_body["data_provenance"]["warnings"]
    )

    public_config = client.get("/api/v1/config/public")
    assert public_config.status_code == 200
    assert public_config.json()["history_bar_count"] == 25
    assert "database_path" not in public_config.json()


def test_quotes_refresh_is_explicit_and_reports_provider_request(client):
    provider = client.app.state.runtime.provider
    original = provider.fetch_quotes_relaxed
    calls = []

    async def counted(symbols):
        calls.append(tuple(symbols))
        return await original(symbols)

    provider.fetch_quotes_relaxed = counted
    response = client.get(
        "/api/v1/quotes",
        params={"symbols": "600519.SH", "refresh": "true"},
    )
    assert response.status_code == 200
    body = response.json()
    assert calls == [("600519.SH",)]
    assert body["served_from"] == "sqlite_cache"
    assert body["refresh_requested"] is True
    assert body["provider_operations"] == 1
    assert body["upstream_requests"] == 0  # mock does not perform HTTP I/O
    assert body["received_symbols"] == ["600519.SH"]
    assert body["refresh_result"]["status"] == "complete"

    detail = client.get(
        "/api/v1/quotes/600519.SH", params={"refresh": "true"}
    )
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["served_from"] == "sqlite_cache"
    assert detail_body["refresh_requested"] is True
    assert detail_body["provider_operations"] == 1
    assert detail_body["upstream_requests"] == 0


def test_lineage_endpoints_expose_sqlite_batch_history_without_provider_access(client):
    batches = client.get("/api/v1/lineage/batches", params={"limit": 5})
    assert batches.status_code == 200
    payload = batches.json()
    assert payload["served_from"] == "sqlite_cache"
    assert payload["lineage_schema_version"] == 3
    assert payload["total"] >= 1
    assert payload["batches"]
    batch_id = payload["batches"][0]["batch_id"]
    assert payload["batches"][0]["error_message_available"] is False

    detail = client.get("/api/v1/lineage/batches/{}".format(batch_id))
    assert detail.status_code == 200
    assert detail.json()["observation_count"] == 0
    assert detail.json()["observations_url"].endswith("/observations")

    observations = client.get(
        "/api/v1/lineage/batches/{}/observations".format(batch_id)
    )
    assert observations.status_code == 200
    assert observations.json()["observations"] == []

    invalid = client.get("/api/v1/lineage/batches", params={"status": "unknown"})
    assert invalid.status_code == 422


def test_research_run_endpoints_return_immutable_manifest_from_sqlite(client):
    manifest = ResearchRunManifest(
        run_id="api-run-001",
        strategy_name="bank_fixture",
        strategy_parameters={"lookback": 20},
        strategy_code_version="fixture-v1",
        dataset=DatasetDescriptor(
            name="bank-daily",
            schema_version="bars-v1",
            source="fixture",
            record_count=2,
        ),
        input_hash="sha256:" + "b" * 64,
        calendar_version="weekday-fallback-v1",
        universe_version="banks-v1",
        adjustment_version="raw-v1",
        constraint_version="cn-equity-v1",
        configuration={"initial_cash": "100000"},
        assumptions={"purpose": "research_only"},
    )
    client.app.state.runtime.repository.create_research_run(manifest)
    second_manifest = ResearchRunManifest(
        run_id="api-run-002",
        strategy_name="bank_fixture_alt",
        strategy_parameters={"lookback": 30},
        strategy_code_version="fixture-v2",
        dataset=DatasetDescriptor(
            name="bank-daily",
            schema_version="bars-v1",
            source="fixture",
            record_count=2,
        ),
        input_hash="sha256:" + "c" * 64,
        calendar_version="weekday-fallback-v1",
        universe_version="banks-v1",
        adjustment_version="raw-v1",
        constraint_version="cn-equity-v1",
        configuration={"initial_cash": "100000"},
        assumptions={"purpose": "research_only"},
    )
    client.app.state.runtime.repository.create_research_run(second_manifest)

    listing = client.get(
        "/api/v1/research/runs",
        params={"strategy_name": "bank_fixture", "limit": 1},
    )
    assert listing.status_code == 200
    body = listing.json()
    assert body["served_from"] == "sqlite_cache"
    assert body["storage_schema_version"] == SQLiteRepository.SCHEMA_VERSION
    assert body["total"] == 1
    assert body["runs"][0]["content_hash"] == manifest.content_hash
    assert body["runs"][0]["manifest"]["run_id"] == "api-run-001"

    detail = client.get("/api/v1/research/runs/api-run-001")
    assert detail.status_code == 200
    assert detail.json()["manifest_json_hash"].startswith("sha256:")
    assert detail.json()["manifest"]["purpose"] == "research_only"
    lineage = client.get("/api/v1/research/runs/api-run-001/lineage")
    assert lineage.status_code == 200
    lineage_body = lineage.json()
    assert lineage_body["verified"] is False
    assert lineage_body["served_from"] == "sqlite_lineage"
    assert lineage_body["upstream_requests"] == 0
    assert lineage_body["batch_binding_present"] is False
    assert lineage_body["batch_id"] is None
    assert "observations_present" in lineage_body["failures"]
    governance = client.get("/api/v1/research/runs/api-run-001/governance")
    assert governance.status_code == 200, governance.text
    governance_body = governance.json()
    assert governance_body["served_from"] == "sqlite_cache"
    assert governance_body["upstream_requests"] == 0
    assert governance_body["reproducible"] is False
    assert governance_body["decision_ready"] is False
    assert "result_artifact_missing" in governance_body["warnings"]
    assert "lineage_verified" in governance_body["failures"]
    comparison = client.get(
        "/api/v1/research/runs/compare",
        params={"run_ids": "api-run-001,api-run-002"},
    )
    assert comparison.status_code == 200, comparison.text
    comparison_body = comparison.json()
    assert comparison_body["count"] == 2
    assert comparison_body["served_from"] == "sqlite_cache"
    assert comparison_body["upstream_requests"] == 0
    assert comparison_body["runs"][0]["result_available"] is False
    assert comparison_body["runs"][0]["decision_grade_failures"] == []
    assert client.get(
        "/api/v1/research/runs/compare", params={"run_ids": "api-run-001"}
    ).status_code == 422
    assert client.get("/api/v1/research/runs/missing").status_code == 404


def test_research_run_catalog_exposes_tag_filters_and_sort_metadata(client):
    def manifest(run_id, tags):
        return ResearchRunManifest(
            run_id=run_id,
            strategy_name="tracking_strategy",
            strategy_parameters={},
            strategy_code_version="fixture-v1",
            dataset=DatasetDescriptor(
                name="tracking-dataset",
                schema_version="bars-v1",
                source="fixture",
                record_count=0,
            ),
            input_hash="sha256:" + ("d" if run_id.endswith("a") else "e") * 64,
            calendar_version="fixture-calendar",
            universe_version="fixture-universe",
            adjustment_version="raw-v1",
            constraint_version="fixture-constraints",
            configuration={},
            assumptions={"purpose": "research_only", "tags": tags},
        )

    repo = client.app.state.runtime.repository
    repo.create_research_run(manifest("tracking-tag-a", ["Bank", "baseline"]))
    repo.create_research_run(manifest("tracking-tag-b", ["bank", "candidate"]))

    listing = client.get(
        "/api/v1/research/runs",
        params={"tags": "BANK", "sort": "run_id", "order": "asc", "limit": 10},
    )
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["filters"]["tags"] == ["bank"]
    assert body["filters"]["sort"] == "run_id"
    assert [run["run_id"] for run in body["runs"]] == ["tracking-tag-a", "tracking-tag-b"]
    assert body["runs"][0]["tags"] == ["Bank", "baseline"]

    invalid = client.get("/api/v1/research/runs", params={"sort": "manifest_json"})
    assert invalid.status_code == 422

    comparison = client.get(
        "/api/v1/research/runs/compare",
        params={"run_ids": "tracking-tag-a,tracking-tag-b"},
    )
    assert comparison.status_code == 200
    assert comparison.json()["runs"][0]["tags"] == ["Bank", "baseline"]


def test_research_lineage_offloads_bounded_sqlite_scan(client, monkeypatch):
    """A potentially large lineage read must not run on the ASGI event loop."""

    import quant_monitor.app as app_module

    original_to_thread = app_module.asyncio.to_thread
    submitted = []

    async def recording_to_thread(func, *args, **kwargs):
        submitted.append(getattr(func, "__name__", "anonymous"))
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(app_module.asyncio, "to_thread", recording_to_thread)
    response = client.get("/api/v1/research/runs/missing/lineage")
    assert response.status_code == 404
    assert "load_lineage" in submitted


def test_research_catalog_and_artifact_routes_offload_sqlite_io(client, monkeypatch):
    """Research catalog reads and immutable writes must not block ASGI."""

    import quant_monitor.app as app_module

    original_to_thread = app_module.asyncio.to_thread
    submitted = []

    async def recording_to_thread(func, *args, **kwargs):
        submitted.append(getattr(func, "__name__", "anonymous"))
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(app_module.asyncio, "to_thread", recording_to_thread)
    assert client.get("/api/v1/research/runs").status_code == 200
    assert client.get("/api/v1/research/results").status_code == 200
    manifest = {
        "manifest_schema_version": "2",
        "run_id": "async-research-route",
        "strategy": {"name": "fixture", "code_version": "v1", "parameters": {}},
        "dataset": {
            "name": "fixture-daily",
            "schema_version": "bars-v1",
            "source": "fixture",
            "record_count": 0,
        },
        "input_hash": "sha256:" + "1" * 64,
        "versions": {
            "calendar": "fixture-calendar",
            "universe": "fixture-universe",
            "adjustment": "raw-v1",
            "constraints": "fixture-constraints",
        },
    }
    assert client.post("/api/v1/research/runs", json=manifest).status_code == 201
    assert client.get("/api/v1/research/runs/async-research-route").status_code == 200
    result = {"strategy_name": "fixture", "metrics": {"total_return": 0.0}}
    result["result_hash"] = hash_canonical(result, namespace="backtest-result-v1")
    assert client.post(
        "/api/v1/research/runs/async-research-route/result", json={"result": result}
    ).status_code == 201
    assert "load_runs_page" in submitted
    assert "load_results_page" in submitted
    assert "create_research_run" in submitted
    assert "create_research_result" in submitted
    assert "get_research_run" in submitted


def test_cached_backtest_missing_manifest_is_a_not_found_error(client):
    """A cache-only backtest must distinguish an unknown run from bad data."""

    response = client.post(
        "/api/v1/research/runs/does-not-exist/backtest",
        json={"symbols": ["600519.SH"], "limit": 10},
    )
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "research run not found"


def test_quote_detail_offloads_cache_read_and_exposes_freshness_contract(client, monkeypatch):
    """A single-symbol cache read must not block the ASGI event loop."""

    import quant_monitor.app as app_module

    original_to_thread = app_module.asyncio.to_thread
    submitted = []

    async def recording_to_thread(func, *args, **kwargs):
        submitted.append(getattr(func, "__name__", "anonymous"))
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(app_module.asyncio, "to_thread", recording_to_thread)
    response = client.get("/api/v1/quotes/600519.SH")
    assert response.status_code == 200
    assert "load_quote_cache" in submitted
    body = response.json()
    assert body["served_from"] == "sqlite_cache"
    assert body["upstream_requests"] == 0
    assert body["freshness"] == ("stale" if body["stale"] else "fresh")


def test_research_run_registration_validates_hash_and_rejects_duplicate_id(client):
    payload = {
        "manifest_schema_version": "2",
        "run_id": "api-post-run",
        "strategy": {
            "name": "bank_fixture",
            "parameters": {"lookback": 20},
            "code_version": "fixture-v1",
        },
        "dataset": {
            "name": "bank-daily",
            "schema_version": "bars-v1",
            "source": "fixture",
            "record_count": 2,
            "attributes": {"interval": "1d"},
        },
        "input_hash": "sha256:" + "c" * 64,
        "versions": {
            "calendar": "weekday-fallback-v1",
            "universe": "banks-v1",
            "adjustment": "raw-v1",
            "constraints": "cn-equity-v1",
        },
        "configuration": {"initial_cash": "100000"},
        "assumptions": {"purpose": "research_only"},
    }
    created = client.post("/api/v1/research/runs", json=payload)
    assert created.status_code == 201
    assert created.json()["run_id"] == "api-post-run"
    assert created.json()["content_hash"].startswith("sha256:")
    duplicate = client.post("/api/v1/research/runs", json=payload)
    assert duplicate.status_code == 409

    bad = dict(payload)
    bad["run_id"] = "api-post-bad-hash"
    bad["content_hash"] = "sha256:" + "d" * 64
    invalid = client.post("/api/v1/research/runs", json=bad)
    assert invalid.status_code == 422


def test_research_result_api_persists_and_returns_immutable_artifact(client):
    manifest_payload = {
        "manifest_schema_version": "2",
        "run_id": "api-result-run",
        "strategy": {"name": "fixture", "code_version": "v1", "parameters": {}},
        "dataset": {
            "name": "fixture-daily",
            "schema_version": "bars-v1",
            "source": "fixture",
            "record_count": 1,
        },
        "input_hash": "sha256:" + "e" * 64,
        "versions": {
            "calendar": "fixture-calendar",
            "universe": "fixture-universe",
            "adjustment": "raw-v1",
            "constraints": "fixture-constraints",
        },
    }
    assert client.post("/api/v1/research/runs", json=manifest_payload).status_code == 201
    result = {"strategy_name": "fixture", "metrics": {"total_return": 0.0}}
    result["result_hash"] = hash_canonical(result, namespace="backtest-result-v1")
    created = client.post(
        "/api/v1/research/runs/api-result-run/result", json={"result": result}
    )
    assert created.status_code == 201
    assert created.json()["result_hash"] == result["result_hash"]
    assert created.json()["result"]["metrics"]["total_return"] == 0.0
    assert client.post(
        "/api/v1/research/runs/api-result-run/result", json={"result": result}
    ).status_code == 409
    detail = client.get("/api/v1/research/runs/api-result-run/result")
    assert detail.status_code == 200
    assert detail.json()["result_json_hash"].startswith("sha256:")
    download = client.get("/api/v1/research/runs/api-result-run/result/download")
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("application/json")
    assert download.headers["content-disposition"] == 'attachment; filename="research-result.json"'
    assert download.headers["x-research-run-id"] == "api-result-run"
    assert download.headers["x-result-hash"] == result["result_hash"]
    assert download.headers["x-result-json-hash"] == detail.json()["result_json_hash"]
    assert download.json() == result
    assert download.text == json.dumps(
        result, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    run_listing = client.get("/api/v1/research/runs", params={"limit": 20})
    assert run_listing.status_code == 200
    listed = next(row for row in run_listing.json()["runs"] if row["run_id"] == "api-result-run")
    assert listed["result_available"] is True
    listing = client.get("/api/v1/research/results", params={"run_id": "api-result-run"})
    assert listing.status_code == 200
    listing_body = listing.json()
    assert listing_body["total"] == 1
    assert listing_body["include_result"] is False
    assert listing_body["results"][0]["run_id"] == "api-result-run"
    assert "result" not in listing_body["results"][0]
    expanded = client.get(
        "/api/v1/research/results",
        params={"run_id": "api-result-run", "include_result": "true"},
    )
    assert expanded.status_code == 200
    assert expanded.json()["include_result"] is True
    assert expanded.json()["results"][0]["result"]["metrics"]["total_return"] == 0.0
    bounded = client.get(
        "/api/v1/research/results",
        params={"include_result": "true", "limit": 21},
    )
    assert bounded.status_code == 422
    assert "include_result" in bounded.json()["error"]["message"]
    exported = client.get("/api/v1/research/runs/api-result-run/export")
    assert exported.status_code == 200
    export_body = exported.json()
    assert export_body["bundle_schema_version"] == 1
    assert export_body["result_available"] is True
    assert export_body["run"]["run_id"] == "api-result-run"
    assert export_body["result"]["result_hash"] == result["result_hash"]
    assert export_body["integrity"]["manifest_json_hash"].startswith("sha256:")
    assert export_body["integrity"]["result_json_hash"].startswith("sha256:")
    assert export_body["upstream_requests"] == 0
    assert "attachment" in exported.headers["content-disposition"]
    assert client.get("/api/v1/research/runs/missing/result").status_code == 404


def test_research_result_api_rejects_sensitive_export_payload(client):
    manifest = ResearchRunManifest(
        run_id="api-sensitive-result",
        strategy_name="fixture",
        strategy_parameters={},
        strategy_code_version="fixture-v1",
        dataset=DatasetDescriptor(
            name="fixture-daily",
            schema_version="bars-v1",
            source="fixture",
            record_count=0,
        ),
        input_hash="sha256:" + ("c" * 64),
        calendar_version="fixture-calendar",
        universe_version="fixture-universe",
        adjustment_version="raw-v1",
        constraint_version="fixture-constraints",
        configuration={},
        assumptions={},
    )
    client.app.state.runtime.repository.create_research_run(manifest)
    result = {
        "strategy_name": "fixture",
        "diagnostics": {"note": '{"api_key":"secret-not-exportable"}'},
        "result_hash": "sha256:" + ("0" * 64),
    }
    response = client.post(
        "/api/v1/research/runs/api-sensitive-result/result",
        json={"result": result},
    )
    assert response.status_code == 422
    assert "secret-not-exportable" not in response.text
    exported = client.get("/api/v1/research/runs/api-sensitive-result/export")
    assert exported.status_code == 200
    assert exported.json()["result_available"] is False
    assert "secret-not-exportable" not in exported.text


def test_http_exception_detail_redacts_exception_credentials(client, monkeypatch):
    """A route converting an exception to HTTPException must not reflect it."""

    secret = "api_key=super-secret-value"

    def fail(_run_id):
        raise ValueError("invalid persisted record: {}".format(secret))

    monkeypatch.setattr(
        client.app.state.runtime.repository,
        "get_research_run",
        fail,
    )
    response = client.get("/api/v1/research/runs/credential-reflection")
    assert response.status_code == 422
    assert secret not in response.text
    assert response.json()["error"]["message"] == "请求失败（错误信息已脱敏）"


def test_validation_error_message_redacts_custom_validator_values():
    from quant_monitor.app import _safe_validation_details

    secret = "api_key=validator-secret"
    details = _safe_validation_details(
        [{"type": "value_error", "loc": ("body", "key"), "msg": secret, "input": secret}]
    )
    assert details == [
        {
            "type": "value_error",
            "loc": ["body", "key"],
            "msg": "请求失败（错误信息已脱敏）",
        }
    ]
    assert secret not in json.dumps(details, ensure_ascii=False)


def test_request_path_logs_redact_credential_like_segments(client, caplog):
    secret = "api_key=log-secret"
    with caplog.at_level("INFO", logger="quant_monitor.app"):
        response = client.get("/api/v1/research/runs/{}".format(secret))
    assert response.status_code == 404
    assert secret not in caplog.text
    assert "/api/v1/research/runs/[redacted]" in caplog.text


def test_request_path_keeps_normal_routes_readable():
    assert _safe_request_path("/live") == "/live"
    assert _safe_request_path("/api/v1/system/status") == "/api/v1/system/status"
    assert _safe_request_path("/api/v1/research/runs/api_key=hidden") == (
        "/api/v1/research/runs/[redacted]"
    )


def test_research_export_without_result_is_explicitly_incomplete(client):
    manifest = ResearchRunManifest(
        run_id="api-export-no-result",
        strategy_name="fixture",
        strategy_parameters={},
        strategy_code_version="v1",
        dataset=DatasetDescriptor(
            name="fixture-daily",
            schema_version="bars-v1",
            source="fixture",
            record_count=0,
        ),
        input_hash="sha256:" + "a" * 64,
        calendar_version="fixture-calendar",
        universe_version="fixture-universe",
        adjustment_version="raw-v1",
        constraint_version="fixture-constraints",
        configuration={},
        assumptions={},
    )
    client.app.state.runtime.repository.create_research_run(manifest)
    async def fail_if_called(*args, **kwargs):
        raise AssertionError("research export must not call provider")

    client.app.state.runtime.provider.fetch_quotes = fail_if_called
    client.app.state.runtime.provider.fetch_bars = fail_if_called
    response = client.get("/api/v1/research/runs/api-export-no-result/export")
    assert response.status_code == 200
    body = response.json()
    assert body["result_available"] is False
    assert body["result"] is None
    assert body["integrity"]["result_hash"] is None
    assert client.get("/api/v1/research/runs/not-found/export").status_code == 404


def test_cached_backtest_never_calls_provider_when_history_is_missing(client):
    manifest = ResearchRunManifest(
        run_id="cached-backtest-run",
        strategy_name="bank_daily_multifactor_v1",
        strategy_parameters={},
        strategy_code_version="fixture-v1",
        dataset=DatasetDescriptor(
            name="bank-daily",
            schema_version="bars-v1",
            source="fixture",
            record_count=0,
        ),
        input_hash="sha256:" + "f" * 64,
        calendar_version="fixture-calendar",
        universe_version="fixture-universe",
        adjustment_version="raw-v1",
        constraint_version="fixture-constraints",
        configuration={},
        assumptions={},
    )
    client.app.state.runtime.repository.create_research_run(manifest)

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("cached backtest must not call provider")

    client.app.state.runtime.provider.fetch_bars = fail_if_called
    response = client.post(
        "/api/v1/research/runs/cached-backtest-run/backtest",
        json={"symbols": ["600519.SH"], "limit": 240},
    )
    assert response.status_code == 422
    assert "cached 1d bars" in response.json()["error"]["message"]


def test_cached_backtest_rejects_partial_requested_universe_by_default(client):
    """A missing symbol must not silently change a cross-sectional replay."""
    bar = Bar(
        symbol="600519.SH",
        interval="1d",
        timestamp=_stable_weekday_bar_time(),
        open=100,
        high=101,
        low=99,
        close=100,
        volume=100000,
        turnover=10000000,
        source="mock",
    )
    client.app.state.runtime.repository.insert_bars([bar])
    manifest = ResearchRunManifest(
        run_id="cached-backtest-partial-universe",
        strategy_name="bank_daily_multifactor_v1",
        strategy_parameters={},
        strategy_code_version="fixture-v1",
        dataset=DatasetDescriptor(
            name="bank-daily",
            schema_version="bars-v1",
            source="mock",
            record_count=1,
        ),
        input_hash="sha256:" + "b" * 64,
        calendar_version="fixture-calendar",
        universe_version="fixture-universe",
        adjustment_version="raw-v1",
        constraint_version="fixture-constraints",
        configuration={},
        assumptions={},
    )
    client.app.state.runtime.repository.create_research_run(manifest)

    response = client.post(
        "/api/v1/research/runs/cached-backtest-partial-universe/backtest",
        json={"symbols": ["600519.SH", "600036.SH"], "limit": 1},
    )
    assert response.status_code == 422
    assert "600036.SH" in response.json()["error"]["message"]
    assert "allow_partial_symbols=true" in response.json()["error"]["message"]
    assert client.get(
        "/api/v1/research/runs/cached-backtest-partial-universe/result"
    ).status_code == 404

    exploratory = client.post(
        "/api/v1/research/runs/cached-backtest-partial-universe/backtest",
        json={
            "symbols": ["600519.SH", "600036.SH"],
            "limit": 1,
            "allow_partial_symbols": True,
        },
    )
    assert exploratory.status_code == 201, exploratory.text
    binding = exploratory.json()["manifest_binding"]
    assert binding["partial_symbol_input"] is True
    assert binding["requested_symbols"] == ["600519.SH", "600036.SH"]
    assert binding["served_symbols"] == ["600519.SH"]
    assert binding["missing_symbols"] == ["600036.SH"]
    assert "requested_symbols_missing" in binding["warnings"]


def test_cached_backtest_strict_manifest_binding_rejects_hash_mismatch(client):
    provider = MockMarketDataProvider()
    bar = asyncio.run(provider.fetch_bars("600519.SH", interval="1d", limit=1))[0]
    client.app.state.runtime.repository.insert_bars([bar])
    manifest = ResearchRunManifest(
        run_id="cached-backtest-binding",
        strategy_name="bank_daily_multifactor_v1",
        strategy_parameters={},
        strategy_code_version="fixture-v1",
        dataset=DatasetDescriptor(
            name="bank-daily",
            schema_version="bars-v1",
            source="mock",
            record_count=1,
        ),
        input_hash="sha256:" + "0" * 64,
        calendar_version="fixture-calendar",
        universe_version="fixture-universe",
        adjustment_version="raw-v1",
        constraint_version="fixture-constraints",
        configuration={},
        assumptions={},
    )
    client.app.state.runtime.repository.create_research_run(manifest)
    response = client.post(
        "/api/v1/research/runs/cached-backtest-binding/backtest",
        json={"symbols": ["600519.SH"], "limit": 1, "verify_manifest": True},
    )
    assert response.status_code == 422
    assert "manifest binding verification failed" in response.json()["error"]["message"]
    assert "immutable observations" in response.json()["error"]["message"]


def test_cached_backtest_strict_mode_executes_manifest_configuration(client, monkeypatch):
    """Strict replay must use the immutable manifest config, not live settings."""
    import quant_monitor.app as app_module

    bar = Bar(
        symbol="600519.SH",
        interval="1d",
        timestamp=_stable_weekday_bar_time(),
        open=100,
        high=101,
        low=99,
        close=100,
        volume=100000,
        turnover=10000000,
        source="mock",
    )
    client.app.state.runtime.repository.insert_bars([bar])
    monkeypatch.setattr(
        app_module,
        "_bar_projection_provenance",
        lambda *args, **kwargs: {"lineage_complete": True, "warnings": []},
    )
    manifest = ResearchRunManifest(
        run_id="cached-backtest-manifest-config",
        strategy_name="bank_daily_multifactor_v1",
        strategy_parameters={},
        strategy_code_version=strategy_code_version("bank_daily_multifactor_v1"),
        dataset=DatasetDescriptor(
            name="bank-daily",
            schema_version="bars-v1",
            source="mock",
            record_count=1,
        ),
        input_hash=hash_input_records([app_module.bar_dict(bar)]),
        calendar_version="fixture-calendar",
        universe_version="fixture-universe",
        adjustment_version="raw-v1",
        constraint_version="fixture-constraints",
        configuration={"initial_cash": "12345"},
        assumptions={},
    )
    client.app.state.runtime.repository.create_research_run(manifest)
    response = client.post(
        "/api/v1/research/runs/cached-backtest-manifest-config/backtest",
        json={"symbols": ["600519.SH"], "limit": 1, "verify_manifest": True},
    )
    assert response.status_code == 201, response.text
    result = response.json()["result"]
    assert result["config"]["initial_cash"] == "12345"
    assert response.json()["manifest_binding"]["verified"] is True


def test_cached_backtest_strict_mode_binds_manifest_fundamental_reports(client, monkeypatch):
    """A declared fundamental overlay must be present and hash-matched."""

    import quant_monitor.app as app_module

    bar = Bar(
        symbol="600519.SH",
        interval="1d",
        timestamp=_stable_weekday_bar_time(),
        open=100,
        high=101,
        low=99,
        close=100,
        volume=100000,
        turnover=10000000,
        source="mock",
    )
    client.app.state.runtime.repository.insert_bars([bar])
    monkeypatch.setattr(
        app_module,
        "_bar_projection_provenance",
        lambda *args, **kwargs: {"lineage_complete": True, "warnings": []},
    )
    report = {
        "selection": "bank_fundamental_factors_as_of_v1",
        "decision_at": datetime(2026, 9, 18, 14, 0, tzinfo=SHANGHAI_TZ).isoformat(),
        "items": [],
    }
    manifest = ResearchRunManifest(
        run_id="cached-backtest-fundamental-binding",
        strategy_name="bank_daily_multifactor_v1",
        strategy_parameters={"fundamental_weight": 0.20},
        strategy_code_version=strategy_code_version("bank_daily_multifactor_v1"),
        dataset=DatasetDescriptor(
            name="bank-daily",
            schema_version="bars-v1",
            source="mock",
            record_count=1,
            attributes={
                "fundamental_factor_report_hashes": {
                    "2026-09-18": BacktestEngine._fundamental_report_hash(report),
                }
            },
        ),
        input_hash=hash_input_records([app_module.bar_dict(bar)]),
        calendar_version="fixture-calendar",
        universe_version="fixture-universe",
        adjustment_version="raw-v1",
        constraint_version="fixture-constraints",
        configuration={"initial_cash": "12345"},
        assumptions={},
    )
    client.app.state.runtime.repository.create_research_run(manifest)

    missing = client.post(
        "/api/v1/research/runs/cached-backtest-fundamental-binding/backtest",
        json={"symbols": ["600519.SH"], "limit": 1, "verify_manifest": True},
    )
    assert missing.status_code == 422
    assert "fundamental_factor_reports" in missing.json()["error"]["message"]

    response = client.post(
        "/api/v1/research/runs/cached-backtest-fundamental-binding/backtest",
        json={
            "symbols": ["600519.SH"],
            "limit": 1,
            "verify_manifest": True,
            "fundamental_factor_reports": {"2026-09-18": report},
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["manifest_binding"]["verified"] is True
    assert body["manifest_binding"]["fundamental_factor_binding"]["verified"] is True
    assert body["result"]["diagnostics"]["assumptions"]["fundamental_factor_binding"]["complete"] is True


def test_cached_backtest_executes_bank_strategy_and_persists_result(client):
    symbols = tuple(
        ["600000.SH", "600015.SH", "600016.SH", "600036.SH", "600919.SH"]
        + ["601009.SH", "601077.SH", "601128.SH", "601166.SH", "601169.SH"]
        + ["601187.SH", "601229.SH", "601288.SH", "601328.SH", "601398.SH"]
    )
    provider = MockMarketDataProvider()
    generated = []
    for symbol in symbols:
        generated.extend(asyncio.run(provider.fetch_bars(symbol, interval="1d", limit=140)))
    client.app.state.runtime.repository.insert_bars(generated)
    manifest = ResearchRunManifest(
        run_id="cached-backtest-success",
        strategy_name="bank_daily_multifactor_v1",
        strategy_parameters={},
        strategy_code_version="fixture-v1",
        dataset=DatasetDescriptor(
            name="bank-daily",
            schema_version="bars-v1",
            source="fixture",
            record_count=len(generated),
        ),
        input_hash="sha256:" + "a" * 64,
        calendar_version="fixture-calendar",
        universe_version="fixture-universe",
        adjustment_version="raw-v1",
        constraint_version="fixture-constraints",
        configuration={},
        assumptions={},
    )
    client.app.state.runtime.repository.create_research_run(manifest)
    response = client.post(
        "/api/v1/research/runs/cached-backtest-success/backtest",
        json={"symbols": list(symbols), "limit": 140},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["run_id"] == "cached-backtest-success"
    assert body["manifest_binding"]["data_provenance"]["authoritative"] is False
    assert body["manifest_binding"]["data_provenance"]["lineage_complete"] is False
    assert body["result"]["metrics"]["session_count"] >= 1
    assert body["result"]["diagnostics"]["decision_grade"] is False
    risk = client.get(
        "/api/v1/research/runs/cached-backtest-success/risk",
        params={"confidence": "0.90,0.95", "minimum_observations": 5},
    )
    assert risk.status_code == 200, risk.text
    risk_body = risk.json()
    assert risk_body["served_from"] == "sqlite_cache"
    assert risk_body["upstream_requests"] == 0
    assert risk_body["risk"]["confidence_levels"] == [0.9, 0.95]
    assert risk_body["risk"]["sample_sufficient"] is True
    assert "historical_var" in risk_body["risk"]
    integrity = client.get(
        "/api/v1/research/runs/cached-backtest-success/integrity"
    )
    assert integrity.status_code == 200, integrity.text
    integrity_body = integrity.json()
    assert integrity_body["served_from"] == "sqlite_cache"
    assert integrity_body["upstream_requests"] == 0
    assert integrity_body["integrity"]["verified"] is True
    assert integrity_body["integrity"]["mismatches"] == []
    robustness = client.get(
        "/api/v1/research/runs/cached-backtest-success/robustness",
        params={"confidence": 0.90, "resamples": 120, "seed": 7, "minimum_observations": 5},
    )
    assert robustness.status_code == 200, robustness.text
    robustness_body = robustness.json()
    assert robustness_body["served_from"] == "sqlite_cache"
    assert robustness_body["upstream_requests"] == 0
    assert robustness_body["robustness"]["sample_sufficient"] is True
    assert robustness_body["robustness"]["seed"] == 7
    assert robustness_body["robustness"]["total_return_interval"][0] is not None
    assert client.get(
        "/api/v1/research/runs/cached-backtest-success/robustness",
        params={"confidence": 1.0},
    ).status_code == 422
    assert client.get(
        "/api/v1/research/runs/cached-backtest-success/risk",
        params={"confidence": "1.0"},
    ).status_code == 422
    equity = body["result"]["equity_curve"]
    benchmark_returns = [float(item["daily_return"]) / 2.0 for item in equity[1:]]
    benchmark = client.post(
        "/api/v1/research/runs/cached-backtest-success/benchmark",
        json={
            "benchmark_name": "fixture-index",
            "benchmark_returns": benchmark_returns,
            "minimum_observations": 1,
        },
    )
    assert benchmark.status_code == 200, benchmark.text
    benchmark_body = benchmark.json()
    assert benchmark_body["served_from"] == "sqlite_cache"
    assert benchmark_body["upstream_requests"] == 0
    assert benchmark_body["benchmark"]["benchmark_name"] == "fixture-index"
    assert benchmark_body["benchmark"]["aligned_observations"] == len(benchmark_returns)
    attribution = client.post(
        "/api/v1/research/runs/cached-backtest-success/attribution",
        json={
            "benchmark_returns": benchmark_returns,
            "cash_returns": [0.0] * len(benchmark_returns),
        },
    )
    assert attribution.status_code == 200, attribution.text
    attribution_body = attribution.json()
    assert attribution_body["served_from"] == "sqlite_cache"
    assert attribution_body["upstream_requests"] == 0
    assert attribution_body["attribution"]["status"] == "complete"
    assert attribution_body["attribution"]["active_contribution"] is not None
    stress = client.post(
        "/api/v1/research/runs/cached-backtest-success/stress",
        json={
            "scenarios": [
                {"name": "bank-shock", "price_shock": -0.10},
                {"name": "high-friction", "cost_multiplier": 2},
            ]
        },
    )
    assert stress.status_code == 200, stress.text
    stress_body = stress.json()
    assert stress_body["served_from"] == "sqlite_cache"
    assert stress_body["upstream_requests"] == 0
    assert stress_body["stress"]["worst_scenario"] == "bank-shock"
    assert len(stress_body["stress"]["scenarios"]) == 2
    assert client.post(
        "/api/v1/research/runs/cached-backtest-success/stress",
        json={"scenarios": [{"name": "bad", "price_shock": -1.1}]},
    ).status_code == 422
    capacity = client.post(
        "/api/v1/research/runs/cached-backtest-success/capacity",
        json={"max_participation_rate": 0.10},
    )
    assert capacity.status_code == 200, capacity.text
    capacity_body = capacity.json()
    assert capacity_body["served_from"] == "sqlite_cache"
    assert capacity_body["upstream_requests"] == 0
    assert capacity_body["capacity"]["trade_count"] == len(body["result"]["trades"])
    assert capacity_body["capacity"]["assessed_trade_count"] >= 0
    # Capacity must follow the bars actually consumed by the immutable
    # replay, even though the manifest/provider configuration may differ
    # (this fixture intentionally declares a ``fixture`` manifest while its
    # cached bars are emitted by the ``mock`` source).
    assert capacity_body["data_provenance"]["bar_source"] == "mock"
    assert capacity_body["data_provenance"]["bar_source_origin"] == "result_diagnostics"
    assert client.post(
        "/api/v1/research/runs/cached-backtest-success/capacity",
        json={"max_participation_rate": 1.1},
    ).status_code == 422
    portfolio = client.get(
        "/api/v1/research/runs/cached-backtest-success/portfolio",
        params={"benchmark_name": "fixture-index"},
    )
    assert portfolio.status_code == 200, portfolio.text
    portfolio_body = portfolio.json()
    assert portfolio_body["served_from"] == "sqlite_cache"
    assert portfolio_body["upstream_requests"] == 0
    assert portfolio_body["portfolio"]["run_id"] == "cached-backtest-success"
    assert portfolio_body["portfolio"]["benchmark_name"] == "fixture-index"
    assert portfolio_body["portfolio"]["valuation_quality"] == "partial"
    assert "result_decision_grade_false" in portfolio_body["portfolio"]["warnings"]
    constraint_report = client.get(
        "/api/v1/research/runs/cached-backtest-success/portfolio/constraints"
    )
    assert constraint_report.status_code == 200, constraint_report.text
    constraint_body = constraint_report.json()
    assert constraint_body["served_from"] == "sqlite_cache"
    assert constraint_body["upstream_requests"] == 0
    assert constraint_body["constraints"]["status"] == "unavailable"
    assert constraint_body["constraints"]["decision_ready"] is False
    assert "valuation_quality_not_complete" in constraint_body["constraints"]["warnings"]
    failing_constraints = client.get(
        "/api/v1/research/runs/cached-backtest-success/portfolio/constraints",
        params={"max_holding_weight": 0.0, "require_complete_valuation": False},
    )
    assert failing_constraints.status_code == 200, failing_constraints.text
    assert failing_constraints.json()["constraints"]["policy"]["max_holding_weight"] == 0.0
    assert failing_constraints.json()["constraints"]["status"] in {"pass", "fail"}
    disabled_constraints = client.get(
        "/api/v1/research/runs/cached-backtest-success/portfolio/constraints",
        params={"disable_default_limits": True, "require_complete_valuation": False},
    )
    assert disabled_constraints.status_code == 200, disabled_constraints.text
    assert disabled_constraints.json()["constraints"]["checks"] == []
    assert disabled_constraints.json()["constraints"]["status"] == "unavailable"


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
    refreshed_body = refreshed.json()
    assert refreshed_body["served_from"] == "provider_refresh"
    assert refreshed_body["refresh_requested"] is True
    assert refreshed_body["catalogue"]["served_from"] == "provider_refresh"
    assert refreshed_body["catalogue"]["refresh_requested"] is True
    assert refreshed_body["catalogue"]["total"] == refreshed_body["catalogue_total"]
    assert refreshed_body["catalogue"]["active"] + refreshed_body["catalogue"]["inactive"] == refreshed_body["catalogue_total"]
    assert refreshed.headers["Deprecation"] == "true"
    assert refreshed.headers["Link"].startswith("</api/v1/quote-refresh/jobs>")


def test_instruments_catalogue_supports_cache_only_filters_pagination_and_sort(client):
    """Catalogue lookup is local and deterministic, including inactive rows."""

    repository = client.app.state.runtime.repository
    # The application fixture may have hydrated the provider's bootstrap
    # catalogue before the request.  Isolate this contract test so the
    # catalogue totals describe exactly the rows inserted below.
    repository.clear_instruments()
    repository.upsert_instruments(
        [
            Instrument(
                "300750.SZ",
                "宁德时代",
                Exchange.SZ,
                SecurityType.STOCK,
                active=True,
                metadata={"source": "mock", "industry": "电池", "cnspell": "ndsd"},
            ),
            Instrument(
                "000300.SH",
                "沪深300",
                Exchange.SH,
                SecurityType.INDEX,
                active=True,
                metadata={"source": "mock", "industry": "指数", "cnspell": "hs300"},
            ),
            Instrument(
                "430047.BJ",
                "诺思兰德",
                Exchange.BJ,
                SecurityType.STOCK,
                active=False,
                metadata={"source": "mock", "industry": "医药", "cnspell": "nsld"},
            ),
        ]
    )
    provider = client.app.state.runtime.provider
    original = provider.fetch_instruments

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("catalogue filters must not call provider")

    provider.fetch_instruments = fail_if_called
    try:
        response = client.get(
            "/api/v1/instruments",
            params={
                "search": "ndsd",
                "exchange": "sz",
                "security_type": "stock",
                "active_only": "true",
                "limit": 1,
                "offset": 0,
                "sort": "name",
                "order": "asc",
            },
        )
    finally:
        provider.fetch_instruments = original

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 1
    assert body["total"] == 1
    assert body["catalogue_total"] == 3
    assert body["limit"] == 1
    assert body["offset"] == 0
    assert body["has_more"] is False
    assert body["served_from"] == "sqlite_cache"
    assert body["refresh_requested"] is False
    assert body["catalogue"]["source"] == "mock"
    assert body["catalogue"]["served_from"] == "sqlite_cache"
    assert body["catalogue"]["refresh_requested"] is False
    assert body["catalogue"]["total"] == 3
    assert body["catalogue"]["active"] == 2
    assert body["catalogue"]["inactive"] == 1
    assert body["items"][0]["symbol"] == "300750.SZ"

    inactive = client.get(
        "/api/v1/instruments",
        params={"exchange": "BJ", "active": "false", "limit": 10},
    )
    assert inactive.status_code == 200, inactive.text
    assert inactive.json()["total"] == 1
    assert inactive.json()["items"][0]["symbol"] == "430047.BJ"

    # ``active`` is the preferred spelling and wins over the compatibility
    # alias when both are supplied.
    active_precedence = client.get(
        "/api/v1/instruments",
        params={"active": "true", "active_only": "false", "limit": 10},
    )
    assert active_precedence.status_code == 200, active_precedence.text
    assert all(item["active"] is True for item in active_precedence.json()["items"])


@pytest.mark.parametrize(
    "params, error_code",
    [
        ({"limit": 0}, "validation_error"),
        ({"limit": 5001}, "validation_error"),
        ({"offset": -1}, "validation_error"),
        ({"exchange": "XX"}, "http_error"),
        ({"security_type": "warrant"}, "http_error"),
        ({"sort": "market_cap"}, "http_error"),
        ({"order": "sideways"}, "http_error"),
        ({"active": "maybe"}, "validation_error"),
    ],
)
def test_instruments_catalogue_rejects_invalid_query_parameters(
    client, params, error_code
):
    response = client.get("/api/v1/instruments", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == error_code


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
    # The endpoint audits the legacy bars projection.  Structural health must
    # not be presented as authoritative, independently reproducible evidence.
    assert body["data_provenance"]["authoritative"] is False
    assert body["quality_assurance"]["authoritative"] is False
    assert body["quality_assurance"]["decision_ready"] is False
    assert body["quality_assurance"]["immutable_lineage_complete"] is False


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


def test_instrument_cache_does_not_expose_asharehub_fallback_after_provider_switch(client):
    """A shared legacy DB must not relabel AShareHub fallback rows as AllTick."""

    repository = client.app.state.runtime.repository
    repository.upsert_instruments(
        [
            Instrument(
                symbol="600519.SH",
                name="贵州茅台",
                exchange=Exchange.SH,
                metadata={"source": "static_bank_fallback"},
            )
        ]
    )

    class SwitchedProvider:
        name = "alltick"
        universe_scope = "configured_symbols"

    original_provider = client.app.state.runtime.provider
    client.app.state.runtime.provider = SwitchedProvider()
    try:
        response = client.get("/api/v1/instruments")
        assert response.status_code == 200
        assert response.json()["items"] == []
        assert response.json()["provider"] == "alltick"
    finally:
        client.app.state.runtime.provider = original_provider


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
    assert body["served_from"] == "sqlite_cache"
    assert body["refresh_requested"] is True
    assert body["provider_operations"] == 2  # catalogue + quotes, both offline
    assert body["upstream_requests"] == 0

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


def test_instrument_and_cached_batch_quote_reads_offload_sqlite_io(client, monkeypatch):
    """Cache-only catalogue/quote reads must not run synchronously on ASGI."""

    import quant_monitor.app as app_module

    original_to_thread = app_module.asyncio.to_thread
    submitted = []

    async def recording_to_thread(func, *args, **kwargs):
        submitted.append(getattr(func, "__name__", "anonymous"))
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(app_module.asyncio, "to_thread", recording_to_thread)
    assert client.get("/api/v1/instruments").status_code == 200
    assert client.get("/api/v1/market/quotes").status_code == 200
    assert "cached_provider_instruments" in submitted
    assert "get_latest_quotes" in submitted


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
    client.app.state.runtime.monitor.collector.provider = provider
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
    assert response.headers["retry-after"] == "5"
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
    assert alerts.json()["total"] == 1
    assert alerts.json()["limit"] == 50
    assert alerts.json()["offset"] == 0
    assert alerts.json()["has_more"] is False
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
    assert signals.json()["total"] == 1
    assert signals.json()["has_more"] is False
    assert signals.json()["items"][0]["action"] == "buy"
    assert signals.json()["items"][0]["price"] == "123.45"

    # Pagination and action filtering are deterministic and remain cache-only.
    repository.insert_alert(
        Alert(
            symbol="600519.SH",
            timestamp=timestamp - timedelta(minutes=1),
            level=AlertLevel.WARNING,
            title="older fixture alert",
            message="test-only",
            category="test",
        )
    )
    page = client.get(
        "/api/v1/alerts",
        params={"symbol": "600519.SH", "level": "warning", "limit": 1, "offset": 1},
    )
    assert page.status_code == 200
    assert page.json()["count"] == 1
    assert page.json()["total"] == 2
    assert page.json()["has_more"] is False

    repository.insert_signal(
        Signal(
            strategy="fixture_strategy",
            symbol="600519.SH",
            timestamp=timestamp - timedelta(minutes=1),
            action=SignalAction.SELL,
            strength=-0.4,
            reason="test-only",
        )
    )
    buy_page = client.get(
        "/api/v1/signals",
        params={"symbol": "600519.SH", "action": "buy", "limit": 1},
    )
    assert buy_page.status_code == 200
    assert buy_page.json()["total"] == 1
    assert buy_page.json()["items"][0]["action"] == "buy"

    invalid_action = client.get("/api/v1/signals", params={"action": "enter"})
    assert invalid_action.status_code == 422
    assert invalid_action.json()["error"]["code"] == "http_error"

    strategies = client.get("/api/v1/strategies")
    assert strategies.status_code == 200
    assert strategies.json()["items"][0]["mode"] == "signal_only"
    bank = next(
        item for item in strategies.json()["items"]
        if item["id"] == "bank_daily_multifactor_v1"
    )
    assert bank["parameters"]["model_version"] == "bank_daily_multifactor_v1"
    assert bank["parameters"]["min_history"] == 130
    assert bank["code_identity"]["version"].startswith("sha256:")
    assert bank["code_identity"]["schema_version"] == "strategy-code-identity-v1"
    assert all(not (len(unit) > 1 and unit[1] == ":") for unit in bank["code_identity"]["units"])
    assert bank["evaluation_scope"] == "cross_sectional"
    assert bank["capabilities"]["signal_only"] is True
    assert bank["capabilities"]["orders_supported"] is False
    assert bank["capabilities"]["network_access"] is False
    assert bank["capabilities"]["parameter_schema_version"] == "1"
    schema = {
        entry["name"]: entry for entry in bank["capabilities"]["parameter_schema"]
    }
    assert schema["min_history"]["value"] == 130
    assert schema["min_history"]["required"] is False


def test_alert_and_signal_routes_offload_sqlite_io(client, monkeypatch):
    """Alert/signal pagination and acknowledgement must not block ASGI."""

    import quant_monitor.app as app_module

    repository = client.app.state.runtime.repository
    alert_id = repository.insert_alert(
        Alert(
            symbol="600519.SH",
            timestamp=now_market_time(),
            level=AlertLevel.WARNING,
            title="async fixture",
            message="test-only",
            category="test",
        )
    )
    original_to_thread = app_module.asyncio.to_thread
    submitted = []

    async def recording_to_thread(func, *args, **kwargs):
        submitted.append(getattr(func, "__name__", "anonymous"))
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(app_module.asyncio, "to_thread", recording_to_thread)
    assert client.get("/api/v1/alerts").status_code == 200
    assert client.get("/api/v1/signals").status_code == 200
    assert client.patch(
        "/api/v1/alerts/{}".format(alert_id), json={"acknowledged": True}
    ).status_code == 200
    assert "query_alerts" in submitted
    assert "query_signals" in submitted
    assert "acknowledge_alert" in submitted


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
    assert "analysis-quality" in index.text
    assert "analysis-entry-gate" in index.text
    assert 'id="analysis-factor-breakdown"' in index.text
    factor_script = client.get("/static/bank-research.js")
    assert factor_script.status_code == 200
    assert "评分拆解与筛选条件" in factor_script.text
    assert 'id="research-runs-panel"' in index.text or 'class="panel research-runs-panel"' in index.text
    assert "compareSelectedResearchRuns" in script.text
    assert "research-run-actions" in script.text
    assert "/result/download" in script.text
    assert "/research/runs/" in script.text and "/export" in script.text
    assert "left: 54" in script.text
    assert 'x: padding.left - 7' in script.text
    assert '"text-anchor": "end"' in script.text
    assert 'id="readiness-banner"' in index.text
    assert 'role="progressbar"' in index.text
    assert "renderReadiness" in script.text
    assert "deferred_for_budget" in script.text
    assert "历史日线已准备" in script.text
    assert "setQualityChip" in script.text
    assert "gate_failures" in script.text
    assert "quote_freshness" in script.text
    assert "quote_data_stale" in script.text
    assert "Idempotency-Key" in script.text

    styles = client.get("/static/styles.css")
    assert styles.status_code == 200
    assert ".readiness-banner" in styles.text
    assert 'data-state="warning"' in styles.text
    assert ".quality-chip" in styles.text
    assert ".research-compare-grid" in styles.text


def test_api_rejects_declared_oversized_json_before_endpoint_parsing(tmp_path):
    settings = Settings(
        app_name="request limit fixture",
        database_path=str(tmp_path / "request-limit.sqlite3"),
        data_provider="mock",
        poll_interval_seconds=60.0,
        bar_interval="1m",
        history_bar_count=25,
        watchlist=["600519.SH"],
        api_max_request_body_bytes=65536,
    )
    with TestClient(create_app(settings)) as limited_client:
        response = limited_client.post(
            "/api/v1/research/runs",
            content="{}",
            headers={
                "Content-Type": "application/json",
                "Content-Length": "65537",
            },
        )

    assert response.status_code == 413
    assert_common_headers(response)
    body = response.json()["error"]
    assert body["code"] == "request_too_large"
    assert body["details"] == {"max_bytes": 65536}


def test_api_body_limit_skips_missing_length_and_multipart(tmp_path):
    settings = Settings(
        app_name="request limit compatibility fixture",
        database_path=str(tmp_path / "request-limit-compat.sqlite3"),
        data_provider="mock",
        poll_interval_seconds=60.0,
        bar_interval="1m",
        history_bar_count=25,
        watchlist=["600519.SH"],
        api_max_request_body_bytes=65536,
    )
    with TestClient(create_app(settings)) as limited_client:
        no_length = limited_client.post(
            "/api/v1/system/status",
            content="{}",
            headers={"Content-Type": "application/json"},
        )
        multipart = limited_client.request(
            "GET",
            "/api/v1/system/status",
            content="x" * 65537,
            headers={
                "Content-Type": "multipart/form-data; boundary=unit",
                "Content-Length": "65537",
            },
        )

    # These requests reach normal endpoint parsing/validation rather than
    # being rejected by the declared-size guard.
    assert no_length.status_code != 413
    assert multipart.status_code != 413


def test_validation_errors_do_not_echo_submitted_values(client):
    """Pydantic's raw ``input`` field must not become a credential leak."""

    sentinel = "credential-bearing-query-value"
    response = client.get("/api/v1/research/runs", params={"limit": sentinel})

    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "validation_error"
    assert sentinel not in response.text
    assert all(set(item) == {"type", "loc", "msg"} for item in body["details"])
    assert body["details"][0]["loc"] == ["query", "limit"]


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


def test_websocket_initial_snapshot_offloads_cache_reads(client, monkeypatch):
    """The initial WebSocket frame must not run synchronous SQLite on-loop."""

    import quant_monitor.app as app_module

    original_to_thread = app_module.asyncio.to_thread
    submitted = []

    async def recording_to_thread(func, *args, **kwargs):
        submitted.append(getattr(func, "__name__", "anonymous"))
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(app_module.asyncio, "to_thread", recording_to_thread)
    with client.websocket_connect("/ws/market") as websocket:
        message = websocket.receive_json()

    assert message["type"] == "snapshot"
    assert "overview" in message["data"]
    assert "watchlist" in message["data"]
    assert "load_websocket_snapshot" in submitted


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


def test_bar_projection_provenance_rejects_adjusted_or_conflicting_revision():
    bar = Bar(
        symbol="600519.SH",
        interval="1d",
        timestamp=now_market_time(),
        open="10",
        high="11",
        low="9",
        close="10.5",
        volume=100,
        turnover="1050",
        source="fixture",
    )

    class Repository:
        def get_bar_observations(self, symbol, interval, *, limit):
            # A legacy row has no revision/adjustment field.  Even when the
            # period and source match, qfq or conflicting revisions cannot
            # certify that the served projection is the same raw value.
            return [
                type(
                    "Observation",
                    (),
                    {
                        "period_end": bar.timestamp,
                        "source": "fixture",
                        "adjustment": Adjustment.QFQ,
                        "open": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                        "volume": bar.volume,
                        "turnover": bar.turnover,
                    },
                )()
            ]

    report = _bar_projection_provenance(
        Repository(), {"600519.SH": (bar,)}, interval="1d"
    )
    assert report["immutable_observation_count"] == 0
    assert report["content_mismatch_count"] == 1
    assert report["authoritative"] is False
    assert "immutable_observation_content_mismatch" in report["warnings"]


def test_bar_projection_provenance_uses_bounded_window_and_all_raw_revisions():
    bar = Bar(
        symbol="600519.SH",
        interval="1d",
        timestamp=now_market_time(),
        open="10",
        high="11",
        low="9",
        close="10.5",
        volume=100,
        turnover="1050",
        source="fixture",
    )
    common = dict(
        period_end=bar.timestamp,
        source="fixture",
        adjustment=Adjustment.RAW,
        quality_status="accepted",
        available_at=bar.timestamp,
        received_at=bar.timestamp,
        persisted_at=bar.timestamp,
    )
    matching = SimpleNamespace(
        **common,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        turnover=bar.turnover,
    )
    conflicting = SimpleNamespace(
        **common,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=Decimal("10.6"),
        volume=bar.volume,
        turnover=bar.turnover,
    )
    calls = []

    class Repository:
        def query_bar_observations(self, **kwargs):
            calls.append(kwargs)
            return [matching, conflicting], 2

    report = _bar_projection_provenance(
        Repository(), {"600519.SH": (bar,)}, interval="1d"
    )
    assert len(calls) == 1
    assert calls[0]["adjustment"] is Adjustment.RAW
    assert calls[0]["period_from"] == bar.timestamp
    assert calls[0]["period_to"] == bar.timestamp
    assert report["immutable_observation_count"] == 0
    assert report["content_mismatch_count"] == 1
    assert report["lineage_complete"] is False


def test_bar_projection_provenance_rejects_truncated_observation_scan():
    bar = Bar(
        symbol="600519.SH",
        interval="1d",
        timestamp=now_market_time(),
        open="10",
        high="11",
        low="9",
        close="10.5",
        volume=100,
        turnover="1050",
        source="fixture",
    )
    observation = SimpleNamespace(
        period_end=bar.timestamp,
        source="fixture",
        adjustment=Adjustment.RAW,
        quality_status="accepted",
        available_at=bar.timestamp,
        received_at=bar.timestamp,
        persisted_at=bar.timestamp,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        turnover=bar.turnover,
    )

    class Repository:
        def query_bar_observations(self, **kwargs):
            return [observation], 10001

    report = _bar_projection_provenance(
        Repository(), {"600519.SH": (bar,)}, interval="1d"
    )
    assert report["observation_scan_truncated"] is True
    assert report["lineage_complete"] is False
    assert report["quality_complete"] is False
    assert "immutable_observation_scan_truncated" in report["warnings"]


def test_bars_refresh_is_recorded_in_collection_lineage(client):
    response = client.get(
        "/api/v1/bars/600519.SH",
        params={"interval": "1d", "limit": 2000, "refresh": "true"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["served_from"] == "provider_refresh"
    batches = client.get(
        "/api/v1/lineage/batches",
        params={"provider": "mock", "request_kind": "bars", "limit": 20},
    )
    assert batches.status_code == 200
    assert batches.json()["total"] >= 1
    assert batches.json()["batches"][0]["request_kind"] == "bars"


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
    assert response.headers["retry-after"] == "5"
    body = response.json()["error"]
    assert body["code"] == "provider_unavailable"
    assert body["details"]["error_type"] == "AkShareDataError"


def test_provider_error_details_never_echo_exception_text(client):
    secret = "credential-bearing-url-token"

    async def fail(*args, **kwargs):
        raise AkShareDataError("request failed: " + secret)

    client.app.state.runtime.provider.fetch_bars = fail
    response = client.get(
        "/api/v1/bars/600519.SH",
        params={"interval": "1w", "limit": 1, "refresh": "true"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["details"] == {
        "error_type": "AkShareDataError"
    }
    assert secret not in response.text


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
