from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quant_monitor.backtesting import (
    BacktestConfig,
    WalkForwardConfig,
    WalkForwardValidationError,
    run_walk_forward,
)
from quant_monitor.domain import Bar, SHANGHAI_TZ
from quant_monitor.strategies import Strategy


class HoldStrategy(Strategy):
    @property
    def name(self):
        return "hold"

    def evaluate(self, context):
        return ()


class BrokenFactory:
    def __call__(self):
        raise RuntimeError("fixture failure")


class BuyAfterWarmup(Strategy):
    """Emit once only after seeing three observations in the strategy context."""

    def __init__(self):
        self._emitted = False

    @property
    def name(self):
        return "buy_after_warmup"

    def evaluate(self, context):
        if self._emitted or len(context.bars) < 3:
            return ()
        self._emitted = True
        from quant_monitor.domain import Signal, SignalAction

        return (
            Signal(
                strategy=self.name,
                symbol=context.symbol,
                timestamp=context.timestamp,
                action=SignalAction.BUY,
                strength=1.0,
            ),
        )


def _bars(count=20):
    start = datetime(2025, 1, 1, 15, tzinfo=SHANGHAI_TZ)
    return [
        Bar(
            symbol="600000.SH",
            interval="1d",
            timestamp=start + timedelta(days=index),
            open=Decimal("10"),
            high=Decimal("10"),
            low=Decimal("10"),
            close=Decimal("10"),
            volume=100000,
            turnover=Decimal("1000000"),
            source="fixture",
        )
        for index in range(count)
    ]


def test_walk_forward_keeps_test_window_out_of_selection_and_records_failures():
    scorer_timestamps = []

    def scorer(result):
        scorer_timestamps.append(result.ended_at)
        return result.metrics.total_return

    report = run_walk_forward(
        {"600000.SH": _bars()},
        {"broken": BrokenFactory(), "hold": HoldStrategy},
        config=WalkForwardConfig(
            train_sessions=5,
            validation_sessions=3,
            test_sessions=3,
            step_sessions=3,
            max_folds=2,
        ),
        backtest_config=BacktestConfig(initial_cash=Decimal("10000")),
        scorer=scorer,
    )
    assert len(report.folds) == 2
    assert all(fold.selected_candidate_id == "hold" for fold in report.folds)
    assert all(
        max(item.window.validation_sessions[-1] for item in report.folds)
        >= timestamp.date()
        for timestamp in scorer_timestamps
    )
    assert all(
        candidate.error is not None
        for candidate in report.folds[0].candidates
        if candidate.candidate_id == "broken"
    )
    assert report.to_dict()["test_used_for_selection"] is False


def test_walk_forward_requires_complete_chronological_window():
    with pytest.raises(WalkForwardValidationError, match="required"):
        run_walk_forward(
            {"600000.SH": _bars(10)},
            {"hold": HoldStrategy},
            config=WalkForwardConfig(
                train_sessions=5,
                validation_sessions=3,
                test_sessions=3,
            ),
        )


def test_walk_forward_preserves_pre_window_bars_as_indicator_warmup_context():
    """Validation/test phases must not reset rolling indicator history."""

    report = run_walk_forward(
        {"600000.SH": _bars(6)},
        {"warmup": BuyAfterWarmup},
        config=WalkForwardConfig(
            train_sessions=2,
            validation_sessions=2,
            test_sessions=2,
            max_folds=1,
        ),
        backtest_config=BacktestConfig(initial_cash=Decimal("10000")),
    )
    fold = report.folds[0]
    candidate = fold.candidates[0]
    assert candidate.validation_result is not None
    assert candidate.validation_result.trades
    assert fold.test_result.trades
    # The bars before each phase are context only: no equity point leaks into
    # the phase result, while the first phase evaluation can still warm up.
    assert len(candidate.validation_result.equity_curve) == 2
    assert len(fold.test_result.equity_curve) == 2
