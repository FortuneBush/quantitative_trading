"""Deterministic risk analytics for research-only backtest results.

The functions in this module deliberately operate on an already materialized
``BacktestResult``.  They never fetch prices, infer missing sessions, or
silently turn an unavailable statistic into zero.  This makes the report safe
to expose through a REST endpoint while keeping provider and storage concerns
outside the backtesting package.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .models import BacktestResult, EquityPoint


def _finite(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _decimal_float(value: Any, *, nonnegative: bool = False) -> Optional[float]:
    """Parse an optional finite decimal without leaking payload exceptions."""

    if value is None:
        return None
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            return None
        if nonnegative and number < 0:
            return None
        parsed = float(number)
        if number != 0 and parsed == 0.0:
            return None
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError, ArithmeticError, OverflowError):
        return None


def _historical_tail(
    returns: Sequence[float], confidence: float
) -> Tuple[Optional[float], Optional[float]]:
    """Return historical loss VaR and CVaR as positive fractions.

    The lower-tail order statistic uses the empirical *nearest-rank*
    convention: ``ceil((1-confidence) * n)`` (one-based), clamped to the
    first observation.  This matters for short samples: using ``floor`` would
    select the next observation for most confidence/sample-size combinations
    and can report zero loss even when the worst observation is materially
    negative.  CVaR averages all observations at or below that threshold,
    including zero-loss observations when the threshold is non-negative.
    ``None`` is returned for an empty sample; the caller enforces its minimum
    sample policy.
    """

    if not returns:
        return None, None
    ordered = sorted(float(item) for item in returns)
    # Nearest-rank empirical quantile.  ``ceil`` is deliberately applied
    # before converting to a zero-based index (rank 1 -> index 0).
    tail_mass = (1.0 - confidence) * len(ordered)
    # Decimal-looking confidence levels such as 0.95 frequently produce a
    # value infinitesimally above an integer (for example 1.0000000000000009)
    # in binary floating point.  Treat only that representation noise as the
    # intended integer boundary; genuine fractional ranks still round up.
    nearest_integer = round(tail_mass)
    if math.isclose(tail_mass, nearest_integer, rel_tol=0.0, abs_tol=1e-12):
        tail_mass = float(nearest_integer)
    rank = max(1, int(math.ceil(tail_mass)))
    index = min(len(ordered) - 1, rank - 1)
    threshold = ordered[index]
    losses = [max(0.0, -item) for item in ordered if item <= threshold]
    var = max(0.0, -threshold)
    cvar = sum(losses) / len(losses) if losses else 0.0
    return _finite(var), _finite(cvar)


def _field(value: Any, name: str, default: Any = None) -> Any:
    """Read a field from either a domain object or a JSON mapping."""

    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


@dataclass(frozen=True)
class RiskReport:
    """Immutable risk report with explicit sample sufficiency metadata."""

    method: str
    confidence_levels: Tuple[float, ...]
    return_observations: int
    minimum_observations: int
    sample_sufficient: bool
    historical_var: Mapping[str, Optional[float]]
    historical_cvar: Mapping[str, Optional[float]]
    worst_session_return: Optional[float]
    best_session_return: Optional[float]
    downside_session_ratio: Optional[float]
    current_drawdown: Optional[float]
    maximum_drawdown: Optional[float]
    maximum_drawdown_duration_sessions: Optional[int]
    ending_nav: Optional[str]
    cash_weight: Optional[float]
    gross_exposure: Optional[float]
    top_position_weight: Optional[float]
    position_count: Optional[int]
    concentration_warning: Optional[bool]
    warnings: Tuple[str, ...]
    # Number of equity points after the initial point whose daily return was
    # absent, non-numeric, or non-finite.  Persisted research payloads can be
    # older than the current result schema; exposing this count keeps the
    # sample gate auditable instead of silently treating malformed returns as
    # zero.
    missing_return_observations: int = 0
    missing_position_observations: int = 0
    position_data_complete: bool = True

    def to_dict(self) -> Dict[str, Any]:
        """Return a stable JSON-compatible projection."""

        return {
            "method": self.method,
            "confidence_levels": list(self.confidence_levels),
            "return_observations": self.return_observations,
            "minimum_observations": self.minimum_observations,
            "sample_sufficient": self.sample_sufficient,
            "historical_var": dict(self.historical_var),
            "historical_cvar": dict(self.historical_cvar),
            "worst_session_return": self.worst_session_return,
            "best_session_return": self.best_session_return,
            "downside_session_ratio": self.downside_session_ratio,
            "current_drawdown": self.current_drawdown,
            "maximum_drawdown": self.maximum_drawdown,
            "maximum_drawdown_duration_sessions": self.maximum_drawdown_duration_sessions,
            "ending_nav": self.ending_nav,
            "cash_weight": self.cash_weight,
            "gross_exposure": self.gross_exposure,
            "top_position_weight": self.top_position_weight,
            "position_count": self.position_count,
            "concentration_warning": self.concentration_warning,
            "warnings": list(self.warnings),
            "missing_return_observations": self.missing_return_observations,
            "missing_position_observations": self.missing_position_observations,
            "position_data_complete": self.position_data_complete,
        }


def build_risk_report(
    result: BacktestResult,
    *,
    confidence_levels: Sequence[float] = (0.95, 0.99),
    minimum_observations: int = 20,
) -> RiskReport:
    """Build a historical tail-risk and ending-exposure report.

    ``minimum_observations`` is a reporting gate, not an interpolation rule:
    when it is not met or any return is missing/invalid, tail metrics are
    ``None``.  Descriptive return statistics still describe the valid subset.
    Exposure requires a complete set of finite, non-negative position values;
    partial holdings are never presented as an under-estimated exposure.
    """

    if not isinstance(result, BacktestResult):
        raise TypeError("result must be a BacktestResult")
    return _build_risk_report(
        equity=result.equity_curve,
        metrics=result.metrics,
        diagnostics=result.diagnostics,
        positions=result.positions,
        confidence_levels=confidence_levels,
        minimum_observations=minimum_observations,
    )


def build_risk_report_from_payload(
    payload: Mapping[str, Any],
    *,
    confidence_levels: Sequence[float] = (0.95, 0.99),
    minimum_observations: int = 20,
) -> RiskReport:
    """Build a report from a persisted ``backtest_result_dict`` payload.

    This adapter is intentionally read-only and provider-independent.  It
    allows an API process to recompute a report from an immutable JSON artifact
    without pretending that the original strategy or market data is available.
    """

    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    equity = payload.get("equity_curve")
    metrics = payload.get("metrics")
    diagnostics = payload.get("diagnostics", {})
    positions = payload.get("positions")
    if not isinstance(equity, (list, tuple)) or not equity:
        raise ValueError("payload equity_curve must be a non-empty array")
    if not isinstance(metrics, Mapping):
        raise ValueError("payload metrics must be an object")
    if not isinstance(diagnostics, Mapping):
        raise ValueError("payload diagnostics must be an object")
    if positions is not None and not isinstance(positions, Mapping):
        raise ValueError("payload positions must be an object")
    return _build_risk_report(
        equity=equity,
        metrics=metrics,
        diagnostics=diagnostics,
        positions=positions,
        confidence_levels=confidence_levels,
        minimum_observations=minimum_observations,
    )


def _build_risk_report(
    *,
    equity: Sequence[Any],
    metrics: Any,
    diagnostics: Any,
    positions: Optional[Mapping[str, Any]],
    confidence_levels: Sequence[float],
    minimum_observations: int,
) -> RiskReport:
    """Shared implementation for domain objects and persisted mappings."""

    if isinstance(minimum_observations, bool) or not isinstance(minimum_observations, int):
        raise TypeError("minimum_observations must be an integer")
    if minimum_observations < 1:
        raise ValueError("minimum_observations must be positive")
    levels = tuple(float(item) for item in confidence_levels)
    if not levels or any(not math.isfinite(item) or not 0.0 < item < 1.0 for item in levels):
        raise ValueError("confidence_levels must contain values between zero and one")
    if len(set(levels)) != len(levels):
        raise ValueError("confidence_levels must be unique")
    levels = tuple(sorted(levels))

    equity = tuple(equity)
    # A malformed persisted payload must fail closed.  Do not coerce missing
    # returns to zero (which would understate tail risk) and do not let one bad
    # point crash the entire risk endpoint.  Only finite numeric observations
    # enter the historical sample; the count is returned for auditability.
    parsed_returns = []
    missing_return_observations = 0
    for point in equity[1:]:
        raw_return = _field(point, "daily_return")
        try:
            if isinstance(raw_return, bool):
                raise ValueError("boolean is not a return")
            parsed = float(raw_return)
            if not math.isfinite(parsed):
                raise ValueError("return must be finite")
        except (TypeError, ValueError, OverflowError):
            missing_return_observations += 1
            continue
        parsed_returns.append(parsed)
    returns = tuple(parsed_returns)
    sufficient = (
        len(returns) >= minimum_observations and missing_return_observations == 0
    )
    warnings = []
    if missing_return_observations:
        warnings.append("missing_or_invalid_return_observations")
    if len(returns) < minimum_observations:
        warnings.append("insufficient_return_observations_for_historical_tail_metrics")
    diagnostic_warnings = _field(diagnostics, "warnings", ()) or ()
    if diagnostic_warnings:
        warnings.extend(str(item) for item in diagnostic_warnings)

    var: Dict[str, Optional[float]] = {}
    cvar: Dict[str, Optional[float]] = {}
    for confidence in levels:
        key = "{:.2f}".format(confidence)
        if sufficient:
            value, tail = _historical_tail(returns, confidence)
        else:
            value, tail = None, None
        var[key] = value
        cvar[key] = tail

    worst = min(returns) if returns else None
    best = max(returns) if returns else None
    downside_ratio = (
        sum(item < 0.0 for item in returns) / len(returns) if returns else None
    )
    ending_nav_raw = _field(equity[-1], "nav") if equity else None
    try:
        ending_nav = Decimal(str(ending_nav_raw)) if ending_nav_raw is not None else None
        if ending_nav is not None and not ending_nav.is_finite():
            ending_nav = None
    except (TypeError, ValueError, ArithmeticError, OverflowError):
        ending_nav = None
    nav_float = (
        _decimal_float(ending_nav)
        if ending_nav is not None and ending_nav > 0
        else None
    )
    if nav_float == 0.0:
        nav_float = None
    position_data_complete = positions is not None
    position_count = len(positions) if positions is not None else None
    position_items = tuple(positions.values()) if positions is not None else ()
    missing_position_observations = 0
    market_values = []
    for item in position_items:
        value = _decimal_float(_field(item, "market_value"), nonnegative=True)
        if value is not None:
            market_values.append(value)
        else:
            missing_position_observations += 1
    if positions is None:
        warnings.append("missing_position_data")
    if missing_position_observations:
        position_data_complete = False
        warnings.append("missing_or_invalid_position_market_values")
    total_market_value = sum(market_values)
    if not math.isfinite(total_market_value):
        position_data_complete = False
        warnings.append("invalid_total_position_market_value")
    # A persisted result is an untrusted research artifact.  Domain
    # ``EquityPoint`` objects already reject negative cash, but the payload
    # adapter intentionally accepts legacy JSON mappings and therefore must
    # not let an impossible cash balance leak into a seemingly valid
    # exposure ratio.  Treat negative, non-finite, or cash-greater-than-NAV
    # values as unavailable rather than emitting a negative/>100% weight.
    cash_value = (
        _decimal_float(_field(equity[-1], "cash"), nonnegative=True)
        if equity
        else None
    )
    cash_weight = None
    if nav_float is not None and cash_value is not None:
        if cash_value <= nav_float:
            cash_weight = cash_value / nav_float
        else:
            warnings.append("ending_cash_exceeds_ending_nav")
    if nav_float is None:
        warnings.append("missing_or_invalid_ending_nav")
    elif cash_value is None:
        warnings.append("missing_or_invalid_ending_cash")
    gross_exposure = (
        _finite(total_market_value / nav_float)
        if nav_float is not None and position_data_complete
        else None
    )
    top_position_weight = (
        _finite(max(market_values) / nav_float)
        if nav_float is not None and position_data_complete and market_values
        else None
    )
    concentration_warning = None
    if top_position_weight is not None:
        concentration_warning = top_position_weight > 0.25
    elif position_data_complete and position_count == 0 and nav_float is not None:
        concentration_warning = False
    if concentration_warning:
        warnings.append("top_position_exceeds_25_percent_of_ending_nav")

    # Preserve warning order while avoiding duplicate lineage messages.
    unique_warnings = tuple(dict.fromkeys(item for item in warnings if item))
    return RiskReport(
        method="historical_loss_tail_v1",
        confidence_levels=levels,
        return_observations=len(returns),
        minimum_observations=minimum_observations,
        sample_sufficient=sufficient,
        historical_var=var,
        historical_cvar=cvar,
        worst_session_return=_finite(worst),
        best_session_return=_finite(best),
        downside_session_ratio=_finite(downside_ratio),
        current_drawdown=_finite(_field(equity[-1], "drawdown") if equity else None),
        maximum_drawdown=_finite(_field(metrics, "maximum_drawdown")),
        maximum_drawdown_duration_sessions=_field(
            metrics, "maximum_drawdown_duration_sessions"
        ),
        ending_nav=str(ending_nav) if ending_nav is not None else None,
        cash_weight=_finite(cash_weight),
        gross_exposure=_finite(gross_exposure),
        top_position_weight=_finite(top_position_weight),
        position_count=position_count,
        concentration_warning=concentration_warning,
        warnings=unique_warnings,
        missing_return_observations=missing_return_observations,
        missing_position_observations=missing_position_observations,
        position_data_complete=position_data_complete,
    )


__all__ = ["RiskReport", "build_risk_report", "build_risk_report_from_payload"]
