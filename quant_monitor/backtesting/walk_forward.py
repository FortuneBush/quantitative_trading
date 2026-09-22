"""Strict, offline walk-forward validation for research strategies.

This module deliberately does not try to *fit* a strategy or to inspect a
provider.  Callers provide a deterministic mapping of candidate factories;
every candidate is replayed on a training window and a validation window,
then the best validation candidate is frozen before its test window is run.
The test window is consequently never passed to the scorer or to candidate
selection.  A fresh strategy instance is created for every phase, which keeps
stateful strategy implementations from leaking observations between phases.

The implementation is an orchestration layer around :class:`BacktestEngine`:
all execution, T+1, costs and trading constraints remain in the existing
engine.  No database, filesystem or network I/O is performed here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from statistics import median
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from quant_monitor.domain import Bar, ensure_market_time, normalize_symbol
from quant_monitor.strategies import CohortStrategy, Strategy
from quant_monitor.trading_calendar import TradingCalendar

from .engine import BacktestEngine, BacktestValidationError
from .models import BacktestConfig, BacktestResult, TradingConstraint


StrategyFactory = Callable[[], Union[Strategy, CohortStrategy]]
ScoreFunction = Callable[[BacktestResult], float]


class WalkForwardValidationError(ValueError):
    """Raised when a walk-forward request cannot satisfy its split contract."""


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("{} must be an integer".format(name))
    if value <= 0:
        raise ValueError("{} must be positive".format(name))
    return value


def _optional_finite(value: Any, name: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("{} must be a real number or None".format(name))
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("{} must be finite".format(name))
    return result


def _session_date(value: Any, name: str) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise TypeError("{} datetime must be timezone-aware".format(name))
        return ensure_market_time(value).date()
    if not isinstance(value, date):
        raise TypeError("{} must contain dates or timezone-aware datetimes".format(name))
    return value


@dataclass(frozen=True)
class WalkForwardConfig:
    """Chronological train/validation/test window policy.

    ``step_sessions`` controls how far the next fold advances.  A rolling
    training window is used by default; ``expanding_train=True`` anchors every
    training window at the first available session.  ``selection_metric`` is
    maximized (maximum drawdown is represented as its negative value).
    """

    train_sessions: int = 252
    validation_sessions: int = 63
    test_sessions: int = 63
    step_sessions: Optional[int] = None
    expanding_train: bool = False
    selection_metric: str = "sharpe_ratio"
    minimum_train_score: Optional[float] = None
    max_folds: Optional[int] = None

    def __post_init__(self) -> None:
        for name in ("train_sessions", "validation_sessions", "test_sessions"):
            object.__setattr__(self, name, _positive_int(getattr(self, name), name))
        step = self.test_sessions if self.step_sessions is None else _positive_int(
            self.step_sessions, "step_sessions"
        )
        object.__setattr__(self, "step_sessions", step)
        if not isinstance(self.expanding_train, bool):
            raise TypeError("expanding_train must be a boolean")
        metric = str(self.selection_metric).strip().lower()
        allowed = {
            "total_return",
            "annualized_return",
            "sharpe_ratio",
            "sortino_ratio",
            "calmar_ratio",
            "positive_session_ratio",
            "maximum_drawdown",
        }
        if metric not in allowed:
            raise ValueError(
                "selection_metric must be one of {}".format(", ".join(sorted(allowed)))
            )
        object.__setattr__(self, "selection_metric", metric)
        object.__setattr__(
            self,
            "minimum_train_score",
            _optional_finite(self.minimum_train_score, "minimum_train_score"),
        )
        if self.max_folds is not None:
            object.__setattr__(self, "max_folds", _positive_int(self.max_folds, "max_folds"))


@dataclass(frozen=True)
class WalkForwardWindow:
    """One non-overlapping chronological split."""

    fold_index: int
    train_sessions: Tuple[date, ...]
    validation_sessions: Tuple[date, ...]
    test_sessions: Tuple[date, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "fold_index", _positive_int(self.fold_index, "fold_index"))
        for name in ("train_sessions", "validation_sessions", "test_sessions"):
            values = tuple(_session_date(item, name) for item in getattr(self, name))
            if not values:
                raise ValueError("{} cannot be empty".format(name))
            if any(left >= right for left, right in zip(values, values[1:])):
                raise ValueError("{} must be strictly increasing".format(name))
            object.__setattr__(self, name, values)
        if self.train_sessions[-1] >= self.validation_sessions[0]:
            raise ValueError("training and validation windows overlap")
        if self.validation_sessions[-1] >= self.test_sessions[0]:
            raise ValueError("validation and test windows overlap")

    @property
    def all_sessions(self) -> Tuple[date, ...]:
        return self.train_sessions + self.validation_sessions + self.test_sessions

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fold_index": self.fold_index,
            "train_sessions": [item.isoformat() for item in self.train_sessions],
            "validation_sessions": [item.isoformat() for item in self.validation_sessions],
            "test_sessions": [item.isoformat() for item in self.test_sessions],
            "train_start": self.train_sessions[0].isoformat(),
            "train_end": self.train_sessions[-1].isoformat(),
            "validation_start": self.validation_sessions[0].isoformat(),
            "validation_end": self.validation_sessions[-1].isoformat(),
            "test_start": self.test_sessions[0].isoformat(),
            "test_end": self.test_sessions[-1].isoformat(),
        }


@dataclass(frozen=True)
class WalkForwardCandidateReport:
    """Audit record for one candidate's train/validation attempts."""

    candidate_id: str
    train_score: Optional[float]
    validation_score: Optional[float]
    train_result: Optional[BacktestResult]
    validation_result: Optional[BacktestResult]
    selection_eligible: bool
    error: Optional[str] = None

    def __post_init__(self) -> None:
        candidate_id = str(self.candidate_id).strip()
        if not candidate_id:
            raise ValueError("candidate_id cannot be empty")
        object.__setattr__(self, "candidate_id", candidate_id)
        for name in ("train_score", "validation_score"):
            object.__setattr__(self, name, _optional_finite(getattr(self, name), name))
        if not isinstance(self.selection_eligible, bool):
            raise TypeError("selection_eligible must be a boolean")
        if self.error is not None:
            error = str(self.error).strip()
            object.__setattr__(self, "error", error or "candidate evaluation failed")

    def to_dict(self) -> Dict[str, Any]:
        from quant_monitor.serialization import backtest_result_dict

        result: Dict[str, Any] = {
            "candidate_id": self.candidate_id,
            "train_score": self.train_score,
            "validation_score": self.validation_score,
            "selection_eligible": self.selection_eligible,
            "error": self.error,
        }
        if self.train_result is not None:
            result["train_result"] = backtest_result_dict(self.train_result)
        if self.validation_result is not None:
            result["validation_result"] = backtest_result_dict(self.validation_result)
        return result


@dataclass(frozen=True)
class WalkForwardFoldReport:
    """One fold's candidate audit and frozen out-of-sample result."""

    window: WalkForwardWindow
    candidates: Tuple[WalkForwardCandidateReport, ...]
    selected_candidate_id: str
    test_result: BacktestResult
    warnings: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        candidates = tuple(self.candidates)
        if not candidates:
            raise ValueError("fold candidates cannot be empty")
        ids = [item.candidate_id for item in candidates]
        if len(set(ids)) != len(ids):
            raise ValueError("fold candidate IDs must be unique")
        selected = str(self.selected_candidate_id).strip()
        if selected not in ids:
            raise ValueError("selected candidate is not present in fold candidates")
        if not any(item.candidate_id == selected and item.selection_eligible for item in candidates):
            raise ValueError("selected candidate must be eligible")
        if not isinstance(self.test_result, BacktestResult):
            raise TypeError("test_result must be a BacktestResult")
        warnings = tuple(str(item).strip() for item in self.warnings)
        if any(not item for item in warnings):
            raise ValueError("warnings cannot contain empty values")
        if len(set(warnings)) != len(warnings):
            raise ValueError("warnings must be unique")
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "selected_candidate_id", selected)
        object.__setattr__(self, "warnings", warnings)

    def to_dict(self) -> Dict[str, Any]:
        from quant_monitor.serialization import backtest_result_dict

        return {
            "window": self.window.to_dict(),
            "candidates": [item.to_dict() for item in self.candidates],
            "selected_candidate_id": self.selected_candidate_id,
            "test_result": backtest_result_dict(self.test_result),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class WalkForwardSummary:
    """Aggregated statistics across independent out-of-sample folds."""

    fold_count: int
    test_session_count: int
    compounded_test_return: float
    mean_test_return: float
    median_test_return: float
    worst_test_return: float
    best_test_return: float
    positive_fold_ratio: float
    mean_test_sharpe: float
    warnings: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for name in ("fold_count", "test_session_count"):
            object.__setattr__(self, name, _positive_int(getattr(self, name), name))
        for name in (
            "compounded_test_return",
            "mean_test_return",
            "median_test_return",
            "worst_test_return",
            "best_test_return",
            "positive_fold_ratio",
            "mean_test_sharpe",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value):
                raise ValueError("{} must be finite".format(name))
            object.__setattr__(self, name, value)
        if not 0.0 <= self.positive_fold_ratio <= 1.0:
            raise ValueError("positive_fold_ratio must be between zero and one")
        warnings = tuple(str(item).strip() for item in self.warnings)
        if any(not item for item in warnings):
            raise ValueError("warnings cannot contain empty values")
        if len(set(warnings)) != len(warnings):
            raise ValueError("warnings must be unique")
        object.__setattr__(self, "warnings", warnings)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fold_count": self.fold_count,
            "test_session_count": self.test_session_count,
            "compounded_test_return": self.compounded_test_return,
            "mean_test_return": self.mean_test_return,
            "median_test_return": self.median_test_return,
            "worst_test_return": self.worst_test_return,
            "best_test_return": self.best_test_return,
            "positive_fold_ratio": self.positive_fold_ratio,
            "mean_test_sharpe": self.mean_test_sharpe,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class WalkForwardReport:
    """Complete deterministic report, including failed candidate attempts."""

    config: WalkForwardConfig
    folds: Tuple[WalkForwardFoldReport, ...]
    summary: WalkForwardSummary
    warnings: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        folds = tuple(self.folds)
        if not folds:
            raise ValueError("walk-forward report must contain at least one fold")
        if not isinstance(self.summary, WalkForwardSummary):
            raise TypeError("summary must be a WalkForwardSummary")
        warnings = tuple(str(item).strip() for item in self.warnings)
        if any(not item for item in warnings):
            raise ValueError("warnings cannot contain empty values")
        if len(set(warnings)) != len(warnings):
            raise ValueError("warnings must be unique")
        object.__setattr__(self, "folds", folds)
        object.__setattr__(self, "warnings", warnings)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "config": {
                "train_sessions": self.config.train_sessions,
                "validation_sessions": self.config.validation_sessions,
                "test_sessions": self.config.test_sessions,
                "step_sessions": self.config.step_sessions,
                "expanding_train": self.config.expanding_train,
                "selection_metric": self.config.selection_metric,
                "minimum_train_score": self.config.minimum_train_score,
                "max_folds": self.config.max_folds,
            },
            "summary": self.summary.to_dict(),
            "folds": [item.to_dict() for item in self.folds],
            "warnings": list(self.warnings),
            "research_only": True,
            "test_used_for_selection": False,
        }


def _default_score(result: BacktestResult, metric: str) -> float:
    value = float(getattr(result.metrics, metric))
    # Higher is always better for selection.  A lower drawdown is therefore
    # represented by its negative value, while the report retains the raw
    # maximum drawdown in the underlying result.
    return -value if metric == "maximum_drawdown" else value


def _validate_score(value: Any, candidate_id: str, phase: str) -> float:
    if isinstance(value, bool):
        raise TypeError("{} scorer returned a boolean for {}".format(phase, candidate_id))
    score = float(value)
    if not math.isfinite(score):
        raise ValueError("{} scorer returned a non-finite score for {}".format(phase, candidate_id))
    return score


def _normalize_sessions(
    observed: Sequence[date],
    session_calendar: Optional[Sequence[Union[date, datetime]]],
    trading_calendar: Optional[TradingCalendar],
) -> Tuple[date, ...]:
    if session_calendar is not None and trading_calendar is not None:
        raise WalkForwardValidationError(
            "session_calendar and trading_calendar are mutually exclusive"
        )
    first, last = observed[0], observed[-1]
    if trading_calendar is not None:
        try:
            values = tuple(trading_calendar.sessions_between(first, last))
        except Exception as exc:
            raise WalkForwardValidationError(
                "trading_calendar cannot provide observed range: {}".format(type(exc).__name__)
            ) from exc
    elif session_calendar is not None:
        values = tuple(_session_date(item, "session_calendar") for item in session_calendar)
        if any(left >= right for left, right in zip(values, values[1:])):
            raise WalkForwardValidationError(
                "session_calendar must be strictly increasing and unique"
            )
    else:
        values = tuple(observed)
    session_set = set(values)
    missing = [item for item in observed if item not in session_set]
    if missing:
        raise WalkForwardValidationError(
            "session calendar omits observed bar session {}".format(missing[0].isoformat())
        )
    bounded = tuple(item for item in values if first <= item <= last)
    if not bounded:
        raise WalkForwardValidationError("session calendar has no observed-range sessions")
    return bounded


def _observed_bar_sessions(bars_by_symbol: Mapping[str, Sequence[Bar]]) -> Tuple[date, ...]:
    observed = set()
    for raw_symbol, bars in dict(bars_by_symbol or {}).items():
        symbol = normalize_symbol(raw_symbol)
        if not isinstance(bars, Sequence) or isinstance(bars, (str, bytes)):
            raise TypeError("bars for {} must be a sequence".format(symbol))
        for bar in bars:
            if not isinstance(bar, Bar):
                raise TypeError("bars must contain Bar instances")
            if bar.symbol != symbol:
                raise BacktestValidationError("bar symbol does not match mapping key")
            observed.add(bar.timestamp.date())
    if not observed:
        raise WalkForwardValidationError("at least one daily bar is required")
    return tuple(sorted(observed))


def _make_windows(sessions: Tuple[date, ...], config: WalkForwardConfig) -> Tuple[WalkForwardWindow, ...]:
    required = config.train_sessions + config.validation_sessions + config.test_sessions
    if len(sessions) < required:
        raise WalkForwardValidationError(
            "{} sessions are required but only {} are available".format(required, len(sessions))
        )
    windows: List[WalkForwardWindow] = []
    start = 0
    fold_index = 1
    while start + required <= len(sessions):
        train_start = 0 if config.expanding_train else start
        train_end = start + config.train_sessions
        validation_end = train_end + config.validation_sessions
        test_end = validation_end + config.test_sessions
        windows.append(
            WalkForwardWindow(
                fold_index=fold_index,
                train_sessions=sessions[train_start:train_end],
                validation_sessions=sessions[train_end:validation_end],
                test_sessions=sessions[validation_end:test_end],
            )
        )
        if config.max_folds is not None and len(windows) >= config.max_folds:
            break
        start += config.step_sessions
        fold_index += 1
    if not windows:
        raise WalkForwardValidationError("no complete walk-forward window can be formed")
    return tuple(windows)


def _slice_bars(
    bars_by_symbol: Mapping[str, Sequence[Bar]], sessions: Sequence[date]
) -> Dict[str, Tuple[Bar, ...]]:
    allowed = set(sessions)
    sliced: Dict[str, Tuple[Bar, ...]] = {}
    for raw_symbol, bars in dict(bars_by_symbol or {}).items():
        symbol = normalize_symbol(raw_symbol)
        values = tuple(item for item in bars if item.timestamp.date() in allowed)
        if values:
            sliced[symbol] = values
    if not sliced:
        raise WalkForwardValidationError("split contains no bars")
    return sliced


def _slice_bars_with_context(
    bars_by_symbol: Mapping[str, Sequence[Bar]], sessions: Sequence[date]
) -> Dict[str, Tuple[Bar, ...]]:
    """Return phase bars plus strictly prior observations as warm-up context.

    A walk-forward split must prevent test-period execution and scoring from
    seeing future bars, but it must not reset rolling indicators at every
    boundary.  The previous implementation passed only the phase window to
    ``BacktestEngine``; a 20-day moving average therefore emitted no signal
    at the start of validation/test (and stateful strategies silently changed
    behaviour).  ``BacktestEngine`` accepts leading bars outside an explicit
    session calendar as context while still producing equity only for the
    requested phase, so include all history up to the phase end here.
    """

    allowed = set(sessions)
    if not allowed:
        raise WalkForwardValidationError("split cannot have an empty session set")
    end = max(allowed)
    sliced: Dict[str, Tuple[Bar, ...]] = {}
    for raw_symbol, bars in dict(bars_by_symbol or {}).items():
        symbol = normalize_symbol(raw_symbol)
        values = tuple(
            item for item in bars
            if item.timestamp.date() <= end
        )
        if any(item.timestamp.date() in allowed for item in values):
            sliced[symbol] = values
    if not sliced:
        raise WalkForwardValidationError("split contains no bars")
    return sliced


def _slice_constraints(
    constraints: Iterable[TradingConstraint], sessions: Sequence[date], symbols: Iterable[str]
) -> Tuple[TradingConstraint, ...]:
    allowed_sessions = set(sessions)
    allowed_symbols = set(symbols)
    return tuple(
        item
        for item in constraints
        if item.session_date in allowed_sessions and item.symbol in allowed_symbols
    )


def run_walk_forward(
    bars_by_symbol: Mapping[str, Sequence[Bar]],
    candidate_factories: Mapping[str, StrategyFactory],
    *,
    config: Optional[WalkForwardConfig] = None,
    backtest_config: Optional[BacktestConfig] = None,
    trading_constraints: Iterable[TradingConstraint] = (),
    session_calendar: Optional[Sequence[Union[date, datetime]]] = None,
    trading_calendar: Optional[TradingCalendar] = None,
    scorer: Optional[ScoreFunction] = None,
) -> WalkForwardReport:
    """Run strict train/validation/test folds using fresh strategy instances.

    Candidate factories are called once for each phase that reaches them.
    All train and validation attempts are retained, including failures, so a
    report cannot hide a failed experiment.  Only candidates with successful
    train and validation results and (when configured) a sufficient training
    score can be selected.  The selected candidate is instantiated only after
    validation selection and then run exactly once on the test window.
    """

    policy = config or WalkForwardConfig()
    if not isinstance(policy, WalkForwardConfig):
        raise TypeError("config must be a WalkForwardConfig")
    if not isinstance(candidate_factories, Mapping) or not candidate_factories:
        raise WalkForwardValidationError("candidate_factories must be a non-empty mapping")
    factories: Dict[str, StrategyFactory] = {}
    for raw_id, factory in sorted(dict(candidate_factories).items(), key=lambda item: str(item[0])):
        candidate_id = str(raw_id).strip()
        if not candidate_id:
            raise WalkForwardValidationError("candidate IDs cannot be empty")
        if candidate_id in factories:
            raise WalkForwardValidationError("candidate IDs must be unique")
        if not callable(factory):
            raise TypeError("candidate factory {} must be callable".format(candidate_id))
        factories[candidate_id] = factory
    observed = _observed_bar_sessions(bars_by_symbol)
    sessions = _normalize_sessions(observed, session_calendar, trading_calendar)
    windows = _make_windows(sessions, policy)
    constraint_values = tuple(trading_constraints)
    if any(not isinstance(item, TradingConstraint) for item in constraint_values):
        raise TypeError("trading_constraints must contain TradingConstraint instances")
    engine_config = backtest_config or BacktestConfig()
    if not isinstance(engine_config, BacktestConfig):
        raise TypeError("backtest_config must be a BacktestConfig")

    score_fn = scorer or (lambda result: _default_score(result, policy.selection_metric))
    fold_reports: List[WalkForwardFoldReport] = []
    report_warnings: List[str] = []
    for window in windows:
        phase_bars = {
            "train": _slice_bars_with_context(bars_by_symbol, window.train_sessions),
            "validation": _slice_bars_with_context(
                bars_by_symbol, window.validation_sessions
            ),
            "test": _slice_bars_with_context(bars_by_symbol, window.test_sessions),
        }
        phase_constraints = {
            phase: _slice_constraints(constraint_values, values, bars.keys())
            for phase, (values, bars) in {
                "train": (window.train_sessions, phase_bars["train"]),
                "validation": (window.validation_sessions, phase_bars["validation"]),
                "test": (window.test_sessions, phase_bars["test"]),
            }.items()
        }
        candidate_reports: List[WalkForwardCandidateReport] = []
        eligible: List[WalkForwardCandidateReport] = []
        for candidate_id, factory in factories.items():
            train_result: Optional[BacktestResult] = None
            validation_result: Optional[BacktestResult] = None
            train_score: Optional[float] = None
            validation_score: Optional[float] = None
            error: Optional[str] = None
            try:
                strategy = factory()
                if not isinstance(strategy, (Strategy, CohortStrategy)):
                    raise TypeError("factory did not return a Strategy or CohortStrategy")
                train_result = BacktestEngine(strategy, engine_config).run(
                    phase_bars["train"],
                    trading_constraints=phase_constraints["train"],
                    session_calendar=window.train_sessions,
                )
                train_score = _validate_score(score_fn(train_result), candidate_id, "train")
                if policy.minimum_train_score is not None and train_score < policy.minimum_train_score:
                    error = "train score below minimum_train_score"
                else:
                    strategy = factory()
                    if not isinstance(strategy, (Strategy, CohortStrategy)):
                        raise TypeError("factory did not return a Strategy or CohortStrategy")
                    validation_result = BacktestEngine(strategy, engine_config).run(
                        phase_bars["validation"],
                        trading_constraints=phase_constraints["validation"],
                        session_calendar=window.validation_sessions,
                    )
                    validation_score = _validate_score(
                        score_fn(validation_result), candidate_id, "validation"
                    )
            except Exception as exc:
                error = "{}: {}".format(type(exc).__name__, str(exc))
            candidate = WalkForwardCandidateReport(
                candidate_id=candidate_id,
                train_score=train_score,
                validation_score=validation_score,
                train_result=train_result,
                validation_result=validation_result,
                selection_eligible=(
                    train_result is not None
                    and validation_result is not None
                    and train_score is not None
                    and validation_score is not None
                    and error is None
                ),
                error=error,
            )
            candidate_reports.append(candidate)
            if candidate.selection_eligible:
                eligible.append(candidate)
        if not eligible:
            raise WalkForwardValidationError(
                "fold {} has no eligible candidate; all train/validation attempts failed or were filtered".format(
                    window.fold_index
                )
            )
        # Validation is the primary selection criterion; training score is
        # only a deterministic tie-breaker.  The test phase starts afterwards.
        selected = max(
            eligible,
            key=lambda item: (
                float(item.validation_score),
                float(item.train_score),
                item.candidate_id,
            ),
        )
        try:
            strategy = factories[selected.candidate_id]()
            if not isinstance(strategy, (Strategy, CohortStrategy)):
                raise TypeError("factory did not return a Strategy or CohortStrategy")
            test_result = BacktestEngine(strategy, engine_config).run(
                phase_bars["test"],
                trading_constraints=phase_constraints["test"],
                session_calendar=window.test_sessions,
            )
        except Exception as exc:
            raise WalkForwardValidationError(
                "fold {} selected candidate {} failed test: {}: {}".format(
                    window.fold_index, selected.candidate_id, type(exc).__name__, str(exc)
                )
            ) from exc
        warnings: List[str] = []
        failed = [item for item in candidate_reports if not item.selection_eligible]
        if failed:
            warnings.append(
                "{} candidate experiment(s) failed or were filtered in fold {}".format(
                    len(failed), window.fold_index
                )
            )
        fold_reports.append(
            WalkForwardFoldReport(
                window=window,
                candidates=tuple(candidate_reports),
                selected_candidate_id=selected.candidate_id,
                test_result=test_result,
                warnings=tuple(warnings),
            )
        )

    test_results = [item.test_result for item in fold_reports]
    returns = [float(item.metrics.total_return) for item in test_results]
    if any(left.test_sessions[-1] >= right.test_sessions[0] for left, right in zip(
        (item.window for item in fold_reports), (item.window for item in fold_reports[1:])
    )):
        report_warnings.append("test windows overlap; compounded return is descriptive, not additive")
    compounded = 1.0
    for value in returns:
        compounded *= 1.0 + value
    summary = WalkForwardSummary(
        fold_count=len(test_results),
        test_session_count=sum(item.metrics.session_count for item in test_results),
        compounded_test_return=compounded - 1.0,
        mean_test_return=sum(returns) / len(returns),
        median_test_return=float(median(returns)),
        worst_test_return=min(returns),
        best_test_return=max(returns),
        positive_fold_ratio=sum(value > 0 for value in returns) / len(returns),
        mean_test_sharpe=sum(float(item.metrics.sharpe_ratio) for item in test_results)
        / len(test_results),
        warnings=tuple(report_warnings),
    )
    return WalkForwardReport(
        config=policy,
        folds=tuple(fold_reports),
        summary=summary,
        warnings=tuple(report_warnings),
    )


__all__ = [
    "ScoreFunction",
    "StrategyFactory",
    "WalkForwardCandidateReport",
    "WalkForwardConfig",
    "WalkForwardFoldReport",
    "WalkForwardReport",
    "WalkForwardSummary",
    "WalkForwardValidationError",
    "WalkForwardWindow",
    "run_walk_forward",
]
