"""SQLite and HTTP contracts for point-in-time fundamentals."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from quant_monitor.app import create_app
from quant_monitor.config import Settings
from quant_monitor.domain import FundamentalObservation
from quant_monitor.storage import SQLiteRepository
from quant_monitor.storage.migrations import (
    MigrationRunner,
    V1_INITIAL_SCHEMA,
    V2_MIGRATION_METADATA,
    V3_COLLECTION_LINEAGE,
    V4_RESEARCH_RUNS,
    V5_RESEARCH_RESULTS,
    V6_COLLECTION_JOBS,
)


def _observation(
    observation_id: str,
    value: Optional[str],
    available_at: str,
    revision_id: str,
    *,
    metric: str = "roe",
    period_end: date = date(2025, 12, 31),
    scope: str = "consolidated",
    source: str = "fixture",
) -> FundamentalObservation:
    return FundamentalObservation(
        observation_id=observation_id,
        symbol="600036.SH",
        metric=metric,
        value=None if value is None else Decimal(value),
        unit="percent",
        scope=scope,
        period_start=date(period_end.year, 1, 1),
        period_end=period_end,
        available_at=datetime.fromisoformat(available_at),
        received_at=datetime.fromisoformat(available_at),
        source=source,
        revision_id=revision_id,
        metadata={"fixture": True},
    )


def test_fundamental_repository_is_idempotent_and_append_only(tmp_path):
    repository = SQLiteRepository(tmp_path / "fundamentals.sqlite3")
    repository.initialize()
    observation = _observation(
        "old", "10", "2026-02-01T09:00:00+08:00", "r1"
    )
    assert repository.insert_fundamental_observations([observation]) == 1
    assert repository.insert_fundamental_observations([observation]) == 0
    with pytest.raises(ValueError, match="conflicting fundamental observation_id"):
        repository.insert_fundamental_observations(
            [_observation("old", "11", "2026-02-01T09:00:00+08:00", "r1")]
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        repository._conn().execute(
            "UPDATE fundamental_observations SET value = '99' WHERE observation_id = 'old'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        repository._conn().execute(
            "DELETE FROM fundamental_observations WHERE observation_id = 'old'"
        )
    repository.close()


def test_fundamental_repository_selects_visible_latest_period_and_revision(tmp_path):
    repository = SQLiteRepository(tmp_path / "fundamentals.sqlite3")
    repository.initialize()
    repository.insert_fundamental_observations(
        [
            _observation("old", "10", "2026-02-01T09:00:00+08:00", "r1"),
            _observation("new", "11", "2026-03-01T09:00:00+08:00", "r2"),
            _observation("future", "99", "2026-04-01T09:00:00+08:00", "r3"),
            _observation(
                "prior-period",
                "8",
                "2026-03-05T09:00:00+08:00",
                "r1",
                period_end=date(2024, 12, 31),
            ),
        ]
    )
    selected, candidate_count = repository.query_fundamentals_as_of(
        decision_at=datetime.fromisoformat("2026-03-15T15:00:00+08:00"),
    )
    assert candidate_count == 3
    assert [(item.metric, item.value, item.revision_id) for item in selected] == [
        ("roe", Decimal("11"), "r2")
    ]
    repository.close()


def test_fundamental_cohort_query_scopes_before_ambiguity_resolution(tmp_path):
    repository = SQLiteRepository(tmp_path / "scoped-fundamentals.sqlite3")
    repository.initialize()
    fact = _observation("valid", "10", "2026-02-01T09:00:00+08:00", "r1")
    repository.insert_fundamental_observations([
        fact,
        replace(fact, observation_id="other-bank-a", symbol="601398.SH"),
        replace(fact, observation_id="other-bank-b", symbol="601398.SH", scope="standalone"),
        replace(fact, observation_id="other-metric-a", metric="other_metric"),
        replace(fact, observation_id="other-metric-b", metric="other_metric", source="other"),
    ])
    cutoff = datetime.fromisoformat("2026-03-15T15:00:00+08:00")
    try:
        selected, count = repository.query_fundamentals_as_of(
            decision_at=cutoff, symbols=["600036.sh"], metrics=["ROE"],
        )
        assert selected == [fact]
        assert count == 1
        assert repository.query_fundamentals_as_of(
            decision_at=cutoff, symbols=[],
        ) == ([], 0)
        assert repository.query_fundamentals_as_of(
            decision_at=cutoff, metrics=[],
        ) == ([], 0)
        with pytest.raises(ValueError):
            repository.query_fundamentals_as_of(decision_at=cutoff)
        with pytest.raises(ValueError, match="not both"):
            repository.query_fundamentals_as_of(decision_at=cutoff, symbol="600036.SH", symbols=[])
        with pytest.raises(TypeError, match="sequences"):
            repository.query_fundamentals_as_of(decision_at=cutoff, symbols="600036.SH")
    finally:
        repository.close()


def test_legacy_v6_database_upgrades_to_v8_without_touching_existing_rows(tmp_path):
    path = tmp_path / "legacy-v6.sqlite3"
    with sqlite3.connect(path) as connection:
        MigrationRunner(
            (
                V1_INITIAL_SCHEMA,
                V2_MIGRATION_METADATA,
                V3_COLLECTION_LINEAGE,
                V4_RESEARCH_RUNS,
                V5_RESEARCH_RESULTS,
                V6_COLLECTION_JOBS,
            )
        ).migrate(connection)
        connection.execute(
            "INSERT INTO bars (symbol, interval, timestamp, open, high, low, close, volume, turnover, source) "
            "VALUES ('600000.SH', '1d', '2026-09-03T15:30:00+08:00', '10', '11', '9', '10', 1, '10', 'fixture')"
        )
        connection.commit()
    repository = SQLiteRepository(path)
    repository.initialize()
    repository.close()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SQLiteRepository.SCHEMA_VERSION
        assert connection.execute("SELECT COUNT(*) FROM fundamental_observations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM corporate_actions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM adjustment_factors").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_fundamental_as_of_api_is_cache_only_and_returns_quality(client_tmp_path):
    settings = Settings(
        app_name="fundamental API fixture",
        database_path=str(client_tmp_path / "fundamentals-api.sqlite3"),
        data_provider="mock",
        poll_interval_seconds=60.0,
        bar_interval="1m",
        history_bar_count=10,
        watchlist=["600519.SH"],
        mock_seed=20260812,
    )
    with TestClient(create_app(settings)) as client:
        client.app.state.runtime.repository.insert_fundamental_observations(
            [
                _observation("api-old", "10", "2026-02-01T09:00:00+08:00", "r1"),
                _observation("api-new", "11", "2026-03-01T09:00:00+08:00", "r2"),
            ]
        )
        response = client.get(
            "/api/v1/fundamentals/as-of?as_of=2026-03-15T15:00:00%2B08:00&symbol=600036.SH"
        )
        assert response.status_code == 200
        body = response.json()
        assert body["served_from"] == "sqlite_cache"
        assert body["provider_operations"] == 0
        assert body["candidate_count"] == 2
        assert body["count"] == 1
        assert body["quality"]["status"] == "ok"
        assert body["items"][0]["value"] == "11"
        assert client.get("/api/v1/fundamentals/as-of").status_code == 422


@pytest.fixture
def client_tmp_path(tmp_path):
    return tmp_path
