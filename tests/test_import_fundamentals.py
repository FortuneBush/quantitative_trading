"""Offline tests for the explicit point-in-time fundamental importer."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pytest

from quant_monitor.storage import SQLiteRepository
from scripts.import_fundamentals import main, parse_observations


def _row(*, observation_id="fixture-roe-1", value="0.12"):
    return {
        "observation_id": observation_id,
        "symbol": "600036.SH",
        "metric": "roe",
        "value": value,
        "unit": "ratio",
        "scope": "consolidated",
        "period_start": "2024-01-01",
        "period_end": "2024-12-31",
        "available_at": "2025-01-02T08:00:00+08:00",
        "received_at": "2025-01-02T08:01:00+08:00",
        "source": "fixture",
        "revision_id": "r1",
        "metadata": {"filing_type": "annual"},
    }


def test_json_import_is_append_only_and_idempotent(tmp_path, capsys):
    database = tmp_path / "fundamentals.sqlite3"
    source = tmp_path / "facts.json"
    source.write_text(json.dumps({"schema_version": "fundamental_observations_v1", "observations": [_row()]}), encoding="utf-8")

    assert main([str(database), str(source)]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["inserted_count"] == 1
    assert first["database_changed"] is True

    assert main([str(database), str(source)]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["record_count"] == 1
    assert second["inserted_count"] == 0
    assert second["database_changed"] is False

    repository = SQLiteRepository(database)
    repository.initialize()
    try:
        selected, total = repository.query_fundamentals_as_of(
            decision_at=datetime.fromisoformat("2025-01-03T15:00:00+08:00"),
            symbol="600036.SH",
            metric="roe",
        )
        assert total == 1
        assert [item.observation_id for item in selected] == ["fixture-roe-1"]
    finally:
        repository.close()


def test_jsonl_and_dry_run_validate_without_creating_database(tmp_path, capsys):
    database = tmp_path / "dry-run.sqlite3"
    source = tmp_path / "facts.jsonl"
    source.write_text(
        "\n".join(
            [
                json.dumps(_row(observation_id="a")),
                json.dumps(_row(observation_id="b", value="0.13")),
            ]
        ),
        encoding="utf-8",
    )

    assert main([str(database), str(source), "--dry-run"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "validated"
    assert result["format"] == "jsonl"
    assert result["record_count"] == 2
    assert not database.exists()


def test_content_hash_is_verified_and_sensitive_material_is_rejected(tmp_path, capsys):
    source = tmp_path / "bad.json"
    bad = _row()
    bad["content_hash"] = "sha256:" + "0" * 64
    source.write_text(json.dumps([bad]), encoding="utf-8")
    assert main([str(tmp_path / "hash.sqlite3"), str(source)]) == 2
    assert "content_hash" not in capsys.readouterr().err

    secret = _row()
    # Deliberately fake and shorter than repository secret scanners' live-key
    # signature; the importer rejects the sensitive field name itself.
    secret["metadata"] = {"api_key": "ash_" + "A" * 20}
    source.write_text(json.dumps([secret]), encoding="utf-8")
    assert main([str(tmp_path / "secret.sqlite3"), str(source)]) == 2
    assert "ash_" not in capsys.readouterr().err


def test_parser_rejects_naive_datetimes_and_unknown_fields(tmp_path):
    source = tmp_path / "invalid.json"
    row = _row()
    row["available_at"] = "2025-01-02T08:00:00"
    row["unexpected"] = True
    source.write_text(json.dumps([row]), encoding="utf-8")
    with pytest.raises(ValueError):
        parse_observations(source)


def test_single_record_jsonl_and_precision_are_preserved(tmp_path):
    source = tmp_path / "one.jsonl"
    exact = "0.12345678901234567890123456789"
    source.write_text(json.dumps(_row(value=exact)) + "\n", encoding="utf-8")
    format_name, rows = parse_observations(source)
    assert format_name == "jsonl"
    assert str(rows[0].value) == exact


@pytest.mark.parametrize("encoded", [
    '{"items": [], "items": []}',
    '{"items": [], "observations": []}',
    '{"items": [], "schema_version": null}',
    '{"items": [], "metadata": {"value": NaN}}',
])
def test_ambiguous_or_nonfinite_json_is_rejected(tmp_path, encoded):
    source = tmp_path / "invalid.json"
    source.write_text(encoded, encoding="utf-8")
    with pytest.raises(ValueError):
        parse_observations(source)


@pytest.mark.parametrize("field,value", [
    ("available_at", "2025-01-02T08:00:00"),
    ("received_at", "2025-01-01T00:00:00+08:00"),
    ("observation_id", 12),
    ("source", True),
    ("value", True),
    ("value", 0.123456789),
    ("value", "NaN"),
    ("metadata", {"ApiKey": "fixture-secret"}),
    ("metadata", {"a\u200bpi_key": "fixture-secret"}),
    ("metadata", {"note": "https://example.test/data?token=fixture-secret"}),
])
def test_invalid_input_rejected_before_any_database_write(tmp_path, capsys, field, value):
    source = tmp_path / "invalid.json"
    database = tmp_path / "absent.sqlite3"
    row = _row()
    row[field] = value
    source.write_text(json.dumps([row]), encoding="utf-8")
    assert main([str(database), str(source)]) == 2
    error = capsys.readouterr().err
    assert "fixture-secret" not in error
    assert not database.exists()


def test_dry_run_detects_internal_conflicts_without_opening_database(tmp_path, capsys):
    source = tmp_path / "conflict.json"
    database = tmp_path / "absent.sqlite3"
    source.write_text(json.dumps([_row(), _row(value="0.99")]), encoding="utf-8")
    assert main([str(database), str(source), "--dry-run"]) == 2
    assert not database.exists()


def test_existing_conflict_rolls_back_all_new_facts(tmp_path, capsys):
    source = tmp_path / "facts.json"
    database = tmp_path / "facts.sqlite3"
    source.write_text(json.dumps([_row()]), encoding="utf-8")
    assert main([str(database), str(source)]) == 0
    source.write_text(json.dumps([
        _row(observation_id="new-before-conflict"), _row(value="0.99"),
    ]), encoding="utf-8")
    # Dry-run explicitly validates only the file, not existing DB identities.
    assert main([str(database), str(source), "--dry-run"]) == 0
    output = capsys.readouterr().out.splitlines()
    assert json.loads(output[-1])["validation_scope"] == "input_only"
    assert main([str(database), str(source)]) == 2
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT observation_id FROM fundamental_observations").fetchall() == [("fixture-roe-1",)]


def test_byte_and_record_limits_bound_input(tmp_path):
    source = tmp_path / "facts.jsonl"
    source.write_text("\n".join(json.dumps(_row(observation_id=str(i))) for i in range(3)), encoding="utf-8")
    with pytest.raises(ValueError, match="byte"):
        parse_observations(source, max_bytes=8)
    with pytest.raises(ValueError, match="record"):
        parse_observations(source, max_records=2)


def test_duplicate_nested_keys_and_invalid_encoding_are_rejected(tmp_path):
    source = tmp_path / "facts.json"
    encoded = json.dumps([_row()]).replace('"filing_type": "annual"', '"filing_type": "annual", "filing_type": "quarter"')
    source.write_text(encoded, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        parse_observations(source)
    source.write_bytes(b"\xff\xfe")
    with pytest.raises(ValueError, match="read"):
        parse_observations(source)


def test_database_error_has_nonzero_exit_without_traceback(tmp_path, capsys):
    source = tmp_path / "facts.json"
    source.write_text(json.dumps([_row()]), encoding="utf-8")
    database = tmp_path / "invalid.sqlite3"
    database.write_bytes(b"not a SQLite database")
    assert main([str(database), str(source)]) == 2
    assert "Traceback" not in capsys.readouterr().err


def test_dry_run_and_import_share_deterministic_input_hash(tmp_path, capsys):
    source = tmp_path / "facts.json"
    database = tmp_path / "facts.sqlite3"
    source.write_text(json.dumps([_row()]), encoding="utf-8")
    assert main([str(database), str(source), "--dry-run"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert main([str(database), str(source)]) == 0
    actual = json.loads(capsys.readouterr().out)
    assert dry["input_hash"] == actual["input_hash"]
    assert dry["input_hash"].startswith("sha256:")
