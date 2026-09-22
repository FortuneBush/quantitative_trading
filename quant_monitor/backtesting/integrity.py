"""Fail-closed integrity audits for deterministic backtest artifacts.

``BacktestResult`` validates structural relationships (for example the number
of equity points and trades), but historically allowed a caller to construct a
result whose reported performance metrics did not match its curve.  That is a
dangerous distinction for persisted research artifacts: a result hash proves
that a payload was not changed after serialization, not that the numbers were
calculated from the curve.

This module recomputes the engine's observable metrics from an already
materialized result or JSON payload.  It never accesses a provider, database,
clock, or random source.  The report is deliberately an audit projection and
does not mutate or promote the result.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple, Union

from .models import BacktestResult
from quant_monitor.domain import SignalAction, normalize_symbol


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _has_field(value: Any, name: str) -> bool:
    return name in value if isinstance(value, Mapping) else hasattr(value, name)


def _decimal(value: Any, name: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("{} must be a valid decimal".format(name)) from exc
    if not result.is_finite():
        raise ValueError("{} must be finite".format(name))
    return result


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise TypeError("{} must be a finite number".format(name))
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError("{} must be a finite number".format(name)) from exc
    if not math.isfinite(result):
        raise ValueError("{} must be finite".format(name))
    return result


def _close(left: float, right: float, tolerance: float) -> bool:
    return math.isclose(left, right, rel_tol=tolerance, abs_tol=tolerance)


def _decimal_equal(left: Any, right: Decimal) -> bool:
    try:
        return _decimal(left, "metric") == right
    except (TypeError, ValueError):
        return False


def _decimal_float(value: Decimal, name: str) -> float:
    """Convert a decimal to a finite float without leaking OverflowError."""

    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("{} must fit in a finite float".format(name)) from exc
    if not math.isfinite(result):
        raise ValueError("{} must fit in a finite float".format(name))
    return result


@dataclass(frozen=True)
class MetricIntegrityReport:
    """Immutable, explainable audit result for one backtest artifact."""

    method: str
    verified: bool
    tolerance: float
    observation_count: int
    trade_count: int
    checks: Mapping[str, bool]
    mismatches: Tuple[str, ...]
    # A legacy artifact can pass every available curve check without carrying
    # evidence for a realized-trade distribution. Never equate that limited
    # verification with a verified trade ledger or rewrite its stored hash.
    trade_statistics_status: str = "unavailable"
    unavailable_checks: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if str(self.method).strip() != "backtest_metric_integrity_v1":
            raise ValueError("unsupported metric integrity method")
        tolerance = _number(self.tolerance, "tolerance")
        if tolerance < 0:
            raise ValueError("tolerance cannot be negative")
        object.__setattr__(self, "tolerance", tolerance)
        for name in ("observation_count", "trade_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("{} must be a non-negative integer".format(name))
        if not isinstance(self.verified, bool):
            raise TypeError("verified must be a boolean")
        checks = {}
        for key, value in dict(self.checks).items():
            if not isinstance(value, bool):
                raise TypeError("checks values must be booleans")
            checks[str(key)] = value
        object.__setattr__(self, "checks", MappingProxyType(dict(sorted(checks.items()))))
        mismatches = tuple(str(item).strip() for item in self.mismatches if str(item).strip())
        if len(set(mismatches)) != len(mismatches):
            raise ValueError("mismatches must be unique")
        if self.trade_statistics_status not in {"verified", "unavailable", "failed"}:
            raise ValueError("invalid trade_statistics_status")
        object.__setattr__(self, "mismatches", mismatches)
        # ``failed`` is an explicit audit outcome, not merely a label.  Keep a
        # manually constructed report from accidentally becoming a passing
        # report when its caller forgot to add a mismatch entry.
        object.__setattr__(
            self,
            "verified",
            self.verified
            and self.trade_statistics_status != "failed"
            and not mismatches
            and all(checks.values()),
        )
        object.__setattr__(self, "unavailable_checks", tuple(self.unavailable_checks))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "method": self.method,
            "verified": self.verified,
            "tolerance": self.tolerance,
            "observation_count": self.observation_count,
            "trade_count": self.trade_count,
            "checks": dict(self.checks),
            "mismatches": list(self.mismatches),
            "trade_statistics_status": self.trade_statistics_status,
            "unavailable_checks": list(self.unavailable_checks),
            "warnings": list(self.warnings),
        }


def audit_backtest_result_metrics(
    result: BacktestResult, *, tolerance: float = 1e-9
) -> MetricIntegrityReport:
    """Audit metrics against a materialized :class:`BacktestResult`."""

    if not isinstance(result, BacktestResult):
        raise TypeError("result must be a BacktestResult")
    return _audit_parts(
        equity=result.equity_curve,
        trades=result.trades,
        config=result.config,
        metrics=result.metrics,
        tolerance=tolerance,
    )


def audit_backtest_payload_metrics(
    payload: Mapping[str, Any], *, tolerance: float = 1e-9
) -> MetricIntegrityReport:
    """Audit a serialized ``backtest_result_dict`` payload without replaying it."""

    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    equity = payload.get("equity_curve")
    trades = payload.get("trades", ())
    config = payload.get("config")
    metrics = payload.get("metrics")
    if not isinstance(equity, (list, tuple)) or not equity:
        raise ValueError("payload equity_curve must be a non-empty array")
    if not isinstance(trades, (list, tuple)):
        raise ValueError("payload trades must be an array")
    if not isinstance(config, Mapping) or not isinstance(metrics, Mapping):
        raise ValueError("payload config and metrics must be objects")
    return _audit_parts(
        equity=equity,
        trades=trades,
        config=config,
        metrics=metrics,
        tolerance=tolerance,
    )


def _audit_parts(
    *,
    equity: Sequence[Any],
    trades: Sequence[Any],
    config: Any,
    metrics: Any,
    tolerance: float,
) -> MetricIntegrityReport:
    tolerance = _number(tolerance, "tolerance")
    if tolerance < 0:
        raise ValueError("tolerance cannot be negative")
    if not equity:
        raise ValueError("equity_curve cannot be empty")
    if not isinstance(trades, Sequence):
        raise TypeError("trades must be a sequence")
    if not isinstance(metrics, Mapping) and not hasattr(metrics, "__dict__"):
        raise TypeError("metrics must be an object")

    checks: Dict[str, bool] = {}
    mismatches = []
    unavailable = []
    warnings = []
    def check(name: str, passed: bool) -> None:
        checks[name] = bool(passed)
        if not passed:
            mismatches.append(name)

    try:
        initial_cash = _decimal(_field(config, "initial_cash"), "config.initial_cash")
        starting_nav = _decimal(_field(metrics, "starting_nav"), "metrics.starting_nav")
        ending_nav = _decimal(_field(metrics, "ending_nav"), "metrics.ending_nav")
        navs = tuple(_decimal(_field(point, "nav"), "equity.nav") for point in equity)
        daily_returns = tuple(_number(_field(point, "daily_return"), "equity.daily_return") for point in equity[1:])
    except (TypeError, ValueError):
        # A malformed artifact is an audit failure, not an excuse to infer a
        # passing result.  Keep the stable report shape for API consumers.
        check("artifact_fields_parseable", False)
        return MetricIntegrityReport(
            method="backtest_metric_integrity_v1", verified=False,
            tolerance=tolerance, observation_count=len(equity), trade_count=len(trades),
            checks=checks, mismatches=tuple(mismatches),
        )
    check("artifact_fields_parseable", True)
    # BacktestConfig requires positive initial cash.  A serialized artifact is
    # not trusted merely because it has the right JSON shape; reject values
    # that would make return/turnover math undefined instead of throwing below.
    check("positive_initial_cash", initial_cash > 0)
    check("positive_nav_curve", all(value > 0 for value in navs))
    check("starting_nav_matches_initial_cash", starting_nav == initial_cash)
    check("ending_nav_matches_equity_curve", ending_nav == navs[-1])
    session_count = _field(metrics, "session_count")
    trade_count = _field(metrics, "trade_count")
    check(
        "session_count_matches_equity_curve",
        not isinstance(session_count, bool)
        and isinstance(session_count, int)
        and session_count == len(equity),
    )
    check(
        "trade_count_matches_ledger",
        not isinstance(trade_count, bool)
        and isinstance(trade_count, int)
        and trade_count == len(trades),
    )

    # The engine's config contract treats annualization as a strictly
    # positive integer.  Do not silently coerce strings, booleans, zero, or
    # negative values in an uploaded artifact; those values otherwise lead to
    # division/domain errors or a misleading fallback to 252 sessions.
    annualization_raw = _field(config, "annualization_sessions", None)
    if annualization_raw is None:
        annualization = 252
    elif (
        isinstance(annualization_raw, bool)
        or not isinstance(annualization_raw, int)
        or annualization_raw <= 0
    ):
        check("valid_annualization_sessions", False)
        return MetricIntegrityReport(
            method="backtest_metric_integrity_v1", verified=False,
            tolerance=tolerance, observation_count=len(equity), trade_count=len(trades),
            checks=checks, mismatches=tuple(mismatches),
        )
    else:
        annualization = annualization_raw
    check("valid_annualization_sessions", True)

    if initial_cash <= 0 or any(value <= 0 for value in navs):
        # Keep a stable, explainable report rather than allowing a malformed
        # zero/negative NAV to raise ZeroDivisionError below.
        return MetricIntegrityReport(
            method="backtest_metric_integrity_v1", verified=False,
            tolerance=tolerance, observation_count=len(equity), trade_count=len(trades),
            checks=checks, mismatches=tuple(mismatches),
        )

    try:
        risk_free_rate = _decimal(
            _field(config, "annual_risk_free_rate", 0),
            "annual_risk_free_rate",
        )
    except (TypeError, ValueError, InvalidOperation):
        check("valid_annual_risk_free_rate", False)
        return MetricIntegrityReport(
            method="backtest_metric_integrity_v1", verified=False,
            tolerance=tolerance, observation_count=len(equity), trade_count=len(trades),
            checks=checks, mismatches=tuple(mismatches),
        )
    check("valid_annual_risk_free_rate", risk_free_rate >= 0)
    if risk_free_rate < 0:
        return MetricIntegrityReport(
            method="backtest_metric_integrity_v1", verified=False,
            tolerance=tolerance, observation_count=len(equity), trade_count=len(trades),
            checks=checks, mismatches=tuple(mismatches),
        )

    # Daily returns and drawdowns are both independently persisted fields.  A
    # mismatch here is a useful warning even when all aggregate metrics happen
    # to look plausible.
    try:
        curve_returns = tuple(
            _decimal_float(navs[i] / navs[i - 1] - Decimal("1"), "equity.daily_return")
            for i in range(1, len(navs))
        )
    except (TypeError, ValueError, InvalidOperation, ZeroDivisionError):
        check("derived_nav_metrics_parseable", False)
        return MetricIntegrityReport(
            method="backtest_metric_integrity_v1", verified=False,
            tolerance=tolerance, observation_count=len(equity), trade_count=len(trades),
            checks=checks, mismatches=tuple(mismatches),
        )
    check("derived_nav_metrics_parseable", True)
    check("daily_returns_match_nav_curve", len(curve_returns) == len(daily_returns) and all(_close(a, b, tolerance) for a, b in zip(curve_returns, daily_returns)))
    peak = navs[0]
    derived_drawdowns = []
    for nav in navs:
        if nav > peak:
            peak = nav
        derived_drawdowns.append(float(nav / peak - Decimal("1")) if peak else 0.0)
    stored_drawdowns = tuple(_number(_field(point, "drawdown"), "equity.drawdown") for point in equity)
    check("drawdowns_match_nav_curve", all(_close(a, b, tolerance) for a, b in zip(derived_drawdowns, stored_drawdowns)))

    periods = len(equity) - 1
    ratio = _decimal_float(navs[-1] / initial_cash, "equity.nav_ratio")
    expected_total = ratio - 1.0
    expected_annual = ratio ** (annualization / periods) - 1.0 if periods and ratio > 0 else expected_total
    risk_free = float(risk_free_rate) / annualization
    if len(daily_returns) >= 2:
        volatility = statistics.stdev(daily_returns) * math.sqrt(annualization)
        sharpe = ((statistics.fmean(daily_returns) - risk_free) / statistics.stdev(daily_returns) * math.sqrt(annualization)) if statistics.stdev(daily_returns) > 0 else 0.0
    else:
        volatility = sharpe = 0.0
    excess = [item - risk_free for item in daily_returns]
    downside = math.sqrt(statistics.fmean([min(item, 0.0) ** 2 for item in excess])) * math.sqrt(annualization) if excess else 0.0
    sortino = (statistics.fmean(excess) * annualization / downside) if downside > 0 else 0.0
    max_dd = max((-item for item in derived_drawdowns), default=0.0)
    calmar = expected_annual / max_dd if max_dd > 0 else 0.0
    positive_ratio = sum(item > 0 for item in daily_returns) / len(daily_returns) if daily_returns else 0.0
    duration = current = 0
    for item in derived_drawdowns:
        if item < 0:
            current += 1
            duration = max(duration, current)
        else:
            current = 0
    average_nav = sum(navs, Decimal("0")) / Decimal(len(navs))
    try:
        traded_notional = sum((_decimal(_field(item, "gross_notional"), "trade.gross_notional") for item in trades), Decimal("0"))
        turnover = _decimal_float(traded_notional / average_nav, "metrics.turnover") if average_nav > 0 else 0.0
        expected_decimals = {
            "total_commission_matches_ledger": sum((_decimal(_field(item, "commission"), "trade.commission") for item in trades), Decimal("0")),
            "total_stamp_duty_matches_ledger": sum((_decimal(_field(item, "stamp_duty"), "trade.stamp_duty") for item in trades), Decimal("0")),
            "total_slippage_cost_matches_ledger": sum((_decimal(_field(item, "slippage_cost"), "trade.slippage_cost") for item in trades), Decimal("0")),
        }
    except (TypeError, ValueError, InvalidOperation):
        check("trade_ledger_amounts_parseable", False)
        return MetricIntegrityReport(
            method="backtest_metric_integrity_v1", verified=False,
            tolerance=tolerance, observation_count=len(equity), trade_count=len(trades),
            checks=checks, mismatches=tuple(mismatches), trade_statistics_status="failed",
        )
    metric_decimals = {"total_commission_matches_ledger": "total_commission", "total_stamp_duty_matches_ledger": "total_stamp_duty", "total_slippage_cost_matches_ledger": "total_slippage_cost"}
    for name, expected in expected_decimals.items():
        check(name, _decimal_equal(_field(metrics, metric_decimals[name]), expected))
    expected_floats = {
        "total_return_matches_nav_curve": ("total_return", expected_total),
        "annualized_return_matches_nav_curve": ("annualized_return", expected_annual),
        "annualized_volatility_matches_returns": ("annualized_volatility", volatility),
        "sharpe_ratio_matches_returns": ("sharpe_ratio", sharpe),
        "maximum_drawdown_matches_nav_curve": ("maximum_drawdown", max_dd),
        "turnover_matches_ledger": ("turnover", turnover),
        "sortino_ratio_matches_returns": ("sortino_ratio", sortino),
        "calmar_ratio_matches_returns": ("calmar_ratio", calmar),
        "positive_session_ratio_matches_returns": ("positive_session_ratio", positive_ratio),
    }
    for name, (field_name, expected) in expected_floats.items():
        if field_name in {"sortino_ratio", "calmar_ratio", "positive_session_ratio"} and not _has_field(metrics, field_name):
            unavailable.append(name)
            continue
        try:
            passed = _close(_number(_field(metrics, field_name), "metrics." + field_name), expected, tolerance)
        except (TypeError, ValueError):
            passed = False
        check(name, passed)
    trade_status = _audit_trade_statistics(
        trades, metrics, tolerance, check, unavailable, warnings
    )
    if _has_field(metrics, "maximum_drawdown_duration_sessions"):
        stored_duration = _field(metrics, "maximum_drawdown_duration_sessions")
        check(
            "maximum_drawdown_duration_matches_nav_curve",
            not isinstance(stored_duration, bool)
            and isinstance(stored_duration, int)
            and stored_duration == duration,
        )
    else:
        unavailable.append("maximum_drawdown_duration_matches_nav_curve")
    return MetricIntegrityReport(
        method="backtest_metric_integrity_v1", verified=not mismatches,
        tolerance=tolerance, observation_count=len(equity), trade_count=len(trades),
        checks=checks, mismatches=tuple(mismatches),
        trade_statistics_status=trade_status,
        unavailable_checks=tuple(unavailable), warnings=tuple(warnings),
    )


def _audit_trade_statistics(trades, metrics, tolerance, check, unavailable, warnings):
    """Audit present legacy fields or the complete explicitly versioned group.

    The engine does not inject corporate-action cost adjustments. Reconstruct
    FIFO acquisition costs from its ledger, including commissions, rather
    than trusting P&L copied between the sell and the aggregate metrics.
    """

    fields = (
        "closed_trade_count", "winning_trade_count", "losing_trade_count",
        "break_even_trade_count", "win_rate", "gross_profit", "gross_loss",
        "realized_pnl", "profit_factor", "expectancy_per_closed_trade",
    )
    declared = _field(metrics, "trade_statistics_status")
    strict = _has_field(metrics, "trade_statistics_status") and declared != "unavailable"
    has_statistics = strict or (any(_has_field(metrics, name) for name in fields) and declared != "unavailable")
    failures = []

    def record(name, passed):
        check(name, passed)
        if not passed:
            failures.append(name)

    # Even an old payload may not smuggle realized amounts into buy records.
    actions = []
    try:
        for item in trades:
            action = SignalAction(_field(item, "action"))
            if action not in (SignalAction.BUY, SignalAction.SELL):
                raise ValueError("invalid trade action")
            actions.append(action)
        record("trade_actions_valid", True)
    except (TypeError, ValueError):
        record("trade_actions_valid", False)
        return "failed"
    record("buy_realized_pnl_absent", all(
        _field(item, "realized_pnl") is None
        for item, action in zip(trades, actions) if action == SignalAction.BUY
    ))
    if declared == "unavailable":
        empty_defaults = all(
            _field(metrics, name) in (None, 0, "0")
            for name in fields if name not in {"win_rate", "profit_factor", "expectancy_per_closed_trade"}
        ) and all(_field(metrics, name) is None for name in ("win_rate", "profit_factor", "expectancy_per_closed_trade"))
        record("unavailable_trade_statistics_have_no_computed_values", empty_defaults)
    sells = [item for item, action in zip(trades, actions) if action == SignalAction.SELL]
    if not has_statistics:
        unavailable.append("trade_distribution_matches_ledger")
        warnings.append("legacy_trade_statistics_unavailable")
        return "failed" if failures else "unavailable"

    complete = all(_field(item, "realized_pnl") is not None for item in sells)
    record("sell_realized_pnl_complete", complete)
    if not complete:
        return "failed"
    start_unavailable = len(unavailable)
    try:
        realized = [_decimal(_field(item, "realized_pnl"), "trade.realized_pnl") for item in sells]
        # Reconstruct closing costs from filled quantities. A sell without
        # matching purchases or with an invented P&L cannot be verified.
        holdings = {}
        cost_basis_matches = True
        for item, action in zip(trades, actions):
            symbol = normalize_symbol(_field(item, "symbol"))
            quantity = _field(item, "quantity")
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
                raise ValueError("invalid quantity")
            gross = _decimal(_field(item, "gross_notional"), "trade.gross_notional")
            commission = _decimal(_field(item, "commission"), "trade.commission")
            stamp = _decimal(_field(item, "stamp_duty"), "trade.stamp_duty")
            if gross <= 0 or commission < 0 or stamp < 0:
                raise ValueError("invalid trade amount")
            lots = holdings.setdefault(symbol, [])
            if action == SignalAction.BUY:
                if stamp != 0:
                    raise ValueError("buy stamp duty must be zero")
                lots.append([quantity, gross + commission])
                continue
            remaining = quantity
            removed_cost = Decimal("0")
            while remaining and lots:
                size, cost = lots[0]
                removed = min(remaining, size)
                per_share = cost / Decimal(size)
                removed_cost += per_share * Decimal(removed)
                remaining -= removed
                if removed == size:
                    lots.pop(0)
                else:
                    lots[0] = [size - removed, per_share * Decimal(size - removed)]
            if remaining:
                raise ValueError("sell has no matching acquisition cost")
            expected = (gross - commission - stamp - removed_cost).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if expected != _decimal(_field(item, "realized_pnl"), "trade.realized_pnl"):
                cost_basis_matches = False
        record("realized_pnl_matches_acquisition_cost", cost_basis_matches)
        closed = len(sells)
        gross_profit = sum((value for value in realized if value > 0), Decimal("0"))
        gross_loss = sum((-value for value in realized if value < 0), Decimal("0"))
        net = sum(realized, Decimal("0"))
        expected = {
            "closed_trade_count": closed,
            "winning_trade_count": sum(value > 0 for value in realized),
            "losing_trade_count": sum(value < 0 for value in realized),
            "break_even_trade_count": sum(value == 0 for value in realized),
            "win_rate": sum(value > 0 for value in realized) / closed if closed else None,
            "gross_profit": gross_profit, "gross_loss": gross_loss, "realized_pnl": net,
            "profit_factor": float(gross_profit / gross_loss) if gross_loss else None,
            "expectancy_per_closed_trade": net / Decimal(closed) if closed else None,
        }
        for field_name, value in expected.items():
            name = ("expectancy" if field_name == "expectancy_per_closed_trade" else field_name) + "_matches_ledger"
            if not _has_field(metrics, field_name):
                if strict:
                    record(name, False)
                else:
                    unavailable.append(name)
                continue
            stored = _field(metrics, field_name)
            if value is None:
                if stored is None:
                    record(name, True)
                elif not strict and _decimal_equal(stored, Decimal("0")):
                    # Historical zero-denominator convention is preserved in
                    # the immutable artifact but never validated as a ratio.
                    unavailable.append(name)
                    if "legacy_undefined_zero_convention" not in warnings:
                        warnings.append("legacy_undefined_zero_convention")
                else:
                    record(name, False)
            elif field_name.endswith("_count"):
                record(name, not isinstance(stored, bool) and isinstance(stored, int) and stored == value)
            elif isinstance(value, Decimal):
                if field_name == "expectancy_per_closed_trade" and not strict:
                    # This one monetary average was previously persisted as
                    # float. Keep finite legacy rounding auditable without
                    # loosening Decimal validation for new results.
                    record(name, _close(_number(stored, name), float(value), tolerance))
                else:
                    record(name, _decimal_equal(stored, value))
            else:
                record(name, _close(_number(stored, name), value, tolerance))
        if strict:
            record("trade_statistics_status_matches_ledger", declared == ("available" if closed else "no_closed_trades"))
            record("profit_factor_status_matches_ledger", _field(metrics, "profit_factor_status") == (
                "available" if gross_loss else "no_losses" if closed else "no_closed_trades"
            ))
    except (TypeError, ValueError, InvalidOperation):
        record("trade_distribution_fields_parseable", False)
    return "failed" if failures else "unavailable" if len(unavailable) > start_unavailable else "verified"


__all__ = ["MetricIntegrityReport", "audit_backtest_result_metrics", "audit_backtest_payload_metrics"]
