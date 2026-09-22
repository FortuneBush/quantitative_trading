r"""Check SQLite backup freshness and integrity without contacting a provider.

The command is suitable for Task Scheduler/cron and emits one JSON document
for machine-readable RPO evidence.  A zero exit status means every archive is
within the configured age bound *and* passes the application's read-only
schema/integrity verification.  Missing, stale, future-dated or corrupt
archives produce a non-zero exit status without exposing exception text.

Examples (PowerShell)::

    .\.venv\Scripts\python.exe scripts\backup_slo.py backups\quant.sqlite3
    .\.venv\Scripts\python.exe scripts\backup_slo.py backups\*.sqlite3 --max-age 86400
"""

from __future__ import annotations

import argparse
import glob
import json
import math
from pathlib import Path
import sys
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant_monitor.storage import check_backup_freshness, verify_sqlite


def _check_one(path: Path, *, max_age: float, timeout: float) -> dict[str, Any]:
    """Return a bounded, JSON-safe status record for one archive."""

    try:
        freshness = check_backup_freshness(path, max_age_seconds=max_age)
    except (FileNotFoundError, OSError, TypeError, ValueError, OverflowError) as exc:
        return {
            "path": str(path),
            "state": "error",
            "fresh": False,
            "error_type": type(exc).__name__,
        }
    result = freshness.to_dict()
    # Verify even stale archives so an operator can distinguish an expired
    # retention window from an unusable artifact in one scheduled run.
    try:
        verification = verify_sqlite(path, timeout_seconds=timeout)
    except Exception as exc:  # noqa: BLE001 - CLI emits a stable error type only
        result.update(
            {
                "state": "invalid",
                "fresh": False,
                "error_type": type(exc).__name__,
            }
        )
        return result
    result["verification"] = {
        "schema_version": verification.schema_version,
        "table_count": verification.table_count,
        "integrity_check": verification.integrity_check,
        "sha256": verification.sha256,
    }
    result["fresh"] = bool(freshness.fresh)
    return result


def _iter_paths(values: Iterable[Path]) -> list[Path]:
    """Expand files and glob patterns while retaining deterministic order."""

    paths: list[Path] = []
    for value in values:
        text = str(value)
        if any(char in text for char in "*?["):
            paths.extend(Path(item) for item in sorted(glob.glob(text)))
        else:
            paths.append(value)
    # De-duplicate aliases without changing the first-seen order.
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path.expanduser().resolve())
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="检查本地 SQLite 备份新鲜度（RPO）与完整性"
    )
    parser.add_argument(
        "backup",
        nargs="+",
        type=Path,
        help="备份文件路径；可使用通配符",
    )
    parser.add_argument(
        "--max-age",
        type=float,
        default=86400.0,
        help="允许的最大备份年龄（秒，默认 86400）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="单个备份完整性校验的 SQLite 锁等待上限（秒）",
    )
    args = parser.parse_args(argv)
    try:
        max_age = float(args.max_age)
        timeout = float(args.timeout)
        if not math.isfinite(max_age) or not math.isfinite(timeout):
            raise ValueError
        if max_age < 0 or timeout <= 0:
            raise ValueError
    except (TypeError, ValueError):
        parser.error("--max-age 必须非负，--timeout 必须为正数")
    paths = _iter_paths(args.backup)
    if not paths:
        parser.error("没有匹配的备份文件")
    records = [_check_one(path, max_age=max_age, timeout=timeout) for path in paths]
    payload = {
        "ok": all(record.get("fresh") is True for record in records),
        "max_age_seconds": max_age,
        "archives": records,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
