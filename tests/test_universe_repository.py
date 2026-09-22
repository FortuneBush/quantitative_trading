"""Offline persistence and HTTP contracts for point-in-time universes."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from quant_monitor.app import create_app
from quant_monitor.config import Settings
from quant_monitor.research import UniverseMembership, UniverseSnapshot
from quant_monitor.storage import SQLiteRepository


def _membership(symbol: str, *, known_at: str = "2026-01-01T09:00:00+08:00"):
    return UniverseMembership(
        universe="banks",
        symbol=symbol,
        effective_from=date(2020, 1, 1),
        effective_to=None,
        known_at=datetime.fromisoformat(known_at),
        exchange=symbol.rsplit(".", 1)[1],
        security_type="stock",
        industry="银行",
        source="fixture-security-master",
        version="banks-membership-v1",
        revision_id="r1-" + symbol,
    )


def _snapshot(
    symbols=("600036.SH",),
    *,
    session_date=date(2026, 2, 2),
    knowledge_cutoff="2026-02-02T15:00:00+08:00",
    version="banks-v1",
):
    return UniverseSnapshot(
        universe="banks",
        session_date=session_date,
        knowledge_cutoff=datetime.fromisoformat(knowledge_cutoff),
        version=version,
        members=tuple(sorted((_membership(symbol) for symbol in symbols), key=lambda row: row.symbol)),
    )


def test_universe_snapshot_storage_is_idempotent_append_only_and_conflict_safe(tmp_path):
    repository = SQLiteRepository(tmp_path / "universes.sqlite3")
    repository.initialize()
    first = _snapshot()
    assert repository.insert_universe_snapshots([first, first], source="fixture") == 1
    assert repository.insert_universe_snapshots([first], source="fixture") == 0

    conflict = _snapshot(("600519.SH",))
    with pytest.raises(ValueError, match="conflicting universe snapshot"):
        repository.insert_universe_snapshots([conflict], source="fixture")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        repository._conn().execute(
            "UPDATE universe_snapshots SET version = 'tampered' WHERE universe = 'banks'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        repository._conn().execute("DELETE FROM universe_snapshots WHERE universe = 'banks'")
    repository.close()


def test_universe_snapshot_as_of_hides_future_knowledge_and_does_not_use_catalogue(tmp_path):
    repository = SQLiteRepository(tmp_path / "universes.sqlite3")
    repository.initialize()
    old = _snapshot(knowledge_cutoff="2026-02-02T15:00:00+08:00")
    future = _snapshot(
        symbols=("600036.SH", "600519.SH"),
        knowledge_cutoff="2026-03-02T15:00:00+08:00",
    )
    assert repository.insert_universe_snapshots(
        [old],
        source="fixture",
        recorded_at=datetime.fromisoformat("2026-02-03T09:00:00+08:00"),
    ) == 1
    assert repository.insert_universe_snapshots(
        [future],
        source="fixture",
        recorded_at=datetime.fromisoformat("2026-03-03T09:00:00+08:00"),
    ) == 1
    items, total = repository.query_universe_snapshots_as_of(
        as_of=datetime.fromisoformat("2026-02-15T15:00:00+08:00"),
        universe="banks",
    )
    assert total == 1
    assert [item.content_hash for item in items] == [old.content_hash]
    assert items[0].symbols == ("600036.SH",)
    repository.close()


def test_late_imported_historical_snapshot_is_hidden_before_local_receipt(tmp_path):
    repository = SQLiteRepository(tmp_path / "late-universe.sqlite3")
    repository.initialize()
    snapshot = _snapshot(knowledge_cutoff="2026-01-02T15:00:00+08:00")
    repository.insert_universe_snapshots(
        [snapshot],
        source="late-fixture",
        recorded_at=datetime.fromisoformat("2026-02-03T09:00:00+08:00"),
    )
    before_receipt, before_total = repository.query_universe_snapshots_as_of(
        as_of=datetime.fromisoformat("2026-01-31T15:00:00+08:00"),
        universe="banks",
    )
    after_receipt, after_total = repository.query_universe_snapshots_as_of(
        as_of=datetime.fromisoformat("2026-02-03T09:00:00+08:00"),
        universe="banks",
    )
    assert before_receipt == []
    assert before_total == 0
    assert [item.content_hash for item in after_receipt] == [snapshot.content_hash]
    assert after_total == 1
    repository.close()


def test_universe_snapshot_api_is_cache_only_and_requires_timezone_aware_as_of(tmp_path):
    settings = Settings(
        app_name="universe API fixture",
        database_path=str(tmp_path / "universes-api.sqlite3"),
        data_provider="mock",
        poll_interval_seconds=60.0,
        bar_interval="1m",
        history_bar_count=10,
        watchlist=["600036.SH"],
        mock_seed=20260812,
    )
    with TestClient(create_app(settings)) as client:
        snapshot = _snapshot()
        client.app.state.runtime.repository.insert_universe_snapshots(
            [snapshot],
            source="fixture",
            recorded_at=datetime.fromisoformat("2026-02-03T09:00:00+08:00"),
        )
        response = client.get(
            "/api/v1/research/universes/as-of?as_of=2026-02-15T15:00:00%2B08:00&universe=banks"
        )
        assert response.status_code == 200
        body = response.json()
        assert body["served_from"] == "sqlite_cache"
        assert body["provider_operations"] == 0
        assert body["count"] == 1
        assert body["items"][0]["content_hash"] == snapshot.content_hash
        assert body["items"][0]["members"][0]["symbol"] == "600036.SH"
        assert body["items"][0]["source"] == "fixture"
        assert body["items"][0]["recorded_at"] == "2026-02-03T09:00:00+08:00"
        assert client.get("/api/v1/research/universes/as-of?as_of=2026-02-15").status_code == 422
        # The compatibility alias must remain read-only as well.
        assert client.get(
            "/api/v1/universes/as-of?as_of=2026-02-15T15:00:00%2B08:00"
        ).status_code == 200


def test_v8_database_upgrades_to_v9_without_backfilling_live_catalogue(tmp_path):
    """The v9 migration is additive and does not infer historical snapshots."""

    from quant_monitor.storage.migrations import (
        MigrationRunner,
        V1_INITIAL_SCHEMA,
        V2_MIGRATION_METADATA,
        V3_COLLECTION_LINEAGE,
        V4_RESEARCH_RUNS,
        V5_RESEARCH_RESULTS,
        V6_COLLECTION_JOBS,
        V7_FUNDAMENTAL_OBSERVATIONS,
        V8_CORPORATE_ACTIONS_AND_ADJUSTMENT_FACTORS,
    )

    path = tmp_path / "v8.sqlite3"
    with sqlite3.connect(path) as connection:
        MigrationRunner(
            (
                V1_INITIAL_SCHEMA,
                V2_MIGRATION_METADATA,
                V3_COLLECTION_LINEAGE,
                V4_RESEARCH_RUNS,
                V5_RESEARCH_RESULTS,
                V6_COLLECTION_JOBS,
                V7_FUNDAMENTAL_OBSERVATIONS,
                V8_CORPORATE_ACTIONS_AND_ADJUSTMENT_FACTORS,
            )
        ).migrate(connection)
    repository = SQLiteRepository(path)
    repository.initialize()
    assert repository.query_universe_snapshots_as_of(
        as_of=datetime.fromisoformat("2026-02-15T15:00:00+08:00")
    ) == ([], 0)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        assert connection.execute("SELECT COUNT(*) FROM universe_snapshots").fetchone()[0] == 0
    repository.close()
