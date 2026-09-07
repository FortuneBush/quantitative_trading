"""AShareHub v2 market-data provider.

The adapter intentionally uses only the Python standard library.  AShareHub's
official SDK currently requires a newer Python than this project promises, and
the REST contract is small enough to keep vendor details isolated here.

AShareHub's free plan is request-count limited.  Every outbound attempt is
therefore guarded by a persistent, market-local daily soft budget.  Quotes are
requested as one HTTP basket (at most 200 symbols), cached, and never polled
repeatedly while the market is closed.  Authentication is sent only in the
``X-API-Key`` header and is never included in URLs, status payloads or errors.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import inspect
import json
import os
import tempfile
import time
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
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
    now_market_time,
)
from quant_monitor.market import expected_latest_daily_date, market_status

from .base import (
    BarFetchResult,
    BarFetchTransport,
    MarketDataProvider,
    MarketDataProviderError,
)


ASHAREHUB_BASE_URL = "https://asharehub.com"
ASHAREHUB_REALTIME_BATCH_LIMIT = 200
ASHAREHUB_OFFICIAL_DAILY_LIMIT = 100
ASHAREHUB_EMPTY_DAILY_RECHECK_SECONDS = 900.0


@contextmanager
def _interprocess_lock(path: Path):
    """Serialize budget updates across local processes using a stable lock file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class AShareHubError(MarketDataProviderError):
    """Base error raised by the AShareHub adapter."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        operation: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.operation = operation


class AShareHubConfigurationError(AShareHubError):
    """Required local configuration is missing or unsafe."""


class AShareHubAuthenticationError(AShareHubError):
    """The configured API key was rejected."""


class AShareHubPermissionError(AShareHubError):
    """The key cannot access an endpoint."""


class AShareHubRateLimitError(AShareHubError):
    """AShareHub returned HTTP 429."""


class AShareHubBudgetError(AShareHubError):
    """The local daily soft budget is exhausted or cannot be persisted."""

    def __init__(
        self,
        message: str,
        *,
        reserved: bool = False,
        status_code: Optional[int] = None,
        operation: Optional[str] = None,
    ) -> None:
        super().__init__(message, status_code=status_code, operation=operation)
        self.reserved = bool(reserved)


class AShareHubRequestError(AShareHubError):
    """AShareHub rejected request parameters or a resource."""


class AShareHubTransportError(AShareHubError):
    """A sanitized network or upstream-server failure."""


class AShareHubDataError(AShareHubError):
    """A successful response did not satisfy the documented contract."""


class _HTTPStatusError(RuntimeError):
    """Internal status carrier which deliberately excludes response content."""

    def __init__(self, status: int) -> None:
        super().__init__("AShareHub HTTP status {}".format(int(status)))
        self.status = int(status)


def _sync_json_request(
    method: str,
    url: str,
    headers: Mapping[str, str],
    params: Mapping[str, str],
    timeout: float,
) -> Any:
    """Perform one REST call without ever placing the API key in the URL."""

    query = urlencode(dict(params))
    full_url = "{}?{}".format(url, query) if query else url
    request = Request(full_url, headers=dict(headers), method=method.upper())
    try:
        with urlopen(request, timeout=float(timeout)) as response:
            raw = response.read()
    except HTTPError as exc:
        # The body may contain echoed request data.  Do not parse or retain it.
        raise _HTTPStatusError(exc.code) from None
    except (OSError, TimeoutError, URLError):
        raise AShareHubTransportError("AShareHub transport failed") from None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AShareHubDataError("AShareHub returned a non-JSON response") from None


def _decimal(value: Any, *, positive: bool = False) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in {"nan", "none", "null", "inf", "-inf"}:
        return None
    try:
        result = Decimal(text)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        return None
    return result


def _integer(value: Any, *, multiplier: Decimal = Decimal("1")) -> Optional[int]:
    parsed = _decimal(value)
    if parsed is None:
        return None
    converted = parsed * multiplier
    if converted != converted.to_integral_value():
        return None
    try:
        return int(converted)
    except (OverflowError, ValueError):
        return None


def _quote_time(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
    return ensure_market_time(parsed)


def _compact_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    text = str(value).strip().replace("-", "")
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None


def _daily_time(value: Any) -> Optional[datetime]:
    parsed = _compact_date(value)
    if parsed is None:
        return None
    # The upstream daily row is an after-close product.  Stamping it at the
    # date's midnight would falsely imply availability before the session.
    return datetime(parsed.year, parsed.month, parsed.day, 15, 30, tzinfo=SHANGHAI_TZ)


def _previous_or_same_weekday(value: date) -> date:
    """Return the latest Monday-Friday date at or before ``value``."""

    current = value
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current


def _expected_latest_daily_date(value: datetime) -> date:
    """Compatibility wrapper around the shared weekday-fallback helper."""

    return expected_latest_daily_date(value)


def _default_budget_path() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        root = Path(local)
    else:
        root = Path.home() / ".local" / "share"
    return root / "a-share-quant-monitor" / "asharehub-budget.json"


class AShareHubMarketDataProvider(MarketDataProvider):
    """Quota-aware AShareHub HTTP adapter for realtime snapshots and daily bars."""

    def __init__(
        self,
        api_key: str,
        *,
        symbols: Sequence[str] = (),
        instrument_names: Optional[Mapping[str, str]] = None,
        base_url: str = ASHAREHUB_BASE_URL,
        request_timeout_seconds: float = 15.0,
        quote_cache_seconds: float = 300.0,
        bars_cache_seconds: float = 86_400.0,
        instruments_cache_seconds: float = 604_800.0,
        daily_soft_limit: int = 90,
        budget_path: Optional[os.PathLike] = None,
        retries: int = 0,
        retry_backoff_seconds: float = 0.5,
        strict_symbols: bool = True,
        requester: Optional[Callable[..., Any]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        monotonic: Optional[Callable[[], float]] = None,
        market_status_fetcher: Optional[Callable[[datetime], Mapping[str, Any]]] = None,
    ) -> None:
        normalized_key = str(api_key or "").strip()
        if not normalized_key:
            raise AShareHubConfigurationError(
                "AShareHub requires QUANT_MONITOR_ASHAREHUB_API_KEY"
            )
        if float(request_timeout_seconds) <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        for name, value in (
            ("quote_cache_seconds", quote_cache_seconds),
            ("bars_cache_seconds", bars_cache_seconds),
            ("instruments_cache_seconds", instruments_cache_seconds),
            ("retry_backoff_seconds", retry_backoff_seconds),
        ):
            if float(value) < 0:
                raise ValueError("{} cannot be negative".format(name))
        if int(retries) < 0:
            raise ValueError("retries cannot be negative")
        if not 1 <= int(daily_soft_limit) <= ASHAREHUB_OFFICIAL_DAILY_LIMIT:
            raise ValueError("daily_soft_limit must be between 1 and 100")

        configured = tuple(dict.fromkeys(normalize_symbol(item) for item in symbols))
        self._api_key = normalized_key
        self._configured_symbols = configured
        self._instrument_names = {
            normalize_symbol(key): str(value).strip()
            for key, value in dict(instrument_names or {}).items()
            if str(value).strip()
        }
        self.base_url = str(base_url).rstrip("/")
        parsed_base_url = urlsplit(self.base_url)
        if parsed_base_url.scheme.lower() != "https" and parsed_base_url.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("AShareHub base_url must use HTTPS")
        self.request_timeout_seconds = float(request_timeout_seconds)
        self.quote_cache_seconds = float(quote_cache_seconds)
        self.bars_cache_seconds = float(bars_cache_seconds)
        self.instruments_cache_seconds = float(instruments_cache_seconds)
        self.daily_soft_limit = int(daily_soft_limit)
        self.budget_path = Path(budget_path) if budget_path is not None else _default_budget_path()
        self._credential_fingerprint = hashlib.sha256(
            normalized_key.encode("utf-8")
        ).hexdigest()
        self._budget_lock_path = self.budget_path.with_name(
            self.budget_path.name + ".lock"
        )
        self.retries = int(retries)
        self.retry_backoff_seconds = float(retry_backoff_seconds)
        self.strict_symbols = bool(strict_symbols)
        self._requester = requester or _sync_json_request
        self._clock = clock or now_market_time
        self._monotonic = monotonic or time.monotonic
        self._market_status_fetcher = market_status_fetcher or market_status

        self._request_lock: Optional[asyncio.Lock] = None
        self._quote_lock: Optional[asyncio.Lock] = None
        self._bar_locks: Dict[Tuple[str, Optional[str], Optional[str]], asyncio.Lock] = {}
        self._instrument_lock: Optional[asyncio.Lock] = None
        self._quotes: Dict[str, Quote] = {}
        self._quote_attempted_at: Dict[str, float] = {}
        self._last_quote_symbols: Tuple[str, ...] = ()
        self._last_quote_request_at: Optional[datetime] = None
        self._off_market_quote_attempted = False
        self._bar_cache: Dict[
            Tuple[str, Optional[str], Optional[str]],
            Tuple[float, Tuple[Bar, ...], date],
        ] = {}
        self._seeded_bars: Dict[str, Dict[datetime, Bar]] = {}
        # ``_seeded_bars`` is the compatibility merged view.  Keep a separate
        # marker for rows first observed over the network in this process so a
        # later range lookup is not mislabeled as a SQLite-seeded cache hit.
        self._network_bar_keys: Set[Tuple[str, datetime]] = set()
        self._instrument_cache: Optional[Tuple[float, Tuple[Instrument, ...]]] = None
        self._instrument_cache_persisted = False
        self._last_http_status: Optional[int] = None
        self._last_error_type: Optional[str] = None
        self._last_success_at: Optional[datetime] = None
        self._last_quote_success_at: Optional[datetime] = None
        self._last_bar_success_at: Optional[datetime] = None
        self._last_catalogue_success_at: Optional[datetime] = None
        self._fatal_error: Optional[AShareHubError] = None
        self._fatal_error_date: Optional[str] = None
        self._request_in_progress = False
        self._closed = False

        self._budget_date = self._market_now().date().isoformat()
        self._budget_used = 0
        self._last_request_at: Optional[datetime] = None
        self._load_initial_budget()

    @property
    def name(self) -> str:
        return "asharehub"

    @property
    def universe_scope(self) -> str:
        return "provider_catalogue"

    @property
    def quote_universe_scope(self) -> str:
        # The quote route must not treat the static full-market catalogue as a
        # polling target: one realtime basket is deliberately the watchlist.
        return "configured_symbols"

    @property
    def configured_symbols(self) -> Tuple[str, ...]:
        return self._configured_symbols

    def configure_symbols(self, symbols: Sequence[str]) -> Tuple[str, ...]:
        """Replace the polling basket before monitoring starts.

        Runtime universe discovery uses the cached provider catalogue when it
        is available.  Keeping this explicit avoids reaching into adapter
        internals and preserves the documented 200-symbol realtime limit.
        """

        normalized = tuple(
            dict.fromkeys(normalize_symbol(item) for item in symbols)
        )
        if not normalized:
            raise ValueError("AShareHub requires at least one configured symbol")
        if len(normalized) > ASHAREHUB_REALTIME_BATCH_LIMIT:
            raise ValueError("AShareHub realtime basket cannot exceed 200 symbols")
        self._configured_symbols = normalized
        self._last_quote_symbols = tuple(
            symbol for symbol in self._last_quote_symbols if symbol in normalized
        )
        return normalized

    @property
    def supported_intervals(self) -> Tuple[str, ...]:
        return ("1d",)

    def _market_now(self) -> datetime:
        return ensure_market_time(self._clock())

    def _market_open(self) -> bool:
        try:
            return bool(self._market_status_fetcher(self._market_now()).get("is_trading"))
        except Exception:
            # Failing closed protects a scarce quota from a tight polling loop.
            return False

    def _today_budget(self) -> Tuple[str, int]:
        today = self._market_now().date().isoformat()
        if self._budget_date != today:
            self._budget_date = today
            self._budget_used = 0
            self._last_request_at = None
        return today, self._budget_used

    @property
    def realtime_status(self) -> Mapping[str, Any]:
        budget_date, used = self._today_budget()
        market_open = self._market_open()
        fatal_is_current = self._fatal_error_date == budget_date
        upstream_quota_exhausted = fatal_is_current and isinstance(
            self._fatal_error, AShareHubRateLimitError
        )
        permission_circuit_open = fatal_is_current and isinstance(
            self._fatal_error, AShareHubPermissionError
        )
        local_budget_exhausted = used >= self.daily_soft_limit
        if self._closed:
            state = "closed"
        elif upstream_quota_exhausted:
            state = "upstream_quota_exhausted"
        elif local_budget_exhausted:
            state = "quota_exhausted"
        elif permission_circuit_open:
            state = "permission_denied"
        elif self._request_in_progress:
            state = "http_polling"
        elif self._last_error_type:
            state = "error"
        elif self._last_success_at is not None:
            state = "http_polling"
        else:
            state = "idle"

        next_refresh = None
        if market_open and self._last_quote_request_at is not None:
            next_refresh = (
                self._last_quote_request_at + timedelta(seconds=self.quote_cache_seconds)
            ).isoformat()
        return {
            "enabled": True,
            "state": state,
            "mode": "http_polling",
            "requested_symbols": len(self._configured_symbols),
            "subscribed_symbols": len(self._last_quote_symbols),
            "subscription_limit": ASHAREHUB_REALTIME_BATCH_LIMIT,
            "limited_symbols": max(
                0, len(self._configured_symbols) - ASHAREHUB_REALTIME_BATCH_LIMIT
            ),
            "last_error_type": self._last_error_type,
            "last_http_status": self._last_http_status,
            "requests_used_today": used,
            "daily_soft_limit": self.daily_soft_limit,
            "requests_remaining_soft": max(0, self.daily_soft_limit - used),
            "local_budget_exhausted": local_budget_exhausted,
            "upstream_quota_exhausted": upstream_quota_exhausted,
            "permission_circuit_open": permission_circuit_open,
            "budget_date": budget_date,
            "last_request_at": (
                self._last_request_at.isoformat() if self._last_request_at else None
            ),
            "last_success_at": (
                self._last_success_at.isoformat() if self._last_success_at else None
            ),
            "last_quote_success_at": (
                self._last_quote_success_at.isoformat()
                if self._last_quote_success_at
                else None
            ),
            "last_bar_success_at": (
                self._last_bar_success_at.isoformat()
                if self._last_bar_success_at
                else None
            ),
            "last_catalogue_success_at": (
                self._last_catalogue_success_at.isoformat()
                if self._last_catalogue_success_at
                else None
            ),
            "next_refresh_at": next_refresh,
            "quote_cache_seconds": self.quote_cache_seconds,
            "market_open": market_open,
            "order_book_enabled": False,
        }

    def _load_initial_budget(self) -> None:
        if not self.budget_path.exists():
            return
        try:
            payload = json.loads(self.budget_path.read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping):
                raise ValueError
            stored_date = str(payload.get("date") or "")
            stored_used = int(payload.get("used", 0))
            if stored_used < 0:
                raise ValueError
            if stored_date == self._budget_date:
                stored_fingerprint = str(payload.get("credential_fingerprint") or "")
                if stored_fingerprint and stored_fingerprint != self._credential_fingerprint:
                    return
                self._budget_used = stored_used
                last = payload.get("last_request_at")
                parsed = _quote_time(last)
                self._last_request_at = parsed
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            raise AShareHubConfigurationError(
                "AShareHub local budget state is unreadable; refusing network requests"
            ) from None

    def _refresh_budget_from_disk(self) -> None:
        today, _ = self._today_budget()
        if not self.budget_path.exists():
            return
        try:
            payload = json.loads(self.budget_path.read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping):
                raise ValueError
            if str(payload.get("date") or "") == today:
                stored_fingerprint = str(payload.get("credential_fingerprint") or "")
                if stored_fingerprint and stored_fingerprint != self._credential_fingerprint:
                    return
                stored_used = int(payload.get("used", 0))
                if stored_used < 0:
                    raise ValueError
                self._budget_used = max(self._budget_used, stored_used)
                parsed = _quote_time(payload.get("last_request_at"))
                if parsed is not None:
                    self._last_request_at = parsed
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            raise AShareHubBudgetError(
                "AShareHub local budget state is unreadable; refusing request"
            ) from None

    def _persist_budget(self) -> None:
        payload = {
            "date": self._budget_date,
            "used": self._budget_used,
            "last_request_at": (
                self._last_request_at.isoformat() if self._last_request_at else None
            ),
            "credential_fingerprint": self._credential_fingerprint,
        }
        temporary = None
        try:
            self.budget_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(self.budget_path.parent),
                prefix=".asharehub-budget-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temporary), str(self.budget_path))
        except OSError:
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass
            raise AShareHubBudgetError(
                "AShareHub local budget could not be persisted; request was not sent"
            ) from None

    def _consume_budget(self, *, reserve_requests: int = 0) -> None:
        if isinstance(reserve_requests, bool) or not isinstance(reserve_requests, int):
            raise TypeError("reserve_requests must be an integer")
        if reserve_requests < 0 or reserve_requests >= self.daily_soft_limit:
            raise ValueError("reserve_requests must be non-negative and below the soft limit")
        try:
            with _interprocess_lock(self._budget_lock_path):
                self._refresh_budget_from_disk()
                today, used = self._today_budget()
                spendable_limit = self.daily_soft_limit - reserve_requests
                if used >= spendable_limit:
                    raise AShareHubBudgetError(
                        "AShareHub local daily soft limit reached with {} requests reserved".format(
                            reserve_requests
                        ),
                        reserved=reserve_requests > 0 and used < self.daily_soft_limit,
                    )
                self._budget_date = today
                self._budget_used = used + 1
                self._last_request_at = self._market_now()
                self._persist_budget()
        except AShareHubBudgetError:
            raise
        except OSError:
            raise AShareHubBudgetError(
                "AShareHub local budget lock is unavailable; request was not sent"
            ) from None

    def _request_guard(self) -> asyncio.Lock:
        if self._request_lock is None:
            self._request_lock = asyncio.Lock()
        return self._request_lock

    def _quote_guard(self) -> asyncio.Lock:
        if self._quote_lock is None:
            self._quote_lock = asyncio.Lock()
        return self._quote_lock

    def _instrument_guard(self) -> asyncio.Lock:
        if self._instrument_lock is None:
            self._instrument_lock = asyncio.Lock()
        return self._instrument_lock

    @staticmethod
    def _map_status(status: int, operation: str) -> AShareHubError:
        if status == 401:
            return AShareHubAuthenticationError(
                "AShareHub API key authentication failed",
                status_code=status,
                operation=operation,
            )
        if status == 403:
            return AShareHubPermissionError(
                "AShareHub endpoint permission denied",
                status_code=status,
                operation=operation,
            )
        if status == 429:
            return AShareHubRateLimitError(
                "AShareHub daily request limit reached",
                status_code=status,
                operation=operation,
            )
        if status in {400, 404, 405, 409, 422}:
            return AShareHubRequestError(
                "AShareHub rejected {} (HTTP {})".format(operation, status),
                status_code=status,
                operation=operation,
            )
        if status >= 500:
            return AShareHubTransportError(
                "AShareHub server unavailable for {} (HTTP {})".format(
                    operation, status
                ),
                status_code=status,
                operation=operation,
            )
        return AShareHubRequestError(
            "AShareHub request failed for {} (HTTP {})".format(operation, status),
            status_code=status,
            operation=operation,
        )

    async def _invoke_requester(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        params: Mapping[str, str],
    ) -> Any:
        try:
            result = await asyncio.to_thread(
                self._requester,
                method,
                url,
                dict(headers),
                dict(params),
                self.request_timeout_seconds,
            )
            if inspect.isawaitable(result):
                result = await result
            return result
        except asyncio.CancelledError:
            raise
        except (_HTTPStatusError, AShareHubError):
            raise
        except Exception:
            # Never interpolate a vendor/transport exception: it may include
            # request headers and therefore the API key.
            raise AShareHubTransportError("AShareHub transport failed") from None

    async def _request_json(
        self,
        path: str,
        *,
        operation: str,
        params: Optional[Mapping[str, str]] = None,
        reserve_requests: int = 0,
    ) -> List[Mapping[str, Any]]:
        rows, _ = await self._request_json_with_receipt(
            path,
            operation=operation,
            params=params,
            reserve_requests=reserve_requests,
        )
        return rows

    async def _request_json_with_receipt(
        self,
        path: str,
        *,
        operation: str,
        params: Optional[Mapping[str, str]] = None,
        reserve_requests: int = 0,
    ) -> Tuple[List[Mapping[str, Any]], datetime]:
        """Return decoded rows and the receipt instant from the same request.

        The timestamp is returned alongside the response instead of recovered
        from ``_last_*_success_at`` so overlapping per-symbol parsing cannot
        accidentally attach another request's receipt time to this batch.
        """

        if self._closed:
            raise AShareHubConfigurationError("AShareHub provider is closed")
        if self._fatal_error is not None:
            today = self._market_now().date().isoformat()
            if isinstance(
                self._fatal_error,
                (
                    AShareHubBudgetError,
                    AShareHubPermissionError,
                    AShareHubRateLimitError,
                ),
            ) and self._fatal_error_date != today:
                self._today_budget()
                self._fatal_error = None
                self._fatal_error_date = None
            elif isinstance(self._fatal_error, AShareHubBudgetError):
                self._today_budget()
                if self._budget_used < self.daily_soft_limit:
                    self._fatal_error = None
                    self._fatal_error_date = None
                else:
                    raise self._fatal_error
            else:
                raise self._fatal_error
        headers = {
            "Accept": "application/json",
            "X-API-Key": self._api_key,
        }
        url = "{}{}".format(self.base_url, path)
        async with self._request_guard():
            last_error: Optional[AShareHubError] = None
            for attempt in range(self.retries + 1):
                self._request_in_progress = True
                try:
                    self._consume_budget(reserve_requests=reserve_requests)
                    decoded = await self._invoke_requester(
                        "GET", url, headers, dict(params or {})
                    )
                    self._last_http_status = 200
                    if not isinstance(decoded, list):
                        raise AShareHubDataError(
                            "AShareHub {} response must be a top-level JSON array".format(
                                operation
                            ),
                            operation=operation,
                        )
                    if not all(isinstance(row, Mapping) for row in decoded):
                        raise AShareHubDataError(
                            "AShareHub {} response contains a non-object row".format(
                                operation
                            ),
                            operation=operation,
                        )
                    response_received_at = self._market_now()
                    self._last_success_at = response_received_at
                    if operation == "realtime":
                        self._last_quote_success_at = self._last_success_at
                    elif operation == "market-daily":
                        self._last_bar_success_at = self._last_success_at
                    elif operation == "reference-stocks":
                        self._last_catalogue_success_at = self._last_success_at
                    self._last_error_type = None
                    return [dict(row) for row in decoded], response_received_at
                except _HTTPStatusError as exc:
                    self._last_http_status = exc.status
                    last_error = self._map_status(exc.status, operation)
                    if isinstance(
                        last_error,
                        (
                            AShareHubAuthenticationError,
                            AShareHubPermissionError,
                            AShareHubRateLimitError,
                        ),
                    ):
                        self._fatal_error = last_error
                        self._fatal_error_date = self._market_now().date().isoformat()
                except AShareHubError as exc:
                    last_error = exc
                    if exc.status_code is not None:
                        self._last_http_status = exc.status_code
                    if isinstance(
                        exc,
                        (
                            AShareHubAuthenticationError,
                            AShareHubPermissionError,
                            AShareHubRateLimitError,
                            AShareHubBudgetError,
                        ),
                    ):
                        self._fatal_error = exc
                        self._fatal_error_date = self._market_now().date().isoformat()
                finally:
                    self._request_in_progress = False

                retryable = isinstance(last_error, AShareHubTransportError)
                if attempt >= self.retries or not retryable:
                    break
                delay = self.retry_backoff_seconds * (2 ** attempt)
                if delay:
                    await asyncio.sleep(delay)

            assert last_error is not None
            self._last_error_type = type(last_error).__name__
            raise last_error from None

    @staticmethod
    def _quote_from_row(row: Mapping[str, Any], requested: Sequence[str]) -> Optional[Quote]:
        try:
            symbol = normalize_symbol(str(row.get("symbol") or ""))
        except ValueError:
            return None
        if symbol not in requested:
            return None
        last = _decimal(row.get("price"), positive=True)
        timestamp = _quote_time(row.get("trade_time"))
        volume = _integer(row.get("volume"))
        turnover = _decimal(row.get("amount"))
        if last is None or timestamp is None or volume is None or turnover is None:
            return None

        opened = _decimal(row.get("open"), positive=True)
        high = _decimal(row.get("high"), positive=True)
        low = _decimal(row.get("low"), positive=True)
        previous = _decimal(row.get("pre_close"), positive=True)
        if high is not None and high < max(last, opened or last):
            high = None
        if low is not None and low > min(last, opened or last):
            low = None
        if high is not None and low is not None and high < low:
            high = low = None
        return Quote(
            symbol=symbol,
            timestamp=timestamp,
            last=last,
            open=opened,
            high=high,
            low=low,
            previous_close=previous,
            bid=None,
            ask=None,
            bid_size=0,
            ask_size=0,
            volume=volume,
            turnover=turnover,
            source="asharehub",
        )

    def _cached_quotes(
        self, requested: Sequence[str], *, allow_stale: bool
    ) -> Optional[List[Quote]]:
        now = self._monotonic()
        attempted = all(symbol in self._quote_attempted_at for symbol in requested)
        if not attempted:
            return None
        if not allow_stale and any(
            now - self._quote_attempted_at[symbol] >= self.quote_cache_seconds
            for symbol in requested
        ):
            return None
        return [self._quotes[symbol] for symbol in requested if symbol in self._quotes]

    async def _fetch_quotes(
        self, symbols: Sequence[str], *, strict: bool
    ) -> List[Quote]:
        requested = tuple(dict.fromkeys(normalize_symbol(item) for item in symbols))
        if not requested:
            return []
        if len(requested) > ASHAREHUB_REALTIME_BATCH_LIMIT:
            raise ValueError(
                "AShareHub realtime basket supports at most 200 symbols per request"
            )

        market_open = self._market_open()
        if market_open:
            self._off_market_quote_attempted = False
        cached = self._cached_quotes(requested, allow_stale=not market_open)
        if cached is not None:
            return self._finish_quotes(requested, cached, strict=strict)
        if not market_open and self._off_market_quote_attempted:
            cached = [self._quotes[item] for item in requested if item in self._quotes]
            return self._finish_quotes(requested, cached, strict=strict)

        async with self._quote_guard():
            market_open = self._market_open()
            if market_open:
                self._off_market_quote_attempted = False
            cached = self._cached_quotes(requested, allow_stale=not market_open)
            if cached is not None:
                return self._finish_quotes(requested, cached, strict=strict)
            if not market_open and self._off_market_quote_attempted:
                cached = [self._quotes[item] for item in requested if item in self._quotes]
                return self._finish_quotes(requested, cached, strict=strict)

            if not market_open:
                # Set before I/O: a failure must not cause a two-second closed-
                # market collector loop to burn the remaining daily quota.
                self._off_market_quote_attempted = True
            rows = await self._request_json(
                "/v2/market/realtime",
                operation="realtime",
                params={"symbol": ",".join(requested)},
            )
            attempted_at = self._monotonic()
            parsed: Dict[str, Quote] = {}
            for row in rows:
                quote = self._quote_from_row(row, requested)
                if quote is None:
                    continue
                current = parsed.get(quote.symbol)
                if current is None or quote.timestamp >= current.timestamp:
                    parsed[quote.symbol] = quote
            for symbol in requested:
                self._quote_attempted_at[symbol] = attempted_at
                if symbol in parsed:
                    self._quotes[symbol] = parsed[symbol]
                else:
                    self._quotes.pop(symbol, None)
            self._last_quote_request_at = self._market_now()
            self._last_quote_symbols = tuple(
                symbol for symbol in self._configured_symbols if symbol in self._quotes
            )
            result = [parsed[symbol] for symbol in requested if symbol in parsed]
            try:
                result = self._finish_quotes(requested, result, strict=strict)
            except AShareHubDataError:
                self._last_error_type = "AShareHubDataError"
                raise
            self._last_error_type = None
            return result

    @staticmethod
    def _finish_quotes(
        requested: Sequence[str], quotes: Sequence[Quote], *, strict: bool
    ) -> List[Quote]:
        by_symbol = {item.symbol: item for item in quotes}
        missing = [symbol for symbol in requested if symbol not in by_symbol]
        if missing and strict:
            raise AShareHubDataError(
                "AShareHub realtime response has no usable quote for: {}".format(
                    ", ".join(missing)
                ),
                operation="realtime",
            )
        return [by_symbol[symbol] for symbol in requested if symbol in by_symbol]

    async def fetch_quotes(self, symbols: Sequence[str]) -> List[Quote]:
        return await self._fetch_quotes(symbols, strict=self.strict_symbols)

    async def fetch_quotes_relaxed(self, symbols: Sequence[str]) -> List[Quote]:
        return await self._fetch_quotes(symbols, strict=False)

    def seed_quotes(self, quotes: Sequence[Quote]) -> int:
        """Hydrate provider cache from SQLite without making rows newly fresh."""

        now = self._market_now()
        now_mono = self._monotonic()
        seeded = 0
        for quote in quotes:
            if not str(quote.source).startswith("asharehub"):
                continue
            if quote.symbol not in self._configured_symbols:
                continue
            if quote.timestamp > now + timedelta(minutes=5):
                # A persisted future row is not a fresh quote.  Ignore it so an
                # open-market call performs a real refresh instead of serving
                # bad clock/vendor data until its timestamp catches up.
                continue
            age = max(0.0, (now - quote.timestamp).total_seconds())
            attempted_at = now_mono - age
            current = self._quotes.get(quote.symbol)
            if current is None or quote.timestamp >= current.timestamp:
                self._quotes[quote.symbol] = quote
                self._quote_attempted_at[quote.symbol] = attempted_at
                seeded += 1
        self._last_quote_symbols = tuple(
            symbol for symbol in self._configured_symbols if symbol in self._quotes
        )
        return seeded

    @staticmethod
    def _bar_from_row(row: Mapping[str, Any], symbol: str) -> Optional[Bar]:
        try:
            row_symbol = normalize_symbol(str(row.get("symbol") or ""))
        except ValueError:
            return None
        if row_symbol != symbol:
            return None
        timestamp = _daily_time(row.get("trade_date"))
        opened = _decimal(row.get("open"), positive=True)
        high = _decimal(row.get("high"), positive=True)
        low = _decimal(row.get("low"), positive=True)
        close = _decimal(row.get("close"), positive=True)
        volume = _integer(row.get("vol"), multiplier=Decimal("100"))
        amount = _decimal(row.get("amount"))
        if None in (timestamp, opened, high, low, close, volume, amount):
            return None
        assert timestamp is not None
        assert opened is not None and high is not None and low is not None and close is not None
        assert volume is not None and amount is not None
        if high < max(opened, low, close) or low > min(opened, high, close):
            return None
        return Bar(
            symbol=symbol,
            interval="1d",
            timestamp=timestamp,
            open=opened,
            high=high,
            low=low,
            close=close,
            volume=volume,
            turnover=amount * Decimal("1000"),
            source="asharehub",
        )

    def seed_bars(self, bars: Sequence[Bar]) -> int:
        seeded = 0
        for bar in bars:
            if (
                bar.interval != "1d"
                or not str(bar.source).startswith("asharehub")
            ):
                continue
            bucket = self._seeded_bars.setdefault(bar.symbol, {})
            key = (bar.symbol, bar.timestamp)
            if key not in self._network_bar_keys:
                bucket[bar.timestamp] = bar
            seeded += 1
        return seeded

    def _seeded_transport(self, bars: Sequence[Bar]) -> BarFetchTransport:
        if any((bar.symbol, bar.timestamp) in self._network_bar_keys for bar in bars):
            return BarFetchTransport.MEMORY_CACHE
        return BarFetchTransport.SQLITE_CACHE

    def _seeded_result(
        self,
        symbol: str,
        start_date: Optional[str],
        end_date: Optional[str],
        limit: int,
        *,
        required_latest_date: Optional[date] = None,
    ) -> Optional[List[Bar]]:
        items = list(self._seeded_bars.get(symbol, {}).values())
        if start_date is not None:
            items = [item for item in items if item.timestamp.strftime("%Y%m%d") >= start_date]
        if end_date is not None:
            items = [item for item in items if item.timestamp.strftime("%Y%m%d") <= end_date]
        items.sort(key=lambda item: item.timestamp)
        if len(items) < int(limit):
            return None
        if (
            required_latest_date is not None
            and items[-1].timestamp.date() < required_latest_date
        ):
            return None
        return items[-int(limit) :]

    def has_current_bar_history(
        self,
        symbol: str,
        interval: str = "1d",
        limit: int = 200,
    ) -> bool:
        """Check local daily-history freshness without consuming API quota."""

        normalized_symbol = normalize_symbol(symbol)
        if str(interval).strip().lower() != "1d" or int(limit) <= 0:
            return False
        effective_end = self._market_now()
        effective_start = effective_end - timedelta(days=max(365, int(limit) * 3))
        start_date = effective_start.strftime("%Y%m%d")
        end_date = effective_end.strftime("%Y%m%d")
        required_latest_date = min(
            _previous_or_same_weekday(effective_end.date()),
            _expected_latest_daily_date(effective_end),
        )
        if self._seeded_result(
            normalized_symbol,
            start_date,
            end_date,
            int(limit),
            required_latest_date=required_latest_date,
        ) is not None:
            return True
        cached = self._bar_cache.get((normalized_symbol, start_date, end_date))
        cache_ttl = self._bar_cache_ttl(cached, required_latest_date)
        return bool(
            cached is not None
            and self._monotonic() - cached[0] < cache_ttl
            and cached[2] >= required_latest_date
        )

    def has_current_bar_data(
        self,
        symbol: str,
        interval: str = "1d",
        minimum_rows: int = 1,
    ) -> bool:
        """Check that cached rows themselves reach the expected latest date.

        Unlike :meth:`has_current_bar_history`, an empty successful refresh is
        not enough.  This stricter predicate is used for research readiness so
        a fresh quote cannot disguise stale daily inputs.
        """

        normalized_symbol = normalize_symbol(symbol)
        if str(interval).strip().lower() != "1d" or int(minimum_rows) <= 0:
            return False
        effective_end = self._market_now()
        effective_start = effective_end - timedelta(
            days=max(365, int(minimum_rows) * 3)
        )
        required_latest_date = min(
            _previous_or_same_weekday(effective_end.date()),
            _expected_latest_daily_date(effective_end),
        )
        return self._seeded_result(
            normalized_symbol,
            effective_start.strftime("%Y%m%d"),
            effective_end.strftime("%Y%m%d"),
            int(minimum_rows),
            required_latest_date=required_latest_date,
        ) is not None

    async def fetch_bars(
        self,
        symbol: str,
        interval: str = "1m",
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 200,
        reserve_requests: int = 0,
    ) -> List[Bar]:
        result = await self.fetch_bars_with_lineage(
            symbol,
            interval=interval,
            start=start,
            end=end,
            limit=limit,
            reserve_requests=reserve_requests,
        )
        return list(result.bars)

    async def fetch_bars_with_lineage(
        self,
        symbol: str,
        interval: str = "1m",
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 200,
        reserve_requests: int = 0,
    ) -> BarFetchResult:
        normalized_symbol = normalize_symbol(symbol)
        selected_interval = str(interval).strip().lower()
        if selected_interval != "1d":
            raise ValueError("AShareHub only provides 1d A-share bars")
        if int(limit) <= 0:
            raise ValueError("limit must be positive")
        start_time = ensure_market_time(start) if start is not None else None
        end_time = ensure_market_time(end) if end is not None else None
        if start_time is not None and end_time is not None and start_time > end_time:
            return BarFetchResult(
                bars=(),
                observations=(),
                transport=BarFetchTransport.NO_REQUEST,
            )

        effective_end = end_time or self._market_now()
        effective_start = start_time
        if effective_start is None:
            effective_start = effective_end - timedelta(days=max(365, int(limit) * 3))
        start_date = effective_start.strftime("%Y%m%d")
        end_date = effective_end.strftime("%Y%m%d")
        key = (normalized_symbol, start_date, end_date)

        # An explicit historical end is complete when the cached series covers
        # that requested date range.  Open-ended/current requests must instead
        # cover the latest after-close date expected by the weekday fallback;
        # merely having ``limit`` old rows is not proof of freshness.
        now = self._market_now()
        if end_time is None or effective_end.date() >= now.date():
            required_latest_date = min(
                _previous_or_same_weekday(effective_end.date()),
                _expected_latest_daily_date(now),
            )
        else:
            required_latest_date = _previous_or_same_weekday(effective_end.date())

        seeded = self._seeded_result(
            normalized_symbol,
            start_date,
            end_date,
            int(limit),
            required_latest_date=required_latest_date,
        )
        if seeded is not None:
            return BarFetchResult(
                bars=tuple(seeded),
                observations=(),
                transport=self._seeded_transport(seeded),
            )
        cached = self._bar_cache.get(key)
        cache_ttl = self._bar_cache_ttl(cached, required_latest_date)
        if (
            cached is not None
            and self._monotonic() - cached[0] < cache_ttl
            and cached[2] >= required_latest_date
        ):
            return BarFetchResult(
                bars=tuple(cached[1][-int(limit) :]),
                observations=(),
                transport=BarFetchTransport.MEMORY_CACHE,
            )

        lock = self._bar_locks.setdefault(key, asyncio.Lock())
        async with lock:
            seeded = self._seeded_result(
                normalized_symbol,
                start_date,
                end_date,
                int(limit),
                required_latest_date=required_latest_date,
            )
            if seeded is not None:
                return BarFetchResult(
                    bars=tuple(seeded),
                    observations=(),
                    transport=self._seeded_transport(seeded),
                )
            cached = self._bar_cache.get(key)
            cache_ttl = self._bar_cache_ttl(cached, required_latest_date)
            if (
                cached is not None
                and self._monotonic() - cached[0] < cache_ttl
                and cached[2] >= required_latest_date
            ):
                return BarFetchResult(
                    bars=tuple(cached[1][-int(limit) :]),
                    observations=(),
                    transport=BarFetchTransport.MEMORY_CACHE,
                )

            seeded_bucket = self._seeded_bars.get(normalized_symbol, {})
            latest_seed_date = max(
                (timestamp.date() for timestamp in seeded_bucket),
                default=None,
            )
            request_start_date = start_date
            if latest_seed_date is not None:
                request_start_date = max(
                    start_date, latest_seed_date.strftime("%Y%m%d")
                )

            rows, response_received_at = await self._request_json_with_receipt(
                "/v2/market/daily",
                operation="market-daily",
                params={
                    "symbol": normalized_symbol,
                    "start_date": request_start_date,
                    "end_date": end_date,
                },
                reserve_requests=reserve_requests,
            )
            parsed: Dict[datetime, Bar] = {}
            parsed_observations: List[Bar] = []
            for row in rows:
                bar = self._bar_from_row(row, normalized_symbol)
                if bar is None:
                    continue
                compact_timestamp = bar.timestamp.strftime("%Y%m%d")
                if not start_date <= compact_timestamp <= end_date:
                    continue
                parsed_observations.append(bar)
                parsed[bar.timestamp] = bar
            if rows and not parsed:
                self._last_error_type = "AShareHubDataError"
                raise AShareHubDataError(
                    "AShareHub daily response contains no usable rows for {}".format(
                        normalized_symbol
                    ),
                    operation="market-daily",
                )
            combined: Dict[datetime, Bar] = dict(
                self._seeded_bars.get(normalized_symbol, {})
            )
            combined.update(parsed)
            bars = tuple(
                combined[timestamp]
                for timestamp in sorted(combined)
                if start_date <= timestamp.strftime("%Y%m%d") <= end_date
            )
            self._bar_cache[key] = (
                self._monotonic(),
                bars,
                required_latest_date,
            )
            bucket = self._seeded_bars.setdefault(normalized_symbol, {})
            for bar in parsed.values():
                bucket[bar.timestamp] = bar
                self._network_bar_keys.add((bar.symbol, bar.timestamp))
            self._last_error_type = None
            return BarFetchResult(
                bars=tuple(bars[-int(limit) :]),
                observations=tuple(
                    sorted(parsed_observations, key=lambda item: item.timestamp)
                ),
                transport=BarFetchTransport.NETWORK,
                response_received_at=response_received_at,
            )

    def _bar_cache_ttl(
        self,
        cached: Optional[Tuple[float, Tuple[Bar, ...], date]],
        required_latest_date: date,
    ) -> float:
        if cached is None:
            return self.bars_cache_seconds
        rows = cached[1]
        if not rows or rows[-1].timestamp.date() < required_latest_date:
            return min(
                self.bars_cache_seconds,
                ASHAREHUB_EMPTY_DAILY_RECHECK_SECONDS,
            )
        return self.bars_cache_seconds

    async def fetch_bars_many(
        self,
        symbols: Sequence[str],
        interval: str = "1m",
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 200,
    ) -> List[Bar]:
        # AShareHub documents no multi-symbol daily endpoint.  Keep calls
        # sequential so local daily-budget consumption remains predictable.
        result: List[Bar] = []
        for symbol in dict.fromkeys(normalize_symbol(item) for item in symbols):
            result.extend(
                await self.fetch_bars(
                    symbol,
                    interval=interval,
                    start=start,
                    end=end,
                    limit=limit,
                )
            )
        return result

    @staticmethod
    def _instrument_from_row(row: Mapping[str, Any]) -> Optional[Instrument]:
        try:
            symbol = normalize_symbol(str(row.get("symbol") or ""))
            exchange = Exchange(symbol.rsplit(".", 1)[1])
        except (ValueError, KeyError):
            return None
        name = str(row.get("name") or "").strip()
        if not name:
            return None
        status = str(row.get("list_status") or "").strip().upper()
        metadata = {"source": "asharehub"}
        for field in (
            "area",
            "industry",
            "fullname",
            "enname",
            "cnspell",
            "market",
            "exchange",
            "curr_type",
            "list_status",
            "delist_date",
            "is_hs",
        ):
            value = row.get(field)
            if value not in (None, ""):
                metadata[field] = value
        return Instrument(
            symbol=symbol,
            name=name,
            exchange=exchange,
            security_type=SecurityType.STOCK,
            listed_at=_compact_date(row.get("list_date")),
            lot_size=100,
            active=status in {"", "L"},
            metadata=metadata,
        )

    async def fetch_instruments(self) -> List[Instrument]:
        cached = self._instrument_cache
        if cached is not None and self._monotonic() - cached[0] < self.instruments_cache_seconds:
            return list(cached[1])
        async with self._instrument_guard():
            cached = self._instrument_cache
            if cached is not None and self._monotonic() - cached[0] < self.instruments_cache_seconds:
                return list(cached[1])
            rows = await self._request_json(
                "/v2/reference/stocks",
                operation="reference-stocks",
            )
            instruments = tuple(
                item
                for item in (self._instrument_from_row(row) for row in rows)
                if item is not None
            )
            if rows and not instruments:
                self._last_error_type = "AShareHubDataError"
                raise AShareHubDataError(
                    "AShareHub stock catalogue contains no usable rows",
                    operation="reference-stocks",
                )
            self._instrument_cache = (self._monotonic(), instruments)
            self._last_error_type = None
            return list(instruments)

    def seed_instruments(self, instruments: Sequence[Instrument]) -> int:
        """Hydrate the long-lived catalogue cache from SQLite on restart."""

        accepted = tuple(
            item
            for item in instruments
            if isinstance(item, Instrument)
            and str(item.metadata.get("source") or "") == "asharehub"
        )
        if not accepted:
            return 0
        self._instrument_cache = (self._monotonic(), accepted)
        self._instrument_cache_persisted = True
        return len(accepted)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._quotes.clear()
        self._quote_attempted_at.clear()
        self._bar_cache.clear()
        self._seeded_bars.clear()
        self._network_bar_keys.clear()
        self._instrument_cache = None
        self._api_key = ""


__all__ = [
    "ASHAREHUB_BASE_URL",
    "ASHAREHUB_REALTIME_BATCH_LIMIT",
    "AShareHubError",
    "AShareHubConfigurationError",
    "AShareHubAuthenticationError",
    "AShareHubPermissionError",
    "AShareHubRateLimitError",
    "AShareHubBudgetError",
    "AShareHubRequestError",
    "AShareHubTransportError",
    "AShareHubDataError",
    "AShareHubMarketDataProvider",
]
