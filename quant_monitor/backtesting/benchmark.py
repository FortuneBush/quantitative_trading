"""Deterministic benchmark-comparison analytics for research backtests.

This module deliberately consumes an already materialized backtest and a
caller-supplied benchmark return series.  It never fetches benchmark prices,
fills missing observations, or treats a short sample as statistically
meaningful.  The result is therefore safe to expose from a cached research
endpoint and keeps provider concerns outside the backtesting package.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from quant_monitor.domain import ensure_market_time

from .models import BacktestResult


def _finite(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _return(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise TypeError("{} must be a finite number".format(name))
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError("{} must be a finite number".format(name)) from exc
    if not math.isfinite(result):
        raise ValueError("{} must be finite".format(name))
    if result < -1.0:
        raise ValueError("{} cannot be below -1".format(name))
    return result


def _session_date(value: Any, *, allow_datetime_text: bool = False) -> date:
    if isinstance(value, datetime):
        # Session identity is a market-time concept.  A naive datetime cannot
        # be mapped deterministically to the Shanghai trading date (and would
        # violate the domain-wide timezone invariant), so reject it instead
        # of silently taking ``.date()``.
        if value.tzinfo is None or value.utcoffset() is None:
            raise TypeError("benchmark datetime keys must be timezone-aware")
        return ensure_market_time(value).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        if allow_datetime_text:
            try:
                parsed_datetime = datetime.fromisoformat(value)
            except ValueError:
                parsed_datetime = None
            if parsed_datetime is not None:
                if parsed_datetime.tzinfo is None or parsed_datetime.utcoffset() is None:
                    raise TypeError("benchmark datetime keys must be timezone-aware")
                return ensure_market_time(parsed_datetime).date()
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                "benchmark mapping keys must be ISO dates in YYYY-MM-DD format"
            ) from exc
        if parsed.isoformat() != value:
            raise ValueError("benchmark mapping keys must use YYYY-MM-DD")
        return parsed
    raise TypeError("benchmark mapping keys must be dates or ISO date strings")


def _compound(returns: Sequence[float]) -> Optional[float]:
    if not returns:
        return None
    value = 1.0
    for item in returns:
        value *= 1.0 + item
    return _finite(value - 1.0)


def _annualize(total_return: Optional[float], observations: int, annualization: int) -> Optional[float]:
    if total_return is None or observations <= 0:
        return None
    base = 1.0 + total_return
    if base <= 0.0:
        return None
    return _finite(base ** (float(annualization) / observations) - 1.0)


def _annualize_arithmetic(value: Optional[float], annualization: int) -> Optional[float]:
    """Annualize a one-period regression intercept using arithmetic scaling.

    Jensen alpha is the intercept of a *daily arithmetic* return regression.
    Compounding that intercept as if it were a portfolio total return (the
    ``_annualize`` helper above) changes the statistic's meaning and can
    explode for ordinary daily values.  Keep the regression convention by
    scaling the daily intercept by the configured number of sessions.
    """

    if value is None:
        return None
    return _finite(float(value) * float(annualization))


@dataclass(frozen=True)
class BenchmarkReport:
    """Immutable portfolio-vs-benchmark report.

    Return values are decimal fractions (``0.01`` means one percent).  A
    statistic that cannot be estimated from the aligned sample is ``None``;
    ``warnings`` explains why it is unavailable.
    """

    benchmark_name: str
    method: str
    portfolio_observations: int
    benchmark_observations: int
    aligned_observations: int
    minimum_observations: int
    sample_sufficient: bool
    portfolio_total_return: Optional[float]
    benchmark_total_return: Optional[float]
    excess_total_return: Optional[float]
    portfolio_annualized_return: Optional[float]
    benchmark_annualized_return: Optional[float]
    alpha_annualized: Optional[float]
    beta: Optional[float]
    correlation: Optional[float]
    tracking_error_annualized: Optional[float]
    information_ratio: Optional[float]
    upside_capture: Optional[float]
    downside_capture: Optional[float]
    warnings: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "benchmark_name": self.benchmark_name,
            "method": self.method,
            "portfolio_observations": self.portfolio_observations,
            "benchmark_observations": self.benchmark_observations,
            "aligned_observations": self.aligned_observations,
            "minimum_observations": self.minimum_observations,
            "sample_sufficient": self.sample_sufficient,
            "portfolio_total_return": self.portfolio_total_return,
            "benchmark_total_return": self.benchmark_total_return,
            "excess_total_return": self.excess_total_return,
            "portfolio_annualized_return": self.portfolio_annualized_return,
            "benchmark_annualized_return": self.benchmark_annualized_return,
            "alpha_annualized": self.alpha_annualized,
            "beta": self.beta,
            "correlation": self.correlation,
            "tracking_error_annualized": self.tracking_error_annualized,
            "information_ratio": self.information_ratio,
            "upside_capture": self.upside_capture,
            "downside_capture": self.downside_capture,
            "warnings": list(self.warnings),
        }


def build_benchmark_report(
    result: Union[BacktestResult, Mapping[str, Any]],
    benchmark_returns: Union[Sequence[Any], Mapping[Any, Any]],
    *,
    benchmark_name: str = "benchmark",
    minimum_observations: int = 20,
    annualization_sessions: int = 252,
    annual_risk_free_rate: float = 0.0,
) -> BenchmarkReport:
    """Compare a backtest's daily returns with an aligned benchmark series.

    A sequence must contain exactly one return for each interval after the
    first equity point.  A mapping is keyed by session date (``date``,
    ``datetime`` or ISO date string); it is aligned by date and missing dates
    remain visible through a warning.  Mapping input is useful when the
    benchmark calendar differs from the portfolio's supplied sessions.
    """

    if isinstance(result, BacktestResult):
        equity_source = result.equity_curve
    elif isinstance(result, Mapping):
        equity_source = result.get("equity_curve")
        if not isinstance(equity_source, (list, tuple)):
            raise ValueError("payload equity_curve must be an array")
    else:
        raise TypeError("result must be a BacktestResult or persisted payload")
    if not isinstance(benchmark_name, str) or not benchmark_name.strip():
        raise ValueError("benchmark_name cannot be empty")
    if isinstance(minimum_observations, bool) or not isinstance(minimum_observations, int):
        raise TypeError("minimum_observations must be an integer")
    if minimum_observations < 1:
        raise ValueError("minimum_observations must be positive")
    if isinstance(annualization_sessions, bool) or not isinstance(annualization_sessions, int):
        raise TypeError("annualization_sessions must be an integer")
    if annualization_sessions < 1:
        raise ValueError("annualization_sessions must be positive")
    risk_free = _return(annual_risk_free_rate, "annual_risk_free_rate")
    equity = tuple(equity_source)
    portfolio = tuple(_return(_field(point, "daily_return"), "portfolio return") for point in equity[1:])
    session_dates = tuple(
        _session_date(_field(point, "timestamp"), allow_datetime_text=True)
        for point in equity[1:]
    )
    warnings: List[str] = []
    if isinstance(benchmark_returns, Mapping):
        by_date = {}
        for key, value in benchmark_returns.items():
            session = _session_date(key)
            if session in by_date:
                raise ValueError("benchmark mapping contains duplicate session dates")
            by_date[session] = _return(value, "benchmark return")
        benchmark_count = len(by_date)
        pairs = [(portfolio[index], by_date[session]) for index, session in enumerate(session_dates) if session in by_date]
        if len(pairs) < len(session_dates):
            warnings.append("benchmark_sessions_missing_for_portfolio_dates")
    else:
        if isinstance(benchmark_returns, (str, bytes)):
            raise TypeError("benchmark_returns must be a sequence of numbers or date mapping")
        try:
            raw = tuple(benchmark_returns)
        except TypeError as exc:
            raise TypeError("benchmark_returns must be a sequence of numbers or date mapping") from exc
        benchmark_count = len(raw)
        if benchmark_count != len(portfolio):
            raise ValueError(
                "benchmark return sequence length must equal portfolio return observations ({})".format(
                    len(portfolio)
                )
            )
        pairs = list(zip(portfolio, tuple(_return(item, "benchmark return") for item in raw)))

    aligned_portfolio = tuple(item[0] for item in pairs)
    aligned_benchmark = tuple(item[1] for item in pairs)
    aligned_count = len(pairs)
    sufficient = aligned_count >= minimum_observations
    if not sufficient:
        warnings.append("insufficient_aligned_observations_for_benchmark_statistics")
    if not aligned_count:
        warnings.append("no_aligned_benchmark_observations")

    portfolio_total = _compound(aligned_portfolio)
    benchmark_total = _compound(aligned_benchmark)
    excess_total = (
        _finite(portfolio_total - benchmark_total)
        if portfolio_total is not None and benchmark_total is not None
        else None
    )
    portfolio_ann = _annualize(portfolio_total, aligned_count, annualization_sessions)
    benchmark_ann = _annualize(benchmark_total, aligned_count, annualization_sessions)

    beta = correlation = alpha_ann = None
    tracking = information = None
    upside = downside = None
    if sufficient:
        p_mean = statistics.fmean(aligned_portfolio)
        b_mean = statistics.fmean(aligned_benchmark)
        if aligned_count >= 2:
            # Use the sample (N-1) denominator consistently for covariance,
            # volatility and correlation. Mixing population and sample
            # denominators biases beta by (N-1)/N.
            p_stdev = statistics.stdev(aligned_portfolio)
            b_stdev = statistics.stdev(aligned_benchmark)
            covariance = sum(
                (p - p_mean) * (b - b_mean)
                for p, b in zip(aligned_portfolio, aligned_benchmark)
            ) / float(aligned_count - 1)
            if b_stdev > 0.0:
                beta = _finite(covariance / (b_stdev * b_stdev))
            else:
                warnings.append("benchmark_return_variance_is_zero")
            if p_stdev > 0.0 and b_stdev > 0.0:
                correlation = _finite(covariance / (p_stdev * b_stdev))
            elif b_stdev == 0.0:
                correlation = None
                if "benchmark_return_variance_is_zero" not in warnings:
                    warnings.append("benchmark_return_variance_is_zero")
            else:
                warnings.append("portfolio_return_variance_is_zero")
            active = tuple(p - b for p, b in zip(aligned_portfolio, aligned_benchmark))
            active_stdev = statistics.stdev(active)
            tracking = _finite(active_stdev * math.sqrt(annualization_sessions))
            if active_stdev > 0.0:
                information = _finite(statistics.fmean(active) / active_stdev * math.sqrt(annualization_sessions))
            else:
                warnings.append("active_return_variance_is_zero")
        else:
            warnings.append("at_least_two_observations_required_for_regression_statistics")
        if beta is not None:
            daily_alpha = (p_mean - risk_free / annualization_sessions) - beta * (
                b_mean - risk_free / annualization_sessions
            )
            alpha_ann = _annualize_arithmetic(daily_alpha, annualization_sessions)
        up = [(p, b) for p, b in zip(aligned_portfolio, aligned_benchmark) if b > 0.0]
        down = [(p, b) for p, b in zip(aligned_portfolio, aligned_benchmark) if b < 0.0]
        if up:
            denominator = statistics.fmean(item[1] for item in up)
            if denominator != 0.0:
                upside = _finite(statistics.fmean(item[0] for item in up) / denominator)
        if down:
            denominator = statistics.fmean(item[1] for item in down)
            if denominator != 0.0:
                downside = _finite(statistics.fmean(item[0] for item in down) / denominator)
        if not up:
            warnings.append("no_positive_benchmark_sessions_for_upside_capture")
        if not down:
            warnings.append("no_negative_benchmark_sessions_for_downside_capture")

    return BenchmarkReport(
        benchmark_name=benchmark_name.strip(),
        method="historical_regression_and_capture_v1",
        portfolio_observations=len(portfolio),
        benchmark_observations=benchmark_count,
        aligned_observations=aligned_count,
        minimum_observations=minimum_observations,
        sample_sufficient=sufficient,
        portfolio_total_return=portfolio_total,
        benchmark_total_return=benchmark_total,
        excess_total_return=excess_total,
        portfolio_annualized_return=portfolio_ann,
        benchmark_annualized_return=benchmark_ann,
        alpha_annualized=alpha_ann,
        beta=beta,
        correlation=correlation,
        tracking_error_annualized=tracking,
        information_ratio=information,
        upside_capture=upside,
        downside_capture=downside,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def build_benchmark_report_from_payload(
    payload: Mapping[str, Any],
    benchmark_returns: Union[Sequence[Any], Mapping[Any, Any]],
    *,
    benchmark_name: str = "benchmark",
    minimum_observations: int = 20,
    annualization_sessions: int = 252,
    annual_risk_free_rate: float = 0.0,
) -> BenchmarkReport:
    """Compare a persisted ``backtest_result_dict`` payload offline."""

    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    return build_benchmark_report(
        payload,
        benchmark_returns,
        benchmark_name=benchmark_name,
        minimum_observations=minimum_observations,
        annualization_sessions=annualization_sessions,
        annual_risk_free_rate=annual_risk_free_rate,
    )


__all__ = [
    "BenchmarkReport",
    "build_benchmark_report",
    "build_benchmark_report_from_payload",
]
