#!/usr/bin/env python3
"""Verify a downloaded research-run export without starting the service.

The API export is intentionally a portable JSON envelope.  This command
reconstructs the manifest contract and recomputes both semantic and byte
identity hashes for the manifest and (when present) the immutable result.
It never opens SQLite, contacts a provider, or trusts the envelope's own
``integrity`` flags.  A valid bundle can still be ``incomplete`` when the
run has no result; pass ``--require-result`` when a complete research result
is required by an archive job.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

# When invoked as ``python scripts/verify_research_export.py`` Python puts the
# scripts directory—not the project root—at sys.path[0].  Keep the command
# usable from a clean Windows shell without requiring PYTHONPATH.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant_monitor.research.manifest import (
    DatasetDescriptor,
    ManifestValidationError,
    ResearchRunManifest,
    canonical_json,
    hash_canonical,
)


def _digest_json_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hash_matches(value: Any, expected: Any) -> bool:
    return isinstance(value, str) and value == expected


def _manifest_from_payload(payload: Mapping[str, Any]) -> ResearchRunManifest:
    if not isinstance(payload, Mapping):
        raise ManifestValidationError("run.manifest must be an object")
    strategy = payload.get("strategy")
    dataset = payload.get("dataset")
    versions = payload.get("versions")
    if not isinstance(strategy, Mapping) or not isinstance(dataset, Mapping):
        raise ManifestValidationError("run.manifest is missing strategy or dataset")
    if not isinstance(versions, Mapping):
        raise ManifestValidationError("run.manifest is missing versions")
    try:
        descriptor = DatasetDescriptor(
            name=dataset["name"],
            schema_version=dataset["schema_version"],
            source=dataset["source"],
            record_count=dataset["record_count"],
            attributes=dataset.get("attributes", {}),
        )
        return ResearchRunManifest(
            run_id=payload["run_id"],
            strategy_name=strategy["name"],
            strategy_parameters=strategy.get("parameters", {}),
            strategy_code_version=strategy["code_version"],
            dataset=descriptor,
            input_hash=payload["input_hash"],
            calendar_version=versions["calendar"],
            universe_version=versions["universe"],
            adjustment_version=versions["adjustment"],
            constraint_version=versions["constraints"],
            configuration=payload.get("configuration", {}),
            assumptions=payload.get("assumptions", {}),
            manifest_schema_version=payload.get("manifest_schema_version", ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ManifestValidationError("run.manifest is malformed") from exc


def verify_bundle(bundle: Mapping[str, Any], *, require_result: bool = False) -> Dict[str, Any]:
    """Return a conservative verification report for one parsed bundle."""

    if not isinstance(bundle, Mapping):
        raise ManifestValidationError("bundle must be a JSON object")
    if bundle.get("bundle_schema_version") != 1:
        raise ManifestValidationError("unsupported bundle_schema_version")
    run = bundle.get("run")
    integrity = bundle.get("integrity")
    if not isinstance(run, Mapping) or not isinstance(integrity, Mapping):
        raise ManifestValidationError("bundle is missing run or integrity")
    manifest_payload = run.get("manifest")
    manifest = _manifest_from_payload(manifest_payload)
    manifest_json = canonical_json(manifest.to_dict())
    checks: Dict[str, bool] = {
        "run_identity": run.get("run_id") == manifest.run_id
        and manifest_payload.get("run_id") == manifest.run_id,
        "manifest_content_hash": _hash_matches(run.get("content_hash"), manifest.content_hash)
        and _hash_matches(manifest_payload.get("content_hash"), manifest.content_hash),
        "manifest_json_hash": _hash_matches(
            run.get("manifest_json_hash"), _digest_json_text(manifest_json)
        ),
        "integrity_manifest_content_hash": _hash_matches(
            integrity.get("run_content_hash"), run.get("content_hash")
        ),
        "integrity_manifest_json_hash": _hash_matches(
            integrity.get("manifest_json_hash"), run.get("manifest_json_hash")
        ),
    }
    result = bundle.get("result")
    result_available = bundle.get("result_available")
    if result is None:
        if result_available is not False:
            checks["result_availability_flag"] = False
        else:
            checks["result_availability_flag"] = True
        complete = False
        status = "incomplete"
    else:
        if not isinstance(result, Mapping):
            raise ManifestValidationError("result must be an object or null")
        result_payload = result.get("result")
        if not isinstance(result_payload, Mapping):
            raise ManifestValidationError("result.result must be an object")
        result_json = canonical_json(result_payload)
        declared_result_hash = result.get("result_hash")
        unsigned = dict(result_payload)
        payload_result_hash = unsigned.pop("result_hash", None)
        expected_result_hash = hash_canonical(unsigned, namespace="backtest-result-v1")
        expected_json_hash = _digest_json_text(result_json)
        checks.update(
            {
                "result_run_id": result.get("run_id") == run.get("run_id"),
                "result_strategy_matches_manifest": result_payload.get("strategy_name")
                == manifest.strategy_name,
                "result_content_hash": _hash_matches(declared_result_hash, expected_result_hash)
                and _hash_matches(payload_result_hash, expected_result_hash),
                "result_json_hash": _hash_matches(
                    result.get("result_json_hash"), expected_json_hash
                ),
                "integrity_result_hash": _hash_matches(
                    integrity.get("result_hash"), declared_result_hash
                ),
                "integrity_result_json_hash": _hash_matches(
                    integrity.get("result_json_hash"), result.get("result_json_hash")
                ),
                "result_availability_flag": result_available is True,
            }
        )
        complete = all(checks.values())
        status = "verified" if complete else "invalid"
    valid = all(checks.values()) and (not require_result or result is not None)
    if valid and result is None:
        status = "incomplete"
    return {
        "valid": valid,
        "complete": complete,
        "status": status,
        "run_id": run.get("run_id"),
        "checks": checks,
        "result_available": result is not None,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="离线校验 QuantMonitor 研究导出包")
    parser.add_argument("bundle", type=Path, help="/api/v1/research/runs/{id}/export 保存的 JSON 文件")
    parser.add_argument(
        "--require-result",
        action="store_true",
        help="没有不可变结果时返回失败（适用于归档门禁）",
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="仅输出 JSON 报告")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
        report = verify_bundle(bundle, require_result=args.require_result)
    except (OSError, UnicodeError, json.JSONDecodeError, ManifestValidationError, ValueError) as exc:
        report = {"valid": False, "complete": False, "status": "invalid", "error": str(exc)}
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        if report.get("valid"):
            print("研究包校验通过：{} ({})".format(report.get("run_id", "unknown"), report["status"]))
        else:
            failed = [key for key, value in report.get("checks", {}).items() if value is False]
            detail = report.get("error") or ("失败检查：" + ", ".join(failed))
            print("研究包校验失败：{}".format(detail), file=sys.stderr)
    return 0 if report.get("valid") else 1


if __name__ == "__main__":
    raise SystemExit(main())
