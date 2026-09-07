"""Dependency-free technical indicator calculations."""

from __future__ import annotations

import math
import statistics
from datetime import datetime
from decimal import Decimal
from typing import Iterable, List, Optional, Sequence, Tuple, Union

from quant_monitor.domain import Bar, Metric, ensure_market_time


Number = Union[int, float, Decimal]


def _numbers(values: Iterable[Number]) -> List[float]:
    result = [float(value) for value in values]
    if any(not math.isfinite(value) for value in result):
        raise ValueError("indicator input values must be finite")
    return result


def _window(value: int) -> int:
    normalized = int(value)
    if normalized <= 0:
        raise ValueError("indicator window must be positive")
    return normalized


class IndicatorCalculator:
    """Pure calculations usable by live monitoring and future backtests."""

    @staticmethod
    def sma(values: Sequence[Number], window: int) -> Optional[float]:
        normalized = _window(window)
        series = _numbers(values)
        if len(series) < normalized:
            return None
        return statistics.fmean(series[-normalized:])

    @staticmethod
    def sma_series(values: Sequence[Number], window: int) -> List[Optional[float]]:
        normalized = _window(window)
        series = _numbers(values)
        output: List[Optional[float]] = [None] * len(series)
        running = 0.0
        for index, value in enumerate(series):
            running += value
            if index >= normalized:
                running -= series[index - normalized]
            if index >= normalized - 1:
                output[index] = running / normalized
        return output

    @staticmethod
    def ema(values: Sequence[Number], window: int) -> Optional[float]:
        normalized = _window(window)
        series = _numbers(values)
        if len(series) < normalized:
            return None
        initial = statistics.fmean(series[:normalized])
        multiplier = 2.0 / (normalized + 1.0)
        current = initial
        for value in series[normalized:]:
            current = (value - current) * multiplier + current
        return current

    @staticmethod
    def rsi(values: Sequence[Number], window: int = 14) -> Optional[float]:
        normalized = _window(window)
        series = _numbers(values)
        if len(series) <= normalized:
            return None
        changes = [right - left for left, right in zip(series, series[1:])]
        gains = [max(change, 0.0) for change in changes]
        losses = [max(-change, 0.0) for change in changes]
        average_gain = statistics.fmean(gains[:normalized])
        average_loss = statistics.fmean(losses[:normalized])
        for gain, loss in zip(gains[normalized:], losses[normalized:]):
            average_gain = (average_gain * (normalized - 1) + gain) / normalized
            average_loss = (average_loss * (normalized - 1) + loss) / normalized
        if average_loss == 0.0:
            return 100.0 if average_gain > 0.0 else 50.0
        relative_strength = average_gain / average_loss
        return 100.0 - (100.0 / (1.0 + relative_strength))

    @staticmethod
    def returns(values: Sequence[Number]) -> List[float]:
        series = _numbers(values)
        result: List[float] = []
        for previous, current in zip(series, series[1:]):
            if previous == 0:
                raise ValueError("cannot calculate a return from a zero price")
            result.append(current / previous - 1.0)
        return result

    @staticmethod
    def volatility(
        values: Sequence[Number],
        window: int = 20,
        annualization: Optional[float] = None,
    ) -> Optional[float]:
        normalized = _window(window)
        returns = IndicatorCalculator.returns(values)
        if len(returns) < normalized:
            return None
        sample = returns[-normalized:]
        value = statistics.stdev(sample) if len(sample) > 1 else 0.0
        if annualization is not None:
            if float(annualization) <= 0:
                raise ValueError("annualization must be positive")
            value *= math.sqrt(float(annualization))
        return value

    @staticmethod
    def change_percent(values: Sequence[Number], periods: int = 1) -> Optional[float]:
        normalized = _window(periods)
        series = _numbers(values)
        if len(series) <= normalized:
            return None
        previous = series[-normalized - 1]
        if previous == 0:
            return None
        return (series[-1] / previous - 1.0) * 100.0

    @classmethod
    def metrics_from_bars(
        cls,
        bars: Sequence[Bar],
        sma_windows: Sequence[int] = (5, 10, 20),
        ema_windows: Sequence[int] = (12, 26),
        rsi_window: int = 14,
        volatility_window: int = 20,
    ) -> List[Metric]:
        """Calculate the latest ready metrics for one symbol and interval."""

        if not bars:
            return []
        ordered = sorted(bars, key=lambda item: item.timestamp)
        symbol = ordered[0].symbol
        interval = ordered[0].interval
        if any(bar.symbol != symbol or bar.interval != interval for bar in ordered):
            raise ValueError("metrics_from_bars requires one symbol and one interval")
        closes = [bar.close for bar in ordered]
        timestamp = ordered[-1].timestamp
        common = {"interval": interval}
        metrics: List[Metric] = []
        for window in sma_windows:
            value = cls.sma(closes, window)
            if value is not None:
                metrics.append(
                    Metric(symbol, "sma_{}".format(int(window)), timestamp, value, int(window), common)
                )
        for window in ema_windows:
            value = cls.ema(closes, window)
            if value is not None:
                metrics.append(
                    Metric(symbol, "ema_{}".format(int(window)), timestamp, value, int(window), common)
                )
        rsi_value = cls.rsi(closes, rsi_window)
        if rsi_value is not None:
            metrics.append(
                Metric(symbol, "rsi_{}".format(int(rsi_window)), timestamp, rsi_value, int(rsi_window), common)
            )
        volatility = cls.volatility(closes, volatility_window)
        if volatility is not None:
            metrics.append(
                Metric(
                    symbol,
                    "volatility_{}".format(int(volatility_window)),
                    timestamp,
                    volatility,
                    int(volatility_window),
                    common,
                )
            )
        change = cls.change_percent(closes)
        if change is not None:
            metrics.append(Metric(symbol, "change_pct_1", timestamp, change, 1, common))
        return metrics
