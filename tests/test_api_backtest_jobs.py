"""HTTP contract coverage for the bounded asynchronous backtest facade."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from quant_monitor.app import create_app
from quant_monitor.backtesting import BacktestEngine
from quant_monitor.config import Settings
from quant_monitor.domain import Bar, SHANGHAI_TZ
from quant_monitor.research import DatasetDescriptor, ResearchRunManifest


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        app_name="async backtest fixture",
        database_path=str(tmp_path / "backtest-jobs.sqlite3"),
        data_provider="mock",
        poll_interval_seconds=60.0,
        watchlist=["600519.SH"],
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_async_backtest_job_returns_immediately_and_exposes_terminal_failure(client):
    response = client.post(
        "/api/v1/research/runs/unknown-run/backtest/jobs",
        headers={"Idempotency-Key": "async-test-1"},
        json={"symbols": ["600519.SH"], "limit": 1},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] in {"queued", "running", "failed"}
    assert body["served_from"] == "in_process_job_manager"
    assert body["upstream_requests"] == 0
    job_id = body["job_id"]

    # The run is intentionally absent, so the worker should fail with a typed
    # 404-derived lifecycle state without touching the provider.
    for _ in range(20):
        detail = client.get(
            "/api/v1/research/runs/unknown-run/backtest/jobs/{}".format(job_id)
        )
        assert detail.status_code == 200
        if detail.json()["terminal"]:
            break
        time.sleep(0.01)
    assert detail.json()["status"] == "failed"
    assert detail.json()["error_type"] == "HTTPException"

    replay = client.post(
        "/api/v1/research/runs/unknown-run/backtest/jobs",
        headers={"Idempotency-Key": "async-test-1"},
        json={"symbols": ["600519.SH"], "limit": 1},
    )
    assert replay.status_code == 202
    assert replay.json()["job_id"] == job_id
    assert replay.json()["idempotency_replayed"] is True

    listing = client.get(
        "/api/v1/research/runs/unknown-run/backtest/jobs", params={"limit": 20}
    )
    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    assert listing.json()["items"][0]["job_id"] == job_id


def test_async_backtest_job_rejects_invalid_id_before_admission(client):
    response = client.post(
        "/api/v1/research/runs/%s/backtest/jobs" % ("x" * 129),
        json={},
    )
    assert response.status_code == 422


def test_async_backtest_idempotency_key_conflict_is_explicit(client):
    headers = {"Idempotency-Key": "same-request-key"}
    first = client.post(
        "/api/v1/research/runs/unknown-run/backtest/jobs",
        headers=headers,
        json={"symbols": ["600519.SH"], "limit": 1},
    )
    assert first.status_code == 202
    conflict = client.post(
        "/api/v1/research/runs/unknown-run/backtest/jobs",
        headers=headers,
        json={"symbols": ["600036.SH"], "limit": 1},
    )
    assert conflict.status_code == 409
    assert "different request parameters" in conflict.json()["error"]["message"]


def _seed_backtest(client, run_id):
    repository = client.app.state.runtime.repository
    repository.insert_bars([
        Bar(
            symbol="600519.SH", interval="1d",
            timestamp=datetime(2026, 8, 24, 15, tzinfo=SHANGHAI_TZ),
            open=100, high=101, low=99, close=100,
            volume=10000, turnover=1000000, source="mock",
        )
    ])
    repository.create_research_run(ResearchRunManifest(
        run_id=run_id, strategy_name="bank_daily_multifactor_v1",
        strategy_parameters={}, strategy_code_version="fixture-v1",
        dataset=DatasetDescriptor(
            name="bank-daily", schema_version="bars-v1", source="mock", record_count=1,
        ),
        input_hash="sha256:" + "f" * 64,
        calendar_version="fixture-calendar", universe_version="fixture-universe",
        adjustment_version="raw-v1", constraint_version="fixture-constraints",
        configuration={}, assumptions={},
    ))
    return "/api/v1/research/runs/" + run_id


def _await_terminal(client, job_url):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(job_url)
        assert response.status_code == 200, response.text
        if response.json()["terminal"]:
            return response.json()
        time.sleep(0.01)
    pytest.fail("research job did not reach a terminal state")


def test_cancel_computation_does_not_write_an_artifact(client, monkeypatch):
    base = _seed_backtest(client, "cancel-computation")
    started, release, exited = (threading.Event() for _ in range(3))
    original = BacktestEngine.run

    def blocked_run(engine, *args, **kwargs):
        started.set()
        try:
            assert release.wait(10), "test did not release engine"
            return original(engine, *args, **kwargs)
        finally:
            exited.set()

    monkeypatch.setattr(BacktestEngine, "run", blocked_run)
    try:
        submitted = client.post(base + "/backtest/jobs", json={"limit": 1})
        assert submitted.status_code == 202, submitted.text
        job_url = base + "/backtest/jobs/" + submitted.json()["job_id"]
        assert started.wait(5)
        cancelled = client.post(job_url + "/cancel").json()
        assert cancelled["status"] == "cancelling"
        assert cancelled["terminal"] is False
        assert cancelled["completed_at"] is None
        assert client.get(base + "/backtest/jobs").json()["active"] == 1
        assert client.get(base + "/result").status_code == 404
    finally:
        release.set()
    terminal = _await_terminal(client, job_url)
    assert terminal["status"] == "cancelled"
    assert terminal["result_available"] is False
    assert exited.is_set()
    assert client.get(base + "/result").status_code == 404


@pytest.mark.parametrize("commit_fails", [False, True])
def test_cancel_during_commit_reports_the_actual_durable_outcome(
    client, monkeypatch, commit_fails
):
    base = _seed_backtest(client, "cancel-commit")
    started, release = threading.Event(), threading.Event()
    repository = client.app.state.runtime.repository
    original = repository.create_research_result

    def blocked_commit(*args, **kwargs):
        started.set()
        assert release.wait(10), "test did not release commit"
        if commit_fails:
            raise ValueError("fixture commit failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(repository, "create_research_result", blocked_commit)
    try:
        submitted = client.post(base + "/backtest/jobs", json={"limit": 1})
        assert submitted.status_code == 202, submitted.text
        job_url = base + "/backtest/jobs/" + submitted.json()["job_id"]
        assert started.wait(5)
        assert client.post(job_url + "/cancel").json()["status"] == "cancelling"
        assert client.post(job_url + "/cancel").json()["terminal"] is False
        assert client.get(base + "/result").status_code == 404
    finally:
        release.set()
    terminal = _await_terminal(client, job_url)
    stored = client.get(base + "/result")
    if commit_fails:
        assert terminal["status"] == "failed"
        assert terminal["result_available"] is False
        assert terminal["error_type"] == "HTTPException"
        assert stored.status_code == 404
    else:
        assert terminal["status"] == "succeeded"
        assert terminal["result_available"] is True
        assert terminal["error_type"] is None
        assert stored.status_code == 200
        assert stored.json()["result_hash"] == terminal["result_hash"]


def test_application_shutdown_joins_research_thread_before_closing_sqlite(tmp_path, monkeypatch):
    started, release, exited, stopping = (threading.Event() for _ in range(4))
    close_order = []
    original_run = BacktestEngine.run

    def blocked_run(engine, *args, **kwargs):
        started.set()
        try:
            assert release.wait(10), "test did not release engine"
            return original_run(engine, *args, **kwargs)
        finally:
            exited.set()

    monkeypatch.setattr(BacktestEngine, "run", blocked_run)

    def application_lifetime():
        settings = Settings(
            data_provider="mock", watchlist=["600519.SH"],
            database_path=str(tmp_path / "shutdown.sqlite3"), poll_interval_seconds=60,
        )
        with TestClient(create_app(settings)) as client:
            base = _seed_backtest(client, "shutdown-computation")
            repository = client.app.state.runtime.repository
            original_close = repository.close

            def checked_close():
                close_order.append(exited.is_set())
                return original_close()

            repository.close = checked_close
            manager = client.app.state.research_backtest_jobs
            original_stop = manager.stop

            async def marked_stop():
                stopping.set()
                await original_stop()

            manager.stop = marked_stop
            assert client.post(base + "/backtest/jobs", json={"limit": 1}).status_code == 202
            assert started.wait(5)
        return manager

    with ThreadPoolExecutor(max_workers=1) as executor:
        lifetime = executor.submit(application_lifetime)
        try:
            assert stopping.wait(5), "application did not begin shutdown"
            assert not lifetime.done()
            assert close_order == []
        finally:
            release.set()
        manager = lifetime.result(timeout=10)
    assert exited.is_set()
    assert close_order == [True]
    assert not manager._tasks
