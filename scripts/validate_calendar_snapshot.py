"""Validate an offline trading-calendar snapshot without network access."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant_monitor.trading_calendar import CalendarSnapshotError, load_calendar_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate an immutable offline A-share calendar snapshot"
    )
    parser.add_argument("path", type=Path, help="snapshot JSON path")
    parser.add_argument(
        "--expected-fingerprint",
        help="require this exact lowercase SHA-256 snapshot fingerprint",
    )
    parser.add_argument(
        "--expected-identifier",
        help="require this exact calendar identifier",
    )
    parser.add_argument(
        "--expected-source",
        help="require this exact declared source label",
    )
    parser.add_argument(
        "--expected-version",
        help="require this exact source revision/version label",
    )
    args = parser.parse_args()
    try:
        calendar = load_calendar_snapshot(
            args.path,
            expected_fingerprint=args.expected_fingerprint,
            expected_identifier=args.expected_identifier,
            expected_source=args.expected_source,
            expected_version=args.expected_version,
        )
    except (CalendarSnapshotError, ValueError) as exc:
        parser.error("invalid calendar snapshot: {}".format(exc))
    first, last = calendar.coverage
    print(
        json.dumps(
            {
                "identifier": calendar.metadata.identifier,
                "source": calendar.metadata.source,
                "version": calendar.metadata.version,
                "verified": calendar.metadata.verified,
                "coverage": [first.isoformat(), last.isoformat()],
                "session_count": len(calendar.session_dates),
                "fingerprint": calendar.fingerprint,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
