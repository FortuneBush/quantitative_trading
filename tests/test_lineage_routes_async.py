"""Concurrency contracts for collection-lineage API routes.

The lineage tables are append-only but can become large in a long-running
monitor.  These tests keep the API contract explicit: route handlers must
submit repository reads to ``asyncio.to_thread`` rather than running SQLite
on the ASGI event loop.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from quant_monitor.app import create_app
from quant_monitor.config import Settings


@pytest.fixture
def lineage_client(tmp_path):
    settings = Settings(
        app_name="lineage async fixture",
        database_path=str(tmp_path / "lineage-routes.sqlite3"),
        data_provider="mock",
        poll_interval_seconds=60.0,
        bar_interval="1m",
        history_bar_count=10,
        watchlist=["600519.SH"],
        mock_seed=20260914,
    )
    with TestClient(create_app(settings)) as client:
        yield client


def test_collection_lineage_reads_are_submitted_off_loop(lineage_client, monkeypatch):
    """All collection-lineage reads use the worker executor."""

    import quant_monitor.app as app_module

    original_to_thread = app_module.asyncio.to_thread
    submitted = []

    async def recording_to_thread(func, *args, **kwargs):
        submitted.append(getattr(func, "__name__", "anonymous"))
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(app_module.asyncio, "to_thread", recording_to_thread)

    batches = lineage_client.get("/api/v1/lineage/batches", params={"limit": 5})
    assert batches.status_code == 200, batches.text
    batch_id = batches.json()["batches"][0]["batch_id"]

    detail = lineage_client.get("/api/v1/lineage/batches/{}".format(batch_id))
    assert detail.status_code == 200, detail.text
    observations = lineage_client.get(
        "/api/v1/lineage/batches/{}/observations".format(batch_id)
    )
    assert observations.status_code == 200, observations.text
    all_observations = lineage_client.get("/api/v1/lineage/observations")
    assert all_observations.status_code == 200, all_observations.text

    # The route-local detail loader combines row and cardinality reads in one
    # worker turn, avoiding both event-loop blocking and torn responses.
    assert "query_collection_batches" in submitted
    assert "load_batch_detail" in submitted
    assert submitted.count("query_bar_observations") >= 2


def test_collection_lineage_detail_not_found_still_offloads(lineage_client, monkeypatch):
    """A missing batch follows the same non-blocking read path."""

    import quant_monitor.app as app_module

    original_to_thread = app_module.asyncio.to_thread
    submitted = []

    async def recording_to_thread(func, *args, **kwargs):
        submitted.append(getattr(func, "__name__", "anonymous"))
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(app_module.asyncio, "to_thread", recording_to_thread)
    response = lineage_client.get("/api/v1/lineage/batches/missing")
    assert response.status_code == 404
    assert "load_batch_detail" in submitted
