"""Pure tests for the research governance gate."""

from __future__ import annotations

import pytest

from quant_monitor.research import ResearchGovernanceReport, build_governance_report
from quant_monitor.research.governance import REQUIRED_CHECKS


def test_governance_report_is_fail_closed_and_explainable():
    report = build_governance_report(
        "run-1",
        manifest_integrity=True,
        result_integrity=True,
        result_metrics_integrity=True,
        strategy_matches_manifest=True,
        lineage_verified=False,
        decision_grade=False,
        warnings=("lineage_attestation_missing", "lineage_attestation_missing"),
    )
    assert report.artifacts_consistent is True
    assert report.reproducible is False
    assert report.decision_ready is False
    assert report.failures == ("decision_grade", "lineage_verified", "replay_verified")
    assert report.warnings == ("lineage_attestation_missing",)
    assert report.to_dict()["checks"]["lineage_verified"] is False


def test_governance_report_requires_all_named_checks_for_decision_ready():
    report = ResearchGovernanceReport(
        "run-2",
        {
            "decision_grade": True,
            "lineage_verified": True,
            "manifest_integrity": True,
            "result_integrity": True,
            "result_metrics_integrity": True,
            "strategy_matches_manifest": True,
            "replay_verified": True,
        },
    )
    assert report.reproducible is True
    assert report.decision_ready is True
    assert report.failures == ()


def test_governance_report_rejects_empty_or_unknown_method():
    with pytest.raises(ValueError, match="run_id"):
        ResearchGovernanceReport(" ", {"check": True})
    with pytest.raises(ValueError, match="unsupported governance method"):
        ResearchGovernanceReport("run", {"check": True}, method="other")
    with pytest.raises(ValueError, match="complete governance contract"):
        ResearchGovernanceReport("run", {"manifest_integrity": True})


@pytest.mark.parametrize("name", REQUIRED_CHECKS)
@pytest.mark.parametrize("invalid", ["false", "true", 0, 1, None, [], {}])
def test_governance_rejects_truthy_or_coerced_evidence(name, invalid):
    checks = dict.fromkeys(REQUIRED_CHECKS, True)
    checks[name] = invalid
    with pytest.raises(TypeError, match="must be booleans"):
        ResearchGovernanceReport("run", checks)


def test_governance_distinguishes_replay_from_artifact_consistency():
    checks = dict.fromkeys(REQUIRED_CHECKS, True)
    checks["replay_verified"] = False
    report = ResearchGovernanceReport("run", checks)
    assert report.artifacts_consistent is True
    assert report.reproducible is report.decision_ready is False
    checks["replay_verified"] = True
    assert report.checks["replay_verified"] is False
    report.to_dict()["checks"]["replay_verified"] = True
    assert report.reproducible is False
    checks["lineage_verified"] = False
    reproduced = ResearchGovernanceReport("run", checks)
    assert reproduced.reproducible is True
    assert reproduced.decision_ready is False
