"""Offline tests for backup RPO freshness and the scheduled probe CLI."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import pytest

from quant_monitor.storage import SQLiteRepository, check_backup_freshness
from scripts import backup_slo


def _set_mtime(path: Path, value: datetime) -> None:
    timestamp = value.timestamp()
    os.utime(path, (timestamp, timestamp))


def test_check_backup_freshness_distinguishes_fresh_stale_and_future(tmp_path: Path):
    archive = tmp_path / "archive.sqlite3"
    archive.write_bytes(b"placeholder")
    now = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)

    _set_mtime(archive, now - timedelta(seconds=30))
    fresh = check_backup_freshness(archive, max_age_seconds=60, now=now)
    assert fresh.state == "fresh"
    assert fresh.fresh is True
    assert fresh.age_seconds == pytest.approx(30)

    stale = check_backup_freshness(archive, max_age_seconds=10, now=now)
    assert stale.state == "stale"
    assert stale.fresh is False

    _set_mtime(archive, now + timedelta(seconds=5))
    future = check_backup_freshness(archive, max_age_seconds=60, now=now)
    assert future.state == "future"
    assert future.age_seconds == pytest.approx(-5)


def test_check_backup_freshness_validates_path_age_and_clock(tmp_path: Path):
    archive = tmp_path / "archive.sqlite3"
    archive.write_bytes(b"placeholder")
    with pytest.raises(FileNotFoundError):
        check_backup_freshness(tmp_path / "missing.sqlite3", max_age_seconds=60)
    with pytest.raises(ValueError, match="max_age_seconds"):
        check_backup_freshness(archive, max_age_seconds=float("nan"))
    with pytest.raises(TypeError, match="timezone-aware"):
        check_backup_freshness(archive, max_age_seconds=60, now=datetime.now())


def test_backup_slo_cli_returns_structured_success_for_valid_fresh_archive(
    tmp_path: Path, capsys
):
    archive = tmp_path / "archive.sqlite3"
    repository = SQLiteRepository(archive)
    repository.initialize()
    repository.close()
    code = backup_slo.main([str(archive), "--max-age", "3600"])
    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["ok"] is True
    assert output["archives"][0]["fresh"] is True
    assert output["archives"][0]["verification"]["integrity_check"] == "ok"


def test_backup_slo_cli_fails_stale_archive_without_provider_io(tmp_path: Path, capsys):
    archive = tmp_path / "archive.sqlite3"
    repository = SQLiteRepository(archive)
    repository.initialize()
    repository.close()
    old = datetime.now(timezone.utc) - timedelta(days=2)
    _set_mtime(archive, old)
    code = backup_slo.main([str(archive), "--max-age", "60"])
    output = json.loads(capsys.readouterr().out)
    assert code == 1
    assert output["ok"] is False
    assert output["archives"][0]["state"] == "stale"
    # Integrity is still reported, allowing operators to distinguish age from
    # corruption in one scheduled invocation.
    assert output["archives"][0]["verification"]["integrity_check"] == "ok"


def test_backup_slo_cli_reports_missing_archive_without_traceback(tmp_path: Path, capsys):
    code = backup_slo.main([str(tmp_path / "missing.sqlite3")])
    output = json.loads(capsys.readouterr().out)
    assert code == 1
    assert output["ok"] is False
    assert output["archives"][0]["state"] == "error"
    assert output["archives"][0]["error_type"] == "FileNotFoundError"


@pytest.mark.parametrize(
    "option,value",
    [("--max-age", "nan"), ("--max-age", "inf"), ("--timeout", "nan"), ("--timeout", "inf")],
)
def test_backup_slo_cli_rejects_non_finite_limits(option: str, value: str):
    with pytest.raises(SystemExit) as exc_info:
        backup_slo.main(["backup.sqlite3", option, value])
    assert exc_info.value.code == 2
