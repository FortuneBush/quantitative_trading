"""Verify or rehearse restoring a SQLite backup, entirely offline.

Examples (PowerShell)::

    .\.venv\Scripts\python.exe scripts\verify_backup.py backups\asharehub.sqlite3
    .\.venv\Scripts\python.exe scripts\verify_backup.py backups\asharehub.sqlite3 --restore-dir tmp\restore-drill

The restore mode requires a missing or empty directory and never starts the
application or contacts a market-data provider.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant_monitor.storage import restore_sqlite_to_empty_dir, verify_sqlite


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify a local SQLite backup or restore it into an empty directory"
    )
    parser.add_argument("backup", type=Path, help="SQLite backup path")
    parser.add_argument(
        "--restore-dir",
        type=Path,
        help="optional missing/empty directory for a non-destructive restore drill",
    )
    parser.add_argument(
        "--filename",
        default="restored.sqlite3",
        help="restored filename (only used with --restore-dir)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="verification/restore SQLite lock timeout in seconds",
    )
    args = parser.parse_args()
    if args.restore_dir is None:
        result = verify_sqlite(args.backup, timeout_seconds=args.timeout)
    else:
        result = restore_sqlite_to_empty_dir(
            args.backup,
            args.restore_dir,
            filename=args.filename,
            timeout_seconds=args.timeout,
        )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
