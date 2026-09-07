"""Immutable value objects for deterministic, research-only backtests."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Mapping, Optional, Tuple

from quant_monitor.domain import Signal, SignalAction, ensure_market_time, normalize_symbol


def _strict_int(value: Any, name: str, *, positive: bool = False) -> int:
    """Validate integer contract fields without coercing bool/float/string."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("{} must be an integer".format(name))
    if value < 0 or (positive and value == 0):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError("{} must be {}".format(name, qualifier))
    return value


def _strict_bool(value: Any, name: str) -> bool:
    """Reject truthy substitutes at research-boundary boolean fields."""

    if not isinstance(value, bool):
        raise TypeError("{} must be a boolean".format(name))
    return value


def _decimal(value: Any, name: str, *, positive: bool = False) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("{} must be a valid decimal".format(name)) from exc
    if not result.is_finite():
        raise ValueError("{} must be finite".format(name))
    if result < 0 or (positive and result == 0):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError("{} must be {}".format(name, qualifier))
    return result


def _decimal_signed(value: Any, name: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("{} must be a valid decimal".format(name)) from exc
    if not result.is_finite():
        raise ValueError("{} must be finite".format(name))
    return result


@dataclass(frozen=True)
class BacktestConfig:
    """Execution and performance assumptions for a daily-bar backtest.

    Commission, stamp-duty and slippage values are decimal rates.  For
    example, ``Decimal("0.0003")`` means three basis points.  Prices and cash
    are kept as :class:`~decimal.Decimal`; return/risk statistics are floats,
    matching the project's metric convention.
    """

    initial_cash: Decimal = Decimal("1000000")
    buy_commission_rate: Decimal = Decimal("0.0003")
    sell_commission_rate: Decimal = Decimal("0.0003")
    minimum_commission: Decimal = Decimal("5")
    buy_slippage_rate: Decimal = Decimal("0.0005")
    sell_slippage_rate: Decimal = Decimal("0.0005")
    stamp_duty_rate: Decimal = Decimal("0.0005")
    lot_size: int = 100
    price_tick: Decimal = Decimal("0.01")
    max_position_weight: Decimal = Decimal("1")
    annualization_sessions: int = 252
    annual_risk_free_rate: Decimal = Decimal("0")
    interval: str = "1d"
    price_basis: str = "raw_unadjusted"
    daily_evaluation_time: time = time(15, 30)
    daily_execution_time: time = time(9, 30)
    default_signal_valid_sessions: int = 1
    maximum_signal_valid_sessions: int = 20

    def __post_init__(self) -> None:
        for name in (
            "initial_cash",
            "buy_commission_rate",
            "sell_commission_rate",
            "minimum_commission",
            "buy_slippage_rate",
            "sell_slippage_rate",
            "stamp_duty_rate",
            "price_tick",
            "max_position_weight",
            "annual_risk_free_rate",
        ):
            object.__setattr__(
                self,
                name,
                _decimal(
                    getattr(self, name),
                    name,
                    positive=name in ("initial_cash", "price_tick", "max_position_weight"),
                ),
            )
        for name in (
            "buy_commission_rate",
            "sell_commission_rate",
            "buy_slippage_rate",
            "sell_slippage_rate",
            "stamp_duty_rate",
        ):
            if getattr(self, name) >= 1:
                raise ValueError("{} must be below 1".format(name))
        if self.max_position_weight > 1:
            raise ValueError("max_position_weight must not exceed 1")
        lot_size = _strict_int(self.lot_size, "lot_size", positive=True)
        annualization_sessions = _strict_int(
            self.annualization_sessions, "annualization_sessions", positive=True
        )
        interval = str(self.interval).strip().lower()
        if interval != "1d":
            raise ValueError("the backtest engine currently supports completed 1d bars only")
        price_basis = str(self.price_basis).strip()
        if not price_basis:
            raise ValueError("price_basis cannot be empty")
        for name in ("daily_evaluation_time", "daily_execution_time"):
            value = getattr(self, name)
            if not isinstance(value, time):
                raise TypeError("{} must be a datetime.time".format(name))
            if value.tzinfo is not None and value.utcoffset() is not None:
                raise ValueError("{} must be timezone-naive Shanghai wall time".format(name))
            object.__setattr__(self, name, value.replace(tzinfo=None))
        if self.daily_evaluation_time < time(15, 30):
            raise ValueError("daily_evaluation_time must be at or after 15:30")
        if self.daily_evaluation_time <= self.daily_execution_time:
            raise ValueError("daily_evaluation_time must be after daily_execution_time")
        for name in ("default_signal_valid_sessions", "maximum_signal_valid_sessions"):
            value = _strict_int(getattr(self, name), name, positive=True)
            object.__setattr__(self, name, value)
        if self.default_signal_valid_sessions > self.maximum_signal_valid_sessions:
            raise ValueError(
                "default_signal_valid_sessions cannot exceed maximum_signal_valid_sessions"
            )
        object.__setattr__(self, "lot_size", lot_size)
        object.__setattr__(self, "annualization_sessions", annualization_sessions)
        object.__setattr__(self, "interval", interval)
        object.__setattr__(self, "price_basis", price_basis)


@dataclass(frozen=True)
class TradingConstraint:
    """Point-in-time execution permission for one security and session.

    A missing constraint permits a hypothetical fill when a positive-volume
    bar exists, but that fill is explicitly marked *unverified*.  Use this
    object for known suspension or limit-lock state; the engine deliberately
    does not infer limit rules from a symbol prefix.
    """

    symbol: str
    session_date: date
    suspended: bool = False
    can_buy: bool = True
    can_sell: bool = True
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        value = self.session_date
        if isinstance(value, datetime):
            value = ensure_market_time(value).date()
        if not isinstance(value, date):
            raise TypeError("session_date must be a date")
        object.__setattr__(self, "session_date", value)
        object.__setattr__(self, "suspended", _strict_bool(self.suspended, "suspended"))
        object.__setattr__(self, "can_buy", _strict_bool(self.can_buy, "can_buy"))
        object.__setattr__(self, "can_sell", _strict_bool(self.can_sell, "can_sell"))
        object.__setattr__(self, "reason", str(self.reason))


@dataclass(frozen=True)
class Trade:
    symbol: str
    action: SignalAction
    signal_timestamp: datetime
    execution_timestamp: datetime
    quantity: int
    raw_price: Decimal
    execution_price: Decimal
    gross_notional: Decimal
    commission: Decimal
    stamp_duty: Decimal
    slippage_cost: Decimal
    cash_after: Decimal
    tradeability_verified: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "action", SignalAction(self.action))
        object.__setattr__(self, "signal_timestamp", ensure_market_time(self.signal_timestamp))
        object.__setattr__(
            self, "execution_timestamp", ensure_market_time(self.execution_timestamp)
        )
        if self.action not in (SignalAction.BUY, SignalAction.SELL):
            raise ValueError("trade action must be buy or sell")
        object.__setattr__(
            self, "quantity", _strict_int(self.quantity, "trade quantity", positive=True)
        )
        for name in (
            "raw_price",
            "execution_price",
            "gross_notional",
            "commission",
            "stamp_duty",
            "slippage_cost",
            "cash_after",
        ):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
        if self.raw_price == 0 or self.execution_price == 0 or self.gross_notional == 0:
            raise ValueError("trade prices and gross_notional must be positive")
        if self.signal_timestamp >= self.execution_timestamp:
            raise ValueError("trade execution must be after its signal")
        object.__setattr__(
            self,
            "tradeability_verified",
            _strict_bool(self.tradeability_verified, "tradeability_verified"),
        )


@dataclass(frozen=True)
class SkippedExecution:
    """A hypothetical signal fill that was deliberately not fabricated."""

    signal: Signal
    attempted_at: Optional[datetime]
    reason: str
    detail: str = ""
    will_retry: bool = False

    def __post_init__(self) -> None:
        if self.attempted_at is not None:
            object.__setattr__(self, "attempted_at", ensure_market_time(self.attempted_at))
        reason = str(self.reason).strip()
        if not reason:
            raise ValueError("skipped-execution reason cannot be empty")
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "detail", str(self.detail))
        object.__setattr__(self, "will_retry", _strict_bool(self.will_retry, "will_retry"))


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: int
    settled_quantity: int
    average_cost: Decimal
    last_price: Decimal
    market_value: Decimal
    unrealized_pnl: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        quantity = _strict_int(self.quantity, "position quantity", positive=True)
        settled_quantity = _strict_int(
            self.settled_quantity, "settled_quantity"
        )
        if not 0 <= settled_quantity <= quantity:
            raise ValueError("settled_quantity must be between zero and quantity")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "settled_quantity", settled_quantity)
        for name in ("average_cost", "last_price", "market_value"):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
        object.__setattr__(
            self,
            "unrealized_pnl",
            _decimal_signed(self.unrealized_pnl, "unrealized_pnl"),
        )


@dataclass(frozen=True)
class EquityPoint:
    timestamp: datetime
    cash: Decimal
    market_value: Decimal
    nav: Decimal
    daily_return: float
    drawdown: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", ensure_market_time(self.timestamp))
        for name in ("cash", "market_value", "nav"):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
        for name in ("daily_return", "drawdown"):
            value = float(getattr(self, name))
            if not math.isfinite(value):
                raise ValueError("{} must be finite".format(name))
            object.__setattr__(self, name, value)


@dataclass(frozen=True)
class BacktestMetrics:
    starting_nav: Decimal
    ending_nav: Decimal
    total_return: float
    annualized_return: float
    annualized_volatility: float
    sharpe_ratio: float
    maximum_drawdown: float
    turnover: float
    total_commission: Decimal
    total_stamp_duty: Decimal
    total_slippage_cost: Decimal
    session_count: int
    trade_count: int

    def __post_init__(self) -> None:
        for name in (
            "starting_nav",
            "ending_nav",
            "total_commission",
            "total_stamp_duty",
            "total_slippage_cost",
        ):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
        for name in (
            "total_return",
            "annualized_return",
            "annualized_volatility",
            "sharpe_ratio",
            "maximum_drawdown",
            "turnover",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value):
                raise ValueError("{} must be finite".format(name))
            object.__setattr__(self, name, value)
        if self.annualized_volatility < 0 or self.maximum_drawdown < 0 or self.turnover < 0:
            raise ValueError("risk and turnover metrics cannot be negative")
        for name in ("session_count", "trade_count"):
            value = _strict_int(getattr(self, name), name)
            object.__setattr__(self, name, value)


@dataclass(frozen=True)
class BacktestDiagnostics:
    """Data-lineage assumptions that bound how results may be interpreted."""

    calendar: str
    calendar_source: str
    calendar_version: str
    calendar_verified: bool
    price_basis: str
    bar_availability: str
    execution_bar_availability: str
    constraint_coverage: float
    constrained_session_count: int
    execution_attempt_count: int
    unverified_fill_count: int
    assumptions: Mapping[str, str]
    warnings: Tuple[str, ...]
    decision_grade: bool

    def __post_init__(self) -> None:
        for name in (
            "calendar",
            "calendar_source",
            "calendar_version",
            "price_basis",
            "bar_availability",
            "execution_bar_availability",
        ):
            value = str(getattr(self, name)).strip()
            if not value:
                raise ValueError("{} cannot be empty".format(name))
            object.__setattr__(self, name, value)
        _strict_bool(self.calendar_verified, "calendar_verified")
        # No calendar implementation currently carries independently verified
        # exchange lineage.  Caller-supplied truth must therefore fail closed.
        object.__setattr__(self, "calendar_verified", False)
        coverage = float(self.constraint_coverage)
        if not math.isfinite(coverage) or not 0.0 <= coverage <= 1.0:
            raise ValueError("constraint_coverage must be between zero and one")
        object.__setattr__(self, "constraint_coverage", coverage)
        for name in (
            "constrained_session_count",
            "execution_attempt_count",
            "unverified_fill_count",
        ):
            value = _strict_int(getattr(self, name), name)
            object.__setattr__(self, name, value)
        warnings = tuple(str(item).strip() for item in self.warnings)
        if any(not item for item in warnings):
            raise ValueError("warnings cannot contain empty values")
        if len(set(warnings)) != len(warnings):
            raise ValueError("warnings must be unique")
        object.__setattr__(self, "warnings", warnings)
        assumptions = {
            str(key).strip(): str(value).strip()
            for key, value in dict(self.assumptions).items()
        }
        if any(not key or not value for key, value in assumptions.items()):
            raise ValueError("assumptions keys and values cannot be empty")
        object.__setattr__(
            self, "assumptions", MappingProxyType(dict(sorted(assumptions.items())))
        )
        _strict_bool(self.decision_grade, "decision_grade")
        # Until an evidence-backed gate validates every required lineage
        # dimension, callers cannot promote a research result by constructor
        # input alone.
        object.__setattr__(self, "decision_grade", False)


@dataclass(frozen=True)
class BacktestResult:
    """Complete immutable result of one deterministic research run.

    ``turnover`` is total absolute executed notional divided by average NAV;
    both a buy and its eventual sell therefore contribute to turnover.
    """

    strategy_name: str
    config: BacktestConfig
    started_at: datetime
    ended_at: datetime
    equity_curve: Tuple[EquityPoint, ...]
    trades: Tuple[Trade, ...]
    skipped_executions: Tuple[SkippedExecution, ...]
    metrics: BacktestMetrics
    diagnostics: BacktestDiagnostics
    positions: Mapping[str, Position] = field(default_factory=dict)

    def __post_init__(self) -> None:
        name = str(self.strategy_name).strip()
        if not name:
            raise ValueError("strategy_name cannot be empty")
        object.__setattr__(self, "strategy_name", name)
        object.__setattr__(self, "started_at", ensure_market_time(self.started_at))
        object.__setattr__(self, "ended_at", ensure_market_time(self.ended_at))
        object.__setattr__(self, "equity_curve", tuple(self.equity_curve))
        object.__setattr__(self, "trades", tuple(self.trades))
        object.__setattr__(self, "skipped_executions", tuple(self.skipped_executions))
        if not isinstance(self.metrics, BacktestMetrics):
            raise TypeError("metrics must be a BacktestMetrics instance")
        if not isinstance(self.diagnostics, BacktestDiagnostics):
            raise TypeError("diagnostics must be a BacktestDiagnostics instance")
        if not self.equity_curve:
            raise ValueError("equity_curve cannot be empty")
        if self.started_at != self.equity_curve[0].timestamp:
            raise ValueError("started_at must equal the first equity timestamp")
        if self.ended_at != self.equity_curve[-1].timestamp:
            raise ValueError("ended_at must equal the final equity timestamp")
        if self.started_at > self.ended_at:
            raise ValueError("started_at cannot be after ended_at")
        if any(
            left.timestamp >= right.timestamp
            for left, right in zip(self.equity_curve, self.equity_curve[1:])
        ):
            raise ValueError("equity_curve timestamps must be strictly increasing")
        if self.metrics.session_count != len(self.equity_curve):
            raise ValueError("metrics session_count does not match equity_curve")
        if self.metrics.trade_count != len(self.trades):
            raise ValueError("metrics trade_count does not match trades")
        if self.metrics.ending_nav != self.equity_curve[-1].nav:
            raise ValueError("metrics ending_nav does not match equity_curve")
        if self.metrics.starting_nav != self.config.initial_cash:
            raise ValueError("metrics starting_nav does not match config initial_cash")
        if self.diagnostics.price_basis != self.config.price_basis:
            raise ValueError("diagnostics price_basis does not match config")
        unverified_fill_count = sum(not item.tradeability_verified for item in self.trades)
        if self.diagnostics.unverified_fill_count != unverified_fill_count:
            raise ValueError("diagnostics unverified_fill_count does not match trades")
        if self.diagnostics.execution_attempt_count < len(self.trades):
            raise ValueError("diagnostics execution_attempt_count is below trade count")
        if self.config.price_basis == "raw_unadjusted" and self.diagnostics.decision_grade:
            raise ValueError("raw_unadjusted results cannot be decision_grade")
        if self.diagnostics.decision_grade:
            raise ValueError("the current backtest engine cannot produce decision_grade results")
        if any(item.execution_timestamp < self.started_at for item in self.trades):
            raise ValueError("trade cannot precede the backtest")
        if any(item.execution_timestamp > self.ended_at for item in self.trades):
            raise ValueError("trade cannot follow the backtest")
        for symbol, position in dict(self.positions).items():
            if normalize_symbol(symbol) != position.symbol:
                raise ValueError("position key does not match position symbol")
        object.__setattr__(
            self,
            "positions",
            MappingProxyType(dict(sorted(dict(self.positions).items()))),
        )
