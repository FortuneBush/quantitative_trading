"""AllTick market-data provider for Shanghai and Shenzhen A shares.

Realtime trades are bootstrapped through the HTTP ``trade-tick`` endpoint and
then refreshed by one token-scoped WebSocket subscription.  Historical bars
use HTTP ``kline``; subsequent multi-symbol refreshes use ``batch-kline`` so a
short collector interval does not multiply the vendor request quota.

All authentication details stay inside this adapter.  In particular, raised
exceptions never include request URLs because AllTick places the token in the
query string.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from quant_monitor.domain import (
    Bar,
    Exchange,
    Instrument,
    Quote,
    SHANGHAI_TZ,
    SecurityType,
    ensure_market_time,
    normalize_symbol,
)

from .base import MarketDataProvider, MarketDataProviderError


ALLTICK_HTTP_BASE_URL = "https://quote.alltick.co/quote-stock-b-api"
ALLTICK_WEBSOCKET_URL = "wss://quote.alltick.co/quote-stock-b-ws-api"

_INTERVAL_TYPES = {
    "1m": 1,
    "5m": 2,
    "15m": 3,
    "30m": 4,
    "60m": 5,
    "1h": 5,
    "1d": 8,
    "1w": 9,
}
_SUPPORTED_EXCHANGES = {Exchange.SH.value, Exchange.SZ.value}
_RETRYABLE_RET_CODES = {429, 605}


class AllTickError(MarketDataProviderError):
    """Base error for the AllTick adapter."""

    def __init__(
        self,
        message: str,
        *,
        ret_code: Optional[int] = None,
        operation: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.ret_code = ret_code
        self.operation = operation


class AllTickConfigurationError(AllTickError):
    """Raised when required local AllTick configuration is missing."""


class AllTickDependencyError(AllTickError):
    """Raised when an optional runtime dependency is unavailable."""


class AllTickAuthenticationError(AllTickError):
    """Raised when AllTick rejects the configured token."""


class AllTickPermissionError(AllTickError):
    """Raised when the token plan cannot access a requested product."""


class AllTickRateLimitError(AllTickError):
    """Raised when the token has exhausted a request budget."""


class AllTickTransportError(AllTickError):
    """Raised for sanitized HTTP/WebSocket transport failures."""


class AllTickDataError(AllTickError):
    """Raised when a successful response cannot produce valid domain data."""


class _HTTPStatusError(RuntimeError):
    def __init__(self, status: int, payload: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__("AllTick HTTP status {}".format(int(status)))
        self.status = int(status)
        self.payload = dict(payload or {})


@dataclass(frozen=True)
class _BarCacheEntry:
    updated_at: float
    bars: Tuple[Bar, ...]
    coverage: int


@dataclass(frozen=True)
class _DepthSnapshot:
    timestamp: datetime
    received_at: float
    bid: Optional[Decimal]
    ask: Optional[Decimal]
    bid_size: int
    ask_size: int
    sequence: Optional[int]


def _decimal(value: Any, *, positive: bool = False) -> Optional[Decimal]:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in {"nan", "none", "null", "inf", "-inf"} or text in {"-", "--"}:
        return None
    try:
        result = Decimal(text)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        return None
    return result


def _integer(value: Any) -> Optional[int]:
    parsed = _decimal(value)
    if parsed is None:
        return None
    if parsed != parsed.to_integral_value():
        return None
    try:
        return int(parsed)
    except (OverflowError, ValueError):
        return None


def _epoch_time(value: Any) -> Optional[datetime]:
    parsed = _decimal(value)
    if parsed is None or parsed <= 0:
        return None
    seconds = float(parsed)
    # Official examples contain both 10-digit seconds and 13-digit
    # milliseconds despite documenting tick_time as milliseconds.
    if seconds >= 100_000_000_000:
        seconds /= 1000.0
    try:
        timestamp = datetime.fromtimestamp(seconds, tz=SHANGHAI_TZ)
    except (OSError, OverflowError, ValueError):
        return None
    return ensure_market_time(timestamp)


def _chunks(values: Sequence[str], size: int) -> Iterable[Tuple[str, ...]]:
    for index in range(0, len(values), size):
        yield tuple(values[index : index + size])


def _sync_json_request(
    method: str,
    url: str,
    params: Mapping[str, str],
    body: Optional[Mapping[str, Any]],
    timeout: float,
) -> Mapping[str, Any]:
    query = urlencode(dict(params))
    full_url = "{}?{}".format(url, query) if query else url
    encoded = None
    headers = {"Accept": "application/json"}
    if body is not None:
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(full_url, data=encoded, headers=headers, method=method.upper())
    try:
        with urlopen(request, timeout=float(timeout)) as response:
            raw = response.read()
    except HTTPError as exc:
        payload: Mapping[str, Any] = {}
        try:
            decoded = json.loads(exc.read().decode("utf-8", errors="replace"))
            if isinstance(decoded, Mapping):
                payload = decoded
        except Exception:
            payload = {}
        raise _HTTPStatusError(exc.code, payload) from None
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AllTickDataError("AllTick returned a non-JSON response") from exc
    if not isinstance(decoded, Mapping):
        raise AllTickDataError("AllTick response must be a JSON object")
    return decoded


class AllTickMarketDataProvider(MarketDataProvider):
    """AllTick HTTP/WebSocket adapter with conservative free-plan defaults."""

    def __init__(
        self,
        token: str,
        *,
        symbols: Sequence[str] = (),
        instrument_names: Optional[Mapping[str, str]] = None,
        http_base_url: str = ALLTICK_HTTP_BASE_URL,
        websocket_url: str = ALLTICK_WEBSOCKET_URL,
        request_timeout_seconds: float = 15.0,
        http_min_interval_seconds: float = 10.0,
        quote_cache_seconds: float = 10.0,
        bars_cache_seconds: float = 120.0,
        retries: int = 2,
        retry_backoff_seconds: float = 0.5,
        batch_size: int = 5,
        kline_batch_size: Optional[int] = None,
        use_websocket: bool = True,
        use_order_book: bool = True,
        bootstrap_daily_reference: bool = True,
        fetch_static_info: bool = True,
        websocket_symbol_limit: Optional[int] = None,
        websocket_heartbeat_seconds: float = 10.0,
        websocket_reconnect_seconds: float = 3.0,
        websocket_subscription_gap_seconds: float = 1.05,
        permission_retry_seconds: float = 300.0,
        requester: Optional[Callable[..., Any]] = None,
        monotonic: Optional[Callable[[], float]] = None,
    ) -> None:
        normalized_token = str(token or "").strip()
        if not normalized_token:
            raise AllTickConfigurationError(
                "AllTick requires QUANT_MONITOR_ALLTICK_TOKEN in the process environment"
            )
        if float(request_timeout_seconds) <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        for name, value in (
            ("http_min_interval_seconds", http_min_interval_seconds),
            ("quote_cache_seconds", quote_cache_seconds),
            ("bars_cache_seconds", bars_cache_seconds),
            ("retry_backoff_seconds", retry_backoff_seconds),
            ("websocket_reconnect_seconds", websocket_reconnect_seconds),
            ("permission_retry_seconds", permission_retry_seconds),
        ):
            if float(value) < 0:
                raise ValueError("{} cannot be negative".format(name))
        if float(websocket_heartbeat_seconds) <= 0:
            raise ValueError("websocket_heartbeat_seconds must be positive")
        if int(retries) < 0:
            raise ValueError("retries cannot be negative")
        if int(batch_size) <= 0:
            raise ValueError("batch_size must be positive")
        selected_kline_batch_size = int(kline_batch_size or batch_size)
        if selected_kline_batch_size <= 0:
            raise ValueError("kline_batch_size must be positive")
        selected_websocket_limit = int(websocket_symbol_limit or batch_size)
        if selected_websocket_limit <= 0:
            raise ValueError("websocket_symbol_limit must be positive")
        if bool(use_websocket) and bool(use_order_book) and float(
            websocket_subscription_gap_seconds
        ) < 1.0:
            raise ValueError(
                "websocket_subscription_gap_seconds must be at least 1 second"
            )

        configured = tuple(dict.fromkeys(normalize_symbol(item) for item in symbols))
        unsupported = [item for item in configured if item.rsplit(".", 1)[1] not in _SUPPORTED_EXCHANGES]
        if unsupported:
            raise ValueError(
                "AllTick documents only .SH and .SZ A-share codes: {}".format(
                    ", ".join(unsupported)
                )
            )

        self._token = normalized_token
        self._configured_symbols = configured
        self._instrument_names = {
            normalize_symbol(key): str(value).strip()
            for key, value in dict(instrument_names or {}).items()
            if str(value).strip()
        }
        self.http_base_url = str(http_base_url).rstrip("/")
        self.websocket_url = str(websocket_url).split("?", 1)[0]
        self.request_timeout_seconds = float(request_timeout_seconds)
        self.http_min_interval_seconds = float(http_min_interval_seconds)
        self.quote_cache_seconds = float(quote_cache_seconds)
        self.bars_cache_seconds = float(bars_cache_seconds)
        self.retries = int(retries)
        self.retry_backoff_seconds = float(retry_backoff_seconds)
        self.batch_size = int(batch_size)
        self.kline_batch_size = selected_kline_batch_size
        self.use_websocket = bool(use_websocket)
        self.use_order_book = bool(use_order_book)
        self.bootstrap_daily_reference = bool(bootstrap_daily_reference)
        self.fetch_static_info = bool(fetch_static_info)
        self.websocket_symbol_limit = selected_websocket_limit
        self.websocket_heartbeat_seconds = float(websocket_heartbeat_seconds)
        self.websocket_reconnect_seconds = float(websocket_reconnect_seconds)
        self.websocket_subscription_gap_seconds = float(
            websocket_subscription_gap_seconds
        )
        self.permission_retry_seconds = float(permission_retry_seconds)
        self._requester = requester or _sync_json_request
        self._monotonic = monotonic or time.monotonic

        self._http_lock: Optional[asyncio.Lock] = None
        self._quote_lock: Optional[asyncio.Lock] = None
        self._quote_fetch_lock: Optional[asyncio.Lock] = None
        self._bar_lock: Optional[asyncio.Lock] = None
        self._ws_lock: Optional[asyncio.Lock] = None
        self._instrument_lock: Optional[asyncio.Lock] = None
        self._last_http_started: Optional[float] = None
        self._quotes: Dict[str, Quote] = {}
        self._quote_http_at: Dict[str, float] = {}
        self._quote_ws_at: Dict[str, float] = {}
        self._quote_sequences: Dict[str, int] = {}
        self._depths: Dict[str, _DepthSnapshot] = {}
        self._depth_sequences: Dict[str, int] = {}
        self._depth_task: Optional["asyncio.Task[None]"] = None
        self._depth_last_error: Optional[str] = None
        self._depth_next_attempt = 0.0
        self._depth_permission_denied: Dict[str, float] = {}
        self._ws_depth_last_error: Optional[str] = None
        self._ws_depth_last_error_code: Optional[int] = None
        self._ws_depth_suppressed_until = 0.0
        self._ws_depth_attempted_symbols: Tuple[str, ...] = ()
        self._ws_resubscribe_requested = False
        self._bar_cache: Dict[Tuple[str, str], _BarCacheEntry] = {}
        self._ws_task: Optional["asyncio.Task[None]"] = None
        self._ws_last_error: Optional[str] = None
        self._ws_connected = False
        self._ws_tick_subscribed = 0
        self._ws_depth_subscribed = 0
        self._reference_task: Optional["asyncio.Task[None]"] = None
        self._reference_last_error: Optional[str] = None
        self._reference_next_attempt = 0.0
        self._instrument_cache: Optional[Tuple[Instrument, ...]] = None
        self._instrument_last_error: Optional[str] = None
        self._instrument_next_attempt = 0.0
        self._instrument_permission_denied: Dict[str, float] = {}
        self._permission_denied_until: Dict[Tuple[str, Tuple[str, ...]], float] = {}
        self._quote_permission_denied: Dict[str, float] = {}
        self._bar_permission_denied: Dict[Tuple[str, str], float] = {}
        self._last_http_error_code: Optional[int] = None
        self._last_http_error_operation: Optional[str] = None
        self._sequence = 0
        self._closed = False

    @property
    def name(self) -> str:
        return "alltick"

    @property
    def universe_scope(self) -> str:
        # AllTick exposes no instrument-discovery API.  Returning the configured
        # universe is explicit and keeps the batch route from claiming it has a
        # complete exchange catalogue.
        return "configured_symbols"

    @property
    def realtime_status(self) -> Mapping[str, Any]:
        task = self._ws_task
        if not self.use_websocket:
            state = "disabled"
        elif task is None:
            state = "idle"
        elif task is not None and not task.done() and self._ws_connected and self._ws_tick_subscribed:
            state = "running"
        elif task is not None and not task.done():
            state = "reconnecting" if self._ws_last_error else "connecting"
        else:
            state = "stopped"
        reference_count = sum(
            1 for symbol in self._configured_symbols if (symbol, "1d") in self._bar_cache
        )
        status_now = self._monotonic()
        return {
            "enabled": self.use_websocket,
            "state": state,
            "requested_symbols": len(self._configured_symbols),
            "subscribed_symbols": self._ws_tick_subscribed,
            "order_book_subscribed_symbols": self._ws_depth_subscribed,
            "subscription_limit": self.websocket_symbol_limit,
            "limited_symbols": max(
                0, len(self._configured_symbols) - self.websocket_symbol_limit
            ),
            "last_error_type": self._ws_last_error,
            "order_book_snapshot_error_type": self._depth_last_error,
            "order_book_realtime_error_type": self._ws_depth_last_error,
            "order_book_realtime_error_code": self._ws_depth_last_error_code,
            "order_book_permission_denied_symbols": sorted(
                symbol
                for symbol, denied_until in self._depth_permission_denied.items()
                if status_now < denied_until
            ),
            "order_book_enabled": self.use_order_book,
            "daily_reference_symbols": reference_count,
            "daily_reference_error_type": self._reference_last_error,
            "daily_reference_enabled": self.bootstrap_daily_reference,
            "static_info_error_type": self._instrument_last_error,
            "static_info_enabled": self.fetch_static_info,
            "static_info_permission_denied_symbols": sorted(
                symbol
                for symbol, denied_until in self._instrument_permission_denied.items()
                if status_now < denied_until
            ),
            "quote_permission_denied_symbols": sorted(
                symbol
                for symbol, denied_until in self._quote_permission_denied.items()
                if status_now < denied_until
            ),
            "bar_permission_denied_symbols": sorted(
                "{}:{}".format(symbol, interval)
                for (symbol, interval), denied_until in self._bar_permission_denied.items()
                if status_now < denied_until
            ),
            "last_http_error_code": self._last_http_error_code,
            "last_http_error_operation": self._last_http_error_operation,
        }

    def _http_guard(self) -> asyncio.Lock:
        if self._http_lock is None:
            self._http_lock = asyncio.Lock()
        return self._http_lock

    def _quote_guard(self) -> asyncio.Lock:
        if self._quote_lock is None:
            self._quote_lock = asyncio.Lock()
        return self._quote_lock

    def _quote_fetch_guard(self) -> asyncio.Lock:
        if self._quote_fetch_lock is None:
            self._quote_fetch_lock = asyncio.Lock()
        return self._quote_fetch_lock

    def _bar_guard(self) -> asyncio.Lock:
        if self._bar_lock is None:
            self._bar_lock = asyncio.Lock()
        return self._bar_lock

    def _websocket_guard(self) -> asyncio.Lock:
        if self._ws_lock is None:
            self._ws_lock = asyncio.Lock()
        return self._ws_lock

    def _instrument_guard(self) -> asyncio.Lock:
        if self._instrument_lock is None:
            self._instrument_lock = asyncio.Lock()
        return self._instrument_lock

    @staticmethod
    def _normalize_supported(symbols: Sequence[str]) -> Tuple[str, ...]:
        normalized = tuple(dict.fromkeys(normalize_symbol(item) for item in symbols))
        unsupported = [item for item in normalized if item.rsplit(".", 1)[1] not in _SUPPORTED_EXCHANGES]
        if unsupported:
            raise ValueError(
                "AllTick documents only .SH and .SZ A-share codes: {}".format(
                    ", ".join(unsupported)
                )
            )
        return normalized

    def _trace(self) -> str:
        return uuid.uuid4().hex

    def _next_sequence(self) -> int:
        self._sequence = (self._sequence + 1) % 2_147_483_647
        return self._sequence or 1

    async def _invoke_requester(
        self,
        method: str,
        endpoint: str,
        params: Mapping[str, str],
        body: Optional[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        call = self._requester
        arguments = (method, "{}{}".format(self.http_base_url, endpoint), params, body, self.request_timeout_seconds)
        if inspect.iscoroutinefunction(call):
            result = await call(*arguments)
        else:
            result = await asyncio.to_thread(call, *arguments)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, Mapping):
            raise AllTickDataError("AllTick response must be a JSON object")
        return result

    @staticmethod
    def _response_code(payload: Mapping[str, Any]) -> int:
        try:
            return int(payload.get("ret", 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _request_symbols(
        query: Optional[Mapping[str, Any]], body: Optional[Mapping[str, Any]]
    ) -> Tuple[str, ...]:
        envelope = query if query is not None else body
        if not isinstance(envelope, Mapping):
            return ()
        data = envelope.get("data")
        if not isinstance(data, Mapping):
            return ()
        raw: List[Any] = []
        code = data.get("code")
        if code:
            raw.append(code)
        for key in ("symbol_list", "data_list"):
            rows = data.get(key)
            if isinstance(rows, list):
                raw.extend(
                    row.get("code")
                    for row in rows
                    if isinstance(row, Mapping) and row.get("code")
                )
        return tuple(sorted({str(item).strip().upper() for item in raw if str(item).strip()}))

    def _business_error(self, payload: Mapping[str, Any], operation: str) -> AllTickError:
        code = self._response_code(payload)
        # Do not echo any vendor-controlled string. AllTick authentication is
        # carried in URLs, so even error/trace fields are treated as untrusted.
        suffix = " (ret={})".format(code)
        if code == 401:
            return AllTickAuthenticationError(
                "AllTick token authentication failed" + suffix,
                ret_code=code,
                operation=operation,
            )
        if code in _RETRYABLE_RET_CODES:
            return AllTickRateLimitError(
                "AllTick rate limit reached for {}{}".format(operation, suffix),
                ret_code=code,
                operation=operation,
            )
        if code == 603:
            return AllTickPermissionError(
                "AllTick plan request size is not allowed for {}{}".format(
                    operation, suffix
                ),
                ret_code=code,
                operation=operation,
            )
        if code == 604:
            return AllTickPermissionError(
                "AllTick token is not authorized for requested code(s) in {}{}".format(
                    operation, suffix
                ),
                ret_code=code,
                operation=operation,
            )
        return AllTickDataError(
            "AllTick rejected {}{}".format(operation, suffix),
            ret_code=code or None,
            operation=operation,
        )

    @staticmethod
    def _paused_code_permission_error(operation: str) -> AllTickPermissionError:
        return AllTickPermissionError(
            "AllTick token is not authorized for requested code(s) in {} "
            "(ret=604; retry is temporarily paused)".format(operation),
            ret_code=604,
            operation=operation,
        )

    async def _request_json(
        self,
        method: str,
        endpoint: str,
        *,
        operation: str,
        query: Optional[Mapping[str, Any]] = None,
        body: Optional[Mapping[str, Any]] = None,
    ) -> Mapping[str, Any]:
        permission_key = (operation, self._request_symbols(query, body))
        denied_until = self._permission_denied_until.get(permission_key)
        now = self._monotonic()
        if denied_until is not None:
            if now < denied_until:
                self._last_http_error_code = 604
                self._last_http_error_operation = operation
                raise self._paused_code_permission_error(operation)
            self._permission_denied_until.pop(permission_key, None)

        params: Dict[str, str] = {"token": self._token}
        if query is not None:
            params["query"] = json.dumps(query, ensure_ascii=False, separators=(",", ":"))

        last_error: Optional[AllTickError] = None
        for attempt in range(self.retries + 1):
            async with self._http_guard():
                now = self._monotonic()
                if self._last_http_started is not None:
                    wait_for = self.http_min_interval_seconds - (now - self._last_http_started)
                    if wait_for > 0:
                        await asyncio.sleep(wait_for)
                self._last_http_started = self._monotonic()
                try:
                    payload = await self._invoke_requester(method, endpoint, params, body)
                except asyncio.CancelledError:
                    raise
                except _HTTPStatusError as exc:
                    if exc.status == 401:
                        last_error = AllTickAuthenticationError(
                            "AllTick token authentication failed (HTTP 401)",
                            operation=operation,
                        )
                    elif exc.status == 429:
                        last_error = AllTickRateLimitError(
                            "AllTick rate limit reached for {} (HTTP 429)".format(operation),
                            operation=operation,
                        )
                    elif exc.status >= 500:
                        last_error = AllTickTransportError(
                            "AllTick {} failed with HTTP {}".format(operation, exc.status)
                        )
                    else:
                        last_error = AllTickDataError(
                            "AllTick {} failed with HTTP {}".format(operation, exc.status)
                        )
                except AllTickError as exc:
                    last_error = exc
                except Exception as exc:
                    # Never include str(exc): urllib/websocket exceptions often
                    # embed the full URL, and AllTick puts the token in it.
                    last_error = AllTickTransportError(
                        "AllTick {} transport failed ({})".format(operation, type(exc).__name__)
                    )
                else:
                    code = self._response_code(payload)
                    if code == 200:
                        data = payload.get("data")
                        if not isinstance(data, Mapping):
                            raise AllTickDataError(
                                "AllTick {} response data must be an object".format(operation)
                            )
                        self._permission_denied_until.pop(permission_key, None)
                        self._last_http_error_code = None
                        self._last_http_error_operation = None
                        return payload
                    last_error = self._business_error(payload, operation)
                    self._last_http_error_code = code or None
                    self._last_http_error_operation = operation
                    if code == 604 and self.permission_retry_seconds:
                        self._permission_denied_until[permission_key] = (
                            self._monotonic() + self.permission_retry_seconds
                        )

            retryable = isinstance(last_error, (AllTickRateLimitError, AllTickTransportError))
            if not retryable or attempt >= self.retries:
                break
            delay = self.retry_backoff_seconds * (2 ** attempt)
            if delay:
                await asyncio.sleep(delay + random.uniform(0, min(0.25, delay / 2)))
        assert last_error is not None
        raise last_error

    def _daily_reference(
        self, symbol: str, timestamp: datetime
    ) -> Tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal], Optional[Decimal]]:
        entry = self._bar_cache.get((symbol, "1d"))
        if entry is None or not entry.bars:
            return None, None, None, None
        bars = list(entry.bars)
        current = bars[-1] if bars[-1].timestamp.date() == timestamp.date() else None
        if current is not None:
            previous = bars[-2].close if len(bars) >= 2 else None
            return current.open, current.high, current.low, previous
        return None, None, None, bars[-1].close

    async def _enrich_cached_quote(self, symbol: str) -> None:
        async with self._quote_guard():
            quote = self._quotes.get(symbol)
            if quote is None:
                return
            opened, high, low, previous = self._daily_reference(symbol, quote.timestamp)
            if all(value is None for value in (opened, high, low, previous)):
                return
            self._quotes[symbol] = Quote(
                symbol=quote.symbol,
                timestamp=quote.timestamp,
                last=quote.last,
                open=opened if opened is not None else quote.open,
                high=high if high is not None else quote.high,
                low=low if low is not None else quote.low,
                previous_close=(
                    previous if previous is not None else quote.previous_close
                ),
                bid=quote.bid,
                ask=quote.ask,
                bid_size=quote.bid_size,
                ask_size=quote.ask_size,
                volume=quote.volume,
                turnover=quote.turnover,
                source=quote.source,
            )

    def _quote_from_tick(
        self,
        row: Mapping[str, Any],
        *,
        source: str = "alltick",
    ) -> Optional[Tuple[Quote, Optional[int]]]:
        try:
            symbol = normalize_symbol(str(row.get("code") or ""))
        except ValueError:
            return None
        if symbol.rsplit(".", 1)[1] not in _SUPPORTED_EXCHANGES:
            return None
        timestamp = _epoch_time(row.get("tick_time"))
        price = _decimal(row.get("price"), positive=True)
        if timestamp is None or price is None:
            return None
        volume = _integer(row.get("volume"))
        turnover = _decimal(row.get("turnover"))
        if volume is None or turnover is None:
            return None
        opened, high, low, previous = self._daily_reference(symbol, timestamp)
        sequence = _integer(row.get("seq"))
        quote = Quote(
            symbol=symbol,
            timestamp=timestamp,
            last=price,
            open=opened,
            high=high,
            low=low,
            previous_close=previous,
            volume=volume,
            turnover=turnover,
            source=source,
        )
        return (
            self._quote_with_depth(quote),
            sequence,
        )

    @staticmethod
    def _best_depth_level(rows: Any) -> Tuple[Optional[Decimal], int]:
        if not isinstance(rows, list):
            return None, 0
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            price = _decimal(row.get("price"), positive=True)
            size = _integer(row.get("volume"))
            if price is not None:
                return price, size if size is not None else 0
        return None, 0

    def _quote_with_depth(
        self, quote: Quote, snapshot: Optional[_DepthSnapshot] = None
    ) -> Quote:
        depth = snapshot or self._depths.get(quote.symbol)
        freshness_seconds = max(
            30.0,
            self.quote_cache_seconds,
            self.websocket_heartbeat_seconds * 3.0,
        )
        usable = depth is not None
        if depth is not None:
            received_age = max(0.0, self._monotonic() - depth.received_at)
            event_lag = (quote.timestamp - depth.timestamp).total_seconds()
            usable = (
                quote.timestamp.date() == depth.timestamp.date()
                and received_age <= freshness_seconds
                and event_lag <= freshness_seconds
            )
        # AllTick trade ticks never contain bid/ask fields. Clear an expired
        # cached book rather than carrying yesterday's or a disconnected
        # snapshot forward on a fresh trade.
        bid = depth.bid if usable and depth is not None else None
        ask = depth.ask if usable and depth is not None else None
        bid_size = depth.bid_size if usable and depth is not None else 0
        ask_size = depth.ask_size if usable and depth is not None else 0
        return Quote(
            symbol=quote.symbol,
            timestamp=quote.timestamp,
            last=quote.last,
            open=quote.open,
            high=quote.high,
            low=quote.low,
            previous_close=quote.previous_close,
            bid=bid,
            ask=ask,
            bid_size=bid_size,
            ask_size=ask_size,
            volume=quote.volume,
            turnover=quote.turnover,
            source=quote.source,
        )

    async def _accept_tick(
        self,
        row: Mapping[str, Any],
        *,
        source: str = "alltick",
        received_via_websocket: bool = False,
    ) -> Optional[Quote]:
        parsed = self._quote_from_tick(row, source=source)
        if parsed is None:
            return None
        quote, sequence = parsed
        async with self._quote_guard():
            previous = self._quotes.get(quote.symbol)
            previous_sequence = self._quote_sequences.get(quote.symbol)
            if previous is not None and quote.timestamp < previous.timestamp:
                return previous
            if (
                sequence is not None
                and previous_sequence is not None
                and sequence <= previous_sequence
                and previous is not None
                and quote.timestamp <= previous.timestamp
            ):
                return previous
            self._quotes[quote.symbol] = quote
            if sequence is not None:
                self._quote_sequences[quote.symbol] = sequence
            if received_via_websocket:
                self._quote_ws_at[quote.symbol] = self._monotonic()
            return quote

    async def _accept_depth(
        self,
        row: Mapping[str, Any],
        *,
        received_via_websocket: bool = False,
    ) -> Optional[Quote]:
        try:
            symbol = normalize_symbol(str(row.get("code") or ""))
        except ValueError:
            return None
        if symbol.rsplit(".", 1)[1] not in _SUPPORTED_EXCHANGES:
            return None
        timestamp = _epoch_time(row.get("tick_time"))
        bids = row.get("bids")
        asks = row.get("asks")
        if timestamp is None or (not isinstance(bids, list) and not isinstance(asks, list)):
            return None
        bid, bid_size = self._best_depth_level(bids)
        ask, ask_size = self._best_depth_level(asks)
        sequence = _integer(row.get("seq"))
        snapshot = _DepthSnapshot(
            timestamp=timestamp,
            received_at=self._monotonic(),
            bid=bid,
            ask=ask,
            bid_size=bid_size,
            ask_size=ask_size,
            sequence=sequence,
        )
        async with self._quote_guard():
            previous_depth = self._depths.get(symbol)
            previous_sequence = self._depth_sequences.get(symbol)
            if previous_depth is not None and timestamp < previous_depth.timestamp:
                return self._quotes.get(symbol)
            if (
                sequence is not None
                and previous_sequence is not None
                and sequence <= previous_sequence
                and previous_depth is not None
                and timestamp <= previous_depth.timestamp
            ):
                return self._quotes.get(symbol)
            self._depths[symbol] = snapshot
            if received_via_websocket:
                self._ws_depth_last_error = None
                self._ws_depth_last_error_code = None
                self._depth_last_error = None
            else:
                self._depth_last_error = None
            if sequence is not None:
                self._depth_sequences[symbol] = sequence
            quote = self._quotes.get(symbol)
            if quote is None:
                return None
            enriched = self._quote_with_depth(quote, snapshot)
            self._quotes[symbol] = enriched
            return enriched

    async def _fetch_quote_rows(
        self, symbols: Sequence[str]
    ) -> Dict[str, AllTickPermissionError]:
        denied: Dict[str, AllTickPermissionError] = {}
        accepted_any = False
        permission_now = self._monotonic()
        eligible = []
        for symbol in symbols:
            denied_until = self._quote_permission_denied.get(symbol, 0.0)
            if permission_now < denied_until:
                denied[symbol] = self._paused_code_permission_error(
                    "trade-tick"
                )
            else:
                self._quote_permission_denied.pop(symbol, None)
                eligible.append(symbol)

        async def fetch_chunk(chunk: Tuple[str, ...]) -> None:
            nonlocal accepted_any
            trace = self._trace()
            envelope = {
                "trace": trace,
                "data": {"symbol_list": [{"code": item} for item in chunk]},
            }
            try:
                payload = await self._request_json(
                    "GET",
                    "/trade-tick",
                    operation="trade-tick",
                    query=envelope,
                )
            except AllTickPermissionError as exc:
                if exc.ret_code != 604:
                    raise
                if len(chunk) > 1:
                    # A batch-level 604 does not identify the rejected code.
                    # Probe each code once; singleton circuits then prevent the
                    # collector from repeating denied requests every cycle.
                    for symbol in chunk:
                        await fetch_chunk((symbol,))
                    return
                symbol = chunk[0]
                denied[symbol] = exc
                self._quote_permission_denied[symbol] = (
                    self._monotonic() + self.permission_retry_seconds
                )
                return
            data = payload["data"]
            rows = data.get("tick_list")
            if not isinstance(rows, list):
                raise AllTickDataError("AllTick trade-tick response has no tick_list")
            requested = set(chunk)
            accepted: List[str] = []
            for row in rows:
                if not isinstance(row, Mapping) or str(row.get("code") or "").upper() not in requested:
                    continue
                quote = await self._accept_tick(row)
                if quote is not None:
                    accepted.append(quote.symbol)
            fetched_at = self._monotonic()
            async with self._quote_guard():
                for symbol in accepted:
                    self._quote_http_at[symbol] = fetched_at
                    self._quote_permission_denied.pop(symbol, None)
            accepted_any = accepted_any or bool(accepted)

        for chunk in _chunks(tuple(eligible), self.batch_size):
            await fetch_chunk(chunk)
        if accepted_any:
            self._ensure_depth_snapshot_task()
        return denied

    async def _fetch_depth_rows(
        self, symbols: Sequence[str]
    ) -> Dict[str, AllTickPermissionError]:
        denied: Dict[str, AllTickPermissionError] = {}
        permission_now = self._monotonic()
        eligible = []
        for symbol in symbols:
            denied_until = self._depth_permission_denied.get(symbol, 0.0)
            if permission_now < denied_until:
                denied[symbol] = self._paused_code_permission_error(
                    "depth-tick"
                )
            else:
                self._depth_permission_denied.pop(symbol, None)
                eligible.append(symbol)

        async def fetch_chunk(chunk: Tuple[str, ...]) -> None:
            envelope = {
                "trace": self._trace(),
                "data": {"symbol_list": [{"code": item} for item in chunk]},
            }
            try:
                payload = await self._request_json(
                    "GET",
                    "/depth-tick",
                    operation="depth-tick",
                    query=envelope,
                )
            except AllTickPermissionError as exc:
                if exc.ret_code != 604:
                    raise
                if len(chunk) > 1:
                    for symbol in chunk:
                        await fetch_chunk((symbol,))
                    return
                symbol = chunk[0]
                denied[symbol] = exc
                self._depth_permission_denied[symbol] = (
                    self._monotonic() + self.permission_retry_seconds
                )
                return
            rows = payload["data"].get("tick_list")
            if not isinstance(rows, list):
                raise AllTickDataError(
                    "AllTick depth-tick response has no tick_list"
                )
            requested = set(chunk)
            for row in rows:
                if (
                    not isinstance(row, Mapping)
                    or str(row.get("code") or "").upper() not in requested
                ):
                    continue
                await self._accept_depth(row)
                self._depth_permission_denied.pop(
                    str(row.get("code") or "").upper(), None
                )

        for chunk in _chunks(tuple(eligible), self.batch_size):
            await fetch_chunk(chunk)
        return denied

    def _ensure_depth_snapshot_task(self) -> None:
        if (
            not self.use_order_book
            or not self._configured_symbols
            or self._closed
            or self._monotonic() < self._depth_next_attempt
        ):
            return
        task = self._depth_task
        if task is not None and not task.done():
            return
        self._depth_task = asyncio.create_task(
            self._depth_snapshot_runner(), name="alltick-order-book-snapshot"
        )

    async def _depth_snapshot_runner(self) -> None:
        denied: Dict[str, AllTickPermissionError] = {}
        try:
            denied = await self._fetch_depth_rows(self._configured_symbols)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._depth_last_error = type(exc).__name__
            retry_after = max(10.0, self.quote_cache_seconds)
            if isinstance(
                exc, (AllTickAuthenticationError, AllTickPermissionError)
            ):
                retry_after = max(
                    60.0, self.permission_retry_seconds, retry_after
                )
            self._depth_next_attempt = self._monotonic() + retry_after
        else:
            self._depth_last_error = (
                "AllTickPermissionError" if denied else None
            )
            if self.use_order_book and self._ws_connected:
                permission_now = self._monotonic()
                eligible = tuple(
                    symbol
                    for symbol in self._configured_symbols[
                        : self.websocket_symbol_limit
                    ]
                    if permission_now
                    >= self._depth_permission_denied.get(symbol, 0.0)
                )
                retry_recovered_permission = (
                    not denied
                    and self._ws_depth_last_error_code == 604
                    and permission_now >= self._ws_depth_suppressed_until
                )
                if eligible and (
                    eligible != self._ws_depth_attempted_symbols
                    or retry_recovered_permission
                ):
                    # HTTP has either isolated a denied code or confirmed that
                    # a previous denial has recovered. Rebuild only the depth
                    # subscription; the tick list remains unchanged.
                    self._ws_depth_suppressed_until = 0.0
                    self._ws_resubscribe_requested = True
            if denied and len(denied) >= len(self._configured_symbols):
                retry_after = max(
                    60.0, self.permission_retry_seconds, self.quote_cache_seconds
                )
            else:
                retry_after = max(10.0, self.quote_cache_seconds)
            self._depth_next_attempt = self._monotonic() + retry_after

    async def _ensure_websocket(self) -> None:
        if not self.use_websocket or not self._configured_symbols or self._closed:
            return
        async with self._websocket_guard():
            if self._ws_task is not None and not self._ws_task.done():
                return
            try:
                import websockets  # noqa: F401
            except ImportError as exc:
                raise AllTickDependencyError(
                    "AllTick realtime mode requires the websockets package"
                ) from exc
            self._ws_task = asyncio.create_task(
                self._websocket_runner(), name="alltick-realtime-quotes"
            )

    async def start_realtime(self) -> None:
        """Start the WS lifecycle without waiting for any HTTP bootstrap."""

        await self._ensure_websocket()

    def _ensure_daily_reference_task(self) -> None:
        if (
            not self.bootstrap_daily_reference
            or not self._configured_symbols
            or self._closed
            or self._monotonic() < self._reference_next_attempt
        ):
            return
        task = self._reference_task
        if task is not None and not task.done():
            return
        self._reference_task = asyncio.create_task(
            self._daily_reference_runner(), name="alltick-daily-reference"
        )

    async def _daily_reference_runner(self) -> None:
        denied: Dict[str, AllTickPermissionError] = {}
        try:
            denied = await self._refresh_bars_batch(
                self._configured_symbols, "1d"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._reference_last_error = type(exc).__name__
            self._reference_next_attempt = self._monotonic() + max(
                60.0, self.permission_retry_seconds, self.bars_cache_seconds
            )
        else:
            self._reference_last_error = (
                "AllTickPermissionError" if denied else None
            )
            self._reference_next_attempt = self._monotonic() + max(
                1.0, self.bars_cache_seconds
            )

    async def _websocket_runner(self) -> None:
        import websockets

        while not self._closed:
            self._ws_connected = False
            self._ws_tick_subscribed = 0
            self._ws_depth_subscribed = 0
            permission_now = self._monotonic()
            eligible_symbols = tuple(
                symbol
                for symbol in self._configured_symbols
                if permission_now >= self._quote_permission_denied.get(symbol, 0.0)
            )
            subscribed_symbols = eligible_symbols[: self.websocket_symbol_limit]
            if not subscribed_symbols:
                self._ws_last_error = "AllTickPermissionError"
                retry_delay = max(
                    0.1,
                    self.websocket_reconnect_seconds,
                    self.permission_retry_seconds,
                )
                await asyncio.sleep(retry_delay)
                continue
            resubscribe_after_close = False
            try:
                url = "{}?{}".format(self.websocket_url, urlencode({"token": self._token}))
                async with websockets.connect(
                    url,
                    ping_interval=None,
                    open_timeout=self.request_timeout_seconds,
                    close_timeout=5,
                ) as socket:
                    self._ws_connected = True
                    subscribe = {
                        "cmd_id": 22004,
                        "seq_id": self._next_sequence(),
                        "trace": self._trace(),
                        "data": {
                            "symbol_list": [
                                {"code": symbol}
                                for symbol in subscribed_symbols
                            ]
                        },
                    }
                    await socket.send(json.dumps(subscribe, separators=(",", ":")))
                    depth_permission_now = self._monotonic()
                    depth_subscribed_symbols = tuple(
                        symbol
                        for symbol in subscribed_symbols
                        if depth_permission_now
                        >= self._depth_permission_denied.get(symbol, 0.0)
                    )
                    subscribe_depth = (
                        self.use_order_book
                        and bool(depth_subscribed_symbols)
                        and self._monotonic() >= self._ws_depth_suppressed_until
                    )
                    if subscribe_depth:
                        self._ws_depth_attempted_symbols = (
                            depth_subscribed_symbols
                        )
                        await asyncio.sleep(self.websocket_subscription_gap_seconds)
                        depth_subscribe = {
                            "cmd_id": 22002,
                            "seq_id": self._next_sequence(),
                            "trace": self._trace(),
                            "data": {
                                "symbol_list": [
                                    {"code": symbol, "depth_level": 5}
                                    for symbol in depth_subscribed_symbols
                                ]
                            },
                        }
                        await socket.send(
                            json.dumps(depth_subscribe, separators=(",", ":"))
                        )
                    next_heartbeat = self._monotonic() + self.websocket_heartbeat_seconds
                    while not self._closed:
                        if self._ws_resubscribe_requested:
                            self._ws_resubscribe_requested = False
                            resubscribe_after_close = True
                            break
                        timeout = max(0.1, next_heartbeat - self._monotonic())
                        try:
                            raw = await asyncio.wait_for(socket.recv(), timeout=timeout)
                        except asyncio.TimeoutError:
                            heartbeat = {
                                "cmd_id": 22000,
                                "seq_id": self._next_sequence(),
                                "trace": self._trace(),
                                "data": {},
                            }
                            await socket.send(json.dumps(heartbeat, separators=(",", ":")))
                            next_heartbeat = self._monotonic() + self.websocket_heartbeat_seconds
                            continue
                        try:
                            message = json.loads(raw)
                        except (TypeError, json.JSONDecodeError):
                            continue
                        if not isinstance(message, Mapping):
                            continue
                        command = _integer(message.get("cmd_id"))
                        if command in {22001, 22003, 22005} and "ret" in message:
                            if self._response_code(message) != 200:
                                subscription_error = self._business_error(
                                    message, "websocket subscription"
                                )
                                if command == 22003:
                                    # Order book is optional enrichment. A plan
                                    # may permit trades but not depth; keep the
                                    # valid tick subscription alive in that case.
                                    if isinstance(
                                        subscription_error,
                                        AllTickAuthenticationError,
                                    ):
                                        raise subscription_error
                                    self._ws_depth_last_error = type(
                                        subscription_error
                                    ).__name__
                                    self._ws_depth_last_error_code = (
                                        subscription_error.ret_code
                                    )
                                    self._ws_depth_subscribed = 0
                                    if subscription_error.ret_code in {603, 604}:
                                        self._ws_depth_suppressed_until = (
                                            self._monotonic()
                                            + self.permission_retry_seconds
                                        )
                                    if subscription_error.ret_code == 604:
                                        self._depth_next_attempt = 0.0
                                        self._ensure_depth_snapshot_task()
                                else:
                                    raise subscription_error
                            if command == 22005:
                                self._ws_tick_subscribed = len(subscribed_symbols)
                                self._ws_last_error = None
                            elif command == 22003:
                                if self._response_code(message) == 200:
                                    self._ws_depth_subscribed = len(
                                        depth_subscribed_symbols
                                    )
                                    self._ws_depth_last_error = None
                                    self._ws_depth_last_error_code = None
                                    self._ws_depth_suppressed_until = 0.0
                                    self._depth_last_error = None
                        elif command == 22998:
                            data = message.get("data")
                            if isinstance(data, Mapping):
                                await self._accept_tick(
                                    data, received_via_websocket=True
                                )
                        elif command == 22999:
                            data = message.get("data")
                            if isinstance(data, Mapping):
                                await self._accept_depth(
                                    data, received_via_websocket=True
                                )
                        if self._monotonic() >= next_heartbeat:
                            heartbeat = {
                                "cmd_id": 22000,
                                "seq_id": self._next_sequence(),
                                "trace": self._trace(),
                                "data": {},
                            }
                            await socket.send(json.dumps(heartbeat, separators=(",", ":")))
                            next_heartbeat = self._monotonic() + self.websocket_heartbeat_seconds
                if resubscribe_after_close and self.websocket_reconnect_seconds:
                    await asyncio.sleep(self.websocket_reconnect_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Store only the class name; handshake errors can contain the
                # credential-bearing WebSocket URL.
                self._ws_last_error = type(exc).__name__
                self._ws_connected = False
                self._ws_tick_subscribed = 0
                self._ws_depth_subscribed = 0
                reconnect_delay = self.websocket_reconnect_seconds
                if isinstance(
                    exc, (AllTickAuthenticationError, AllTickPermissionError)
                ):
                    if (
                        isinstance(exc, AllTickPermissionError)
                        and exc.ret_code == 604
                        and len(subscribed_symbols) > 1
                    ):
                        # HTTP performs the authoritative per-code isolation.
                        # Retry soon enough to rebuild the subscription without
                        # denied codes, rather than sleeping the full circuit.
                        reconnect_delay = max(reconnect_delay, 10.0)
                    else:
                        reconnect_delay = max(
                            reconnect_delay, self.permission_retry_seconds
                        )
                if not self._closed and reconnect_delay:
                    await asyncio.sleep(reconnect_delay)
        self._ws_connected = False
        self._ws_tick_subscribed = 0
        self._ws_depth_subscribed = 0

    async def _fetch_quotes(self, symbols: Sequence[str], *, strict: bool) -> List[Quote]:
        requested = self._normalize_supported(symbols)
        if not requested:
            return []
        # The realtime lifecycle is independent from the optional HTTP
        # bootstrap. A denied/unavailable /trade-tick request must not prevent
        # a valid WebSocket entitlement from connecting.
        await self._ensure_websocket()
        now = self._monotonic()
        subscribed = (
            set(self._configured_symbols[: self.websocket_symbol_limit])
            if self.use_websocket
            else set()
        )
        async with self._quote_guard():
            missing = []
            for symbol in requested:
                quote = self._quotes.get(symbol)
                fetched_at = self._quote_http_at.get(symbol)
                websocket_at = self._quote_ws_at.get(symbol)
                websocket_fresh = (
                    symbol in subscribed
                    and self._ws_tick_subscribed > 0
                    and websocket_at is not None
                    and now - websocket_at
                    <= max(
                        self.quote_cache_seconds,
                        self.websocket_heartbeat_seconds * 3.0,
                    )
                )
                needs_http_refresh = (
                    not websocket_fresh
                    and (
                        fetched_at is None
                        or now - fetched_at > self.quote_cache_seconds
                    )
                )
                if quote is None or needs_http_refresh:
                    missing.append(symbol)
        permission_errors: Dict[str, AllTickPermissionError] = {}
        try:
            if missing:
                # Startup collection and the batch REST route may request the same
                # symbols concurrently. Recheck under a single-flight lock so one
                # token does not spend quota on duplicate bootstrap requests.
                async with self._quote_fetch_guard():
                    async with self._quote_guard():
                        refreshed_now = self._monotonic()
                        rechecked = []
                        for symbol in missing:
                            websocket_at = self._quote_ws_at.get(symbol)
                            websocket_fresh = (
                                symbol in subscribed
                                and self._ws_tick_subscribed > 0
                                and websocket_at is not None
                                and refreshed_now - websocket_at
                                <= max(
                                    self.quote_cache_seconds,
                                    self.websocket_heartbeat_seconds * 3.0,
                                )
                            )
                            if symbol not in self._quotes or (
                                not websocket_fresh
                                and (
                                    self._quote_http_at.get(symbol) is None
                                    or refreshed_now - self._quote_http_at[symbol]
                                    > self.quote_cache_seconds
                                )
                            ):
                                rechecked.append(symbol)
                        missing = rechecked
                    if missing:
                        permission_errors = await self._fetch_quote_rows(missing)
        finally:
            # Two latest daily bars provide current OHLC and previous close,
            # which /trade-tick intentionally does not include.
            if any(symbol in self._quotes for symbol in requested):
                self._ensure_daily_reference_task()
                # Keep HTTP depth as a periodic fallback even while WebSocket
                # trades are fresh; it also isolates a mixed-permission WS
                # depth batch so the authorized subset can be resubscribed.
                self._ensure_depth_snapshot_task()
        async with self._quote_guard():
            result = []
            for symbol in requested:
                quote = self._quotes.get(symbol)
                if quote is None:
                    continue
                quote = self._quote_with_depth(quote)
                self._quotes[symbol] = quote
                result.append(quote)
        missing_after = [symbol for symbol in requested if symbol not in {item.symbol for item in result}]
        unexplained_missing = [
            symbol for symbol in missing_after if symbol not in permission_errors
        ]
        if strict and unexplained_missing:
            raise AllTickDataError(
                "AllTick returned no usable latest trade for: {}".format(
                    ", ".join(unexplained_missing)
                )
            )
        if missing_after and strict and not result:
            denied_error = next(
                (
                    permission_errors[symbol]
                    for symbol in missing_after
                    if symbol in permission_errors
                ),
                None,
            )
            if denied_error is not None:
                raise denied_error
            raise AllTickDataError(
                "AllTick returned no usable latest trade for: {}".format(
                    ", ".join(missing_after)
                )
            )
        return result

    async def fetch_quotes(self, symbols: Sequence[str]) -> List[Quote]:
        return await self._fetch_quotes(symbols, strict=True)

    async def fetch_quotes_relaxed(self, symbols: Sequence[str]) -> List[Quote]:
        return await self._fetch_quotes(symbols, strict=False)

    @staticmethod
    def _bar_from_row(symbol: str, interval: str, row: Mapping[str, Any]) -> Optional[Bar]:
        timestamp = _epoch_time(row.get("timestamp"))
        opened = _decimal(row.get("open_price"), positive=True)
        closed = _decimal(row.get("close_price"), positive=True)
        high = _decimal(row.get("high_price"), positive=True)
        low = _decimal(row.get("low_price"), positive=True)
        volume = _integer(row.get("volume"))
        turnover = _decimal(row.get("turnover"))
        if timestamp is None or None in (opened, closed, high, low, volume, turnover):
            return None
        assert opened is not None and closed is not None and high is not None and low is not None
        assert volume is not None and turnover is not None
        if high < max(opened, closed, low) or low > min(opened, closed, high):
            return None
        return Bar(
            symbol=symbol,
            interval=interval,
            timestamp=timestamp,
            open=opened,
            high=high,
            low=low,
            close=closed,
            volume=volume,
            turnover=turnover,
            source="alltick",
        )

    @classmethod
    def _parse_bars(
        cls,
        symbol: str,
        interval: str,
        rows: Any,
    ) -> List[Bar]:
        if not isinstance(rows, list):
            raise AllTickDataError("AllTick kline list must be an array")
        by_time: Dict[datetime, Bar] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            bar = cls._bar_from_row(symbol, interval, row)
            if bar is not None:
                by_time[bar.timestamp] = bar
        return [by_time[key] for key in sorted(by_time)]

    @staticmethod
    def _filter_bars(
        bars: Sequence[Bar],
        start: Optional[datetime],
        end: Optional[datetime],
        limit: int,
    ) -> List[Bar]:
        result = [
            bar
            for bar in bars
            if (start is None or bar.timestamp >= start)
            and (end is None or bar.timestamp <= end)
        ]
        return result[-int(limit) :]

    def seed_bars(self, bars: Sequence[Bar]) -> int:
        """Hydrate the in-memory cache from provider-owned SQLite rows.

        Runtime calls this before background tasks start, so no async lock is
        required. Rows from another provider are deliberately ignored.
        """

        grouped: Dict[Tuple[str, str], Dict[datetime, Bar]] = {}
        accepted = 0
        for bar in bars:
            if not isinstance(bar, Bar) or not str(bar.source).startswith("alltick"):
                continue
            try:
                symbol = self._normalize_supported([bar.symbol])[0]
            except ValueError:
                continue
            interval = str(bar.interval).strip().lower()
            if interval not in _INTERVAL_TYPES:
                continue
            grouped.setdefault((symbol, interval), {})[bar.timestamp] = bar
            accepted += 1
        now = self._monotonic()
        for key, by_time in grouped.items():
            ordered = tuple(by_time[item] for item in sorted(by_time))[-500:]
            self._bar_cache[key] = _BarCacheEntry(
                # Mark persisted history stale so the first live cycle follows
                # AllTick's recommendation and refreshes only the latest two
                # bars through /batch-kline instead of re-downloading history.
                updated_at=now - self.bars_cache_seconds - 1.0,
                bars=ordered,
                coverage=len(ordered),
            )
        return accepted

    async def fetch_bars(
        self,
        symbol: str,
        interval: str = "1m",
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 200,
    ) -> List[Bar]:
        normalized = self._normalize_supported([symbol])[0]
        selected_interval = str(interval).strip().lower()
        if selected_interval not in _INTERVAL_TYPES:
            raise ValueError(
                "unsupported AllTick interval: {} (use {})".format(
                    interval, ", ".join(_INTERVAL_TYPES)
                )
            )
        if int(limit) <= 0:
            raise ValueError("limit must be positive")
        start_time = ensure_market_time(start) if start is not None else None
        end_time = ensure_market_time(end) if end is not None else None
        if start_time is not None and end_time is not None and start_time > end_time:
            return []
        requested_limit = min(500, int(limit))
        key = (normalized, selected_interval)
        denied_until = self._bar_permission_denied.get(key, 0.0)
        if self._monotonic() < denied_until:
            raise self._paused_code_permission_error("kline")
        self._bar_permission_denied.pop(key, None)
        async with self._bar_guard():
            cached = self._bar_cache.get(key)
            now = self._monotonic()
            if (
                cached is not None
                and cached.coverage >= requested_limit
                and now - cached.updated_at <= self.bars_cache_seconds
            ):
                return self._filter_bars(cached.bars, start_time, end_time, int(limit))

            trace = self._trace()
            envelope = {
                "trace": trace,
                "data": {
                    "code": normalized,
                    "kline_type": _INTERVAL_TYPES[selected_interval],
                    # AllTick documents that stock codes do not support a
                    # caller-selected end timestamp; filter start/end locally.
                    "kline_timestamp_end": 0,
                    "query_kline_num": requested_limit,
                    "adjust_type": 0,
                },
            }
            try:
                payload = await self._request_json(
                    "GET", "/kline", operation="kline", query=envelope
                )
            except AllTickPermissionError as exc:
                if exc.ret_code == 604:
                    self._bar_permission_denied[key] = (
                        self._monotonic() + self.permission_retry_seconds
                    )
                raise
            data = payload["data"]
            response_code = str(data.get("code") or "").upper()
            if response_code and response_code != normalized:
                raise AllTickDataError("AllTick kline response code does not match request")
            bars = self._parse_bars(normalized, selected_interval, data.get("kline_list"))
            self._bar_cache[key] = _BarCacheEntry(
                updated_at=self._monotonic(),
                bars=tuple(bars),
                coverage=requested_limit,
            )
            self._bar_permission_denied.pop(key, None)
            if selected_interval == "1d":
                await self._enrich_cached_quote(normalized)
            return self._filter_bars(bars, start_time, end_time, int(limit))

    async def _refresh_bars_batch(
        self,
        symbols: Sequence[str],
        interval: str,
    ) -> Dict[str, AllTickPermissionError]:
        denied: Dict[str, AllTickPermissionError] = {}
        permission_now = self._monotonic()
        eligible = []
        for symbol in symbols:
            key = (symbol, interval)
            denied_until = self._bar_permission_denied.get(key, 0.0)
            if permission_now < denied_until:
                denied[symbol] = self._paused_code_permission_error(
                    "batch-kline"
                )
            else:
                self._bar_permission_denied.pop(key, None)
                eligible.append(symbol)

        async def refresh_chunk(chunk: Tuple[str, ...]) -> None:
            trace = self._trace()
            envelope = {
                "trace": trace,
                "data": {
                    "data_list": [
                        {
                            "code": symbol,
                            "kline_type": _INTERVAL_TYPES[interval],
                            "kline_timestamp_end": 0,
                            "query_kline_num": 2,
                            "adjust_type": 0,
                        }
                        for symbol in chunk
                    ]
                },
            }
            try:
                payload = await self._request_json(
                    "POST",
                    "/batch-kline",
                    operation="batch-kline",
                    body=envelope,
                )
            except AllTickPermissionError as exc:
                if exc.ret_code != 604:
                    raise
                if len(chunk) > 1:
                    for symbol in chunk:
                        await refresh_chunk((symbol,))
                    return
                symbol = chunk[0]
                denied[symbol] = exc
                self._bar_permission_denied[(symbol, interval)] = (
                    self._monotonic() + self.permission_retry_seconds
                )
                return
            rows = payload["data"].get("kline_list")
            if not isinstance(rows, list):
                raise AllTickDataError("AllTick batch-kline response has no kline_list")
            by_symbol = {
                str(row.get("code") or "").upper(): row
                for row in rows
                if isinstance(row, Mapping)
            }
            async with self._bar_guard():
                for symbol in chunk:
                    row = by_symbol.get(symbol)
                    if row is None:
                        continue
                    incoming = self._parse_bars(symbol, interval, row.get("kline_data"))
                    key = (symbol, interval)
                    existing = self._bar_cache.get(key)
                    merged: Dict[datetime, Bar] = {
                        bar.timestamp: bar for bar in (existing.bars if existing else ())
                    }
                    merged.update({bar.timestamp: bar for bar in incoming})
                    ordered = tuple(merged[key] for key in sorted(merged))
                    self._bar_cache[key] = _BarCacheEntry(
                        updated_at=self._monotonic(),
                        bars=ordered[-500:],
                        coverage=existing.coverage if existing is not None else len(ordered),
                    )
                    self._bar_permission_denied.pop(key, None)
            if interval == "1d":
                for symbol in chunk:
                    await self._enrich_cached_quote(symbol)

        for chunk in _chunks(tuple(eligible), self.kline_batch_size):
            await refresh_chunk(chunk)
        return denied

    async def fetch_bars_many(
        self,
        symbols: Sequence[str],
        interval: str = "1m",
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 200,
    ) -> List[Bar]:
        normalized = self._normalize_supported(symbols)
        selected_interval = str(interval).strip().lower()
        if selected_interval not in _INTERVAL_TYPES:
            raise ValueError("unsupported AllTick interval: {}".format(interval))
        if int(limit) <= 0:
            raise ValueError("limit must be positive")
        start_time = ensure_market_time(start) if start is not None else None
        end_time = ensure_market_time(end) if end is not None else None
        if start_time is not None and end_time is not None and start_time > end_time:
            return []
        requested_limit = min(500, int(limit))
        permission_errors: Dict[str, AllTickPermissionError] = {}
        for symbol in normalized:
            entry = self._bar_cache.get((symbol, selected_interval))
            if entry is None or entry.coverage < requested_limit:
                try:
                    await self.fetch_bars(
                        symbol,
                        interval=selected_interval,
                        start=start_time,
                        end=end_time,
                        limit=limit,
                    )
                except AllTickPermissionError as exc:
                    if exc.ret_code != 604:
                        raise
                    permission_errors[symbol] = exc
                    continue
        now = self._monotonic()
        stale = [
            symbol
            for symbol in normalized
            if (symbol, selected_interval) in self._bar_cache
            and now - self._bar_cache[(symbol, selected_interval)].updated_at
            > self.bars_cache_seconds
        ]
        if stale:
            permission_errors.update(
                await self._refresh_bars_batch(stale, selected_interval)
            )
        result: List[Bar] = []
        for symbol in normalized:
            if symbol in permission_errors:
                continue
            entry = self._bar_cache.get((symbol, selected_interval))
            if entry is not None:
                result.extend(self._filter_bars(entry.bars, start_time, end_time, int(limit)))
        if not result and permission_errors:
            raise next(iter(permission_errors.values()))
        return result

    def _configured_instruments(
        self, static_by_symbol: Optional[Mapping[str, Mapping[str, Any]]] = None
    ) -> List[Instrument]:
        static_rows = dict(static_by_symbol or {})
        instruments: List[Instrument] = []
        for symbol in self._configured_symbols:
            exchange = Exchange(symbol.rsplit(".", 1)[1])
            row = static_rows.get(symbol, {})
            name = str(
                row.get("name_cn")
                or row.get("name_hk")
                or row.get("name_en")
                or self._instrument_names.get(symbol)
                or symbol
            ).strip()
            lot_size = _integer(row.get("lot_size")) or 100
            metadata = {
                "source": "alltick",
                "catalogue_scope": "configured_symbols",
            }
            for field_name in (
                "board",
                "bps",
                "circulating_shares",
                "currency",
                "dividend_yield",
                "eps",
                "eps_ttm",
                "exchange",
                "name_en",
                "name_hk",
                "total_shares",
            ):
                value = row.get(field_name)
                if value not in (None, ""):
                    metadata[field_name] = value
            instruments.append(
                Instrument(
                    symbol=symbol,
                    name=name,
                    exchange=exchange,
                    security_type=SecurityType.STOCK,
                    lot_size=lot_size,
                    metadata=metadata,
                )
            )
        return instruments

    async def fetch_instruments(self) -> List[Instrument]:
        guard = self._instrument_guard()
        if guard.locked():
            if self._instrument_cache is not None:
                return list(self._instrument_cache)
            return self._configured_instruments()
        async with guard:
            return await self._fetch_instruments_locked()

    async def _fetch_instruments_locked(self) -> List[Instrument]:
        fallback = self._configured_instruments()
        if not self.fetch_static_info or not self._configured_symbols or self._closed:
            return fallback
        now = self._monotonic()
        if self._instrument_cache is not None and now < self._instrument_next_attempt:
            return list(self._instrument_cache)
        if now < self._instrument_next_attempt:
            return fallback

        static_by_symbol: Dict[str, Mapping[str, Any]] = {}
        denied: Dict[str, AllTickPermissionError] = {}
        eligible = []
        for symbol in self._configured_symbols:
            denied_until = self._instrument_permission_denied.get(symbol, 0.0)
            if now < denied_until:
                denied[symbol] = self._paused_code_permission_error(
                    "static-info"
                )
            else:
                self._instrument_permission_denied.pop(symbol, None)
                eligible.append(symbol)

        async def fetch_chunk(chunk: Tuple[str, ...]) -> None:
            envelope = {
                "trace": self._trace(),
                "data": {"symbol_list": [{"code": item} for item in chunk]},
            }
            try:
                payload = await self._request_json(
                    "GET", "/static_info", operation="static-info", query=envelope
                )
            except AllTickPermissionError as exc:
                if exc.ret_code != 604:
                    raise
                if len(chunk) > 1:
                    for symbol in chunk:
                        await fetch_chunk((symbol,))
                    return
                symbol = chunk[0]
                denied[symbol] = exc
                self._instrument_permission_denied[symbol] = (
                    self._monotonic() + self.permission_retry_seconds
                )
                return
            rows = payload["data"].get("static_info_list")
            if not isinstance(rows, list):
                raise AllTickDataError(
                    "AllTick static-info response has no static_info_list"
                )
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                raw_symbol = row.get("symbol") or row.get("code")
                try:
                    symbol = normalize_symbol(str(raw_symbol or ""))
                except ValueError:
                    continue
                if symbol in chunk:
                    static_by_symbol[symbol] = row
                    self._instrument_permission_denied.pop(symbol, None)

        try:
            for chunk in _chunks(tuple(eligible), self.batch_size):
                await fetch_chunk(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._instrument_last_error = type(exc).__name__
            self._instrument_next_attempt = self._monotonic() + max(
                300.0, self.permission_retry_seconds
            )
            return fallback

        instruments = self._configured_instruments(static_by_symbol)
        self._instrument_cache = tuple(instruments)
        self._instrument_last_error = (
            "AllTickPermissionError" if denied else None
        )
        retry_after = (
            max(300.0, self.permission_retry_seconds) if denied else 86_400.0
        )
        self._instrument_next_attempt = self._monotonic() + retry_after
        return instruments

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        task = self._ws_task
        self._ws_task = None
        reference_task = self._reference_task
        self._reference_task = None
        depth_task = self._depth_task
        self._depth_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if reference_task is not None:
            reference_task.cancel()
            try:
                await reference_task
            except asyncio.CancelledError:
                pass
        if depth_task is not None:
            depth_task.cancel()
            try:
                await depth_task
            except asyncio.CancelledError:
                pass
        self._quotes.clear()
        self._quote_http_at.clear()
        self._quote_ws_at.clear()
        self._quote_sequences.clear()
        self._depths.clear()
        self._depth_sequences.clear()
        self._bar_cache.clear()
        self._instrument_cache = None
        self._permission_denied_until.clear()
        self._quote_permission_denied.clear()
        self._bar_permission_denied.clear()
        self._depth_permission_denied.clear()
        self._instrument_permission_denied.clear()


__all__ = [
    "ALLTICK_HTTP_BASE_URL",
    "ALLTICK_WEBSOCKET_URL",
    "AllTickError",
    "AllTickConfigurationError",
    "AllTickDependencyError",
    "AllTickAuthenticationError",
    "AllTickPermissionError",
    "AllTickRateLimitError",
    "AllTickTransportError",
    "AllTickDataError",
    "AllTickMarketDataProvider",
]
