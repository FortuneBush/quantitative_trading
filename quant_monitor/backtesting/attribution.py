"""Deterministic, cash-aware performance attribution for research results.

The attribution layer consumes an already materialized backtest and explicit
return series.  It never downloads a benchmark, infers a cash rate, or reads
the live repository.  This is intentionally an arithmetic contribution report
rather than a factor model: the active component is the residual after the
explicit benchmark and cash contributions have been removed.

All monetary values are returned as strings by :meth:`AttributionReport.to_dict`
because the domain uses :class:`~decimal.Decimal` for money.  Missing benchmark
or cash observations remain ``None`` and the report is marked partial instead
of treating them as zero.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from quant_monitor.domain import ensure_market_time

from .models import BacktestResult


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _decimal(value: Any, name: str, *, nonnegative: bool = False) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("{} must be a finite decimal".format(name)) from exc
    if not result.is_finite() or (nonnegative and result < 0):
        raise ValueError("{} must be a finite{} decimal".format(
            name, " non-negative" if nonnegative else ""
        ))
    return result


def _return(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise TypeError("{} must be a finite number".format(name))
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TypeError("{} must be a finite number".format(name)) from exc
    if not math.isfinite(result) or result < -1.0:
        raise ValueError("{} must be finite and cannot be below -1".format(name))
    return result


def _session_date(value: Any) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise TypeError("attribution datetime keys must be timezone-aware")
        return ensure_market_time(value).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("attribution mapping keys must use YYYY-MM-DD") from exc
        if parsed.isoformat() != value:
            raise ValueError("attribution mapping keys must use YYYY-MM-DD")
        return parsed
    raise TypeError("attribution mapping keys must be dates or ISO date strings")


def _align_returns(
    values: Union[Sequence[Any], Mapping[Any, Any]],
    sessions: Sequence[date],
    *,
    name: str,
) -> Tuple[Tuple[Optional[float], ...], int, Tuple[str, ...]]:
    """Align a sequence or date mapping without filling missing sessions."""

    warnings: List[str] = []
    if isinstance(values, Mapping):
        by_date: Dict[date, float] = {}
        for key, value in values.items():
            session = _session_date(key)
            if session in by_date:
                raise ValueError("{} mapping contains duplicate session dates".format(name))
            by_date[session] = _return(value, "{} return".format(name))
        aligned = tuple(by_date.get(session) for session in sessions)
        count = len(by_date)
        if any(item is None for item in aligned):
            warnings.append("{}_sessions_missing_for_portfolio_dates".format(name))
        return aligned, count, tuple(warnings)
    if isinstance(values, (str, bytes)):
        raise TypeError("{} must be a sequence or date mapping".format(name))
    try:
        raw = tuple(values)
    except TypeError as exc:
        raise TypeError("{} must be a sequence or date mapping".format(name)) from exc
    if len(raw) != len(sessions):
        raise ValueError(
            "{} return sequence length must equal portfolio observations ({})".format(
                name, len(sessions)
            )
        )
    return tuple(_return(value, "{} return".format(name)) for value in raw), len(raw), ()


def _trade_costs(result: Union[BacktestResult, Mapping[str, Any]]) -> Dict[date, Decimal]:
    trades = result.trades if isinstance(result, BacktestResult) else result.get("trades")
    if not isinstance(trades, (list, tuple)):
        raise ValueError("result trades must be an array")
    costs: Dict[date, Decimal] = {}
    for trade in trades:
        timestamp = _field(trade, "execution_timestamp")
        if isinstance(timestamp, str):
            try:
                timestamp = datetime.fromisoformat(timestamp)
            except ValueError as exc:
                raise ValueError("trade execution_timestamp is invalid") from exc
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("trade execution_timestamp must be timezone-aware")
        session = ensure_market_time(timestamp).date()
        cost = sum(
            (
                _decimal(_field(trade, name), "trade {}".format(name), nonnegative=True)
                for name in ("commission", "stamp_duty", "slippage_cost")
            ),
            Decimal("0"),
        )
        costs[session] = costs.get(session, Decimal("0")) + cost
    return costs


@dataclass(frozen=True)
class AttributionRow:
    """One session's arithmetic contribution to NAV change."""

    timestamp: datetime
    previous_nav: Decimal
    previous_cash: Decimal
    total_pnl: Decimal
    portfolio_return: float
    benchmark_return: Optional[float]
    cash_return: Optional[float]
    benchmark_contribution: Optional[Decimal]
    cash_contribution: Optional[Decimal]
    transaction_cost_contribution: Decimal
    active_contribution: Optional[Decimal]
    unallocated_contribution: Optional[Decimal]

    def to_dict(self) -> Dict[str, Any]:
        def money(value: Optional[Decimal]) -> Optional[str]:
            return None if value is None else str(value)

        return {
            "timestamp": self.timestamp.isoformat(),
            "previous_nav": money(self.previous_nav),
            "previous_cash": money(self.previous_cash),
            "total_pnl": money(self.total_pnl),
            "portfolio_return": self.portfolio_return,
            "benchmark_return": self.benchmark_return,
            "cash_return": self.cash_return,
            "benchmark_contribution": money(self.benchmark_contribution),
            "cash_contribution": money(self.cash_contribution),
            "transaction_cost_contribution": money(self.transaction_cost_contribution),
            "active_contribution": money(self.active_contribution),
            "unallocated_contribution": money(self.unallocated_contribution),
        }


@dataclass(frozen=True)
class AttributionReport:
    """A conservative portfolio attribution report.

    ``status`` is ``complete`` only when every portfolio session has both a
    benchmark and a cash return.  ``benchmark_only`` keeps the benchmark
    residual visible but leaves cash and active contributions unavailable.
    """

    method: str
    status: str
    portfolio_observations: int
    benchmark_observations: int
    cash_observations: int
    complete_observations: int
    total_pnl: Decimal
    benchmark_contribution: Optional[Decimal]
    cash_contribution: Optional[Decimal]
    transaction_cost_contribution: Decimal
    active_contribution: Optional[Decimal]
    unallocated_contribution: Optional[Decimal]
    warnings: Tuple[str, ...]
    rows: Tuple[AttributionRow, ...]

    def to_dict(self) -> Dict[str, Any]:
        def money(value: Optional[Decimal]) -> Optional[str]:
            return None if value is None else str(value)

        return {
            "method": self.method,
            "status": self.status,
            "portfolio_observations": self.portfolio_observations,
            "benchmark_observations": self.benchmark_observations,
            "cash_observations": self.cash_observations,
            "complete_observations": self.complete_observations,
            "total_pnl": money(self.total_pnl),
            "benchmark_contribution": money(self.benchmark_contribution),
            "cash_contribution": money(self.cash_contribution),
            "transaction_cost_contribution": money(self.transaction_cost_contribution),
            "active_contribution": money(self.active_contribution),
            "unallocated_contribution": money(self.unallocated_contribution),
            "warnings": list(self.warnings),
            "rows": [row.to_dict() for row in self.rows],
        }


def build_attribution_report(
    result: Union[BacktestResult, Mapping[str, Any]],
    benchmark_returns: Union[Sequence[Any], Mapping[Any, Any]],
    *,
    cash_returns: Optional[Union[Sequence[Any], Mapping[Any, Any]]] = None,
) -> AttributionReport:
    """Build an arithmetic attribution from a materialized backtest.

    ``benchmark_returns`` and ``cash_returns`` are per-session return
    fractions.  A date mapping is aligned to the result's sessions; missing
    dates remain ``None``.  Transaction costs are taken only from the saved
    trade ledger.  The active contribution is available only when both
    benchmark and cash returns are present for every session, so missing cash
    data cannot be mistaken for a zero-yield assumption.
    """

    equity_source = result.equity_curve if isinstance(result, BacktestResult) else result.get("equity_curve")
    if not isinstance(equity_source, (list, tuple)) or len(equity_source) < 2:
        raise ValueError("result equity_curve must contain at least two points")
    points = tuple(equity_source)
    navs: List[Decimal] = []
    cash: List[Decimal] = []
    timestamps: List[datetime] = []
    previous_timestamp: Optional[datetime] = None
    for point in points:
        timestamp = _field(point, "timestamp")
        if isinstance(timestamp, str):
            try:
                timestamp = datetime.fromisoformat(timestamp)
            except ValueError as exc:
                raise ValueError("equity timestamp is invalid") from exc
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("equity timestamps must be timezone-aware")
        timestamp = ensure_market_time(timestamp)
        if previous_timestamp is not None and timestamp <= previous_timestamp:
            raise ValueError("equity timestamps must be strictly increasing")
        previous_timestamp = timestamp
        timestamps.append(timestamp)
        navs.append(_decimal(_field(point, "nav"), "equity nav"))
        cash.append(_decimal(_field(point, "cash"), "equity cash"))
    sessions = tuple(timestamp.date() for timestamp in timestamps[1:])
    benchmark, benchmark_count, benchmark_warnings = _align_returns(
        benchmark_returns, sessions, name="benchmark"
    )
    if cash_returns is None:
        cash_aligned: Tuple[Optional[float], ...] = tuple(None for _ in sessions)
        cash_count = 0
        cash_warnings = ("cash_returns_not_supplied",)
    else:
        cash_aligned, cash_count, cash_warnings = _align_returns(
            cash_returns, sessions, name="cash"
        )
    costs = _trade_costs(result)
    rows: List[AttributionRow] = []
    warnings = list(benchmark_warnings) + list(cash_warnings)
    for index, session in enumerate(sessions, start=1):
        previous_nav = navs[index - 1]
        if previous_nav <= 0:
            raise ValueError("equity nav must be positive for attribution")
        total_pnl = navs[index] - previous_nav
        portfolio_return = float(total_pnl / previous_nav)
        benchmark_return = benchmark[index - 1]
        cash_return = cash_aligned[index - 1]
        benchmark_contribution = (
            previous_nav * Decimal(str(benchmark_return))
            if benchmark_return is not None else None
        )
        cash_contribution = (
            cash[index - 1] * Decimal(str(cash_return))
            if cash_return is not None else None
        )
        transaction_cost = -costs.get(session, Decimal("0"))
        gross_pnl = total_pnl - transaction_cost
        if benchmark_contribution is not None and cash_contribution is not None:
            active = gross_pnl - benchmark_contribution - cash_contribution
            unallocated = Decimal("0")
        else:
            active = None
            unallocated = (
                gross_pnl - benchmark_contribution
                if benchmark_contribution is not None else None
            )
        rows.append(AttributionRow(
            timestamp=timestamps[index], previous_nav=previous_nav,
            previous_cash=cash[index - 1], total_pnl=total_pnl,
            portfolio_return=portfolio_return, benchmark_return=benchmark_return,
            cash_return=cash_return, benchmark_contribution=benchmark_contribution,
            cash_contribution=cash_contribution,
            transaction_cost_contribution=transaction_cost,
            active_contribution=active,
            unallocated_contribution=unallocated,
        ))
    complete = sum(
        row.benchmark_contribution is not None and row.cash_contribution is not None
        for row in rows
    )
    benchmark_total = (
        sum((row.benchmark_contribution for row in rows if row.benchmark_contribution is not None), Decimal("0"))
        if all(row.benchmark_contribution is not None for row in rows) else None
    )
    cash_total = (
        sum((row.cash_contribution for row in rows if row.cash_contribution is not None), Decimal("0"))
        if all(row.cash_contribution is not None for row in rows) else None
    )
    active_total = (
        sum((row.active_contribution for row in rows if row.active_contribution is not None), Decimal("0"))
        if complete == len(rows) else None
    )
    if active_total is not None:
        unallocated_total = Decimal("0")
    elif benchmark_total is not None:
        unallocated_total = sum(
            (row.unallocated_contribution for row in rows if row.unallocated_contribution is not None),
            Decimal("0"),
        )
    else:
        unallocated_total = None
    if not rows:
        warnings.append("no_portfolio_observations")
    status = "complete" if complete == len(rows) else (
        "benchmark_only" if benchmark_total is not None else "unavailable"
    )
    return AttributionReport(
        method="arithmetic_benchmark_cash_attribution_v1",
        status=status,
        portfolio_observations=len(rows), benchmark_observations=benchmark_count,
        cash_observations=cash_count, complete_observations=complete,
        total_pnl=sum((row.total_pnl for row in rows), Decimal("0")),
        benchmark_contribution=benchmark_total, cash_contribution=cash_total,
        transaction_cost_contribution=sum(
            (row.transaction_cost_contribution for row in rows), Decimal("0")
        ),
        active_contribution=active_total, unallocated_contribution=unallocated_total,
        warnings=tuple(dict.fromkeys(warnings)), rows=tuple(rows),
    )


def build_attribution_report_from_payload(
    payload: Mapping[str, Any],
    benchmark_returns: Union[Sequence[Any], Mapping[Any, Any]],
    *,
    cash_returns: Optional[Union[Sequence[Any], Mapping[Any, Any]]] = None,
) -> AttributionReport:
    """Build an attribution report from a persisted result JSON object."""

    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    return build_attribution_report(
        payload, benchmark_returns, cash_returns=cash_returns
    )


__all__ = [
    "AttributionReport",
    "AttributionRow",
    "build_attribution_report",
    "build_attribution_report_from_payload",
]
