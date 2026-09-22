"""Deterministic, offline, research-only portfolio backtesting."""

from .engine import BacktestEngine, BacktestValidationError
from .models import (
    BacktestConfig,
    BacktestDiagnostics,
    BacktestMetrics,
    BacktestResult,
    EquityPoint,
    Position,
    SkippedExecution,
    Trade,
    TradingConstraint,
)
from .risk import RiskReport, build_risk_report, build_risk_report_from_payload
from .benchmark import (
    BenchmarkReport,
    build_benchmark_report,
    build_benchmark_report_from_payload,
)
from .stress import (
    StressReport,
    StressScenario,
    StressScenarioResult,
    run_stress_test,
    run_stress_test_from_payload,
)
from .capacity import (
    CapacityAssumptions,
    CapacityObservation,
    CapacityReport,
    audit_capacity,
    audit_capacity_from_payload,
)
from .walk_forward import (
    ScoreFunction,
    StrategyFactory,
    WalkForwardCandidateReport,
    WalkForwardConfig,
    WalkForwardFoldReport,
    WalkForwardReport,
    WalkForwardSummary,
    WalkForwardValidationError,
    WalkForwardWindow,
    run_walk_forward,
)
from .portfolio import (
    PortfolioHolding,
    PortfolioSnapshot,
    build_portfolio_snapshot,
    portfolio_snapshot_from_payload,
    portfolio_snapshot_from_result,
)
from .constraints import (
    PortfolioConstraintCheck,
    PortfolioConstraintPolicy,
    PortfolioConstraintReport,
    evaluate_portfolio_constraints,
)
from .integrity import (
    MetricIntegrityReport,
    audit_backtest_payload_metrics,
    audit_backtest_result_metrics,
)
from .jobs import ResearchBacktestJob, ResearchBacktestJobManager
from .robustness import (
    BootstrapRobustnessReport,
    build_bootstrap_robustness_report,
    build_bootstrap_robustness_report_from_payload,
)
from .attribution import (
    AttributionReport,
    AttributionRow,
    build_attribution_report,
    build_attribution_report_from_payload,
)

__all__ = [
    "BacktestConfig",
    "BacktestDiagnostics",
    "BacktestEngine",
    "BacktestMetrics",
    "BacktestResult",
    "BacktestValidationError",
    "EquityPoint",
    "Position",
    "SkippedExecution",
    "Trade",
    "TradingConstraint",
    "RiskReport",
    "build_risk_report",
    "build_risk_report_from_payload",
    "BenchmarkReport",
    "build_benchmark_report",
    "build_benchmark_report_from_payload",
    "StressReport",
    "StressScenario",
    "StressScenarioResult",
    "run_stress_test",
    "run_stress_test_from_payload",
    "CapacityAssumptions",
    "CapacityObservation",
    "CapacityReport",
    "audit_capacity",
    "audit_capacity_from_payload",
    "ScoreFunction",
    "StrategyFactory",
    "WalkForwardCandidateReport",
    "WalkForwardConfig",
    "WalkForwardFoldReport",
    "WalkForwardReport",
    "WalkForwardSummary",
    "WalkForwardValidationError",
    "WalkForwardWindow",
    "run_walk_forward",
    "PortfolioHolding",
    "PortfolioSnapshot",
    "build_portfolio_snapshot",
    "portfolio_snapshot_from_payload",
    "portfolio_snapshot_from_result",
    "PortfolioConstraintCheck",
    "PortfolioConstraintPolicy",
    "PortfolioConstraintReport",
    "evaluate_portfolio_constraints",
    "MetricIntegrityReport",
    "audit_backtest_result_metrics",
    "audit_backtest_payload_metrics",
    "ResearchBacktestJob",
    "ResearchBacktestJobManager",
    "BootstrapRobustnessReport",
    "build_bootstrap_robustness_report",
    "build_bootstrap_robustness_report_from_payload",
    "AttributionReport",
    "AttributionRow",
    "build_attribution_report",
    "build_attribution_report_from_payload",
]
