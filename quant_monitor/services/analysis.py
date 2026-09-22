"""Explainable, signal-only quantitative analysis for dashboard consumers.

The analysis layer deliberately has no knowledge of providers, databases or
orders.  It consumes normalized :class:`~quant_monitor.domain.Bar` and
:class:`~quant_monitor.domain.Quote` objects and returns a small, stable
snapshot that can be rendered directly by the local dashboard.  Missing bars
are represented by ``None`` and an explicit ``data_quality`` status instead of
being replaced by zeroes.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from quant_monitor.domain import Bar, Quote, ensure_market_time, normalize_symbol, now_market_time
from quant_monitor.market import expected_latest_daily_date
from quant_monitor.trading_calendar import (
    CalendarRangeError,
    DEFAULT_TRADING_CALENDAR,
    TradingCalendar,
)

from .indicators import IndicatorCalculator


def _window(value: int, name: str) -> int:
    normalized = int(value)
    if normalized <= 0:
        raise ValueError("{} must be positive".format(name))
    return normalized


def _finite(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    normalized = float(value)
    return normalized if math.isfinite(normalized) else None


def _decimal_or_none(value: Optional[Any]) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("price must be a valid decimal")
    if not result.is_finite() or result < 0:
        raise ValueError("price must be a finite non-negative decimal")
    return result


@dataclass(frozen=True)
class AnalysisSnapshot:
    """Latest explainable analysis for one symbol.

    ``trend``, ``momentum`` and ``risk`` are plain mappings so the API contract
    can evolve without adding provider-specific fields to the domain models.
    ``recommendation`` is a research bias only; it is never an order or an
    instruction to trade.
    """

    symbol: str
    name: str
    price: Optional[Decimal]
    change_pct: Optional[float]
    indicators: Mapping[str, Optional[float]]
    trend: Mapping[str, Any]
    momentum: Mapping[str, Any]
    risk: Mapping[str, Any]
    recommendation: Mapping[str, Any]
    timestamp: datetime
    interval: str
    bars_used: int
    source: str = "unknown"
    stale: bool = False
    data_quality: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        name = str(self.name).strip()
        object.__setattr__(self, "name", name or self.symbol)
        object.__setattr__(self, "price", _decimal_or_none(self.price))
        object.__setattr__(self, "change_pct", _finite(self.change_pct))
        object.__setattr__(self, "timestamp", ensure_market_time(self.timestamp))
        interval = str(self.interval).strip().lower()
        object.__setattr__(self, "interval", interval or "unknown")
        bars_used = int(self.bars_used)
        if bars_used < 0:
            raise ValueError("bars_used cannot be negative")
        object.__setattr__(self, "bars_used", bars_used)
        object.__setattr__(self, "source", str(self.source or "unknown"))
        object.__setattr__(self, "stale", bool(self.stale))
        # Copy mappings to prevent accidental mutation after a response has
        # been published to a WebSocket client.
        object.__setattr__(self, "indicators", dict(self.indicators or {}))
        object.__setattr__(self, "trend", dict(self.trend or {}))
        object.__setattr__(self, "momentum", dict(self.momentum or {}))
        object.__setattr__(self, "risk", dict(self.risk or {}))
        object.__setattr__(self, "recommendation", dict(self.recommendation or {}))
        object.__setattr__(self, "data_quality", dict(self.data_quality or {}))


class QuantitativeAnalysisService:
    """Calculate a conservative first-pass analysis from normalized market data.

    The defaults are intentionally conventional and transparent:

    * 5/20-period SMA and 12/26-period EMA for trend;
    * 14-period RSI and 5/20-period returns for momentum;
    * 20-period standard-deviation volatility and a 60-period maximum
      drawdown for risk.

    No indicator is emitted as ``0`` when its lookback is not ready.  The
    ``data_quality`` map identifies the required lookback and missing fields.
    """

    def __init__(
        self,
        sma_windows: Sequence[int] = (5, 20),
        ema_windows: Sequence[int] = (12, 26),
        rsi_window: int = 14,
        volatility_window: int = 20,
        drawdown_window: int = 60,
        stale_after_seconds: Optional[float] = None,
        *,
        calendar: Optional[TradingCalendar] = None,
    ) -> None:
        self.sma_windows = tuple(_window(value, "sma window") for value in sma_windows)
        self.ema_windows = tuple(_window(value, "ema window") for value in ema_windows)
        if not self.sma_windows:
            raise ValueError("sma_windows cannot be empty")
        if not self.ema_windows:
            raise ValueError("ema_windows cannot be empty")
        self.rsi_window = _window(rsi_window, "rsi window")
        self.volatility_window = _window(volatility_window, "volatility window")
        self.drawdown_window = _window(drawdown_window, "drawdown window")
        if stale_after_seconds is not None and float(stale_after_seconds) < 0:
            raise ValueError("stale_after_seconds cannot be negative")
        self.stale_after_seconds = (
            None if stale_after_seconds is None else float(stale_after_seconds)
        )
        if calendar is not None and not isinstance(calendar, TradingCalendar):
            raise TypeError("calendar must implement TradingCalendar")
        self.calendar = calendar if calendar is not None else DEFAULT_TRADING_CALENDAR

    @property
    def required_bars(self) -> int:
        """Minimum closes needed for all default indicators to be ready."""

        return max(
            max(self.sma_windows, default=1),
            max(self.ema_windows, default=1),
            self.rsi_window + 1,
            self.volatility_window + 1,
            self.drawdown_window,
            2,
        )

    @staticmethod
    def _annualization(interval: str) -> float:
        normalized = str(interval).strip().lower()
        if normalized == "1d":
            return 252.0
        if normalized == "1w":
            return 52.0
        if normalized in {"1m", "5m", "15m", "30m", "60m", "1h"}:
            minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60, "1h": 60}[normalized]
            return 252.0 * 240.0 / minutes
        return 1.0

    @staticmethod
    def _max_drawdown(values: Sequence[float]) -> Optional[float]:
        if not values:
            return None
        peak = values[0]
        drawdown = 0.0
        for value in values:
            peak = max(peak, value)
            if peak > 0:
                drawdown = min(drawdown, value / peak - 1.0)
        return drawdown

    @staticmethod
    def _trend_label(score: Optional[float]) -> str:
        if score is None:
            return "insufficient_data"
        if score >= 0.5:
            return "bullish"
        if score <= -0.5:
            return "bearish"
        return "neutral"

    @staticmethod
    def _momentum_label(score: Optional[float]) -> str:
        if score is None:
            return "insufficient_data"
        if score >= 0.5:
            return "strong_positive"
        if score > 0.1:
            return "positive"
        if score <= -0.5:
            return "strong_negative"
        if score < -0.1:
            return "negative"
        return "neutral"

    @staticmethod
    def _risk_label(score: Optional[float]) -> str:
        if score is None:
            return "insufficient_data"
        if score >= 0.67:
            return "high"
        if score >= 0.34:
            return "medium"
        return "low"

    @staticmethod
    def _trend_description(label: str) -> str:
        return {
            "bullish": "价格位于中期均线上方且短期趋势偏强",
            "bearish": "价格位于中期均线下方且短期趋势偏弱",
            "neutral": "均线方向未形成明确趋势",
            "insufficient_data": "历史数据不足，暂不判断趋势",
        }.get(label, label)

    @staticmethod
    def _momentum_description(label: str) -> str:
        return {
            "strong_positive": "近期收益率和 RSI 显示动能偏强",
            "positive": "近期收益率显示动能偏正",
            "neutral": "近期动能中性",
            "negative": "近期收益率显示动能偏负",
            "strong_negative": "近期收益率和 RSI 显示动能偏弱",
            "insufficient_data": "历史数据不足，暂不判断动能",
        }.get(label, label)

    @staticmethod
    def _risk_description(label: str) -> str:
        return {
            "low": "波动率和回撤处于较低水平",
            "medium": "波动率或回撤处于中等水平",
            "high": "波动率或回撤较高，需控制研究风险",
            "insufficient_data": "历史数据不足，暂不评估风险",
        }.get(label, label)

    def analyze(
        self,
        symbol: str,
        bars: Sequence[Bar],
        quote: Optional[Quote] = None,
        *,
        name: Optional[str] = None,
        interval: Optional[str] = None,
        now: Optional[datetime] = None,
        stale_after_seconds: Optional[float] = None,
    ) -> AnalysisSnapshot:
        """Return one snapshot, sorting bars and rejecting mixed symbols.

        ``quote`` is optional so historical analysis remains useful when a
        provider is temporarily unavailable.  If a quote is supplied, it is
        used for the displayed price and one-day change while bars drive all
        lookback indicators.
        """

        normalized_symbol = normalize_symbol(symbol)
        if quote is not None and quote.symbol != normalized_symbol:
            raise ValueError("analysis quote must match the requested symbol")
        ordered = sorted(tuple(bars or ()), key=lambda item: item.timestamp)
        if any(item.symbol != normalized_symbol for item in ordered):
            raise ValueError("analysis bars must match the requested symbol")
        intervals = {item.interval for item in ordered}
        if len(intervals) > 1:
            raise ValueError("analysis bars must use one interval")
        interval = next(iter(intervals), str(interval or "unknown").strip().lower())
        closes = [float(item.close) for item in ordered]
        latest_bar = ordered[-1] if ordered else None
        latest_close = closes[-1] if closes else None

        indicators: Dict[str, Optional[float]] = {}
        for window in self.sma_windows:
            indicators["sma_{}".format(window)] = _finite(
                IndicatorCalculator.sma(closes, window)
            )
        for window in self.ema_windows:
            indicators["ema_{}".format(window)] = _finite(
                IndicatorCalculator.ema(closes, window)
            )
        indicators["rsi_{}".format(self.rsi_window)] = _finite(
            IndicatorCalculator.rsi(closes, self.rsi_window)
        )
        try:
            raw_volatility = IndicatorCalculator.volatility(closes, self.volatility_window)
        except ValueError:
            # A zero/invalid prior close makes a return undefined.  Keep the
            # rest of the snapshot usable and report this indicator as null.
            raw_volatility = None
        indicators["volatility_{}".format(self.volatility_window)] = _finite(raw_volatility)
        annualized_volatility = (
            raw_volatility * math.sqrt(self._annualization(interval))
            if raw_volatility is not None
            else None
        )
        indicators["annualized_volatility_{}".format(self.volatility_window)] = _finite(
            annualized_volatility
        )
        try:
            return_5 = _finite(IndicatorCalculator.change_percent(closes, 5))
        except ValueError:
            return_5 = None
        try:
            return_20 = _finite(IndicatorCalculator.change_percent(closes, 20))
        except ValueError:
            return_20 = None
        indicators["return_5"] = return_5
        indicators["return_20"] = return_20
        drawdown_values = closes[-self.drawdown_window :]
        max_drawdown = (
            _finite(self._max_drawdown(drawdown_values))
            if len(closes) >= self.drawdown_window
            else None
        )
        indicators["max_drawdown"] = max_drawdown
        latest_volume = ordered[-1].volume if ordered else None
        volumes = [int(item.volume) for item in ordered[-self.volatility_window :]]
        average_volume = (
            statistics.fmean(volumes)
            if len(volumes) >= self.volatility_window
            else None
        )
        volume_ratio = (
            float(latest_volume) / average_volume
            if latest_volume is not None and average_volume and average_volume > 0
            else None
        )
        indicators["volume_ratio"] = _finite(volume_ratio)

        price = quote.last if quote is not None else (latest_bar.close if latest_bar else None)
        change_pct: Optional[float]
        if quote is not None and quote.previous_close is not None and quote.previous_close != 0:
            change_pct = float((quote.last / quote.previous_close - Decimal("1")) * Decimal("100"))
        else:
            try:
                change_pct = _finite(IndicatorCalculator.change_percent(closes, 1))
            except ValueError:
                change_pct = None

        sma_short = indicators.get("sma_{}".format(min(self.sma_windows)))
        sma_long = indicators.get("sma_{}".format(max(self.sma_windows)))
        ema_short = indicators.get("ema_{}".format(min(self.ema_windows)))
        ema_long = indicators.get("ema_{}".format(max(self.ema_windows)))
        trend_price = float(price) if price is not None else latest_close
        trend_components: List[float] = []
        if trend_price is not None and sma_long is not None:
            trend_components.append(
                1.0 if trend_price > sma_long else (-1.0 if trend_price < sma_long else 0.0)
            )
        if sma_short is not None and sma_long is not None:
            trend_components.append(
                1.0 if sma_short > sma_long else (-1.0 if sma_short < sma_long else 0.0)
            )
        if ema_short is not None and ema_long is not None:
            trend_components.append(
                1.0 if ema_short > ema_long else (-1.0 if ema_short < ema_long else 0.0)
            )
        trend_score = (
            max(-1.0, min(1.0, statistics.fmean(trend_components)))
            if trend_components
            else None
        )
        trend_label = self._trend_label(trend_score)

        momentum_components: List[float] = []
        if return_5 is not None:
            momentum_components.append(max(-1.0, min(1.0, return_5 / 5.0)))
        if return_20 is not None:
            momentum_components.append(max(-1.0, min(1.0, return_20 / 10.0)))
        rsi = indicators.get("rsi_{}".format(self.rsi_window))
        if rsi is not None:
            momentum_components.append(max(-1.0, min(1.0, (rsi - 50.0) / 25.0)))
        momentum_score = (
            max(-1.0, min(1.0, statistics.fmean(momentum_components)))
            if momentum_components
            else None
        )
        momentum_label = self._momentum_label(momentum_score)

        risk_components: List[float] = []
        if annualized_volatility is not None:
            if annualized_volatility >= 0.60:
                risk_components.append(1.0)
            elif annualized_volatility >= 0.30:
                risk_components.append(0.5)
            else:
                risk_components.append(0.0)
        if max_drawdown is not None:
            if max_drawdown <= -0.20:
                risk_components.append(1.0)
            elif max_drawdown <= -0.10:
                risk_components.append(0.5)
            else:
                risk_components.append(0.0)
        # A risk label is only meaningful when both volatility and drawdown
        # lookbacks are ready.  A partial score would make a short history
        # appear deceptively safe, so keep it null until both components exist.
        risk_score = (
            max(0.0, min(1.0, statistics.fmean(risk_components)))
            if len(risk_components) == 2
            else None
        )
        risk_label = self._risk_label(risk_score)

        if trend_score is None or momentum_score is None or risk_score is None:
            recommendation_action = "insufficient_data"
            recommendation_label = "数据不足"
        elif trend_score >= 0.5 and momentum_score > 0.1 and risk_score < 0.67:
            recommendation_action = "positive_bias"
            recommendation_label = "偏强观察"
        elif trend_score <= -0.5 and momentum_score < -0.1:
            recommendation_action = "negative_bias"
            recommendation_label = "偏弱观察"
        elif risk_score >= 0.67:
            recommendation_action = "risk_caution"
            recommendation_label = "风险谨慎"
        else:
            recommendation_action = "neutral"
            recommendation_label = "中性观察"

        timestamp = latest_bar.timestamp if latest_bar is not None else now_market_time()
        if quote is not None and quote.timestamp > timestamp:
            timestamp = quote.timestamp
        current_time = ensure_market_time(now or now_market_time())
        threshold = self.stale_after_seconds if stale_after_seconds is None else stale_after_seconds
        if threshold is not None and float(threshold) < 0:
            raise ValueError("stale_after_seconds cannot be negative")
        freshness_timestamps = []
        if quote is not None:
            freshness_timestamps.append(quote.timestamp)
        # Match runtime/serialization freshness semantics: a small positive
        # skew is expected while provider and local clocks cross a polling
        # boundary, whereas a timestamp more than five seconds ahead is an
        # actionable replay/clock anomaly.
        future_timestamp_tolerance_seconds = 5.0
        future_quote = bool(
            quote is not None
            and (quote.timestamp - current_time).total_seconds()
            > future_timestamp_tolerance_seconds
        )
        future_bar = bool(
            latest_bar is not None
            and (latest_bar.timestamp - current_time).total_seconds()
            > future_timestamp_tolerance_seconds
        )
        # A fresh quote must not hide stale research inputs.  Daily and weekly
        # series use a session-aware age allowance, while intraday bars keep
        # the caller's strict threshold.
        bar_stale = False
        expected_daily_date = None
        calendar_out_of_coverage = False
        if interval == "1d":
            try:
                expected_daily_date = expected_latest_daily_date(
                    current_time, calendar=self.calendar
                )
            except CalendarRangeError:
                # A finite snapshot cannot certify freshness outside its
                # coverage.  Do not quietly switch back to a weekday rule or
                # keep an apparently positive recommendation in that case.
                calendar_out_of_coverage = True
        if latest_bar is not None:
            if interval == "1d":
                bar_stale = bool(
                    expected_daily_date is not None
                    and latest_bar.timestamp.date() < expected_daily_date
                )
            elif interval == "1w":
                bar_age_limit = max(float(threshold or 0), 11 * 24 * 60 * 60)
                bar_stale = bool(
                    threshold is not None
                    and (current_time - latest_bar.timestamp).total_seconds()
                    > bar_age_limit
                )
            else:
                bar_age_limit = float(threshold or 0)
                bar_stale = bool(
                    threshold is not None
                    and (current_time - latest_bar.timestamp).total_seconds()
                    > bar_age_limit
                )
            if interval not in {"1d", "1w"}:
                freshness_timestamps.append(latest_bar.timestamp)
        stale = bool(
            bar_stale
            or calendar_out_of_coverage
            or future_quote
            or future_bar
            or (
                threshold is not None
                and freshness_timestamps
                and any(
                    (current_time - item).total_seconds() > float(threshold)
                    for item in freshness_timestamps
                )
            )
        )
        sources = []
        if quote is not None and quote.source:
            sources.append(quote.source)
        if latest_bar is not None and latest_bar.source and latest_bar.source not in sources:
            sources.append(latest_bar.source)
        source = "+".join(sources) if sources else "unknown"

        missing = [key for key, value in indicators.items() if value is None]
        quality_status = "ok" if len(ordered) >= self.required_bars else "insufficient"
        if future_quote or future_bar:
            quality_status = "future_timestamp"
        elif calendar_out_of_coverage:
            quality_status = "calendar_out_of_coverage"
        elif stale and quality_status == "ok":
            quality_status = "stale"
        if calendar_out_of_coverage:
            recommendation_action = "insufficient_data"
            recommendation_label = "交易日历覆盖不足"
        data_quality = {
            "status": quality_status,
            "bars_used": len(ordered),
            "bars_required": self.required_bars,
            "missing_indicators": missing,
            "stale": stale,
            "quote_stale": bool(
                threshold is not None
                and quote is not None
                and (current_time - quote.timestamp).total_seconds()
                > float(threshold)
            ),
            "future_timestamp": future_quote or future_bar,
            "future_quote": future_quote,
            "future_bar": future_bar,
            "bar_stale": bar_stale,
            "bar_as_of": latest_bar.timestamp.isoformat() if latest_bar else None,
        }
        if interval == "1d":
            data_quality.update({
                "expected_bar_date": (
                    expected_daily_date.isoformat() if expected_daily_date else None
                ),
                "calendar": self.calendar.metadata.identifier,
                "calendar_source": self.calendar.metadata.source,
                "calendar_version": self.calendar.metadata.version,
                "calendar_notice": self.calendar.metadata.notice,
                "calendar_fingerprint": getattr(self.calendar, "fingerprint", None),
                # Metadata and a reproducibility digest are not an exchange
                # attestation, including for structural Protocol instances.
                "calendar_verified": False,
                "calendar_out_of_coverage": calendar_out_of_coverage,
            })
        return AnalysisSnapshot(
            symbol=normalized_symbol,
            name=name or normalized_symbol,
            price=price,
            change_pct=change_pct,
            indicators=indicators,
            trend={
                "label": trend_label,
                "score": _finite(trend_score),
                "description": self._trend_description(trend_label),
            },
            momentum={
                "label": momentum_label,
                "score": _finite(momentum_score),
                "description": self._momentum_description(momentum_label),
            },
            risk={
                "label": risk_label,
                "score": _finite(risk_score),
                "description": self._risk_description(risk_label),
            },
            recommendation={
                "action": recommendation_action,
                "label": recommendation_label,
                "signal_only": True,
                "reason": [
                    self._trend_description(trend_label),
                    self._momentum_description(momentum_label),
                    self._risk_description(risk_label),
                ],
            },
            timestamp=timestamp,
            interval=interval,
            bars_used=len(ordered),
            source=source,
            stale=stale,
            data_quality=data_quality,
        )

    def analyze_many(
        self,
        items: Iterable[Tuple[str, Sequence[Bar], Optional[Quote]]],
        *,
        names: Optional[Mapping[str, str]] = None,
        interval: Optional[str] = None,
        now: Optional[datetime] = None,
        stale_after_seconds: Optional[float] = None,
    ) -> List[AnalysisSnapshot]:
        """Analyze a sequence while preserving its input order."""

        name_map = names or {}
        return [
            self.analyze(
                symbol,
                bars,
                quote,
                name=name_map.get(normalize_symbol(symbol)),
                interval=interval,
                now=now,
                stale_after_seconds=stale_after_seconds,
            )
            for symbol, bars, quote in items
        ]


__all__ = ["AnalysisSnapshot", "QuantitativeAnalysisService"]
