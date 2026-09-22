import json
from datetime import date
from pathlib import Path
import subprocess
import sys

from quant_monitor.trading_calendar import VersionedSessionCalendar, write_calendar_snapshot


def test_validate_calendar_snapshot_cli_can_pin_identity_without_network(tmp_path):
    calendar = VersionedSessionCalendar(
        (date(2026, 1, 2), date(2026, 1, 5)),
        source="offline-fixture",
        version="2026.v1",
        identifier="cn-a",
    )
    snapshot = tmp_path / "calendar.json"
    write_calendar_snapshot(calendar, snapshot)
    script = Path(__file__).resolve().parents[1] / "scripts" / "validate_calendar_snapshot.py"

    command = [
        sys.executable,
        str(script),
        str(snapshot),
        "--expected-fingerprint",
        calendar.fingerprint,
        "--expected-identifier",
        "cn-a",
        "--expected-source",
        "offline-fixture",
        "--expected-version",
        "2026.v1",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["fingerprint"] == calendar.fingerprint

    mismatch = subprocess.run(
        command[:-1] + ["wrong-version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert mismatch.returncode == 2
    assert "version mismatch" in mismatch.stderr
