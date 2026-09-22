"""Offline lineage API concurrency and fail-closed contract checks."""

import asyncio
import importlib
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from quant_monitor.app import create_app
from quant_monitor.config import Settings
from quant_monitor.domain import (
    Bar,
    CollectionBatch,
    CollectionBatchStatus,
    ObservationQualityStatus,
    SHANGHAI_TZ,
    observation_from_bar,
)
from quant_monitor.research import (
    DatasetDescriptor,
    ResearchRunManifest,
    audit_observation_lineage,
    hash_input_records,
)
from quant_monitor.serialization import bar_dict


@pytest.fixture
def lineage_client(tmp_path):
    settings = Settings(
        database_path=str(tmp_path / "lineage-api.sqlite3"),
        data_provider="mock",
        poll_interval_seconds=60,
        history_bar_count=25,
        watchlist=["600519.SH"],
    )
    with TestClient(create_app(settings)) as client:
        yield client


def _persist_evidence(repository, *, bind_batch=True):
    event_time = datetime(2024, 1, 2, 15, 0, tzinfo=SHANGHAI_TZ)
    received_at = event_time + timedelta(days=1)
    bar = Bar(
        symbol="600519.SH", interval="1d", timestamp=event_time,
        open=Decimal("10"), high=Decimal("11"), low=Decimal("9"),
        close=Decimal("10.5"), volume=100, turnover=Decimal("1050"),
        source="fixture",
    )
    batch = CollectionBatch(
        batch_id="lineage-api-batch", provider="fixture", request_kind="bars",
        input_hash="0" * 64, status=CollectionBatchStatus.RUNNING,
        started_at=received_at - timedelta(seconds=1),
    )
    repository.begin_collection_batch(batch)
    observation = observation_from_bar(
        bar, batch_id=batch.batch_id, received_at=received_at,
        persisted_at=received_at + timedelta(seconds=1),
        quality_status=ObservationQualityStatus.ACCEPTED,
    )
    batch = replace(
        batch, status=CollectionBatchStatus.SUCCEEDED,
        completed_at=received_at + timedelta(seconds=1), item_count=1,
    )
    repository.complete_bar_collection_batch(batch, [observation])
    manifest = ResearchRunManifest(
        run_id="lineage-api-run", strategy_name="fixture",
        strategy_parameters={}, strategy_code_version="v1",
        dataset=DatasetDescriptor(
            name="daily-fixture", schema_version="v1", source="fixture",
            record_count=1,
            attributes={"batch_id": batch.batch_id} if bind_batch else {},
        ),
        input_hash=hash_input_records([bar_dict(bar)]),
        calendar_version="weekday-fallback-v1", universe_version="fixture-v1",
        adjustment_version="raw-v1", constraint_version="research-only-v1",
        configuration={}, assumptions={},
    )
    repository.create_research_run(manifest)
    return manifest, observation, batch, received_at + timedelta(days=1)


def test_lineage_reads_and_audit_run_off_loop_with_unchanged_report(
    lineage_client, monkeypatch,
):
    repository = lineage_client.app.state.runtime.repository
    manifest, observation, batch, decision_at = _persist_evidence(repository)
    expected = audit_observation_lineage(
        [observation], expected_input_hash=manifest.input_hash,
        expected_record_count=1, expected_source="fixture",
        decision_at=decision_at, batches=[batch],
    )
    calls = []

    def off_loop(original, label):
        def checked(*args, **kwargs):
            with pytest.raises(RuntimeError, match="no running event loop"):
                asyncio.get_running_loop()
            calls.append(label)
            return original(*args, **kwargs)
        return checked

    for name in (
        "get_research_run", "query_bar_observations", "get_collection_batch",
    ):
        monkeypatch.setattr(repository, name, off_loop(getattr(repository, name), name))
    app_module = importlib.import_module("quant_monitor.app")
    monkeypatch.setattr(
        app_module, "audit_observation_lineage",
        off_loop(app_module.audit_observation_lineage, "audit"),
    )

    async def no_provider(*args, **kwargs):
        raise AssertionError("lineage API must only use persisted evidence")

    provider = lineage_client.app.state.runtime.provider
    monkeypatch.setattr(provider, "fetch_bars", no_provider)
    monkeypatch.setattr(provider, "fetch_quotes", no_provider)
    response = lineage_client.get(
        "/api/v1/research/runs/lineage-api-run/lineage",
        params={"decision_at": decision_at.isoformat()},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verified"] is expected.verified is True
    assert body["checks"] == dict(expected.checks)
    assert body["failures"] == list(expected.failures)
    assert body["observation_count"] == body["observation_total"] == 1
    assert body["scan_truncated"] is False
    assert body["batch_id"] == batch.batch_id
    assert body["batch_binding_present"] is True
    assert body["decision_at"] == decision_at.isoformat()
    assert body["served_from"] == "sqlite_lineage"
    assert body["upstream_requests"] == 0
    assert calls == [
        "get_research_run", "query_bar_observations", "get_collection_batch", "audit",
    ]


def test_lineage_without_batch_binding_does_not_scan_other_evidence(
    lineage_client, monkeypatch,
):
    repository = lineage_client.app.state.runtime.repository
    _persist_evidence(repository, bind_batch=False)

    def forbidden_scan(*args, **kwargs):
        raise AssertionError("unbound manifest must not scan unrelated batches")

    monkeypatch.setattr(repository, "query_bar_observations", forbidden_scan)
    monkeypatch.setattr(repository, "get_collection_batch", forbidden_scan)
    response = lineage_client.get("/api/v1/research/runs/lineage-api-run/lineage")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verified"] is False
    assert body["batch_binding_present"] is False
    assert body["observation_total"] == body["observation_count"] == 0
    assert "observations_present" in body["failures"]


def test_lineage_worker_preserves_not_found_and_invalid_time_errors(lineage_client):
    assert lineage_client.get(
        "/api/v1/research/runs/missing/lineage"
    ).status_code == 404
    _persist_evidence(lineage_client.app.state.runtime.repository)
    response = lineage_client.get(
        "/api/v1/research/runs/lineage-api-run/lineage",
        params={"decision_at": "invalid-time"},
    )
    assert response.status_code == 422
