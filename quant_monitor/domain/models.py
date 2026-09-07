"""Domain models shared by data sources, storage, strategies and services.

The core deliberately uses dataclasses and standard-library types only.  Money is
represented by :class:`~decimal.Decimal`; timestamps must be timezone-aware and
are normalized to the China A-share market timezone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Dict, Mapping, Optional

try:  # Windows installations do not always ship the IANA timezone database.
    from zoneinfo import ZoneInfo

    SHANGHAI_TZ: tzinfo = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - platform-dependent fallback
    SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


_SYMBOL_RE = re.compile(r"^(?P<code>\d{6})\.(?P<exchange>SH|SZ|BJ)$")


class Exchange(str, Enum):
    SH = "SH"
    SZ = "SZ"
    BJ = "BJ"


class SecurityType(str, Enum):
    STOCK = "stock"
    ETF = "etf"
    INDEX = "index"
    BOND = "bond"
    FUND = "fund"
    OTHER = "other"


class AlertLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class SignalAction(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


def normalize_symbol(symbol: str) -> str:
    """Return an upper-case ``600519.SH`` style symbol or raise ``ValueError``."""

    normalized = str(symbol).strip().upper()
    if not _SYMBOL_RE.fullmatch(normalized):
        raise ValueError("symbol must use the six-digit CODE.EXCHANGE form, e.g. 600519.SH")
    return normalized


def ensure_market_time(value: datetime) -> datetime:
    """Validate an aware datetime and normalize it to Asia/Shanghai."""

    if not isinstance(value, datetime):
        raise TypeError("timestamp must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(SHANGHAI_TZ)


def parse_market_time(value: str) -> datetime:
    """Parse an ISO-8601 timestamp and normalize it to Asia/Shanghai."""

    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return ensure_market_time(datetime.fromisoformat(text))


def now_market_time() -> datetime:
    return datetime.now(tz=SHANGHAI_TZ)


def _decimal(value: Any, field_name: str, *, non_negative: bool = True) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("{} must be a valid decimal".format(field_name)) from exc
    if not result.is_finite():
        raise ValueError("{} must be finite".format(field_name))
    if non_negative and result < 0:
        raise ValueError("{} cannot be negative".format(field_name))
    return result


def _metadata(value: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    return dict(value or {})


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    exchange: Exchange
    security_type: SecurityType = SecurityType.STOCK
    listed_at: Optional[date] = None
    lot_size: int = 100
    price_tick: Decimal = Decimal("0.01")
    active: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        symbol = normalize_symbol(self.symbol)
        exchange = self.exchange if isinstance(self.exchange, Exchange) else Exchange(self.exchange)
        security_type = (
            self.security_type
            if isinstance(self.security_type, SecurityType)
            else SecurityType(self.security_type)
        )
        if symbol.rsplit(".", 1)[1] != exchange.value:
            raise ValueError("instrument exchange does not match symbol suffix")
        if not str(self.name).strip():
            raise ValueError("instrument name cannot be empty")
        if int(self.lot_size) <= 0:
            raise ValueError("lot_size must be positive")
        price_tick = _decimal(self.price_tick, "price_tick")
        if price_tick == 0:
            raise ValueError("price_tick must be positive")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "name", str(self.name).strip())
        object.__setattr__(self, "exchange", exchange)
        object.__setattr__(self, "security_type", security_type)
        object.__setattr__(self, "lot_size", int(self.lot_size))
        object.__setattr__(self, "price_tick", price_tick)
        object.__setattr__(self, "metadata", _metadata(self.metadata))


@dataclass(frozen=True)
class Quote:
    symbol: str
    timestamp: datetime
    last: Decimal
    open: Optional[Decimal] = None
    high: Optional[Decimal] = None
    low: Optional[Decimal] = None
    previous_close: Optional[Decimal] = None
    bid: Optional[Decimal] = None
    ask: Optional[Decimal] = None
    bid_size: int = 0
    ask_size: int = 0
    volume: int = 0
    turnover: Decimal = Decimal("0")
    source: str = "unknown"

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "timestamp", ensure_market_time(self.timestamp))
        for name in ("last", "open", "high", "low", "previous_close", "bid", "ask"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _decimal(value, name))
        for name in ("bid_size", "ask_size", "volume"):
            value = int(getattr(self, name))
            if value < 0:
                raise ValueError("{} cannot be negative".format(name))
            object.__setattr__(self, name, value)
        object.__setattr__(self, "turnover", _decimal(self.turnover, "turnover"))
        if self.high is not None and self.low is not None and self.high < self.low:
            raise ValueError("quote high cannot be below low")
        object.__setattr__(self, "source", str(self.source))


@dataclass(frozen=True)
class Bar:
    symbol: str
    interval: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int = 0
    turnover: Decimal = Decimal("0")
    source: str = "unknown"

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        interval = str(self.interval).strip().lower()
        if not interval:
            raise ValueError("bar interval cannot be empty")
        object.__setattr__(self, "interval", interval)
        object.__setattr__(self, "timestamp", ensure_market_time(self.timestamp))
        for name in ("open", "high", "low", "close"):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
        if self.high < max(self.open, self.low, self.close):
            raise ValueError("bar high must be at least open, low and close")
        if self.low > min(self.open, self.high, self.close):
            raise ValueError("bar low must be at most open, high and close")
        volume = int(self.volume)
        if volume < 0:
            raise ValueError("volume cannot be negative")
        object.__setattr__(self, "volume", volume)
        object.__setattr__(self, "turnover", _decimal(self.turnover, "turnover"))
        object.__setattr__(self, "source", str(self.source))


@dataclass(frozen=True)
class Metric:
    symbol: str
    name: str
    timestamp: datetime
    value: float
    window: Optional[int] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        name = str(self.name).strip()
        if not name:
            raise ValueError("metric name cannot be empty")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "timestamp", ensure_market_time(self.timestamp))
        value = float(self.value)
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("metric value must be finite")
        object.__setattr__(self, "value", value)
        if self.window is not None and int(self.window) <= 0:
            raise ValueError("metric window must be positive")
        if self.window is not None:
            object.__setattr__(self, "window", int(self.window))
        object.__setattr__(self, "metadata", _metadata(self.metadata))


@dataclass(frozen=True)
class Alert:
    symbol: Optional[str]
    timestamp: datetime
    level: AlertLevel
    title: str
    message: str
    category: str = "monitor"
    acknowledged: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)
    id: Optional[int] = None

    def __post_init__(self) -> None:
        if self.symbol is not None:
            object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "timestamp", ensure_market_time(self.timestamp))
        level = self.level if isinstance(self.level, AlertLevel) else AlertLevel(self.level)
        object.__setattr__(self, "level", level)
        if not str(self.title).strip():
            raise ValueError("alert title cannot be empty")
        object.__setattr__(self, "title", str(self.title).strip())
        object.__setattr__(self, "message", str(self.message))
        object.__setattr__(self, "category", str(self.category).strip() or "monitor")
        object.__setattr__(self, "acknowledged", bool(self.acknowledged))
        object.__setattr__(self, "metadata", _metadata(self.metadata))
        if self.id is not None:
            object.__setattr__(self, "id", int(self.id))


@dataclass(frozen=True)
class Signal:
    strategy: str
    symbol: str
    timestamp: datetime
    action: SignalAction
    strength: float
    price: Optional[Decimal] = None
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    id: Optional[int] = None

    def __post_init__(self) -> None:
        strategy = str(self.strategy).strip()
        if not strategy:
            raise ValueError("signal strategy cannot be empty")
        object.__setattr__(self, "strategy", strategy)
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "timestamp", ensure_market_time(self.timestamp))
        action = self.action if isinstance(self.action, SignalAction) else SignalAction(self.action)
        object.__setattr__(self, "action", action)
        strength = float(self.strength)
        if not -1.0 <= strength <= 1.0:
            raise ValueError("signal strength must be between -1 and 1")
        object.__setattr__(self, "strength", strength)
        if self.price is not None:
            object.__setattr__(self, "price", _decimal(self.price, "price"))
        object.__setattr__(self, "reason", str(self.reason))
        object.__setattr__(self, "metadata", _metadata(self.metadata))
        if self.id is not None:
            object.__setattr__(self, "id", int(self.id))
