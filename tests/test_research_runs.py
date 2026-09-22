"""Offline persistence tests for immutable research run manifests."""

from __future__ import annotations

import sqlite3
import hashlib
import json
from datetime import datetime, timezone

import pytest

from quant_monitor.research import DatasetDescriptor, ResearchRunManifest, ResearchRunRecord
from quant_monitor.storage import SQLiteRepository


def _manifest(run_id: str = "run-001", assumptions=None) -> ResearchRunManifest:
    return ResearchRunManifest(
        run_id=run_id,
        strategy_name="bank_value_momentum",
        strategy_parameters={"lookback": 20, "threshold": 0.03},
        strategy_code_version="git:fixture",
        dataset=DatasetDescriptor(
            name="bank_daily",
            schema_version="bars-v1",
            source="fixture",
            record_count=2,
            attributes={"interval": "1d"},
        ),
        input_hash="sha256:" + "a" * 64,
        calendar_version="weekday_fallback-v1",
        universe_version="banks-v1",
        adjustment_version="raw-v1",
        constraint_version="cn-equity-v1",
        configuration={"engine": "fixture"},
        assumptions=assumptions if assumptions is not None else {"fees": "zero"},
    )


def test_research_run_round_trip_and_deterministic_listing(tmp_path):
    repository = SQLiteRepository(tmp_path / "research.sqlite3")
    repository.initialize()
    try:
        created = repository.create_research_run(
            _manifest(), created_at=datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
        )
        assert created.manifest_json_hash.startswith("sha256:")
        assert repository.get_research_run("run-001") == created
        assert repository.get_research_run("missing") is None

        second = repository.create_research_run(
            _manifest("run-002"),
            created_at=datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc),
        )
        page, total = repository.query_research_runs(
            strategy_name="bank_value_momentum", limit=1
        )
        assert total == 2
        assert page == [second]
        assert repository.list_research_runs(limit=10) == [second, created]
    finally:
        repository.close()


def test_research_run_ids_are_immutable_and_duplicate_ids_fail(tmp_path):
    path = tmp_path / "research-immutable.sqlite3"
    repository = SQLiteRepository(path)
    repository.initialize()
    try:
        record = repository.create_research_run(_manifest())
        with pytest.raises(sqlite3.IntegrityError):
            repository.create_research_run(_manifest())
        with sqlite3.connect(str(path)) as connection:
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(
                    "UPDATE research_runs SET strategy_name = 'changed' WHERE run_id = ?",
                    (record.run_id,),
                )
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(
                    "DELETE FROM research_runs WHERE run_id = ?", (record.run_id,)
                )
    finally:
        repository.close()


def test_research_catalog_supports_case_insensitive_tags_and_safe_sorting(tmp_path):
    repository = SQLiteRepository(tmp_path / "research-tags.sqlite3")
    repository.initialize()
    try:
        repository.create_research_run(
            _manifest("tag-old"),
            created_at=datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc),
        )
        repository.create_research_run(
            _manifest("tag-new", assumptions={"purpose": "research_only", "tags": ["Bank", "value"]}),
            created_at=datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc),
        )
        repository.create_research_run(
            _manifest("tag-mid", assumptions={"purpose": "research_only", "tags": "bank,momentum"}),
            created_at=datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc),
        )

        page, total = repository.query_research_runs(tags=["BANK"], sort_by="run_id", sort_order="asc")
        assert total == 2
        assert [item.run_id for item in page] == ["tag-mid", "tag-new"]

        page, total = repository.query_research_runs(tags=["bank", "value"], limit=1)
        assert total == 1
        assert [item.run_id for item in page] == ["tag-new"]

        with pytest.raises(ValueError, match="unsupported research sort"):
            repository.query_research_runs(sort_by="manifest_json")
        with pytest.raises(ValueError, match="sort_order"):
            repository.query_research_runs(sort_order="sideways")
    finally:
        repository.close()


def test_research_run_read_detects_manifest_payload_tampering(tmp_path):
    path = tmp_path / "research-integrity.sqlite3"
    repository = SQLiteRepository(path)
    repository.initialize()
    try:
        record = repository.create_research_run(_manifest())
        with pytest.raises(ValueError, match="manifest_json hash"):
            ResearchRunRecord(
                **{
                    **record.__dict__,
                    "manifest_json": record.manifest_json.replace("lookback", "lookbackx"),
                }
            )
        with pytest.raises(ValueError, match="strategy_name"):
            ResearchRunRecord(
                **{**record.__dict__, "strategy_name": "forged_strategy"}
            )
        # Recomputing the serialized-payload digest must not make a modified
        # strategy configuration look like the original content-addressed
        # manifest.  The record now independently re-derives content_hash.
        payload = json.loads(record.manifest_json)
        payload["strategy"]["parameters"]["lookback"] = 999
        forged_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        forged_json_hash = "sha256:" + hashlib.sha256(forged_json.encode("utf-8")).hexdigest()
        with pytest.raises(ValueError, match="content_hash does not match content"):
            ResearchRunRecord(
                **{
                    **record.__dict__,
                    "manifest_json": forged_json,
                    "manifest_json_hash": forged_json_hash,
                }
            )
    finally:
        repository.close()


def test_research_run_read_rejects_noncanonical_json_with_matching_hash(tmp_path):
    repository = SQLiteRepository(tmp_path / "research-canonical.sqlite3")
    repository.initialize()
    try:
        record = repository.create_research_run(_manifest())
        payload = json.loads(record.manifest_json)
        pretty = json.dumps(payload, ensure_ascii=False, indent=2)
        pretty_hash = "sha256:" + hashlib.sha256(pretty.encode("utf-8")).hexdigest()
        with pytest.raises(ValueError, match="canonical JSON"):
            ResearchRunRecord(
                **{
                    **record.__dict__,
                    "manifest_json": pretty,
                    "manifest_json_hash": pretty_hash,
                }
            )
    finally:
        repository.close()
