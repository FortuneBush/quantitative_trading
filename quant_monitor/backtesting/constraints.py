"""Deterministic portfolio-policy checks for research-only snapshots.

The backtest engine already models point-in-time execution constraints.  This
module covers the *portfolio-level* policy that follows those fills: position
concentration, cash and gross exposure limits, and a bounded number of
holdings.  It intentionally consumes only a :class:`PortfolioSnapshot`; it
does not fetch data, place orders, or mutate a result.

Checks are useful even when a snapshot is not decision-ready.  In that case
the report is ``unavailable`` rather than a misleading pass, while individual
arithmetic violations remain visible as ``fail`` checks.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional, Tuple

from .portfolio import PortfolioSnapshot


def _optional_fraction(value: Any, name: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("{} must be a finite number or None".format(name))
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TypeError("{} must be a finite number or None".format(name)) from exc
    if not math.isfinite(parsed) or parsed < 0.0 or parsed > 1.0:
        raise ValueError("{} must be between zero and one".format(name))
    return parsed


def _optional_nonnegative(value: Any, name: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("{} must be a finite number or None".format(name))
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TypeError("{} must be a finite number or None".format(name)) from exc
    if not math.isfinite(parsed) or parsed < 0.0:
        raise ValueError("{} must be a finite non-negative number".format(name))
    return parsed


@dataclass(frozen=True)
class PortfolioConstraintPolicy:
    """Versioned, explicit limits for a long-only research portfolio."""

    name: str = "long_only_conservative_v1"
    max_holding_weight: Optional[float] = 0.25
    max_concentration_hhi: Optional[float] = 0.20
    min_cash_weight: Optional[float] = 0.0
    max_gross_exposure: Optional[float] = 1.0
    max_positions: Optional[int] = 50
    require_complete_valuation: bool = True

    def __post_init__(self) -> None:
        name = str(self.name).strip()
        if not name:
            raise ValueError("policy name cannot be empty")
        object.__setattr__(self, "name", name)
        for field_name in (
            "max_holding_weight",
            "max_concentration_hhi",
            "min_cash_weight",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_fraction(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "max_gross_exposure",
            _optional_nonnegative(self.max_gross_exposure, "max_gross_exposure"),
        )
        positions = self.max_positions
        if positions is not None:
            if isinstance(positions, bool) or not isinstance(positions, int):
                raise TypeError("max_positions must be an integer or None")
            if positions < 1:
                raise ValueError("max_positions must be positive")
        if not isinstance(self.require_complete_valuation, bool):
            raise TypeError("require_complete_valuation must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "max_holding_weight": self.max_holding_weight,
            "max_concentration_hhi": self.max_concentration_hhi,
            "min_cash_weight": self.min_cash_weight,
            "max_gross_exposure": self.max_gross_exposure,
            "max_positions": self.max_positions,
            "require_complete_valuation": self.require_complete_valuation,
        }


@dataclass(frozen=True)
class PortfolioConstraintCheck:
    """One auditable comparison between an observed portfolio metric and a limit."""

    name: str
    actual: float
    limit: float
    operator: str
    passed: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "actual": self.actual,
            "limit": self.limit,
            "operator": self.operator,
            "passed": self.passed,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PortfolioConstraintReport:
    """Immutable result of evaluating a portfolio policy."""

    method: str
    status: str
    decision_ready: bool
    policy: PortfolioConstraintPolicy
    checks: Tuple[PortfolioConstraintCheck, ...]
    warnings: Tuple[str, ...]

    def __post_init__(self) -> None:
        if self.status not in {"pass", "fail", "unavailable"}:
            raise ValueError("unsupported portfolio constraint status")
        if self.decision_ready != (self.status == "pass"):
            raise ValueError("decision_ready must match pass status")
        checks = tuple(self.checks)
        if any(not isinstance(item, PortfolioConstraintCheck) for item in checks):
            raise TypeError("checks must contain PortfolioConstraintCheck objects")
        if len({item.name for item in checks}) != len(checks):
            raise ValueError("constraint check names must be unique")
        warnings = tuple(str(item).strip() for item in self.warnings)
        if any(not item for item in warnings) or len(set(warnings)) != len(warnings):
            raise ValueError("warnings must be unique and non-empty")
        object.__setattr__(self, "checks", checks)
        object.__setattr__(self, "warnings", warnings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "status": self.status,
            "decision_ready": self.decision_ready,
            "policy": self.policy.to_dict(),
            "checks": [item.to_dict() for item in self.checks],
            "warnings": list(self.warnings),
        }


def evaluate_portfolio_constraints(
    snapshot: PortfolioSnapshot,
    *,
    policy: Optional[PortfolioConstraintPolicy] = None,
) -> PortfolioConstraintReport:
    """Evaluate portfolio-level limits without provider or persistence access.

    A partial/stale valuation can still expose arithmetic checks, but cannot
    receive ``decision_ready``.  This prevents a mathematically valid snapshot
    with unverified marks from being presented as a portfolio approval.
    """

    if not isinstance(snapshot, PortfolioSnapshot):
        raise TypeError("snapshot must be a PortfolioSnapshot")
    selected = policy if policy is not None else PortfolioConstraintPolicy()
    if not isinstance(selected, PortfolioConstraintPolicy):
        raise TypeError("policy must be a PortfolioConstraintPolicy")
    gross_exposure = float(snapshot.invested_market_value / snapshot.nav)
    checks = []

    def add_fraction(name: str, actual: float, limit: Optional[float], operator: str, message: str) -> None:
        if limit is None:
            return
        passed = actual <= limit if operator == "<=" else actual >= limit
        checks.append(
            PortfolioConstraintCheck(
                name=name,
                actual=actual,
                limit=limit,
                operator=operator,
                passed=passed,
                reason=message if passed else "{}: actual {:.6g} violates {} {:.6g}".format(name, actual, operator, limit),
            )
        )

    add_fraction(
        "max_holding_weight",
        snapshot.max_holding_weight,
        selected.max_holding_weight,
        "<=",
        "largest position is within the configured limit",
    )
    add_fraction(
        "max_concentration_hhi",
        snapshot.concentration_hhi,
        selected.max_concentration_hhi,
        "<=",
        "portfolio concentration is within the configured HHI limit",
    )
    add_fraction(
        "min_cash_weight",
        snapshot.cash_weight,
        selected.min_cash_weight,
        ">=",
        "cash weight meets the configured minimum",
    )
    add_fraction(
        "max_gross_exposure",
        gross_exposure,
        selected.max_gross_exposure,
        "<=",
        "gross exposure is within the configured limit",
    )
    if selected.max_positions is not None:
        position_count = len(snapshot.holdings)
        limit = selected.max_positions
        passed = position_count <= limit
        checks.append(
            PortfolioConstraintCheck(
                name="max_positions",
                actual=float(position_count),
                limit=float(limit),
                operator="<=",
                passed=passed,
                reason=(
                    "number of positions is within the configured limit"
                    if passed
                    else "max_positions: actual {} violates <= {}".format(position_count, limit)
                ),
            )
        )

    warnings = []
    if selected.require_complete_valuation and snapshot.valuation_quality != "complete":
        warnings.append("valuation_quality_not_complete")
    if not checks:
        warnings.append("no_portfolio_constraints_configured")
    failed = any(not item.passed for item in checks)
    status = "fail" if failed else ("unavailable" if warnings else "pass")
    return PortfolioConstraintReport(
        method="portfolio_constraints_v1",
        status=status,
        decision_ready=status == "pass",
        policy=selected,
        checks=tuple(checks),
        warnings=tuple(dict.fromkeys(warnings)),
    )


__all__ = [
    "PortfolioConstraintCheck",
    "PortfolioConstraintPolicy",
    "PortfolioConstraintReport",
    "evaluate_portfolio_constraints",
]
