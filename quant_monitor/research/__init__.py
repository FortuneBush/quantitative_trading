"""Deterministic, side-effect-free primitives for research run provenance."""

from .manifest import (
    CANONICALIZATION_VERSION,
    MANIFEST_SCHEMA_VERSION,
    CanonicalizationError,
    DatasetDescriptor,
    ManifestValidationError,
    ResearchRunManifest,
    ResearchRunRecord,
    ResearchResultRecord,
    SensitiveManifestValueError,
    canonical_json,
    hash_canonical,
    hash_input_records,
    verify_input_hash,
)
from .universe import (
    PointInTimeUniverse,
    UniverseMembership,
    UniverseSnapshot,
    UniverseSnapshotError,
)
from .lineage_gate import LineageGateReport, audit_observation_lineage
from .governance import ResearchGovernanceReport, build_governance_report
from .readiness import DecisionReadiness, build_decision_readiness
from .fundamental_factors import (
    BANK_METRIC_SPECS,
    BankFundamentalFactor,
    BankFundamentalFactorReport,
    BankMetricSpec,
    build_bank_fundamental_factors,
)
from .replay import ReplayAuditReport, audit_replay, verify_replay
from .code_identity import (
    CODE_IDENTITY_SCHEMA_VERSION,
    strategy_code_identity,
    strategy_code_version,
)

__all__ = [
    "CANONICALIZATION_VERSION",
    "MANIFEST_SCHEMA_VERSION",
    "CanonicalizationError",
    "DatasetDescriptor",
    "ManifestValidationError",
    "ResearchRunManifest",
    "ResearchRunRecord",
    "ResearchResultRecord",
    "SensitiveManifestValueError",
    "canonical_json",
    "hash_canonical",
    "hash_input_records",
    "verify_input_hash",
    "PointInTimeUniverse",
    "UniverseMembership",
    "UniverseSnapshot",
    "UniverseSnapshotError",
    "LineageGateReport",
    "audit_observation_lineage",
    "ResearchGovernanceReport",
    "build_governance_report",
    "DecisionReadiness",
    "build_decision_readiness",
    "BANK_METRIC_SPECS",
    "BankFundamentalFactor",
    "BankFundamentalFactorReport",
    "BankMetricSpec",
    "build_bank_fundamental_factors",
    "ReplayAuditReport",
    "verify_replay",
    "audit_replay",
    "CODE_IDENTITY_SCHEMA_VERSION",
    "strategy_code_identity",
    "strategy_code_version",
]
