"""Deterministic portfolio stress testing for research backtests.

The stress tester deliberately operates on an already materialized
``BacktestResult``.  It does not call a provider, rerun a strategy, or mutate
the result.  A scenario applies a point-in-time price shock to marked
holdings and optionally scales transaction costs that have been incurred up
to each equity observation.  This keeps the result reproducible while making
the assumptions explicit instead of presenting a stressed number as a
forecast.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

from quant_monitor.domain import ensure_market_time

from .models import BacktestResult


def _decimal(value: Any, name: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("{} must be a valid decimal".format(name)) from exc
    if not result.is_finite():
        raise ValueError("{} must be finite".format(name))
    return result


@dataclass(frozen=True)
class StressScenario:
    """One deterministic stress assumption.

    ``price_shock`` is a proportional mark-to-market shock applied to every
    holding at every observed equity point (``-0.10`` means -10%).  The
    resulting holding value is never allowed below zero by the scenario
    contract.  ``cost_multiplier`` scales commissions, stamp duty and
    slippage already incurred by the replay; ``1`` is the baseline and ``2``
    doubles those costs.  These are scenario assumptions, not trading advice.
    """

    name: str
    price_shock: Decimal = Decimal("0")
    cost_multiplier: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        name = str(self.name).strip()
        if not name:
            raise ValueError("stress scenario name cannot be empty")
        object.__setattr__(self, "name", name)
        shock = _decimal(self.price_shock, "price_shock")
        if shock < Decimal("-1"):
            raise ValueError("price_shock cannot be below -1")
        multiplier = _decimal(self.cost_multiplier, "cost_multiplier")
        if multiplier < 0:
            raise ValueError("cost_multiplier cannot be negative")
        object.__setattr__(self, "price_shock", shock)
        object.__setattr__(self, "cost_multiplier", multiplier)


@dataclass(frozen=True)
class StressScenarioResult:
    """Immutable outcome for one stress scenario."""

    scenario: StressScenario
    baseline_ending_nav: Decimal
    stressed_ending_nav: Decimal
    ending_nav_delta: Decimal
    stressed_total_return: float
    stressed_maximum_drawdown: float
    stressed_minimum_nav: Decimal
    observations: int

    def __post_init__(self) -> None:
        for name in (
            "baseline_ending_nav",
            "stressed_ending_nav",
            "ending_nav_delta",
            "stressed_minimum_nav",
        ):
            value = _decimal(getattr(self, name), name)
            object.__setattr__(self, name, value)
        for name in ("stressed_total_return", "stressed_maximum_drawdown"):
            value = float(getattr(self, name))
            if not math.isfinite(value):
                raise ValueError("{} must be finite".format(name))
            if name == "stressed_maximum_drawdown" and value < 0:
                raise ValueError("stressed_maximum_drawdown cannot be negative")
            object.__setattr__(self, name, value)
        if not isinstance(self.observations, int) or isinstance(self.observations, bool):
            raise TypeError("observations must be an integer")
        if self.observations <= 0:
            raise ValueError("observations must be positive")

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "scenario": {
                "name": self.scenario.name,
                "price_shock": str(self.scenario.price_shock),
                "cost_multiplier": str(self.scenario.cost_multiplier),
            },
            "baseline_ending_nav": str(self.baseline_ending_nav),
            "stressed_ending_nav": str(self.stressed_ending_nav),
            "ending_nav_delta": str(self.ending_nav_delta),
            "stressed_total_return": self.stressed_total_return,
            "stressed_maximum_drawdown": self.stressed_maximum_drawdown,
            "stressed_minimum_nav": str(self.stressed_minimum_nav),
            "observations": self.observations,
        }


@dataclass(frozen=True)
class StressReport:
    """A collection of scenario outcomes and the worst ending-NAV case."""

    baseline_ending_nav: Decimal
    baseline_total_return: float
    scenarios: Tuple[StressScenarioResult, ...]
    worst_scenario: Optional[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "baseline_ending_nav", _decimal(self.baseline_ending_nav, "baseline_ending_nav"))
        value = float(self.baseline_total_return)
        if not math.isfinite(value):
            raise ValueError("baseline_total_return must be finite")
        object.__setattr__(self, "baseline_total_return", value)
        scenarios = tuple(self.scenarios)
        if any(not isinstance(item, StressScenarioResult) for item in scenarios):
            raise TypeError("scenarios must contain StressScenarioResult instances")
        names = [item.scenario.name for item in scenarios]
        if len(set(names)) != len(names):
            raise ValueError("stress scenario names must be unique")
        if self.worst_scenario is not None and self.worst_scenario not in names:
            raise ValueError("worst_scenario must reference a scenario")
        object.__setattr__(self, "scenarios", scenarios)

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "baseline_ending_nav": str(self.baseline_ending_nav),
            "baseline_total_return": self.baseline_total_return,
            "scenarios": [item.to_dict() for item in self.scenarios],
            "worst_scenario": self.worst_scenario,
        }


def _max_drawdown(values: Sequence[Decimal]) -> float:
    peak: Optional[Decimal] = None
    maximum = Decimal("0")
    for value in values:
        if peak is None or value > peak:
            peak = value
        if peak and peak > 0:
            drawdown = Decimal("1") - value / peak
            if drawdown > maximum:
                maximum = drawdown
    return float(maximum)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("timestamp must be an ISO-8601 datetime") from exc
    if not isinstance(value, (datetime, str)):
        raise TypeError("timestamp must be a datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return ensure_market_time(parsed)


def _cost_to_timestamp(trades: Sequence[Any], timestamp) -> Decimal:
    return sum(
        (
            _decimal(_field(trade, "commission", 0), "commission")
            + _decimal(_field(trade, "stamp_duty", 0), "stamp_duty")
            + _decimal(_field(trade, "slippage_cost", 0), "slippage_cost")
            for trade in trades
            if _timestamp(_field(trade, "execution_timestamp")) <= timestamp
        ),
        Decimal("0"),
    )


def run_stress_test(
    result: BacktestResult,
    scenarios: Iterable[StressScenario],
) -> StressReport:
    """Apply scenarios to an immutable result without network or I/O.

    The mark-to-market shock is applied to ``market_value`` while baseline
    cash is retained.  Additional costs are charged only as they become
    incurred, preserving the original observation timeline.  No scenario is
    silently invented: an empty scenario collection returns an empty report.
    """

    if not isinstance(result, BacktestResult):
        raise TypeError("result must be a BacktestResult")
    normalized = tuple(scenarios)
    if any(not isinstance(item, StressScenario) for item in normalized):
        raise TypeError("scenarios must contain StressScenario instances")
    if len({item.name for item in normalized}) != len(normalized):
        raise ValueError("stress scenario names must be unique")
    points = tuple(result.equity_curve)
    baseline_ending = result.metrics.ending_nav
    return _run_stress_parts(
        points=points,
        trades=result.trades,
        initial_cash=result.config.initial_cash,
        baseline_ending=baseline_ending,
        baseline_total_return=result.metrics.total_return,
        scenarios=normalized,
    )


def _run_stress_parts(
    *,
    points: Sequence[Any],
    trades: Sequence[Any],
    initial_cash: Decimal,
    baseline_ending: Decimal,
    baseline_total_return: float,
    scenarios: Sequence[StressScenario],
) -> StressReport:
    """Shared implementation for domain objects and serialized mappings."""

    baseline_ending = _decimal(baseline_ending, "baseline_ending_nav")
    initial_cash = _decimal(initial_cash, "initial_cash")
    if not points:
        raise ValueError("equity_curve must be non-empty")
    outputs = []
    for scenario in scenarios:
        stressed_values = []
        multiplier_delta = scenario.cost_multiplier - Decimal("1")
        mark_factor = Decimal("1") + scenario.price_shock
        for point in points:
            timestamp = _timestamp(_field(point, "timestamp"))
            extra_cost = _cost_to_timestamp(trades, timestamp) * multiplier_delta
            cash = _decimal(_field(point, "cash"), "cash")
            market_value = _decimal(_field(point, "market_value"), "market_value")
            stressed_values.append(cash + market_value * mark_factor - extra_cost)
        ending = stressed_values[-1]
        total_return = float(ending / initial_cash - Decimal("1")) if initial_cash else 0.0
        outputs.append(
            StressScenarioResult(
                scenario=scenario,
                baseline_ending_nav=baseline_ending,
                stressed_ending_nav=ending,
                ending_nav_delta=ending - baseline_ending,
                stressed_total_return=total_return,
                stressed_maximum_drawdown=_max_drawdown(stressed_values),
                stressed_minimum_nav=min(stressed_values),
                observations=len(stressed_values),
            )
        )
    worst = min(outputs, key=lambda item: item.stressed_ending_nav).scenario.name if outputs else None
    return StressReport(
        baseline_ending_nav=baseline_ending,
        baseline_total_return=baseline_total_return,
        scenarios=tuple(outputs),
        worst_scenario=worst,
    )


def run_stress_test_from_payload(
    payload: Mapping[str, Any], scenarios: Iterable[StressScenario]
) -> StressReport:
    """Build a stress report from a serialized backtest payload.

    This helper consumes the stable ``backtest_result_dict`` projection
    directly; it does not need to recreate a strategy or access a provider.
    """

    if not isinstance(payload, Mapping):
        raise TypeError("payload must be an object")
    normalized = tuple(scenarios)
    if any(not isinstance(item, StressScenario) for item in normalized):
        raise TypeError("scenarios must contain StressScenario instances")
    if len({item.name for item in normalized}) != len(normalized):
        raise ValueError("stress scenario names must be unique")
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
    baseline = metrics.get("ending_nav")
    if baseline is None:
        baseline = _field(equity[-1], "nav")
    return _run_stress_parts(
        points=equity,
        trades=trades,
        initial_cash=_field(config, "initial_cash"),
        baseline_ending=baseline,
        baseline_total_return=float(metrics.get("total_return", 0.0)),
        scenarios=normalized,
    )
