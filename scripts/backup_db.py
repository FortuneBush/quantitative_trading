"""Create an integrity-checked SQLite backup without contacting a provider."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant_monitor.storage import backup_sqlite


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup the local quant monitor SQLite database")
    parser.add_argument("source", type=Path, help="source SQLite database path")
    parser.add_argument("destination", type=Path, help="destination backup path")
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="online copy deadline and SQLite lock timeout in seconds",
    )
    args = parser.parse_args()
    result = backup_sqlite(
        args.source,
        args.destination,
        timeout_seconds=args.timeout,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
