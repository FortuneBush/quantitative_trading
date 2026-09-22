r"""Import point-in-time fundamental observations into the local SQLite store.

This is an explicit, offline ingestion boundary.  It never contacts a data
provider and never reads an API key.  Input may be a JSON array/object or
newline-delimited JSON (JSONL).  Observations are validated by the immutable
domain model before the repository's append-only/idempotent write path runs.

Example (PowerShell)::

    .\.venv\Scripts\python.exe scripts\import_fundamentals.py \
        data\quant_monitor_asharehub.db fundamentals.json
    .\.venv\Scripts\python.exe scripts\import_fundamentals.py \
        data\quant_monitor_asharehub.db fundamentals.json --dry-run
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant_monitor.domain import FundamentalObservation
from quant_monitor.research import canonical_json, hash_input_records
from quant_monitor.storage import SQLiteRepository, SchemaMigrationError


INPUT_SCHEMA = "fundamental_observations_v1"
MAX_INPUT_BYTES = 16 * 1024 * 1024
_ALLOWED_FIELDS = frozenset(
    {
        "observation_id",
        "symbol",
        "metric",
        "value",
        "unit",
        "scope",
        "period_start",
        "period_end",
        "available_at",
        "received_at",
        "source",
        "revision_id",
        "metadata",
        "content_hash",
    }
)


class FundamentalImportError(ValueError):
    """Raised when an input file violates the import contract."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise FundamentalImportError("duplicate JSON fields are not permitted")
        result[key] = value
    return result


def _reject_constant(value):
    raise FundamentalImportError("non-finite JSON values are not permitted")


def _decode(text):
    return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


def _load_items(path: Path, *, max_records: int, max_bytes: int) -> tuple[str, List[Mapping[str, Any]]]:
    try:
        # Bound the read itself, not just the row count after allocation.
        with path.open("rb") as stream:
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise FundamentalImportError("input exceeds maximum byte count")
        text = data.decode("utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise FundamentalImportError("unable to read input file") from exc
    if not text.strip():
        raise FundamentalImportError("input file is empty")

    try:
        decoded = _decode(text)
    except json.JSONDecodeError as exc:
        # Only multiple complete JSON objects can fall back to JSONL. An
        # invalid JSON document must not be reinterpreted after a parse error.
        if exc.msg != "Extra data":
            raise FundamentalImportError("invalid JSON input") from None
        rows: List[Mapping[str, Any]] = []
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                item = _decode(line)
            except json.JSONDecodeError as exc:
                raise FundamentalImportError(
                    "invalid JSONL input at line {}".format(line_number)
                ) from exc
            if not isinstance(item, Mapping):
                raise FundamentalImportError(
                    "JSONL line {} must contain an object".format(line_number)
                )
            rows.append(item)
            if len(rows) > max_records:
                raise FundamentalImportError("input exceeds maximum record count")
        if not rows:
            raise FundamentalImportError("input contains no observations")
        return "jsonl", rows

    if isinstance(decoded, list):
        rows = decoded
        schema = INPUT_SCHEMA
    elif isinstance(decoded, Mapping):
        # A single-record JSONL file is also a valid JSON object. Do not
        # accidentally treat its observation fields as wrapper metadata.
        if "observation_id" in decoded:
            return "jsonl" if path.suffix.lower() in {".jsonl", ".ndjson"} else "json", [decoded]
        allowed_wrapper = {"schema_version", "observations", "items"}
        unknown = set(decoded).difference(allowed_wrapper)
        if unknown:
            raise FundamentalImportError("input wrapper contains unsupported fields")
        if "observations" in decoded and "items" in decoded:
            raise FundamentalImportError("input must use observations or items, not both")
        schema = decoded.get("schema_version", INPUT_SCHEMA)
        rows = decoded.get("observations", decoded.get("items"))
    else:
        raise FundamentalImportError("input root must be an array or object")
    if schema != INPUT_SCHEMA:
        raise FundamentalImportError("unsupported input schema version")
    if not isinstance(rows, list) or not rows:
        raise FundamentalImportError("input observations must be a non-empty array")
    if any(not isinstance(item, Mapping) for item in rows):
        raise FundamentalImportError("every observation must be an object")
    return "json", list(rows)


def _parse_date(value: Any, name: str) -> date:
    if not isinstance(value, str):
        raise FundamentalImportError("{} must be an ISO date".format(name))
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError("non-canonical date")
        return parsed
    except ValueError as exc:
        raise FundamentalImportError("{} must be an ISO date".format(name)) from exc


def _parse_datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise FundamentalImportError(
            "{} must be an ISO-8601 timezone-aware datetime".format(name)
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise FundamentalImportError(
            "{} must be an ISO-8601 timezone-aware datetime".format(name)
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FundamentalImportError(
            "{} must include a timezone offset".format(name)
        )
    return parsed


def _build_observation(raw: Mapping[str, Any], row_number: int) -> FundamentalObservation:
    try:
        # Reuse the research privacy boundary, including Unicode/URL-encoded
        # secrets and private paths, instead of a weaker importer deny-list.
        canonical_json(raw)
    except (TypeError, ValueError, RecursionError):
        raise FundamentalImportError(
            "row {} contains unsafe or non-canonical content".format(row_number)
        ) from None
    unknown = set(raw).difference(_ALLOWED_FIELDS)
    if unknown:
        raise FundamentalImportError(
            "row {} contains unsupported fields".format(row_number)
        )
    required = {
        "observation_id",
        "symbol",
        "metric",
        "value",
        "unit",
        "scope",
        "period_start",
        "period_end",
        "available_at",
        "received_at",
        "source",
        "revision_id",
    }
    missing = required.difference(raw)
    if missing:
        raise FundamentalImportError(
            "row {} is missing required fields".format(row_number)
        )
    for name in required.difference({"value"}):
        if not isinstance(raw[name], str) or not raw[name].strip():
            raise FundamentalImportError("row {} requires string fields".format(row_number))
    # Financial precision must survive the JSON boundary without a float
    # round-trip. Null alone denotes a missing fact; bools are not numbers.
    if raw["value"] is not None and not isinstance(raw["value"], str):
        raise FundamentalImportError("row {} value must be a decimal string or null".format(row_number))
    metadata = raw.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise FundamentalImportError("row {} metadata must be an object".format(row_number))
    try:
        observation = FundamentalObservation(
            observation_id=raw["observation_id"],
            symbol=raw["symbol"],
            metric=raw["metric"],
            value=raw["value"],
            unit=raw["unit"],
            scope=raw["scope"],
            period_start=_parse_date(raw["period_start"], "period_start"),
            period_end=_parse_date(raw["period_end"], "period_end"),
            available_at=_parse_datetime(raw["available_at"], "available_at"),
            received_at=_parse_datetime(raw["received_at"], "received_at"),
            source=raw["source"],
            revision_id=raw["revision_id"],
            metadata=dict(metadata),
        )
    except FundamentalImportError:
        raise
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise FundamentalImportError(
            "row {} violates the fundamental observation contract".format(row_number)
        ) from exc
    supplied_hash = raw.get("content_hash")
    if supplied_hash is not None and supplied_hash != observation.content_hash:
        raise FundamentalImportError(
            "row {} content_hash does not match canonical content".format(row_number)
        )
    return observation


def parse_observations(path: Path, *, max_records: int = 50_000, max_bytes: int = MAX_INPUT_BYTES) -> tuple[str, tuple[FundamentalObservation, ...]]:
    """Parse and validate an input file without touching SQLite."""

    if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records <= 0:
        raise ValueError("max_records must be a positive integer")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    try:
        format_name, rows = _load_items(Path(path), max_records=max_records, max_bytes=max_bytes)
    except RecursionError:
        raise FundamentalImportError("input nesting exceeds supported depth") from None
    if len(rows) > max_records:
        raise FundamentalImportError("input exceeds the maximum record count")
    observations = tuple(
        _build_observation(item, index)
        for index, item in enumerate(rows, 1)
    )
    # Match the repository's conflict rule even during dry-run. A later
    # conflicting row must not be hidden by an earlier identical ID.
    seen = {}
    for item in observations:
        if item.observation_id in seen and seen[item.observation_id] != item.content_hash:
            raise FundamentalImportError("input contains conflicting observation IDs")
        seen[item.observation_id] = item.content_hash
    return format_name, observations


def import_observations(
    database: Path,
    observations: Sequence[FundamentalObservation],
    *,
    dry_run: bool = False,
) -> int:
    """Append observations and return the number of newly inserted rows."""

    if dry_run:
        return 0
    repository = SQLiteRepository(database)
    try:
        repository.initialize()
        return repository.insert_fundamental_observations(observations)
    finally:
        repository.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import immutable point-in-time fundamentals into local SQLite"
    )
    parser.add_argument("database", type=Path, help="SQLite database path")
    parser.add_argument("input", type=Path, help="JSON/JSONL observations file")
    parser.add_argument("--dry-run", action="store_true", help="validate input only; do not read/create/change the database")
    parser.add_argument("--max-records", type=int, default=50_000, help="maximum accepted observations (default: 50000)")
    args = parser.parse_args(argv)
    try:
        format_name, observations = parse_observations(
            args.input, max_records=args.max_records
        )
        input_hash = hash_input_records(item.to_dict() for item in observations)
        inserted = import_observations(
            args.database, observations, dry_run=args.dry_run
        )
    except (OSError, TypeError, ValueError, sqlite3.Error, SchemaMigrationError) as exc:
        # Do not echo row contents, metadata or exception text: an invalid
        # input could contain a credential-like value supplied by a caller.
        print("IMPORT_FAIL {}".format(type(exc).__name__), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "validated" if args.dry_run else "imported",
                "format": format_name,
                "record_count": len(observations),
                "input_hash": input_hash,
                "validation_scope": "input_only" if args.dry_run else "input_and_database",
                "inserted_count": inserted,
                "idempotent": not args.dry_run,
                "database_changed": not args.dry_run and inserted > 0,
                "schema": INPUT_SCHEMA,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
