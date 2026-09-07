"""AKShare-backed market-data provider.

AKShare exposes synchronous pandas functions.  This adapter keeps that vendor
surface behind the asynchronous :class:`MarketDataProvider` contract used by
the application.  The real-time Shanghai-A snapshot is intentionally cached
for a short period because the upstream endpoint is paginated and relatively
expensive to request.

The adapter stores *raw* prices (no adjustment) and uses the time at which the
snapshot was received as ``Quote.timestamp``.  AKShare's bulk snapshot does
not expose a reliable exchange timestamp or bid/ask book, so those fields are
left empty rather than fabricated.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import random
import re
import time
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from quant_monitor.domain import (
    Bar,
    Exchange,
    Instrument,
    Quote,
    SecurityType,
    ensure_market_time,
    now_market_time,
    normalize_symbol,
)

from .base import MarketDataProvider, MarketDataProviderError


LOGGER = logging.getLogger(__name__)

_CODE_RE = re.compile(r"^\d{1,6}$")
_SPOT_COLUMNS = {
    "code": ("代码", "股票代码", "code", "symbol"),
    "name": ("名称", "股票名称", "name"),
    "last": ("最新价", "最新", "last", "last_price", "close", "收盘"),
    "bid": ("买入", "买一", "buy", "bid", "bid_price"),
    "ask": ("卖出", "卖一", "sell", "ask", "ask_price"),
    "open": ("今开", "开盘", "open", "open_price"),
    "high": ("最高", "最高价", "high", "high_price"),
    "low": ("最低", "最低价", "low", "low_price"),
    "previous_close": ("昨收", "前收盘", "previous_close", "prev_close"),
    "volume": ("成交量", "volume", "vol"),
    "turnover": ("成交额", "成交金额", "turnover", "amount"),
    "timestamp": ("时间戳", "时间", "更新时间", "timestamp", "data_time"),
}
_BAR_COLUMNS = {
    "timestamp": ("时间", "日期", "datetime", "date", "timestamp"),
    "open": ("开盘", "open", "open_price"),
    "high": ("最高", "最高价", "high", "high_price"),
    "low": ("最低", "最低价", "low", "low_price"),
    "close": ("收盘", "收盘价", "close", "close_price"),
    "volume": ("成交量", "volume", "vol"),
    "turnover": ("成交额", "成交金额", "turnover", "amount"),
}
_MINUTE_PERIODS = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "60m": "60", "1h": "60"}
_HISTORY_PERIODS = {"1d": "daily", "1w": "weekly"}
_MISSING = {"", "-", "--", "—", "nan", "none", "null", "nat"}


class AkShareError(MarketDataProviderError):
    """Base error raised by the AKShare adapter."""


class AkShareDependencyError(AkShareError):
    """Raised when the optional AKShare dependency is unavailable."""


class AkShareDataError(AkShareError):
    """Raised when AKShare returns unusable or incomplete data."""


def _is_empty_frame(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(value.empty)
    except Exception:
        return False


def _copy_frame(value: Any) -> Any:
    try:
        return value.copy()
    except AttributeError:
        return value


def _rows(value: Any) -> Iterable[Mapping[str, Any]]:
    """Yield mapping-like rows from a pandas frame or a test double."""

    if isinstance(value, Mapping):
        # A mapping of column -> values is not a row, but a mapping of field
        # names -> scalars is useful for tiny injected fixtures.
        if all(not isinstance(item, (list, tuple)) for item in value.values()):
            yield value
            return
    if hasattr(value, "to_dict"):
        try:
            records = value.to_dict(orient="records")
            for record in records:
                if isinstance(record, Mapping):
                    yield record
            return
        except TypeError:
            pass
    if hasattr(value, "iterrows"):
        for _, row in value.iterrows():
            if hasattr(row, "to_dict"):
                yield row.to_dict()
            elif isinstance(row, Mapping):
                yield row
        return
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        for item in value:
            if isinstance(item, Mapping):
                yield item


def _value(row: Mapping[str, Any], aliases: Sequence[str], default: Any = None) -> Any:
    for alias in aliases:
        if alias in row:
            return row[alias]
    # Be tolerant of column labels with accidental surrounding whitespace.
    normalized = {str(key).strip(): value for key, value in row.items()}
    for alias in aliases:
        if alias in normalized:
            return normalized[alias]
    return default


def _number(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text.lower() in _MISSING:
        return None
    try:
        result = Decimal(text)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not result.is_finite():
        return None
    return result


def _nonnegative_number(value: Any) -> Decimal:
    result = _number(value)
    return result if result is not None and result >= 0 else Decimal("0")


def _positive_number(value: Any) -> Optional[Decimal]:
    result = _number(value)
    return result if result is not None and result > 0 else None


def _integer(value: Any) -> int:
    result = _number(value)
    if result is None or result <= 0:
        return 0
    try:
        return max(0, int(result))
    except (OverflowError, ValueError):
        return 0


def _code(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().upper()
    if text.endswith(".0"):
        text = text[:-2]
    if "." in text:
        text = text.split(".", 1)[0]
    # Sina/Tencent spot interfaces prefix the numeric code with the market,
    # e.g. ``SH600519`` or ``sh600519``.
    for prefix in ("SH", "SZ", "BJ"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    if not _CODE_RE.fullmatch(text):
        return None
    return text.zfill(6)


def _source_code(symbol: str) -> str:
    return normalize_symbol(symbol).split(".", 1)[0]


def _row_is_shanghai(row: Mapping[str, Any]) -> bool:
    """Identify Shanghai rows when a fallback returns all A-share markets."""

    raw = str(_value(row, _SPOT_COLUMNS["code"], "")).strip().upper()
    for prefix in ("SH", "SZ", "BJ"):
        if raw.startswith(prefix):
            return prefix == "SH"
    # ``stock_zh_a_spot`` (the optional Sina fallback) exposes the numeric
    # ``代码`` column and drops Sina's separate ``symbol`` market prefix.  In
    # the A-share universe Shanghai listings use the 6xxxxx range, while
    # Shenzhen/Beijing listings use 0/3/4/8/9xxxxx.  Keep the numeric rule
    # conservative so a full-market fallback cannot leak non-SH rows into a
    # Shanghai-only provider.  Unknown/non-numeric labels are retained for
    # injected fixtures and the dedicated Shanghai endpoint, which only
    # returns SH rows in normal operation.
    normalized = _code(raw)
    if normalized is not None and raw:
        return normalized.startswith("6")
    return True


def _timestamp(value: Any, *, daily: bool = False) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    else:
        text = str(value).strip()
        if text.lower() in _MISSING:
            return None
        try:
            # pandas-style strings frequently contain a space and fractional
            # seconds; datetime.fromisoformat handles both.
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                import pandas as pd

                parsed = pd.to_datetime(text).to_pydatetime()
            except Exception:
                return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        # AKShare returns China-local timestamps without a timezone.
        from quant_monitor.domain import SHANGHAI_TZ

        parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
    normalized = ensure_market_time(parsed)
    if daily:
        normalized = normalized.replace(hour=0, minute=0, second=0, microsecond=0)
    return normalized


def _spot_timestamp(value: Any, received_at: datetime) -> datetime:
    """Parse a provider timestamp, falling back to the snapshot receive time."""

    text = str(value or "").strip()
    # Sina returns a local clock value such as ``15:30:01``.  Handle it before
    # pandas gets a chance to attach the host machine's current date.
    if re.fullmatch(r"\d{2}:\d{2}:\d{2}(?:\.\d+)?", text):
        # The source supplies no trading date (and may be reporting the most
        # recent trading day while the process is running on a weekend).  Do
        # not manufacture a date; use the receive time and let callers know
        # that it is a snapshot timestamp.
        return received_at
    parsed = _timestamp(value)
    if parsed is not None:
        return parsed
    if text:
        try:
            parsed_time = datetime.strptime(text, "%H:%M:%S").time()
            return ensure_market_time(
                datetime.combine(received_at.date(), parsed_time).replace(
                    tzinfo=received_at.tzinfo
                )
            )
        except ValueError:
            pass
    return received_at


class AkShareMarketDataProvider(MarketDataProvider):
    """Fetch Shanghai A-share snapshots and OHLCV bars through AKShare.

    ``spot_fetcher`` and the history fetchers are injectable to make the
    adapter testable without network access.  In normal operation they are
    resolved lazily from the installed :mod:`akshare` package.
    """

    def __init__(
        self,
        *,
        quote_cache_seconds: float = 15.0,
        bars_cache_seconds: float = 30.0,
        retries: int = 2,
        retry_backoff_seconds: float = 0.5,
        clock: Optional[Callable[[], datetime]] = None,
        client: Any = None,
        spot_fetcher: Optional[Callable[..., Any]] = None,
        fallback_spot_fetcher: Optional[Callable[..., Any]] = None,
        minute_fetcher: Optional[Callable[..., Any]] = None,
        history_fetcher: Optional[Callable[..., Any]] = None,
        strict_symbols: bool = True,
        use_spot_fallback: bool = False,
    ) -> None:
        if float(quote_cache_seconds) < 0:
            raise ValueError("quote_cache_seconds cannot be negative")
        if float(bars_cache_seconds) < 0:
            raise ValueError("bars_cache_seconds cannot be negative")
        if int(retries) < 0:
            raise ValueError("retries cannot be negative")
        if float(retry_backoff_seconds) < 0:
            raise ValueError("retry_backoff_seconds cannot be negative")
        self.quote_cache_seconds = float(quote_cache_seconds)
        self.bars_cache_seconds = float(bars_cache_seconds)
        self.retries = int(retries)
        self.retry_backoff_seconds = float(retry_backoff_seconds)
        self._clock = clock or now_market_time
        self._client = client
        self._spot_fetcher = spot_fetcher
        self._fallback_spot_fetcher = fallback_spot_fetcher
        self._minute_fetcher = minute_fetcher
        self._history_fetcher = history_fetcher
        self.strict_symbols = bool(strict_symbols)
        self.use_spot_fallback = bool(use_spot_fallback)

        self._spot_cache: Optional[Tuple[float, datetime, Any]] = None
        # asyncio primitives are bound to an event loop on Python 3.9.  Keep
        # construction synchronous-friendly and create the lock on first use.
        self._spot_lock: Optional[asyncio.Lock] = None
        self._bar_cache: Dict[Tuple[str, str, Optional[str], Optional[str]], Tuple[float, List[Bar]]] = {}
        self._bar_locks: Dict[Tuple[str, str, Optional[str], Optional[str]], asyncio.Lock] = {}

    @property
    def name(self) -> str:
        return "akshare"

    def _module(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            return importlib.import_module("akshare")
        except ImportError as exc:
            raise AkShareDependencyError(
                "AKShare is not installed; run `python -m pip install akshare`"
            ) from exc

    def _function(self, kind: str) -> Callable[..., Any]:
        explicit = {
            "spot": self._spot_fetcher,
            "minute": self._minute_fetcher,
            "history": self._history_fetcher,
        }.get(kind)
        if explicit is not None:
            return explicit
        module = self._module()
        names = {
            "spot": "stock_sh_a_spot_em",
            "minute": "stock_zh_a_hist_min_em",
            "history": "stock_zh_a_hist",
        }
        function = getattr(module, names[kind], None)
        if function is None:
            raise AkShareDependencyError(
                "installed AKShare does not expose {}".format(names[kind])
            )
        return function

    def _fallback_spot_function(self) -> Optional[Callable[..., Any]]:
        if self._fallback_spot_fetcher is not None:
            return self._fallback_spot_fetcher
        if self._client is not None:
            return getattr(self._client, "stock_zh_a_spot", None)
        try:
            return getattr(self._module(), "stock_zh_a_spot", None)
        except AkShareDependencyError:
            return None

    async def _call(
        self,
        function: Callable[..., Any],
        *args: Any,
        operation: str,
        allow_empty: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Run a synchronous AKShare call off the event loop with bounded retries."""

        last_error: Optional[BaseException] = None
        for attempt in range(self.retries + 1):
            try:
                result = await asyncio.to_thread(function, *args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
                if not allow_empty and _is_empty_frame(result):
                    raise AkShareDataError("{} returned no rows".format(operation))
                return result
            except asyncio.CancelledError:
                raise
            except AkShareDependencyError:
                raise
            except Exception as exc:  # upstream requests expose varied errors
                last_error = exc
                if attempt >= self.retries:
                    break
                delay = self.retry_backoff_seconds * (2 ** attempt)
                if delay:
                    delay += random.uniform(0, min(0.25, delay / 2))
                    await asyncio.sleep(delay)
        raise AkShareDataError(
            "{} failed after {} attempt(s): {}".format(
                operation, self.retries + 1, last_error
            )
        ) from last_error

    async def _spot_frame(self) -> Tuple[Any, datetime]:
        now_mono = time.monotonic()
        if self._spot_cache is not None:
            cached_at, received_at, frame = self._spot_cache
            if now_mono - cached_at <= self.quote_cache_seconds:
                return _copy_frame(frame), received_at

        if self._spot_lock is None:
            self._spot_lock = asyncio.Lock()
        async with self._spot_lock:
            now_mono = time.monotonic()
            if self._spot_cache is not None:
                cached_at, received_at, frame = self._spot_cache
                if now_mono - cached_at <= self.quote_cache_seconds:
                    return _copy_frame(frame), received_at
            try:
                frame = await self._call(
                    self._function("spot"),
                    operation="AKShare Shanghai A snapshot",
                )
                source_kind = "eastmoney"
            except AkShareDataError as primary_error:
                fallback = self._fallback_spot_function()
                if not self.use_spot_fallback or fallback is None:
                    raise
                LOGGER.warning(
                    "AKShare Eastmoney snapshot failed; trying documented Sina snapshot fallback: %s",
                    primary_error,
                )
                frame = await self._call(
                    fallback,
                    operation="AKShare Sina A snapshot fallback",
                )
                source_kind = "sina"
            received_at = ensure_market_time(self._clock())
            # Keep the source marker alongside the cached frame so the quote
            # payload can distinguish the fallback without changing domain
            # models or leaking pandas objects outside this adapter.
            if source_kind == "sina":
                try:
                    frame = _copy_frame(frame)
                    frame.attrs["akshare_source"] = "sina"
                except Exception:
                    pass
            self._spot_cache = (time.monotonic(), received_at, _copy_frame(frame))
            return _copy_frame(frame), received_at

    @staticmethod
    def _spot_records(frame: Any) -> Dict[str, Mapping[str, Any]]:
        records: Dict[str, Mapping[str, Any]] = {}
        for row in _rows(frame):
            if not _row_is_shanghai(row):
                continue
            code = _code(_value(row, _SPOT_COLUMNS["code"]))
            if code is not None:
                records[code] = row
        return records

    @staticmethod
    def _quote_from_row(
        row: Mapping[str, Any],
        symbol: str,
        timestamp: datetime,
        *,
        source: str = "akshare",
        volume_is_shares: bool = False,
    ) -> Optional[Quote]:
        last = _positive_number(_value(row, _SPOT_COLUMNS["last"]))
        if last is None:
            # Suspended/unpriced rows must not be silently converted into a
            # fake zero quote or an old database value.
            return None
        opened = _positive_number(_value(row, _SPOT_COLUMNS["open"]))
        high = _positive_number(_value(row, _SPOT_COLUMNS["high"]))
        low = _positive_number(_value(row, _SPOT_COLUMNS["low"]))
        previous = _positive_number(_value(row, _SPOT_COLUMNS["previous_close"]))
        reference_high = max(last, opened) if opened is not None else last
        reference_low = min(last, opened) if opened is not None else last
        if high is not None and high < reference_high:
            high = None
        if low is not None and low > reference_low:
            low = None
        if high is not None and low is not None and high < low:
            high = low = None
        bid = _positive_number(_value(row, _SPOT_COLUMNS["bid"]))
        ask = _positive_number(_value(row, _SPOT_COLUMNS["ask"]))
        if bid is not None and ask is not None and bid > ask:
            bid = ask = None
        volume = _integer(_value(row, _SPOT_COLUMNS["volume"]))
        # The Sina fallback reports shares while the Eastmoney Shanghai
        # snapshot reports hands.  The domain has no unit field yet; preserve
        # the provider's value and make the source explicit for callers.
        if volume_is_shares:
            volume = int(volume)
        return Quote(
            symbol=symbol,
            timestamp=timestamp,
            last=last,
            open=opened,
            high=high,
            low=low,
            previous_close=previous,
            bid=bid,
            ask=ask,
            bid_size=0,
            ask_size=0,
            # AKShare documents volume in hands; the domain currently stores
            # the provider's reported unit without silently multiplying it.
            volume=volume,
            turnover=_nonnegative_number(_value(row, _SPOT_COLUMNS["turnover"])),
            source=source,
        )

    async def _fetch_quotes(
        self,
        symbols: Sequence[str],
        *,
        strict: bool,
    ) -> List[Quote]:
        requested = tuple(dict.fromkeys(normalize_symbol(symbol) for symbol in symbols))
        if not requested:
            return []
        unsupported = [
            symbol
            for symbol in requested
            if symbol.rsplit(".", 1)[1] != Exchange.SH.value
        ]
        if unsupported and strict:
            raise ValueError(
                "AKShare Shanghai snapshot only supports .SH symbols: {}".format(
                    ", ".join(unsupported)
                )
            )
        frame, received_at = await self._spot_frame()
        records = self._spot_records(frame)
        frame_attrs = getattr(frame, "attrs", {}) or {}
        fallback_source = str(frame_attrs.get("akshare_source", "eastmoney"))
        source_name = "akshare" if fallback_source == "eastmoney" else "akshare:sina"
        volume_is_shares = fallback_source == "sina"
        quotes: List[Quote] = []
        missing: List[str] = []
        for symbol in requested:
            if symbol.rsplit(".", 1)[1] != Exchange.SH.value:
                missing.append(symbol)
                continue
            row = records.get(_source_code(symbol))
            quote = None
            if row:
                quote_timestamp = (
                    _spot_timestamp(
                        _value(row, _SPOT_COLUMNS["timestamp"]), received_at
                    )
                    if fallback_source == "sina"
                    else received_at
                )
                quote = self._quote_from_row(
                    row,
                    symbol,
                    quote_timestamp,
                    source=source_name,
                    volume_is_shares=volume_is_shares,
                )
            if quote is None:
                missing.append(symbol)
            else:
                quotes.append(quote)
        if missing and strict:
            raise AkShareDataError(
                "AKShare Shanghai snapshot has no usable quote for: {}".format(
                    ", ".join(missing)
                )
            )
        if missing:
            LOGGER.warning("AKShare returned no usable quote for %s", ", ".join(missing))
        return quotes

    async def fetch_quotes(self, symbols: Sequence[str]) -> List[Quote]:
        """Fetch a configured symbol set and fail if any quote is unusable."""

        return await self._fetch_quotes(symbols, strict=self.strict_symbols)

    async def fetch_quotes_relaxed(self, symbols: Sequence[str]) -> List[Quote]:
        """Fetch the usable subset of a universe containing suspended stocks."""

        return await self._fetch_quotes(symbols, strict=False)

    async def fetch_instruments(self) -> List[Instrument]:
        frame, _ = await self._spot_frame()
        frame_attrs = getattr(frame, "attrs", {}) or {}
        source_kind = str(frame_attrs.get("akshare_source", "eastmoney"))
        instruments: List[Instrument] = []
        for code, row in self._spot_records(frame).items():
            name = str(_value(row, _SPOT_COLUMNS["name"], code) or code).strip()
            if not name or name.lower() in _MISSING:
                name = code
            instruments.append(
                Instrument(
                    symbol="{}.SH".format(code),
                    name=name,
                    exchange=Exchange.SH,
                    security_type=SecurityType.STOCK,
                    metadata={
                        "source": "akshare" if source_kind == "eastmoney" else "akshare:sina",
                        "volume_unit": "share" if source_kind == "sina" else "hand",
                    },
                )
            )
        return instruments

    @staticmethod
    def _format_minute_time(value: datetime) -> str:
        return ensure_market_time(value).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _format_date(value: datetime) -> str:
        return ensure_market_time(value).strftime("%Y%m%d")

    def _bar_key(
        self,
        symbol: str,
        interval: str,
        start: Optional[datetime],
        end: Optional[datetime],
    ) -> Tuple[str, str, Optional[str], Optional[str]]:
        return (
            symbol,
            interval,
            start.isoformat() if start is not None else None,
            end.isoformat() if end is not None else None,
        )

    @staticmethod
    def _parse_bars(
        frame: Any,
        symbol: str,
        interval: str,
        start: Optional[datetime],
        end: Optional[datetime],
    ) -> List[Bar]:
        result: Dict[datetime, Bar] = {}
        daily = interval in _HISTORY_PERIODS
        for row in _rows(frame):
            timestamp = _timestamp(_value(row, _BAR_COLUMNS["timestamp"]), daily=daily)
            opened = _positive_number(_value(row, _BAR_COLUMNS["open"]))
            high = _positive_number(_value(row, _BAR_COLUMNS["high"]))
            low = _positive_number(_value(row, _BAR_COLUMNS["low"]))
            close = _positive_number(_value(row, _BAR_COLUMNS["close"]))
            if timestamp is None or None in (opened, high, low, close):
                continue
            if high < max(opened, close) or low > min(opened, close):
                continue
            if start is not None and timestamp < start:
                continue
            if end is not None and timestamp > end:
                continue
            result[timestamp] = Bar(
                symbol=symbol,
                interval=interval,
                timestamp=timestamp,
                open=opened,
                high=high,
                low=low,
                close=close,
                volume=_integer(_value(row, _BAR_COLUMNS["volume"])),
                turnover=_nonnegative_number(_value(row, _BAR_COLUMNS["turnover"])),
                source="akshare",
            )
        return [result[key] for key in sorted(result)]

    async def fetch_bars(
        self,
        symbol: str,
        interval: str = "1m",
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 200,
    ) -> List[Bar]:
        normalized_symbol = normalize_symbol(symbol)
        normalized_interval = str(interval).strip().lower()
        if normalized_interval not in _MINUTE_PERIODS and normalized_interval not in _HISTORY_PERIODS:
            raise ValueError(
                "unsupported AKShare interval: {} (use 1m, 5m, 15m, 30m, 60m, 1d or 1w)".format(
                    interval
                )
            )
        if int(limit) <= 0:
            raise ValueError("limit must be positive")
        start_time = ensure_market_time(start) if start is not None else None
        end_time = ensure_market_time(end) if end is not None else None
        if start_time is not None and end_time is not None and start_time > end_time:
            return []

        filter_start = start_time
        filter_end = end_time
        if normalized_interval in _HISTORY_PERIODS:
            # Daily/weekly rows are represented at local midnight.  A caller
            # asking for ``2026-08-20T09:30`` still expects that day's row.
            if filter_start is not None:
                filter_start = filter_start.replace(hour=0, minute=0, second=0, microsecond=0)
            if filter_end is not None:
                filter_end = filter_end.replace(hour=23, minute=59, second=59, microsecond=999999)

        # Keep the default request bounded.  Minute endpoints only retain a
        # short recent window; requesting the whole history is both slow and
        # likely to be rejected by the upstream service.
        effective_end = end_time or ensure_market_time(self._clock())
        if start_time is None:
            if normalized_interval in _MINUTE_PERIODS:
                start_time = effective_end - timedelta(days=7)
            else:
                start_time = effective_end - timedelta(days=max(365, int(limit) * 3))

        key = self._bar_key(normalized_symbol, normalized_interval, start, end)
        now_mono = time.monotonic()
        cached = self._bar_cache.get(key)
        if cached is not None and now_mono - cached[0] <= self.bars_cache_seconds:
            return list(cached[1][-int(limit) :])

        lock = self._bar_locks.setdefault(key, asyncio.Lock())
        async with lock:
            now_mono = time.monotonic()
            cached = self._bar_cache.get(key)
            if cached is not None and now_mono - cached[0] <= self.bars_cache_seconds:
                return list(cached[1][-int(limit) :])

            code = _source_code(normalized_symbol)
            if normalized_interval in _MINUTE_PERIODS:
                frame = await self._call(
                    self._function("minute"),
                    symbol=code,
                    start_date=self._format_minute_time(start_time),
                    end_date=self._format_minute_time(effective_end),
                    period=_MINUTE_PERIODS[normalized_interval],
                    adjust="",
                    operation="AKShare {} minute bars for {}".format(normalized_interval, normalized_symbol),
                    allow_empty=True,
                )
            else:
                frame = await self._call(
                    self._function("history"),
                    symbol=code,
                    period=_HISTORY_PERIODS[normalized_interval],
                    start_date=self._format_date(start_time),
                    end_date=self._format_date(effective_end),
                    adjust="",
                    operation="AKShare {} bars for {}".format(normalized_interval, normalized_symbol),
                    allow_empty=True,
                )
            bars = self._parse_bars(
                frame,
                normalized_symbol,
                normalized_interval,
                filter_start,
                filter_end,
            )
            self._bar_cache[key] = (time.monotonic(), list(bars))
            return list(bars[-int(limit) :])

    async def close(self) -> None:
        self._spot_cache = None
        self._bar_cache.clear()
        self._bar_locks.clear()
        self._spot_lock = None


AKShareMarketDataProvider = AkShareMarketDataProvider


__all__ = [
    "AkShareError",
    "AkShareDependencyError",
    "AkShareDataError",
    "AkShareMarketDataProvider",
    "AKShareMarketDataProvider",
]
