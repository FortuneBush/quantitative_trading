"""Deterministic, offline A-share market-data provider for development."""

from __future__ import annotations

import asyncio
import hashlib
import math
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Callable, Dict, List, Optional, Sequence

from quant_monitor.domain import (
    Bar,
    Exchange,
    Instrument,
    Quote,
    SecurityType,
    ensure_market_time,
    normalize_symbol,
)
from quant_monitor.market import expected_latest_daily_date
from quant_monitor.trading_calendar import DEFAULT_TRADING_CALENDAR

from .base import MarketDataProvider


_INTERVALS = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "60m": timedelta(hours=1),
    "1h": timedelta(hours=1),
    "1d": timedelta(days=1),
    "1w": timedelta(days=7),
}


class MockMarketDataProvider(MarketDataProvider):
    """Generate repeatable quotes and bars without network access.

    The same ``seed``, symbols and invocation order produce the same values.  By
    default time starts at a fixed market timestamp, making tests reproducible;
    pass a ``clock`` to use a controlled or real-time timestamp instead.
    """

    def __init__(
        self,
        seed: int = 20260812,
        base_time: Optional[datetime] = None,
        clock: Optional[Callable[[], datetime]] = None,
        latency_seconds: float = 0.0,
    ) -> None:
        from quant_monitor.domain import parse_market_time

        self._seed = int(seed)
        self._base_time = ensure_market_time(base_time) if base_time else parse_market_time(
            "2026-08-12T09:30:00+08:00"
        )
        self._clock = clock
        self._latency_seconds = max(0.0, float(latency_seconds))
        self._quote_ticks: Dict[str, int] = {}

    @property
    def name(self) -> str:
        return "mock"

    @staticmethod
    def _quantize(value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def _symbol_seed(self, symbol: str) -> int:
        digest = hashlib.sha256((str(self._seed) + ":" + symbol).encode("ascii")).digest()
        return int.from_bytes(digest[:8], "big")

    def _base_price(self, symbol: str) -> Decimal:
        raw = self._symbol_seed(symbol)
        # Broad enough to look realistic while keeping all generated prices > 0.
        return Decimal(500 + raw % 19501) / Decimal(100)

    def _price_at(self, symbol: str, tick: int) -> Decimal:
        raw = self._symbol_seed(symbol)
        phase = (raw % 6283) / 1000.0
        wave = math.sin(phase + tick * 0.37) * 0.012
        # A second, slower bounded wave gives the series trend-like movement
        # without an unbounded drift when callers request old/new timestamps.
        drift = math.sin(phase * 0.7 + tick * 0.013) * 0.003
        multiplier = Decimal(str(max(0.2, 1.0 + wave + drift)))
        return self._quantize(self._base_price(symbol) * multiplier)

    def _timestamp(self, tick: int) -> datetime:
        if self._clock is not None:
            return ensure_market_time(self._clock())
        return self._base_time + timedelta(seconds=tick * 3)

    async def fetch_quotes(self, symbols: Sequence[str]) -> List[Quote]:
        if self._latency_seconds:
            await asyncio.sleep(self._latency_seconds)
        quotes: List[Quote] = []
        for raw_symbol in symbols:
            symbol = normalize_symbol(raw_symbol)
            tick = self._quote_ticks.get(symbol, 0)
            self._quote_ticks[symbol] = tick + 1
            last = self._price_at(symbol, tick)
            previous_close = self._quantize(self._base_price(symbol))
            high = max(last, previous_close)
            low = min(last, previous_close)
            spread = max(Decimal("0.01"), self._quantize(last * Decimal("0.0002")))
            volume = 100_000 + self._symbol_seed(symbol) % 900_000 + tick * 10_000
            quotes.append(
                Quote(
                    symbol=symbol,
                    timestamp=self._timestamp(tick),
                    last=last,
                    open=previous_close,
                    high=high,
                    low=low,
                    previous_close=previous_close,
                    bid=max(Decimal("0.01"), last - spread),
                    ask=last + spread,
                    bid_size=1000 + (tick % 20) * 100,
                    ask_size=1200 + (tick % 15) * 100,
                    volume=volume,
                    turnover=self._quantize(last * volume),
                    source=self.name,
                )
            )
        return quotes

    async def fetch_bars(
        self,
        symbol: str,
        interval: str = "1m",
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 200,
    ) -> List[Bar]:
        if self._latency_seconds:
            await asyncio.sleep(self._latency_seconds)
        symbol = normalize_symbol(symbol)
        normalized_interval = str(interval).strip().lower()
        if normalized_interval not in _INTERVALS:
            raise ValueError("unsupported mock interval: {}".format(interval))
        if int(limit) <= 0:
            raise ValueError("limit must be positive")
        step = _INTERVALS[normalized_interval]
        requested_end = ensure_market_time(end) if end is not None else self._timestamp(
            self._quote_ticks.get(symbol, 0)
        )
        step_seconds = int(step.total_seconds())
        start_time = ensure_market_time(start) if start is not None else None

        # A daily bar represents a completed market session. Generate only
        # fallback-calendar sessions and timestamp them at the project's
        # conservative 15:30 availability boundary so offline research cannot
        # accidentally learn from a close before it exists.
        if normalized_interval == "1d":
            latest_session = expected_latest_daily_date(
                requested_end, calendar=DEFAULT_TRADING_CALENDAR
            )
            timestamps = [
                requested_end.replace(
                    year=session.year,
                    month=session.month,
                    day=session.day,
                    hour=15,
                    minute=30,
                    second=0,
                    microsecond=0,
                )
                for session in DEFAULT_TRADING_CALENDAR.sessions_ending_on(
                    latest_session, int(limit)
                )
            ]
            if start_time is not None:
                timestamps = [item for item in timestamps if item >= start_time]
        elif normalized_interval == "1w":
            end_time = requested_end
            end_time = end_time.replace(hour=0, minute=0, second=0, microsecond=0)
            end_time -= timedelta(days=end_time.weekday())
            if start_time is not None and start_time > end_time:
                return []
            candidate_count = int(limit)
            if start_time is not None:
                span_count = int((end_time - start_time) // step) + 1
                candidate_count = min(candidate_count, span_count)
            first_time = end_time - step * (candidate_count - 1)
            if start_time is not None and first_time < start_time:
                first_time = start_time
            timestamps = [
                first_time + step * index for index in range(candidate_count)
            ]
        else:
            end_time = requested_end
            aligned_epoch = int(end_time.timestamp()) // step_seconds * step_seconds
            end_time = datetime.fromtimestamp(aligned_epoch, tz=end_time.tzinfo)
            if start_time is not None and start_time > end_time:
                return []
            candidate_count = int(limit)
            if start_time is not None:
                span_count = int((end_time - start_time) // step) + 1
                candidate_count = min(candidate_count, span_count)
            first_time = end_time - step * (candidate_count - 1)
            if start_time is not None and first_time < start_time:
                first_time = start_time
            timestamps = [
                first_time + step * index for index in range(candidate_count)
            ]

        bars: List[Bar] = []
        for timestamp in timestamps:
            tick = (
                self._symbol_seed(symbol) % 10_000
                + int((timestamp - self._base_time).total_seconds() // step_seconds)
            )
            open_price = self._price_at(symbol, tick)
            close_price = self._price_at(symbol, tick + 1)
            wick = max(Decimal("0.01"), self._quantize(open_price * Decimal("0.002")))
            high = max(open_price, close_price) + wick
            low = max(Decimal("0.01"), min(open_price, close_price) - wick)
            volume = 10_000 + (self._symbol_seed(symbol) + tick * 7919) % 200_000
            typical_price = (open_price + close_price) / Decimal(2)
            bars.append(
                Bar(
                    symbol=symbol,
                    interval=normalized_interval,
                    timestamp=timestamp,
                    open=open_price,
                    high=self._quantize(high),
                    low=self._quantize(low),
                    close=close_price,
                    volume=volume,
                    turnover=self._quantize(typical_price * volume),
                    source=self.name,
                )
            )
        return bars

    async def fetch_instruments(self) -> List[Instrument]:
        return [
            Instrument("600519.SH", "贵州茅台", Exchange.SH, SecurityType.STOCK),
            Instrument("000001.SZ", "平安银行", Exchange.SZ, SecurityType.STOCK),
            Instrument("300750.SZ", "宁德时代", Exchange.SZ, SecurityType.STOCK),
            Instrument("688981.SH", "中芯国际", Exchange.SH, SecurityType.STOCK),
        ]
