"""Offline tests for immutable persisted backtest result artifacts."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from quant_monitor.research import (
    DatasetDescriptor,
    ResearchResultRecord,
    ResearchRunManifest,
    hash_canonical,
)
from quant_monitor.storage import SQLiteRepository


def _manifest(run_id: str = "run-result-001") -> ResearchRunManifest:
    return ResearchRunManifest(
        run_id=run_id,
        strategy_name="fixture_strategy",
        strategy_parameters={},
        strategy_code_version="git:fixture",
        dataset=DatasetDescriptor(
            name="fixture_daily",
            schema_version="bars-v1",
            source="fixture",
            record_count=2,
            attributes={"interval": "1d"},
        ),
        input_hash="sha256:" + "b" * 64,
        calendar_version="fixture-calendar-v1",
        universe_version="fixture-universe-v1",
        adjustment_version="raw-v1",
        constraint_version="fixture-constraints-v1",
        configuration={},
        assumptions={},
    )


def _result_json() -> str:
    result = {"strategy_name": "fixture_strategy", "metrics": {"total_return": 0.1}}
    result["result_hash"] = hash_canonical(result, namespace="backtest-result-v1")
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def test_research_result_requires_parent_run_and_round_trips(tmp_path):
    repository = SQLiteRepository(tmp_path / "results.sqlite3")
    repository.initialize()
    try:
        with pytest.raises(ValueError, match="research run not found"):
            repository.create_research_result("missing", _result_json())
        repository.create_research_run(
            _manifest(), created_at=datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
        )
        record = repository.create_research_result(
            "run-result-001",
            _result_json(),
            created_at=datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc),
        )
        assert record.result_hash.startswith("sha256:")
        assert repository.get_research_result(record.run_id) == record
        page, total = repository.query_research_results(limit=10)
        assert total == 1
        assert page == [record]
    finally:
        repository.close()


def test_research_result_is_immutable_and_tamper_evident(tmp_path):
    path = tmp_path / "results-immutable.sqlite3"
    repository = SQLiteRepository(path)
    repository.initialize()
    try:
        repository.create_research_run(_manifest())
        record = repository.create_research_result("run-result-001", _result_json())
        with pytest.raises(sqlite3.IntegrityError):
            repository.create_research_result("run-result-001", _result_json())
        with sqlite3.connect(str(path)) as connection:
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(
                    "UPDATE research_results SET result_json = '{}' WHERE run_id = ?",
                    (record.run_id,),
                )
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(
                    "DELETE FROM research_results WHERE run_id = ?", (record.run_id,)
                )
        with pytest.raises(ValueError, match="result_json hash"):
            ResearchResultRecord(
                **{**record.__dict__, "result_json_hash": "sha256:" + "c" * 64}
            )
    finally:
        repository.close()


def test_research_result_rejects_noncanonical_json_even_when_semantic_hash_matches():
    """Offline bundles have one byte representation, not equivalent aliases."""

    result = {"strategy_name": "fixture_strategy", "metrics": {"total_return": 0.1}}
    result["result_hash"] = hash_canonical(result, namespace="backtest-result-v1")
    pretty = json.dumps(result, ensure_ascii=False, indent=2)
    with pytest.raises(ValueError, match="canonical JSON"):
        ResearchResultRecord.from_json(
            "run-result-canonical",
            pretty,
            created_at=datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc),
        )
