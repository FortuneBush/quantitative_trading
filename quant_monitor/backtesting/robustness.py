"""Deterministic bootstrap robustness analytics for research backtests.

The report answers a narrow question: how sensitive are aggregate return and
drawdown statistics to the observed sequence of daily returns?  It resamples
the *already materialized* return observations with replacement.  No provider,
database, clock, or global random state is touched.  A caller must provide a
seed, making the result reproducible and suitable for an immutable research
artifact.  Bootstrap intervals are descriptive uncertainty bands, not a
forecast or a guarantee of future performance.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple, Union

from .models import BacktestResult


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _return(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise TypeError("{} must be a finite number".format(name))
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TypeError("{} must be a finite number".format(name)) from exc
    if not math.isfinite(result):
        raise ValueError("{} must be finite".format(name))
    if result < -1.0:
        raise ValueError("{} cannot be below -1".format(name))
    return result


def _positive_int(value: Any, name: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("{} must be an integer".format(name))
    if value < 0 or (not allow_zero and value == 0):
        raise ValueError("{} must be {}".format(name, "non-negative" if allow_zero else "positive"))
    return value


def _quantile(values: Sequence[float], probability: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _total_return(returns: Sequence[float]) -> float:
    value = 1.0
    for item in returns:
        value *= 1.0 + item
    return value - 1.0


def _maximum_drawdown(returns: Sequence[float]) -> float:
    nav = 1.0
    peak = nav
    maximum = 0.0
    for item in returns:
        nav *= 1.0 + item
        peak = max(peak, nav)
        if peak > 0.0:
            maximum = max(maximum, 1.0 - nav / peak)
    return maximum


@dataclass(frozen=True)
class BootstrapRobustnessReport:
    """Immutable, reproducible bootstrap interval report."""

    method: str
    seed: int
    confidence: float
    resamples: int
    return_observations: int
    minimum_observations: int
    sample_sufficient: bool
    observed_total_return: Optional[float]
    observed_maximum_drawdown: Optional[float]
    total_return_interval: Tuple[Optional[float], Optional[float]]
    maximum_drawdown_interval: Tuple[Optional[float], Optional[float]]
    warnings: Tuple[str, ...]

    def __post_init__(self) -> None:
        if self.method != "iid_bootstrap_robustness_v1":
            raise ValueError("unsupported bootstrap robustness method")
        object.__setattr__(self, "seed", _positive_int(self.seed, "seed", allow_zero=True))
        confidence = float(self.confidence)
        if not math.isfinite(confidence) or not 0.0 < confidence < 1.0:
            raise ValueError("confidence must be between zero and one")
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "resamples", _positive_int(self.resamples, "resamples"))
        for name in ("return_observations", "minimum_observations"):
            object.__setattr__(self, name, _positive_int(getattr(self, name), name))
        for name in ("observed_total_return", "observed_maximum_drawdown"):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(float(value)) or float(value) < 0 and name.endswith("drawdown")):
                raise ValueError("{} must be finite and non-negative when present".format(name))
            object.__setattr__(self, name, None if value is None else float(value))
        for name in ("total_return_interval", "maximum_drawdown_interval"):
            interval = tuple(getattr(self, name))
            if len(interval) != 2:
                raise ValueError("{} must contain lower and upper bounds".format(name))
            if any(value is not None and not math.isfinite(float(value)) for value in interval):
                raise ValueError("{} must contain finite values or None".format(name))
            if interval[0] is not None and interval[1] is not None and interval[0] > interval[1]:
                raise ValueError("{} lower bound cannot exceed upper bound".format(name))
            object.__setattr__(self, name, tuple(None if value is None else float(value) for value in interval))
        if not isinstance(self.sample_sufficient, bool):
            raise TypeError("sample_sufficient must be a boolean")
        warnings = tuple(str(item).strip() for item in self.warnings)
        if any(not item for item in warnings) or len(set(warnings)) != len(warnings):
            raise ValueError("warnings must be unique and non-empty")
        object.__setattr__(self, "warnings", warnings)
        object.__setattr__(self, "sample_sufficient", self.sample_sufficient)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "method": self.method,
            "seed": self.seed,
            "confidence": self.confidence,
            "resamples": self.resamples,
            "return_observations": self.return_observations,
            "minimum_observations": self.minimum_observations,
            "sample_sufficient": self.sample_sufficient,
            "observed_total_return": self.observed_total_return,
            "observed_maximum_drawdown": self.observed_maximum_drawdown,
            "total_return_interval": list(self.total_return_interval),
            "maximum_drawdown_interval": list(self.maximum_drawdown_interval),
            "warnings": list(self.warnings),
            "research_only": True,
        }


def build_bootstrap_robustness_report(
    result: Union[BacktestResult, Mapping[str, Any]],
    *,
    confidence: float = 0.95,
    resamples: int = 2000,
    seed: int = 0,
    minimum_observations: int = 30,
) -> BootstrapRobustnessReport:
    """Build a reproducible IID bootstrap interval report.

    Missing or non-finite returns fail closed: no synthetic interval is
    produced.  ``resamples`` is capped to keep an HTTP caller from causing an
    unbounded CPU allocation; callers needing more samples should run offline.
    """

    confidence = float(confidence)
    if not math.isfinite(confidence) or not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be between zero and one")
    resamples = _positive_int(resamples, "resamples")
    if resamples > 100_000:
        raise ValueError("resamples cannot exceed 100000")
    seed = _positive_int(seed, "seed", allow_zero=True)
    minimum_observations = _positive_int(minimum_observations, "minimum_observations")
    if isinstance(result, BacktestResult):
        equity = result.equity_curve
    elif isinstance(result, Mapping):
        equity = result.get("equity_curve")
    else:
        raise TypeError("result must be a BacktestResult or persisted payload")
    if not isinstance(equity, (list, tuple)) or not equity:
        raise ValueError("result equity_curve must be a non-empty array")
    returns = []
    missing = 0
    for point in tuple(equity)[1:]:
        try:
            returns.append(_return(_field(point, "daily_return"), "daily return"))
        except (TypeError, ValueError):
            missing += 1
    sufficient = len(returns) >= minimum_observations and missing == 0
    warnings = []
    if missing:
        warnings.append("missing_or_invalid_return_observations")
    if len(returns) < minimum_observations:
        warnings.append("insufficient_return_observations_for_bootstrap")
    observed_total = _total_return(returns) if returns and missing == 0 else None
    observed_drawdown = _maximum_drawdown(returns) if returns and missing == 0 else None
    interval = (None, None)
    drawdown_interval = (None, None)
    if sufficient:
        rng = random.Random(seed)
        totals = []
        drawdowns = []
        n = len(returns)
        for _ in range(resamples):
            sample = tuple(returns[rng.randrange(n)] for _ in range(n))
            totals.append(_total_return(sample))
            drawdowns.append(_maximum_drawdown(sample))
        alpha = (1.0 - confidence) / 2.0
        interval = (_quantile(totals, alpha), _quantile(totals, 1.0 - alpha))
        drawdown_interval = (_quantile(drawdowns, alpha), _quantile(drawdowns, 1.0 - alpha))
    else:
        warnings.append("bootstrap_intervals_unavailable")
    return BootstrapRobustnessReport(
        method="iid_bootstrap_robustness_v1",
        seed=seed,
        confidence=confidence,
        resamples=resamples,
        return_observations=len(returns),
        minimum_observations=minimum_observations,
        sample_sufficient=sufficient,
        observed_total_return=observed_total,
        observed_maximum_drawdown=observed_drawdown,
        total_return_interval=interval,
        maximum_drawdown_interval=drawdown_interval,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def build_bootstrap_robustness_report_from_payload(
    payload: Mapping[str, Any], **kwargs: Any
) -> BootstrapRobustnessReport:
    """Build a report from a persisted result payload without I/O."""

    return build_bootstrap_robustness_report(payload, **kwargs)


__all__ = [
    "BootstrapRobustnessReport",
    "build_bootstrap_robustness_report",
    "build_bootstrap_robustness_report_from_payload",
]
