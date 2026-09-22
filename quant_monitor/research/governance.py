"""Fail-closed governance projection for persisted research runs.

The storage layer intentionally keeps manifests and result artifacts immutable,
but clients previously had to call several audit endpoints and combine their
answers themselves. Artifact consistency is not proof of reproducibility:
that requires an independent replay binding the input snapshot, code,
configuration and output. Inputs to this composer are audited service facts,
never flags read from an uploaded result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Tuple

REQUIRED_CHECKS = (
    "decision_grade",
    "lineage_verified",
    "manifest_integrity",
    "replay_verified",
    "result_integrity",
    "result_metrics_integrity",
    "strategy_matches_manifest",
)


@dataclass(frozen=True)
class ResearchGovernanceReport:
    """Immutable, explainable readiness result for one persisted run."""

    run_id: str
    checks: Mapping[str, bool]
    warnings: Tuple[str, ...] = field(default_factory=tuple)
    method: str = "research_governance_v2"
    artifacts_consistent: bool = field(init=False)
    reproducible: bool = field(init=False)
    decision_ready: bool = field(init=False)
    failures: Tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str):
            raise TypeError("run_id must be a string")
        run_id = self.run_id.strip()
        if not run_id or len(run_id) > 128:
            raise ValueError("run_id must contain 1 to 128 characters")
        if self.method != "research_governance_v2":
            raise ValueError("unsupported governance method")
        if not isinstance(self.checks, Mapping):
            raise TypeError("checks must be a mapping")
        checks = dict(self.checks)
        if any(not isinstance(key, str) for key in checks):
            raise TypeError("check names must be strings")
        if any(type(value) is not bool for value in checks.values()):
            raise TypeError("governance checks must be booleans")
        checks = dict(sorted(checks.items()))
        missing = [name for name in REQUIRED_CHECKS if name not in checks]
        unknown = [name for name in checks if name not in REQUIRED_CHECKS]
        if missing or unknown:
            raise ValueError("checks must contain the complete governance contract")
        failures = tuple(key for key, value in checks.items() if not value)
        warnings = tuple(dict.fromkeys(str(item).strip() for item in self.warnings if str(item).strip()))
        # Hashes and recomputed statistics only prove local consistency. A
        # result attached to an unrelated input hash can still pass them.
        artifact_checks = (
            "manifest_integrity",
            "result_integrity",
            "result_metrics_integrity",
            "strategy_matches_manifest",
        )
        artifacts_consistent = all(checks[name] for name in artifact_checks)
        reproducible = artifacts_consistent and checks["replay_verified"]
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "checks", MappingProxyType(checks))
        object.__setattr__(self, "warnings", warnings)
        object.__setattr__(self, "failures", failures)
        object.__setattr__(self, "artifacts_consistent", artifacts_consistent)
        object.__setattr__(self, "reproducible", reproducible)
        object.__setattr__(self, "decision_ready", not failures)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "method": self.method,
            "run_id": self.run_id,
            "artifacts_consistent": self.artifacts_consistent,
            "reproducible": self.reproducible,
            "decision_ready": self.decision_ready,
            "checks": dict(self.checks),
            "failures": list(self.failures),
            "warnings": list(self.warnings),
        }


def build_governance_report(
    run_id: str,
    *,
    manifest_integrity: bool,
    result_integrity: bool,
    result_metrics_integrity: bool,
    strategy_matches_manifest: bool,
    lineage_verified: bool,
    decision_grade: bool,
    replay_verified: bool = False,
    warnings: Optional[Tuple[str, ...]] = None,
) -> ResearchGovernanceReport:
    """Compose trusted service facts, not caller-declared JSON diagnostics.

    ``replay_verified`` requires a completed independent replay against the
    exact bound inputs, code, configuration and result. Neither a content
    hash nor a payload's own ``verified`` field supplies that evidence.
    """

    checks = {
        "manifest_integrity": manifest_integrity,
        "result_integrity": result_integrity,
        "result_metrics_integrity": result_metrics_integrity,
        "strategy_matches_manifest": strategy_matches_manifest,
        "lineage_verified": lineage_verified,
        "decision_grade": decision_grade,
        "replay_verified": replay_verified,
    }
    return ResearchGovernanceReport(run_id, checks, tuple(warnings or ()))


__all__ = ["ResearchGovernanceReport", "build_governance_report"]
