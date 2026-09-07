"""Example moving-average crossover strategy."""

from __future__ import annotations

from typing import Sequence

from quant_monitor.domain import Signal, SignalAction
from quant_monitor.services.indicators import IndicatorCalculator

from .base import Strategy, StrategyContext


class MovingAverageCrossStrategy(Strategy):
    """Emit a signal only when the fast SMA crosses the slow SMA.

    This is intentionally an educational example, not investment advice or a
    production-ready trading strategy.  No order is submitted by the platform.
    """

    def __init__(
        self,
        fast_window: int = 5,
        slow_window: int = 20,
        interval: str = "1m",
        strategy_name: str = "moving_average_cross",
    ) -> None:
        self.fast_window = int(fast_window)
        self.slow_window = int(slow_window)
        if self.fast_window <= 0 or self.slow_window <= 0:
            raise ValueError("moving-average windows must be positive")
        if self.fast_window >= self.slow_window:
            raise ValueError("fast_window must be smaller than slow_window")
        self.interval = str(interval).strip().lower()
        if not self.interval:
            raise ValueError("strategy interval cannot be empty")
        self._name = str(strategy_name).strip()
        if not self._name:
            raise ValueError("strategy_name cannot be empty")

    @property
    def name(self) -> str:
        return self._name

    def evaluate(self, context: StrategyContext) -> Sequence[Signal]:
        bars = [bar for bar in context.bars if bar.interval == self.interval]
        if len(bars) < self.slow_window + 1:
            return ()
        closes = [bar.close for bar in bars]
        previous_fast = IndicatorCalculator.sma(closes[:-1], self.fast_window)
        previous_slow = IndicatorCalculator.sma(closes[:-1], self.slow_window)
        current_fast = IndicatorCalculator.sma(closes, self.fast_window)
        current_slow = IndicatorCalculator.sma(closes, self.slow_window)
        if None in (previous_fast, previous_slow, current_fast, current_slow):
            return ()

        action = None
        if previous_fast <= previous_slow and current_fast > current_slow:
            action = SignalAction.BUY
        elif previous_fast >= previous_slow and current_fast < current_slow:
            action = SignalAction.SELL
        if action is None:
            return ()

        denominator = abs(current_slow) if current_slow else 1.0
        strength = min(1.0, abs(current_fast - current_slow) / denominator * 100.0)
        latest = bars[-1]
        reason = "SMA({}) {:.4f} crossed {} SMA({}) {:.4f}".format(
            self.fast_window,
            current_fast,
            "above" if action == SignalAction.BUY else "below",
            self.slow_window,
            current_slow,
        )
        return (
            Signal(
                strategy=self.name,
                symbol=context.symbol,
                timestamp=latest.timestamp,
                action=action,
                strength=strength,
                price=latest.close,
                reason=reason,
                metadata={
                    "fast_window": self.fast_window,
                    "slow_window": self.slow_window,
                    "fast_sma": current_fast,
                    "slow_sma": current_slow,
                    "interval": self.interval,
                },
            ),
        )
