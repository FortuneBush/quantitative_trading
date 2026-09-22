"""Exercise immutable analysis inputs against REAL SQLite, never a provider."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import asyncio

import pytest
from fastapi.testclient import TestClient

from quant_monitor.app import create_app
from quant_monitor.config import Settings
from quant_monitor.domain import (
    Bar, BarObservation, CollectionBatch, CollectionBatchStatus,
    ObservationQualityStatus, Quote, normalized_request_hash, observation_from_bar,
    parse_market_time,
)
from quant_monitor.services.analysis_inputs import select_analysis_bars
from quant_monitor.storage import SQLiteRepository


NOW = parse_market_time("2026-09-15T15:31:00+08:00")
SYMBOL = "600519.SH"


def bars(count=80, end=None):
    end = end or NOW.replace(hour=15, minute=0, second=0)
    dates = []
    while len(dates) < count:
        if end.weekday() < 5:
            dates.append(end)
        end -= timedelta(days=1)
    return [
        Bar(symbol=SYMBOL, interval="1d", timestamp=timestamp,
            open=100 + index, high=101 + index, low=99 + index,
            close=100 + index, volume=100000, turnover=10000000, source="mock")
        for index, timestamp in enumerate(reversed(dates))
    ]


def observation(bar, *, batch="analysis-input", revision="v1", quality="accepted"):
    return observation_from_bar(
        bar, batch_id=batch, received_at=bar.timestamp + timedelta(seconds=1),
        persisted_at=bar.timestamp + timedelta(seconds=2), revision_id=revision,
        quality_status=ObservationQualityStatus(quality),
    )


def save(repository, records, projected):
    first = records[0]
    running = CollectionBatch(
        batch_id=first.batch_id, provider="mock", request_kind="bars",
        input_hash=normalized_request_hash({"symbol": SYMBOL}),
        status=CollectionBatchStatus.RUNNING,
        started_at=min(item.period_start for item in records),
    )
    repository.begin_collection_batch(running)
    repository.complete_bar_collection_batch(
        replace(running, status=CollectionBatchStatus.SUCCEEDED,
                completed_at=max(item.persisted_at for item in records),
                item_count=len(records)),
        records, legacy_bars=projected,
    )


@pytest.fixture
def repository(tmp_path):
    repo = SQLiteRepository(tmp_path / "analysis-evidence.sqlite3")
    repo.initialize()
    try:
        yield repo
    finally:
        repo.close()


def test_actual_sqlite_success_uses_observation_objects_once(repository, monkeypatch):
    projected = bars(3)
    records = [observation(bar) for bar in projected]
    save(repository, records, projected)
    converted, queries = [], []
    original_convert = BarObservation.to_legacy_bar
    original_query = repository.query_bar_observations

    def convert(item):
        converted.append((item.batch_id, item.revision_id, item.period_end))
        return original_convert(item)

    def query(**kwargs):
        queries.append(kwargs)
        return original_query(**kwargs)

    monkeypatch.setattr(BarObservation, "to_legacy_bar", convert)
    monkeypatch.setattr(repository, "query_bar_observations", query)
    selected, evidence = select_analysis_bars(repository, SYMBOL, "1d", projected, as_of=NOW)
    assert selected == projected
    assert all(left is not right for left, right in zip(selected, projected))
    assert len(converted) == 3
    assert len(queries) == 1
    assert "quality_status" not in queries[0]
    assert queries[0]["period_from"] == projected[0].timestamp
    assert evidence["calculation_input"] == "immutable_observations"
    assert evidence["quality_complete"] is True
    assert len(evidence["selected_observations"]) == 3
    assert evidence["authoritative"] is False


@pytest.mark.parametrize("quality", ["degraded", "quarantined", "untrusted_legacy"])
def test_quality_filter_cannot_hide_an_identical_bad_revision(repository, quality):
    projected = bars(1)
    accepted = observation(projected[0], revision="z-accepted")
    older_bad = replace(accepted, revision_id="a-bad", quality_status=quality)
    save(repository, [accepted, older_bad], projected)
    selected, evidence = select_analysis_bars(repository, SYMBOL, "1d", projected, as_of=NOW)
    assert selected[0] is projected[0]
    assert evidence["lineage_complete"] is True
    assert evidence["quality_complete"] is False
    assert evidence["non_accepted_observation_count"] == 1
    assert evidence["calculation_input"] == "legacy_bars_projection"


def test_more_than_2000_revisions_cannot_hide_an_older_conflict(repository):
    projected = bars(1)
    base = observation(projected[0])
    duplicates = [replace(base, revision_id="same-{}".format(i)) for i in range(2001)]
    conflict = replace(base, revision_id="old-conflict", close=Decimal("100.5"))
    save(repository, duplicates + [conflict], projected)
    _, evidence = select_analysis_bars(repository, SYMBOL, "1d", projected, as_of=NOW)
    assert evidence["observation_scan_total"] == 2002
    assert evidence["content_mismatch_count"] == 1
    assert evidence["quality_complete"] is False


def test_scan_limit_discloses_truncation_and_never_certifies(repository, monkeypatch):
    import quant_monitor.services.analysis_inputs as inputs
    monkeypatch.setattr(inputs, "ANALYSIS_OBSERVATION_SCAN_LIMIT", 2)
    projected = bars(1)
    save(repository, [observation(projected[0], revision=str(i)) for i in range(3)], projected)
    _, evidence = select_analysis_bars(repository, SYMBOL, "1d", projected, as_of=NOW)
    assert evidence["observation_scan_total"] == 3
    assert evidence["observation_scan_count"] == 2
    assert evidence["observation_scan_truncated"] is True
    assert evidence["lineage_complete"] is False
    assert evidence["selected_observations"] == []


def test_future_receipt_of_historical_bar_is_not_usable(repository):
    projected = bars(1)
    future = NOW + timedelta(hours=1)
    record = replace(observation(projected[0]), available_at=future,
                     received_at=future, persisted_at=future)
    save(repository, [record], projected)
    _, evidence = select_analysis_bars(repository, SYMBOL, "1d", projected, as_of=NOW)
    assert evidence["lineage_complete"] is True
    assert evidence["unavailable_observation_count"] == 1
    assert evidence["quality_complete"] is False


def test_concurrent_revision_after_query_cannot_change_validated_inputs(repository, monkeypatch):
    projected = bars(1)
    save(repository, [observation(projected[0])], projected)
    original = repository.query_bar_observations
    calls = []

    def append_after_read(**kwargs):
        snapshot = original(**kwargs)
        calls.append(True)
        if len(calls) == 1:
            newer = replace(observation(projected[0], batch="later"), close=Decimal("100.5"))
            save(repository, [newer], [])
        return snapshot

    monkeypatch.setattr(repository, "query_bar_observations", append_after_read)
    selected, evidence = select_analysis_bars(repository, SYMBOL, "1d", projected, as_of=NOW)
    assert len(calls) == 1
    assert selected[0].close == Decimal("100")
    assert evidence["selected_observations"][0]["batch_id"] == "analysis-input"
    _, next_evidence = select_analysis_bars(repository, SYMBOL, "1d", projected, as_of=NOW)
    assert next_evidence["content_mismatch_count"] == 1


@pytest.mark.parametrize("route", ["single", "batch"])
@pytest.mark.parametrize("scenario", ["ready", "short", "stale", "future_quote", "future_bar", "calendar_gap", "legacy"])
def test_analysis_routes_separate_readiness_from_lineage(tmp_path, monkeypatch, scenario, route):
    import quant_monitor.app as app_module
    from quant_monitor.services import QuantitativeAnalysisService
    from quant_monitor.trading_calendar import VersionedSessionCalendar

    monkeypatch.setattr(app_module, "now_market_time", lambda: NOW)
    if scenario == "calendar_gap":
        calendar = VersionedSessionCalendar(
            sessions=[NOW.date() - timedelta(days=1)], source="fixture", version="v1"
        )
        monkeypatch.setattr(app_module, "QuantitativeAnalysisService",
                            lambda: QuantitativeAnalysisService(calendar=calendar))
    settings = Settings(data_provider="mock", database_path=str(tmp_path / "api.sqlite3"),
                        universe_mode="watchlist", watchlist=["000001.SZ"],
                        history_bar_count=1, poll_interval_seconds=3600)
    with TestClient(create_app(settings)) as client:
        runtime = client.app.state.runtime
        projected = bars(5 if scenario == "short" else 80,
                         end=(NOW.replace(hour=15, minute=0) - timedelta(days=1))
                         if scenario == "stale" else None)
        if scenario == "future_bar":
            projected = bars(80, end=NOW.replace(hour=15, minute=0) + timedelta(days=1))
        if scenario == "legacy":
            runtime.repository.insert_bars(projected)
        else:
            save(runtime.repository, [observation(bar) for bar in projected], projected)
        quote_time = NOW + timedelta(minutes=5) if scenario == "future_quote" else NOW
        runtime.repository.upsert_quotes([
            Quote(symbol=SYMBOL, timestamp=quote_time, last=projected[-1].close, source="mock")
        ])

        async def forbidden(*args, **kwargs):
            raise AssertionError("cache analysis must not fetch a provider")

        monkeypatch.setattr(runtime.provider, "fetch_bars", forbidden)
        monkeypatch.setattr(runtime.provider, "fetch_quotes", forbidden)
        monkeypatch.setattr(runtime.provider, "fetch_quotes_relaxed", forbidden, raising=False)
        query = runtime.repository.query_bar_observations
        conversions = []
        convert = BarObservation.to_legacy_bar

        def convert_off_loop(item):
            with pytest.raises(RuntimeError, match="no running event loop"):
                asyncio.get_running_loop()
            conversions.append(item.period_end)
            return convert(item)

        def query_off_loop(**kwargs):
            with pytest.raises(RuntimeError, match="no running event loop"):
                asyncio.get_running_loop()
            return query(**kwargs)

        monkeypatch.setattr(BarObservation, "to_legacy_bar", convert_off_loop)
        monkeypatch.setattr(runtime.repository, "query_bar_observations", query_off_loop)
        response = client.get(
            "/api/v1/analysis/" + SYMBOL if route == "single" else "/api/v1/analysis",
            params={"interval": "1d", "limit": len(projected), "symbols": SYMBOL},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        if route == "batch":
            assert payload["count"] == 1
            payload = payload["items"][0]
        quality = payload["data_quality"]
        assert quality["decision_ready"] is False
        assert quality["analysis_ready"] is (scenario == "ready")
        assert quality["quality_complete"] is (scenario not in {"future_bar", "legacy"})
        if scenario == "ready":
            assert payload["research_action"] != "insufficient_data"
            assert quality["decision_ready_reason"] == "decision_evidence_unverified"
            assert len(conversions) == len(projected)
            assert quality["observation_authority"] == "immutable_observations"
        else:
            assert payload["research_action"] == "insufficient_data"
            assert payload["recommendation"]["action"] == "insufficient_data"
            assert quality["analysis_readiness_failures"]
