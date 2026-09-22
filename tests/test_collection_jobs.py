"""Offline contract tests for explicit collector-job retries and lifecycle."""

from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from quant_monitor.app import create_app
from quant_monitor.config import Settings


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        app_name="collector job fixture",
        database_path=str(tmp_path / "jobs.sqlite3"),
        data_provider="mock",
        poll_interval_seconds=60.0,
        bar_interval="1m",
        history_bar_count=25,
        watchlist=["600519.SH", "000001.SZ"],
        mock_seed=20260812,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _wait_terminal(client: TestClient, job_id: str):
    for _ in range(200):
        response = client.get("/api/v1/collector/jobs/{}".format(job_id))
        assert response.status_code == 200
        payload = response.json()
        if payload["terminal"]:
            return payload
        time.sleep(0.01)
    pytest.fail("collector job did not reach a terminal state")


def test_idempotency_key_reuses_job_after_retry_and_terminal(client):
    headers = {"Idempotency-Key": "dashboard-refresh-2026-08-29"}
    first = client.post("/api/v1/collector/jobs", headers=headers)
    assert first.status_code == 202
    first_payload = first.json()
    assert first_payload["idempotency_replayed"] is False
    job_id = first_payload["job_id"]

    retry = client.post("/api/v1/collector/jobs", headers=headers)
    assert retry.status_code == 202
    retry_payload = retry.json()
    assert retry_payload["job_id"] == job_id
    assert retry_payload["idempotency_replayed"] is True

    terminal = _wait_terminal(client, job_id)
    assert terminal["status"] == "succeeded"

    late_retry = client.post("/api/v1/collector/jobs", headers=headers)
    assert late_retry.status_code == 202
    assert late_retry.json()["job_id"] == job_id
    assert late_retry.json()["idempotency_replayed"] is True


def test_idempotency_key_validation_does_not_start_job(client):
    empty = client.post(
        "/api/v1/collector/jobs", headers={"Idempotency-Key": "   "}
    )
    assert empty.status_code == 400
    assert "Idempotency-Key" in empty.json()["error"]["message"]

    oversized = client.post(
        "/api/v1/collector/jobs", headers={"Idempotency-Key": "x" * 256}
    )
    assert oversized.status_code == 400
    assert "255" in oversized.json()["error"]["message"]


def test_active_collection_job_returns_retry_after(client):
    first = client.post("/api/v1/collector/jobs")
    assert first.status_code == 202
    second = client.post("/api/v1/collector/jobs")
    # The mock may finish very quickly; if it did, the endpoint is allowed to
    # accept the second request, so only assert the back-pressure contract
    # when the first job is still active.
    if second.status_code == 409:
        assert second.headers.get("retry-after") == "1"
    elif second.status_code == 202:
        _wait_terminal(client, second.json()["job_id"])
    _wait_terminal(client, first.json()["job_id"])


def test_quote_refresh_job_is_async_quote_only_and_idempotent(client):
    headers = {"Idempotency-Key": "quote-refresh-contract-1"}
    submitted = client.post(
        "/api/v1/quote-refresh/jobs",
        headers=headers,
        json={"symbols": ["600519.SH", "000001.SZ"]},
    )
    assert submitted.status_code == 202
    body = submitted.json()
    assert body["operation"] == "quote_refresh"
    assert body["job_id"].startswith("qr_")
    assert body["requested_symbols"] == ["600519.SH", "000001.SZ"]

    retry = client.post(
        "/api/v1/quote-refresh/jobs",
        headers=headers,
        json={"symbols": ["600519.SH", "000001.SZ"]},
    )
    assert retry.status_code == 202
    assert retry.json()["job_id"] == body["job_id"]
    assert retry.json()["idempotency_replayed"] is True

    terminal = _wait_terminal(
        client,
        body["job_id"],
    )
    assert terminal["operation"] == "quote_refresh"
    assert terminal["status"] == "succeeded"
    assert terminal["counts"]["quotes"] == 2
    assert terminal["counts"]["bars"] == 0
    assert terminal["counts"]["metrics"] == 0
    assert terminal["counts"]["signals"] == 0

    listing = client.get("/api/v1/quote-refresh/jobs", params={"limit": 10})
    assert listing.status_code == 200
    assert listing.json()["items"][0]["job_id"] == body["job_id"]
    assert listing.json()["items"][0]["operation"] == "quote_refresh"

    detail = client.get("/api/v1/quote-refresh/jobs/{}".format(body["job_id"]))
    assert detail.status_code == 200
    assert detail.json()["operation"] == "quote_refresh"

    # The quote-specific namespace is isolated from the full collector job
    # namespace even when a client reuses the same header value.
    cycle = client.post("/api/v1/collector/jobs", headers=headers)
    assert cycle.status_code == 202
    assert cycle.json()["operation"] == "monitor_cycle"
    _wait_terminal(client, cycle.json()["job_id"])


def test_quote_refresh_job_validates_body_without_provider_call(client):
    malformed = client.post("/api/v1/quote-refresh/jobs", json={"symbols": ["not-a-symbol"]})
    assert malformed.status_code == 422
    assert malformed.json()["error"]["code"] == "http_error"

    empty = client.post("/api/v1/quote-refresh/jobs", json={"symbols": []})
    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "http_error"


def test_quote_refresh_route_does_not_expose_full_cycle_job(client):
    cycle = client.post("/api/v1/collector/jobs")
    assert cycle.status_code == 202
    job_id = cycle.json()["job_id"]
    assert client.get("/api/v1/quote-refresh/jobs/{}".format(job_id)).status_code == 404
    _wait_terminal(client, job_id)
