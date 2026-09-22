"""Deterministic execution-capacity audit for daily-bar backtests.

Daily bars do not contain an order book, so this module does not claim to
simulate fills.  It compares each recorded fill with an explicit maximum
participation rate of the execution bar's reported volume and marks missing
volume data as unassessed.  The audit is read-only and provider-independent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

from quant_monitor.domain import Bar, ensure_market_time, normalize_symbol

from .models import BacktestResult, Trade


def _decimal(value: Any, name: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("{} must be a valid decimal".format(name)) from exc
    if not result.is_finite():
        raise ValueError("{} must be finite".format(name))
    return result


@dataclass(frozen=True)
class CapacityAssumptions:
    """Explicit daily-volume participation assumption."""

    max_participation_rate: Decimal = Decimal("0.10")

    def __post_init__(self) -> None:
        value = _decimal(self.max_participation_rate, "max_participation_rate")
        if value <= 0 or value > 1:
            raise ValueError("max_participation_rate must be above 0 and at most 1")
        object.__setattr__(self, "max_participation_rate", value)


@dataclass(frozen=True)
class CapacityObservation:
    """Capacity assessment for one execution, or an explicit unassessed fill."""

    symbol: str
    session_date: date
    quantity: int
    gross_notional: Decimal
    bar_volume: Optional[int]
    estimated_volume_notional: Optional[Decimal]
    participation_rate: Optional[float]
    executable_notional: Optional[Decimal]
    capacity_limited: bool
    assessed: bool
    reason: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        if not isinstance(self.session_date, date):
            raise TypeError("session_date must be a date")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int) or self.quantity <= 0:
            raise ValueError("quantity must be a positive integer")
        object.__setattr__(self, "gross_notional", _decimal(self.gross_notional, "gross_notional"))
        if self.gross_notional <= 0:
            raise ValueError("gross_notional must be positive")
        if self.bar_volume is not None:
            if isinstance(self.bar_volume, bool) or not isinstance(self.bar_volume, int) or self.bar_volume < 0:
                raise ValueError("bar_volume must be a non-negative integer or None")
        for name in ("estimated_volume_notional", "executable_notional"):
            value = getattr(self, name)
            if value is not None:
                value = _decimal(value, name)
                if value < 0:
                    raise ValueError("{} cannot be negative".format(name))
            object.__setattr__(self, name, value)
        if self.participation_rate is not None:
            value = float(self.participation_rate)
            if not math.isfinite(value) or value < 0:
                raise ValueError("participation_rate must be finite and non-negative")
            object.__setattr__(self, "participation_rate", value)
        if not isinstance(self.capacity_limited, bool) or not isinstance(self.assessed, bool):
            raise TypeError("capacity_limited and assessed must be booleans")
        if self.assessed and self.bar_volume is None:
            raise ValueError("assessed observations require bar_volume")
        object.__setattr__(self, "reason", None if self.reason is None else str(self.reason))

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "symbol": self.symbol,
            "session_date": self.session_date.isoformat(),
            "quantity": self.quantity,
            "gross_notional": str(self.gross_notional),
            "bar_volume": self.bar_volume,
            "estimated_volume_notional": (
                str(self.estimated_volume_notional)
                if self.estimated_volume_notional is not None
                else None
            ),
            "participation_rate": self.participation_rate,
            "executable_notional": (
                str(self.executable_notional) if self.executable_notional is not None else None
            ),
            "capacity_limited": self.capacity_limited,
            "assessed": self.assessed,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CapacityReport:
    """Aggregate, auditable capacity metrics for a backtest."""

    assumptions: CapacityAssumptions
    trade_count: int
    assessed_trade_count: int
    unassessed_trade_count: int
    capacity_limited_trade_count: int
    requested_notional: Decimal
    executable_notional: Decimal
    maximum_observed_participation_rate: Optional[float]
    observations: Tuple[CapacityObservation, ...]
    warnings: Tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.assumptions, CapacityAssumptions):
            raise TypeError("assumptions must be CapacityAssumptions")
        for name in (
            "trade_count",
            "assessed_trade_count",
            "unassessed_trade_count",
            "capacity_limited_trade_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("{} must be a non-negative integer".format(name))
        if self.trade_count != self.assessed_trade_count + self.unassessed_trade_count:
            raise ValueError("trade counts do not reconcile")
        if self.capacity_limited_trade_count > self.assessed_trade_count:
            raise ValueError("capacity-limited count exceeds assessed count")
        object.__setattr__(self, "requested_notional", _decimal(self.requested_notional, "requested_notional"))
        object.__setattr__(self, "executable_notional", _decimal(self.executable_notional, "executable_notional"))
        if self.requested_notional < 0 or self.executable_notional < 0:
            raise ValueError("notional values cannot be negative")
        if self.maximum_observed_participation_rate is not None:
            value = float(self.maximum_observed_participation_rate)
            if not math.isfinite(value) or value < 0:
                raise ValueError("maximum_observed_participation_rate must be finite and non-negative")
            object.__setattr__(self, "maximum_observed_participation_rate", value)
        observations = tuple(self.observations)
        if len(observations) != self.trade_count or any(not isinstance(item, CapacityObservation) for item in observations):
            raise ValueError("observations must match trade_count")
        warnings = tuple(str(item).strip() for item in self.warnings)
        if any(not item for item in warnings) or len(set(warnings)) != len(warnings):
            raise ValueError("warnings must contain unique non-empty strings")
        object.__setattr__(self, "observations", observations)
        object.__setattr__(self, "warnings", warnings)

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "assumptions": {"max_participation_rate": str(self.assumptions.max_participation_rate)},
            "trade_count": self.trade_count,
            "assessed_trade_count": self.assessed_trade_count,
            "unassessed_trade_count": self.unassessed_trade_count,
            "capacity_limited_trade_count": self.capacity_limited_trade_count,
            "requested_notional": str(self.requested_notional),
            "executable_notional": str(self.executable_notional),
            "maximum_observed_participation_rate": self.maximum_observed_participation_rate,
            "observations": [item.to_dict() for item in self.observations],
            "warnings": list(self.warnings),
        }


def audit_capacity(
    result: BacktestResult,
    bars_by_symbol: Mapping[str, Sequence[Bar]],
    assumptions: Optional[CapacityAssumptions] = None,
) -> CapacityReport:
    """Audit fills against execution-bar volume without changing the result."""

    if not isinstance(result, BacktestResult):
        raise TypeError("result must be a BacktestResult")
    assumptions = assumptions or CapacityAssumptions()
    return _audit_capacity_trades(result.trades, bars_by_symbol, assumptions)


def _audit_capacity_trades(
    trades: Sequence[Any],
    bars_by_symbol: Mapping[str, Sequence[Bar]],
    assumptions: CapacityAssumptions,
) -> CapacityReport:
    """Shared implementation for domain results and persisted payloads."""

    lookup = {}
    seen_symbols = set()
    for raw_symbol, bars in dict(bars_by_symbol or {}).items():
        symbol = normalize_symbol(raw_symbol)
        if symbol in seen_symbols:
            raise ValueError("duplicate normalized symbol in bars_by_symbol: {}".format(symbol))
        seen_symbols.add(symbol)
        for bar in bars:
            if not isinstance(bar, Bar) or bar.symbol != symbol:
                raise ValueError("bars_by_symbol contains an invalid or mismatched bar")
            key = (symbol, bar.timestamp.date())
            if key in lookup:
                raise ValueError("duplicate capacity bar for {} on {}".format(symbol, key[1]))
            lookup[key] = bar

    observations = []
    requested = Decimal("0")
    executable = Decimal("0")
    limited = 0
    unassessed = 0
    maximum: Optional[float] = None
    for trade in trades:
        if isinstance(trade, Trade):
            symbol = trade.symbol
            session = trade.execution_timestamp.date()
            quantity = trade.quantity
            gross_notional = trade.gross_notional
            execution_price = trade.execution_price
        elif isinstance(trade, Mapping):
            try:
                symbol = normalize_symbol(trade["symbol"])
                timestamp = trade["execution_timestamp"]
                if isinstance(timestamp, str):
                    timestamp = datetime.fromisoformat(timestamp)
                if not isinstance(timestamp, datetime) or timestamp.tzinfo is None or timestamp.utcoffset() is None:
                    raise ValueError("execution_timestamp must be timezone-aware")
                session = ensure_market_time(timestamp).date()
                quantity = trade["quantity"]
                if isinstance(quantity, bool) or not isinstance(quantity, int):
                    raise TypeError("quantity must be an integer")
                gross_notional = _decimal(trade["gross_notional"], "gross_notional")
                execution_price = _decimal(trade["execution_price"], "execution_price")
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise ValueError("result payload contains an invalid trade") from exc
            if quantity <= 0 or gross_notional <= 0 or execution_price <= 0:
                raise ValueError("result payload contains an invalid trade")
        else:
            raise TypeError("result trades must contain Trade instances or objects")
        bar = lookup.get((symbol, session))
        requested += gross_notional
        if bar is None or bar.volume <= 0:
            unassessed += 1
            observations.append(
                CapacityObservation(
                    symbol=symbol,
                    session_date=session,
                    quantity=quantity,
                    gross_notional=gross_notional,
                    bar_volume=None if bar is None else bar.volume,
                    estimated_volume_notional=None,
                    participation_rate=None,
                    executable_notional=None,
                    capacity_limited=False,
                    assessed=False,
                    reason="missing_execution_volume" if bar is None else "zero_execution_volume",
                )
            )
            continue
        volume_notional = execution_price * Decimal(bar.volume)
        allowed = volume_notional * assumptions.max_participation_rate
        participation = float(gross_notional / volume_notional)
        maximum = participation if maximum is None else max(maximum, participation)
        is_limited = gross_notional > allowed
        if is_limited:
            limited += 1
            executable += allowed
        else:
            executable += gross_notional
        observations.append(
            CapacityObservation(
                symbol=symbol,
                session_date=session,
                quantity=quantity,
                gross_notional=gross_notional,
                bar_volume=bar.volume,
                estimated_volume_notional=volume_notional,
                participation_rate=participation,
                executable_notional=min(gross_notional, allowed),
                capacity_limited=is_limited,
                assessed=True,
                reason="participation_rate_exceeded" if is_limited else None,
            )
        )
    warnings = []
    if unassessed:
        warnings.append("execution_volume_missing_or_zero_for_some_trades")
    if limited:
        warnings.append("some_trades_exceed_max_participation_rate")
    return CapacityReport(
        assumptions=assumptions,
        trade_count=len(trades),
        assessed_trade_count=len(trades) - unassessed,
        unassessed_trade_count=unassessed,
        capacity_limited_trade_count=limited,
        requested_notional=requested,
        executable_notional=executable,
        maximum_observed_participation_rate=maximum,
        observations=tuple(observations),
        warnings=tuple(warnings),
    )


def audit_capacity_from_payload(
    payload: Mapping[str, Any],
    bars_by_symbol: Mapping[str, Sequence[Bar]],
    assumptions: Optional[CapacityAssumptions] = None,
) -> CapacityReport:
    """Audit a serialized backtest result without reconstructing a strategy."""

    if not isinstance(payload, Mapping):
        raise TypeError("payload must be an object")
    trades = payload.get("trades")
    if not isinstance(trades, (list, tuple)):
        raise ValueError("payload trades must be an array")
    return _audit_capacity_trades(
        trades,
        bars_by_symbol,
        assumptions or CapacityAssumptions(),
    )


__all__ = [
    "CapacityAssumptions",
    "CapacityObservation",
    "CapacityReport",
    "audit_capacity",
    "audit_capacity_from_payload",
]
