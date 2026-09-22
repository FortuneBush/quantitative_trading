"""Immutable research-portfolio snapshots derived from local backtests.

This module intentionally contains no persistence, provider, or broker code.
It turns a completed :class:`BacktestResult` (or explicit holdings) into an
auditable point-in-time portfolio view and fails closed when the accounting
identity does not hold.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Mapping, Optional, Tuple

from quant_monitor.domain import ensure_market_time, normalize_symbol

from .models import BacktestResult, Position


def _decimal(value: Any, name: str, *, positive: bool = False) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("{} must be a valid decimal".format(name)) from exc
    if not result.is_finite():
        raise ValueError("{} must be finite".format(name))
    if result < 0 or (positive and result == 0):
        raise ValueError("{} must be {}".format(name, "positive" if positive else "non-negative"))
    return result


def _finite(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("{} must be a real number".format(name)) from exc
    if not math.isfinite(result):
        raise ValueError("{} must be finite".format(name))
    return result


_KNOWN_PRICE_BASIS = {"raw", "raw_unadjusted", "qfq", "hfq", "unavailable"}


def _basis(value: Any) -> str:
    """Normalize a persisted price-basis label without guessing raw data."""

    if value is None:
        return "unavailable"
    text = str(value).strip().lower()
    if not text:
        return "unavailable"
    if text in _KNOWN_PRICE_BASIS:
        return "raw_unadjusted" if text == "raw" else text
    # Versioned qfq/hfq labels are valid only when their family is explicit.
    if text.startswith("qfq_") or text.startswith("hfq_"):
        return text
    return "unavailable"


def _freeze_json(value: Any) -> Any:
    """Deep-copy JSON-like binding metadata into immutable containers."""

    if isinstance(value, Mapping):
        frozen = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("adjustment_binding keys must be strings")
            frozen[key] = _freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted((_freeze_json(item) for item in value), key=str))
    return value


def _detached_json(value: Any) -> Any:
    """Return a fresh JSON-shaped copy, never exposing frozen internals."""

    if isinstance(value, Mapping):
        return {str(key): _detached_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_detached_json(item) for item in value]
    return value


@dataclass(frozen=True)
class PortfolioHolding:
    """One holding in a research portfolio snapshot."""

    symbol: str
    quantity: int
    settled_quantity: int
    last_price: Decimal
    market_value: Decimal
    weight: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int) or self.quantity <= 0:
            raise ValueError("quantity must be a positive integer")
        if (
            isinstance(self.settled_quantity, bool)
            or not isinstance(self.settled_quantity, int)
            or not 0 <= self.settled_quantity <= self.quantity
        ):
            raise ValueError("settled_quantity must be between zero and quantity")
        object.__setattr__(self, "last_price", _decimal(self.last_price, "last_price", positive=True))
        object.__setattr__(self, "market_value", _decimal(self.market_value, "market_value"))
        if self.market_value != self.last_price * Decimal(self.quantity):
            raise ValueError("market_value must equal quantity * last_price")
        object.__setattr__(self, "weight", _finite(self.weight, "weight"))
        if self.weight < 0 or self.weight > 1:
            raise ValueError("weight must be between zero and one")

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "quantity": self.quantity,
            "settled_quantity": self.settled_quantity,
            "last_price": str(self.last_price),
            "market_value": str(self.market_value),
            "weight": self.weight,
        }


@dataclass(frozen=True)
class PortfolioSnapshot:
    """Point-in-time, research-only portfolio accounting snapshot."""

    run_id: Optional[str]
    as_of: datetime
    nav: Decimal
    cash: Decimal
    invested_market_value: Decimal
    cash_weight: float
    holdings: Tuple[PortfolioHolding, ...]
    max_holding_weight: float
    concentration_hhi: float
    benchmark_name: Optional[str] = None
    rebalance_reason: str = ""
    valuation_quality: str = "complete"
    # A portfolio mark is meaningless without the price convention that
    # produced it.  Keep the backtest's explicit raw/qfq/hfq binding visible
    # instead of silently dropping it at the portfolio boundary.
    strategy_price_basis: str = "unavailable"
    execution_price_basis: str = "unavailable"
    valuation_price_basis: str = "unavailable"
    # Legacy callers may still pass ``price_basis``.  It is retained only as
    # an explicit compatibility field; missing/unknown values never become
    # raw_unadjusted implicitly.
    price_basis: Optional[str] = None
    adjustment_binding: Mapping[str, Any] = field(default_factory=dict)
    warnings: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", ensure_market_time(self.as_of))
        run_id = None if self.run_id is None else str(self.run_id).strip()
        if run_id == "":
            raise ValueError("run_id cannot be empty")
        object.__setattr__(self, "run_id", run_id)
        nav = _decimal(self.nav, "nav", positive=True)
        cash = _decimal(self.cash, "cash")
        invested = _decimal(self.invested_market_value, "invested_market_value")
        if cash + invested != nav:
            raise ValueError("cash + invested_market_value must equal nav")
        object.__setattr__(self, "nav", nav)
        object.__setattr__(self, "cash", cash)
        object.__setattr__(self, "invested_market_value", invested)
        object.__setattr__(self, "cash_weight", _finite(self.cash_weight, "cash_weight"))
        object.__setattr__(self, "max_holding_weight", _finite(self.max_holding_weight, "max_holding_weight"))
        object.__setattr__(self, "concentration_hhi", _finite(self.concentration_hhi, "concentration_hhi"))
        if not 0 <= self.cash_weight <= 1 or not 0 <= self.max_holding_weight <= 1:
            raise ValueError("portfolio weights must be between zero and one")
        if self.concentration_hhi < 0 or self.concentration_hhi > 1:
            raise ValueError("concentration_hhi must be between zero and one")
        holdings = tuple(self.holdings)
        if any(not isinstance(holding, PortfolioHolding) for holding in holdings):
            raise TypeError("holdings must contain PortfolioHolding objects")
        symbols = [holding.symbol for holding in holdings]
        if len(set(symbols)) != len(symbols):
            raise ValueError("portfolio holdings must have unique symbols")
        holding_value = sum((holding.market_value for holding in holdings), Decimal("0"))
        if holding_value != invested:
            raise ValueError("holdings market value must equal invested_market_value")
        expected_cash_weight = float(cash / nav)
        if not math.isclose(self.cash_weight, expected_cash_weight, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("cash_weight does not match cash / nav")
        expected_weights = {
            holding.symbol: float(holding.market_value / nav) for holding in holdings
        }
        for holding in holdings:
            if not math.isclose(
                holding.weight,
                expected_weights[holding.symbol],
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                raise ValueError("holding weight does not match market value / nav")
        expected_max = max(expected_weights.values(), default=0.0)
        expected_hhi = sum(weight * weight for weight in expected_weights.values())
        if not math.isclose(self.max_holding_weight, expected_max, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("max_holding_weight is not derived from holdings")
        if not math.isclose(self.concentration_hhi, expected_hhi, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("concentration_hhi is not derived from holdings")
        weight_sum = sum(holding.weight for holding in holdings) + self.cash_weight
        if not math.isclose(weight_sum, 1.0, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("portfolio weights must sum to one including cash")
        object.__setattr__(self, "holdings", tuple(sorted(holdings, key=lambda item: item.symbol)))
        quality = str(self.valuation_quality).strip().lower()
        if quality not in {"complete", "partial", "stale", "unavailable"}:
            raise ValueError("unsupported valuation_quality")
        object.__setattr__(self, "valuation_quality", quality)
        strategy_basis = _basis(self.strategy_price_basis)
        execution_basis = _basis(self.execution_price_basis)
        valuation_basis = _basis(self.valuation_price_basis)
        legacy_basis = _basis(self.price_basis)
        object.__setattr__(self, "strategy_price_basis", strategy_basis)
        object.__setattr__(self, "execution_price_basis", execution_basis)
        object.__setattr__(self, "valuation_price_basis", valuation_basis)
        # Preserve an explicit legacy label in the wire shape, but expose the
        # canonical valuation basis when callers omitted the compatibility
        # field.  No missing value is guessed as raw.
        object.__setattr__(
            self,
            "price_basis",
            legacy_basis if legacy_basis != "unavailable" else valuation_basis,
        )
        if not isinstance(self.adjustment_binding, Mapping):
            raise TypeError("adjustment_binding must be a mapping")
        object.__setattr__(self, "adjustment_binding", _freeze_json(self.adjustment_binding))
        benchmark = None if self.benchmark_name is None else str(self.benchmark_name).strip()
        object.__setattr__(self, "benchmark_name", benchmark or None)
        object.__setattr__(self, "rebalance_reason", str(self.rebalance_reason).strip())
        warnings = tuple(str(item).strip() for item in self.warnings)
        if any(not item for item in warnings) or len(set(warnings)) != len(warnings):
            raise ValueError("warnings must be unique and non-empty")
        object.__setattr__(self, "warnings", warnings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "as_of": self.as_of.isoformat(),
            "nav": str(self.nav),
            "cash": str(self.cash),
            "invested_market_value": str(self.invested_market_value),
            "cash_weight": self.cash_weight,
            "holdings": [holding.to_dict() for holding in self.holdings],
            "max_holding_weight": self.max_holding_weight,
            "concentration_hhi": self.concentration_hhi,
            "benchmark_name": self.benchmark_name,
            "rebalance_reason": self.rebalance_reason,
            "valuation_quality": self.valuation_quality,
            "strategy_price_basis": self.strategy_price_basis,
            "execution_price_basis": self.execution_price_basis,
            "valuation_price_basis": self.valuation_price_basis,
            "price_basis": self.price_basis,
            "adjustment_binding": _detached_json(self.adjustment_binding),
            "warnings": list(self.warnings),
        }


def build_portfolio_snapshot(
    *,
    as_of: datetime,
    cash: Decimal,
    positions: Mapping[str, Position],
    run_id: Optional[str] = None,
    benchmark_name: Optional[str] = None,
    rebalance_reason: str = "",
    valuation_quality: str = "complete",
    strategy_price_basis: Optional[str] = None,
    execution_price_basis: Optional[str] = None,
    valuation_price_basis: Optional[str] = None,
    price_basis: Optional[str] = None,
    adjustment_binding: Optional[Mapping[str, Any]] = None,
    warnings: Tuple[str, ...] = (),
) -> PortfolioSnapshot:
    """Build a snapshot from explicit positions and enforce NAV invariants."""

    if not isinstance(positions, Mapping):
        raise TypeError("positions must be a mapping")
    normalized: dict[str, Position] = {}
    for raw_symbol, position in positions.items():
        if not isinstance(position, Position):
            raise TypeError("positions must contain Position objects")
        symbol = normalize_symbol(raw_symbol)
        if symbol != position.symbol:
            raise ValueError("position key does not match position symbol")
        if symbol in normalized:
            raise ValueError("duplicate normalized position symbol")
        if position.market_value != position.last_price * Decimal(position.quantity):
            raise ValueError("position market_value must equal quantity * last_price")
        normalized[symbol] = position
    cash_value = _decimal(cash, "cash")
    invested = sum((position.market_value for position in normalized.values()), Decimal("0"))
    nav = cash_value + invested
    if nav <= 0:
        raise ValueError("portfolio NAV must be positive")
    cash_weight = float(cash_value / nav)
    holdings = tuple(
        PortfolioHolding(
            symbol=symbol,
            quantity=position.quantity,
            settled_quantity=position.settled_quantity,
            last_price=position.last_price,
            market_value=position.market_value,
            weight=float(position.market_value / nav),
        )
        for symbol, position in sorted(normalized.items())
    )
    weights = [holding.weight for holding in holdings]
    return PortfolioSnapshot(
        run_id=run_id,
        as_of=as_of,
        nav=nav,
        cash=cash_value,
        invested_market_value=invested,
        cash_weight=cash_weight,
        holdings=holdings,
        max_holding_weight=max(weights, default=0.0),
        concentration_hhi=sum(weight * weight for weight in weights),
        benchmark_name=benchmark_name,
        rebalance_reason=rebalance_reason,
        valuation_quality=valuation_quality,
        strategy_price_basis=(strategy_price_basis if strategy_price_basis is not None else price_basis),
        # The legacy ``price_basis`` field describes only the caller's old
        # single-basis label.  Do not project it onto execution/valuation:
        # those dimensions require explicit evidence from diagnostics.
        execution_price_basis=execution_price_basis,
        valuation_price_basis=valuation_price_basis,
        price_basis=price_basis,
        adjustment_binding=adjustment_binding or {},
        warnings=warnings,
    )


def portfolio_snapshot_from_result(
    result: BacktestResult,
    *,
    run_id: Optional[str] = None,
    benchmark_name: Optional[str] = None,
    valuation_quality: Optional[str] = None,
    warnings: Optional[Tuple[str, ...]] = None,
) -> PortfolioSnapshot:
    """Create a snapshot from the final local backtest state."""

    if not isinstance(result, BacktestResult):
        raise TypeError("result must be a BacktestResult")
    point = result.equity_curve[-1]
    position_market_value = sum(
        (position.market_value for position in result.positions.values()), Decimal("0")
    )
    if point.market_value != position_market_value:
        raise ValueError("final equity market_value does not match positions")
    if point.nav != point.cash + point.market_value:
        raise ValueError("final equity nav does not equal cash + market_value")
    # A decision grade describes research reproducibility/tradeability checks;
    # it is not an independent attestation that every final holding has a
    # trustworthy valuation.  Keep the wrapper conservative until an explicit
    # valuation attestation is part of the result contract.
    derived_quality = "partial"
    derived_warnings = list(result.diagnostics.warnings)
    if result.diagnostics.decision_grade:
        derived_warnings.append("valuation_quality_unattested")
    derived_warnings = tuple(dict.fromkeys(derived_warnings))
    return build_portfolio_snapshot(
        as_of=point.timestamp,
        cash=point.cash,
        positions=result.positions,
        run_id=run_id,
        benchmark_name=benchmark_name,
        valuation_quality=derived_quality if valuation_quality is None else valuation_quality,
        strategy_price_basis=result.diagnostics.assumptions.get("strategy_price_basis"),
        execution_price_basis=result.diagnostics.assumptions.get("execution_price_basis"),
        valuation_price_basis=result.diagnostics.assumptions.get("valuation_price_basis"),
        price_basis=None,
        adjustment_binding=result.diagnostics.assumptions.get("adjustment_factor_binding", {}),
        warnings=derived_warnings if warnings is None else warnings,
    )


def portfolio_snapshot_from_payload(
    payload: Mapping[str, Any],
    *,
    run_id: Optional[str] = None,
    benchmark_name: Optional[str] = None,
) -> PortfolioSnapshot:
    """Build a snapshot from a serialized immutable backtest payload.

    The parser is intentionally strict because this is a research-result
    boundary: malformed or tampered holdings are rejected instead of being
    silently interpreted as zero.
    """

    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    curve = payload.get("equity_curve")
    if not isinstance(curve, (list, tuple)) or not curve:
        raise ValueError("payload equity_curve cannot be empty")
    final = curve[-1]
    if not isinstance(final, Mapping):
        raise ValueError("payload equity_curve entries must be objects")
    try:
        as_of = datetime.fromisoformat(str(final["timestamp"]))
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        cash = _decimal(final["cash"], "cash")
        expected_market_value = _decimal(final["market_value"], "market_value")
        expected_nav = _decimal(final["nav"], "nav", positive=True)
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise ValueError("payload final equity point is invalid") from exc
    raw_positions = payload.get("positions", {})
    if not isinstance(raw_positions, Mapping):
        raise ValueError("payload positions must be an object")
    positions: dict[str, Position] = {}
    for symbol, raw in raw_positions.items():
        if not isinstance(raw, Mapping):
            raise ValueError("payload position must be an object")
        try:
            position = Position(
                symbol=str(raw.get("symbol", symbol)),
                quantity=raw["quantity"],
                settled_quantity=raw["settled_quantity"],
                average_cost=Decimal(str(raw["average_cost"])),
                last_price=Decimal(str(raw["last_price"])),
                market_value=Decimal(str(raw["market_value"])),
                unrealized_pnl=Decimal(str(raw["unrealized_pnl"])),
            )
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise ValueError("payload position is invalid") from exc
        positions[str(symbol)] = position
    position_market_value = sum(
        (position.market_value for position in positions.values()), Decimal("0")
    )
    if expected_market_value != position_market_value:
        raise ValueError("payload market_value does not match positions")
    if expected_nav != cash + expected_market_value:
        raise ValueError("payload NAV does not equal cash + market_value")
    diagnostics = payload.get("diagnostics", {})
    strategy_price_basis = None
    execution_price_basis = None
    valuation_price_basis = None
    price_basis = None
    adjustment_binding: Mapping[str, Any] = {}
    if isinstance(diagnostics, Mapping):
        raw_assumptions = diagnostics.get("assumptions", {})
        if isinstance(raw_assumptions, Mapping):
            strategy_price_basis = raw_assumptions.get("strategy_price_basis")
            execution_price_basis = raw_assumptions.get("execution_price_basis")
            valuation_price_basis = raw_assumptions.get("valuation_price_basis")
        raw_binding = diagnostics.get("assumptions", {})
        if isinstance(raw_binding, Mapping):
            candidate = raw_binding.get("adjustment_factor_binding", {})
            if isinstance(candidate, Mapping):
                adjustment_binding = candidate
    diagnostic_warnings = []
    if isinstance(diagnostics, Mapping):
        raw_warnings = diagnostics.get("warnings", ())
        if isinstance(raw_warnings, (list, tuple)):
            diagnostic_warnings.extend(str(item).strip() for item in raw_warnings if str(item).strip())
        if diagnostics.get("decision_grade") is not True:
            diagnostic_warnings.append("result_decision_grade_false")
    diagnostic_warnings = list(dict.fromkeys(diagnostic_warnings))
    # A serialized caller-provided ``decision_grade`` is not independent
    # valuation evidence.  Keep payload-derived snapshots conservative unless
    # a trusted valuation attestation is added to the payload contract.
    diagnostic_warnings.append("valuation_quality_unattested")
    diagnostic_warnings = tuple(dict.fromkeys(diagnostic_warnings))
    snapshot = build_portfolio_snapshot(
        as_of=as_of,
        cash=cash,
        positions=positions,
        run_id=run_id,
        benchmark_name=benchmark_name,
        valuation_quality="partial",
        strategy_price_basis=strategy_price_basis,
        execution_price_basis=execution_price_basis,
        valuation_price_basis=valuation_price_basis,
        price_basis=price_basis,
        adjustment_binding=adjustment_binding,
        warnings=diagnostic_warnings,
    )
    if snapshot.nav != expected_nav:
        raise ValueError("payload NAV does not match cash and positions")
    return snapshot
