import pytest

from quant_monitor.research.manifest import DatasetDescriptor, ResearchRunManifest, canonical_json, hash_canonical
from scripts.verify_research_export import verify_bundle


def _bundle(*, with_result=True):
    manifest = ResearchRunManifest(
        run_id="offline-export-1",
        strategy_name="fixture_strategy",
        strategy_parameters={"window": 20},
        strategy_code_version="fixture-code-v1",
        dataset=DatasetDescriptor(
            name="fixture-bars",
            schema_version="bars-v1",
            source="fixture",
            record_count=2,
            attributes={"interval": "1d"},
        ),
        input_hash="sha256:" + "1" * 64,
        calendar_version="weekday-fallback-untrusted",
        universe_version="fixture-universe",
        adjustment_version="raw-unadjusted-v1",
        constraint_version="fixture-constraints",
        configuration={"initial_cash": "1000000"},
        assumptions={"purpose": "research_only"},
    )
    manifest_payload = manifest.to_dict()
    manifest_json_hash = "sha256:" + __import__("hashlib").sha256(
        canonical_json(manifest_payload).encode("utf-8")
    ).hexdigest()
    run = {
        "run_id": manifest.run_id,
        "content_hash": manifest.content_hash,
        "manifest_json_hash": manifest_json_hash,
        "manifest": manifest_payload,
    }
    result = None
    integrity = {
        "run_content_hash": manifest.content_hash,
        "manifest_json_hash": manifest_json_hash,
        "result_hash": None,
        "result_json_hash": None,
    }
    if with_result:
        unsigned = {"strategy_name": "fixture_strategy", "metrics": {"total_return": 0.1}}
        result_hash = hash_canonical(unsigned, namespace="backtest-result-v1")
        result_payload = {**unsigned, "result_hash": result_hash}
        result_json_hash = "sha256:" + __import__("hashlib").sha256(
            canonical_json(result_payload).encode("utf-8")
        ).hexdigest()
        result = {
            "run_id": manifest.run_id,
            "result_hash": result_hash,
            "result_json_hash": result_json_hash,
            "result": result_payload,
        }
        integrity.update(result_hash=result_hash, result_json_hash=result_json_hash)
    return {
        "bundle_schema_version": 1,
        "run": run,
        "result": result,
        "result_available": with_result,
        "integrity": integrity,
    }


def test_verify_research_export_checks_manifest_and_result_hashes():
    report = verify_bundle(_bundle())
    assert report["valid"] is True
    assert report["complete"] is True
    assert report["status"] == "verified"
    assert all(report["checks"].values())


def test_verify_research_export_rejects_tampered_result_even_if_envelope_hash_is_unchanged():
    bundle = _bundle()
    bundle["result"]["result"]["metrics"]["total_return"] = 0.2
    report = verify_bundle(bundle)
    assert report["valid"] is False
    assert report["checks"]["result_content_hash"] is False


def test_verify_research_export_binds_envelope_identity_to_manifest():
    bundle = _bundle()
    bundle["run"]["run_id"] = "other-run"
    report = verify_bundle(bundle)
    assert report["valid"] is False
    assert report["checks"]["run_identity"] is False


def test_verify_research_export_rejects_result_from_another_strategy():
    bundle = _bundle()
    payload = bundle["result"]["result"]
    unsigned = {"strategy_name": "other_strategy", "metrics": payload["metrics"]}
    result_hash = hash_canonical(unsigned, namespace="backtest-result-v1")
    replacement = {**unsigned, "result_hash": result_hash}
    import hashlib
    result_json_hash = "sha256:" + hashlib.sha256(canonical_json(replacement).encode("utf-8")).hexdigest()
    bundle["result"]["result"] = replacement
    bundle["result"]["result_hash"] = result_hash
    bundle["result"]["result_json_hash"] = result_json_hash
    bundle["integrity"]["result_hash"] = result_hash
    bundle["integrity"]["result_json_hash"] = result_json_hash
    report = verify_bundle(bundle)
    assert report["valid"] is False
    assert report["checks"]["result_strategy_matches_manifest"] is False


def test_verify_research_export_allows_incomplete_run_but_require_result_fails():
    bundle = _bundle(with_result=False)
    report = verify_bundle(bundle)
    assert report["valid"] is True
    assert report["complete"] is False
    assert report["status"] == "incomplete"
    required = verify_bundle(bundle, require_result=True)
    assert required["valid"] is False


def test_verify_research_export_rejects_manifest_security_values():
    bundle = _bundle()
    bundle["run"]["manifest"]["assumptions"]["token"] = "secret"
    with pytest.raises(ValueError):
        verify_bundle(bundle)
