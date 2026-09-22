"""Provider contract used to isolate vendor-specific market-data APIs."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import asyncio
import hashlib
import json
from typing import List, Optional, Sequence, Tuple

from quant_monitor.domain import Bar, Instrument, Quote, ensure_market_time
from quant_monitor.trading_calendar import TradingCalendar


class BarFetchTransport(str, Enum):
    """How one bar fetch was satisfied.

    ``LEGACY_UNKNOWN`` is deliberately conservative: the compatibility hook
    can call an older provider, but it cannot infer whether that provider used
    the network or an internal cache.  Callers must not promote such a result
    to trusted collection lineage.
    """

    NETWORK = "network"
    MEMORY_CACHE = "memory_cache"
    SQLITE_CACHE = "sqlite_cache"
    NO_REQUEST = "no_request"
    LEGACY_UNKNOWN = "legacy_unknown"


def response_content_hash(value: object) -> str:
    """Digest a parsed provider response without retaining its raw body.

    This is a deterministic receipt for lineage comparison, not a vendor
    signature.  Raw bodies are intentionally never persisted because they can
    echo credentials, request headers, or unrelated account metadata.
    """

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class BarFetchResult:
    """A compatible bar view plus the observations made by this fetch.

    ``bars`` is the provider's current, possibly cache-combined view and keeps
    the historical ``fetch_bars`` behavior.  ``observations`` contains only
    rows parsed from a network response completed during *this* call.  A cache
    hit therefore has no new observations and no ``response_received_at``.
    """

    bars: Tuple[Bar, ...]
    observations: Tuple[Bar, ...]
    transport: BarFetchTransport
    response_received_at: Optional[datetime] = None
    response_content_hash: Optional[str] = None

    def __post_init__(self) -> None:
        bars = tuple(self.bars)
        observations = tuple(self.observations)
        if any(not isinstance(item, Bar) for item in bars):
            raise TypeError("bars must contain Bar values")
        if any(not isinstance(item, Bar) for item in observations):
            raise TypeError("observations must contain Bar values")
        transport = (
            self.transport
            if isinstance(self.transport, BarFetchTransport)
            else BarFetchTransport(self.transport)
        )
        received_at = self.response_received_at
        if received_at is not None:
            received_at = ensure_market_time(received_at)
        if transport is BarFetchTransport.NETWORK:
            if received_at is None:
                raise ValueError("network bar fetch requires response_received_at")
        else:
            if observations:
                raise ValueError("only a network bar fetch can contain observations")
            if received_at is not None:
                raise ValueError(
                    "only a network bar fetch can have response_received_at"
                )
        response_hash = self.response_content_hash
        if response_hash is not None:
            response_hash = str(response_hash).strip().lower()
            if len(response_hash) != 64 or any(
                char not in "0123456789abcdef" for char in response_hash
            ):
                raise ValueError(
                    "response_content_hash must be a lowercase SHA-256 hex digest"
                )
            if transport is not BarFetchTransport.NETWORK:
                raise ValueError(
                    "only a network bar fetch can have response_content_hash"
                )
        object.__setattr__(self, "bars", bars)
        object.__setattr__(self, "observations", observations)
        object.__setattr__(self, "transport", transport)
        object.__setattr__(self, "response_received_at", received_at)
        object.__setattr__(self, "response_content_hash", response_hash)

    @property
    def current_view(self) -> Tuple[Bar, ...]:
        """Explicit alias for the cache-combined compatibility view."""

        return self.bars


class MarketDataProviderError(RuntimeError):
    """Base error for recoverable provider/configuration failures."""


class MarketDataProvider(ABC):
    """Asynchronous interface implemented by every real or simulated data feed.

    A future vendor adapter only needs to translate its payloads into the domain
    models.  Network retries, authentication and rate limiting stay inside that
    adapter and never leak into strategies.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable source name persisted alongside market data."""

    @abstractmethod
    async def fetch_quotes(self, symbols: Sequence[str]) -> List[Quote]:
        """Fetch the latest quote for every requested symbol."""

    async def fetch_quotes_relaxed(self, symbols: Sequence[str]) -> List[Quote]:
        """Fetch available quotes while tolerating missing/unpriced rows.

        Batch universe endpoints often contain suspended instruments.  A
        provider may override this hook to return the usable subset without
        weakening the strict semantics of :meth:`fetch_quotes` used by the
        collector for a configured watchlist.  Providers without a separate
        partial-data policy inherit the strict call unchanged.
        """

        return await self.fetch_quotes(symbols)

    @abstractmethod
    async def fetch_bars(
        self,
        symbol: str,
        interval: str = "1m",
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 200,
    ) -> List[Bar]:
        """Fetch historical bars ordered from oldest to newest."""

    async def fetch_bars_many(
        self,
        symbols: Sequence[str],
        interval: str = "1m",
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 200,
    ) -> List[Bar]:
        """Fetch bars for several symbols, allowing provider-side batching.

        The default preserves the original concurrent per-symbol behavior.
        Providers with a native batch endpoint should override this method to
        protect request quotas and connection budgets.
        """

        batches = await asyncio.gather(
            *(
                self.fetch_bars(
                    symbol,
                    interval=interval,
                    start=start,
                    end=end,
                    limit=limit,
                )
                for symbol in symbols
            )
        )
        return [bar for batch in batches for bar in batch]

    async def fetch_bars_with_lineage(
        self,
        symbol: str,
        interval: str = "1m",
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 200,
    ) -> BarFetchResult:
        """Fetch bars without inventing lineage for legacy providers.

        Providers that can distinguish transport and cache paths should
        override this method.  The default keeps every existing provider
        source-compatible while making the missing evidence explicit.
        """

        bars = await self.fetch_bars(
            symbol,
            interval=interval,
            start=start,
            end=end,
            limit=limit,
        )
        return BarFetchResult(
            bars=tuple(bars),
            observations=(),
            transport=BarFetchTransport.LEGACY_UNKNOWN,
        )

    @property
    def universe_scope(self) -> str:
        """Describe whether ``fetch_instruments`` is complete or configured."""

        return "provider_catalogue"

    @property
    def quote_universe_scope(self) -> str:
        """Describe the safe default scope for bulk quote snapshots.

        Providers with a large catalogue and a metered quote API can return
        ``configured_symbols`` so generic REST routes never fan out across the
        entire security master implicitly.
        """

        return self.universe_scope

    @property
    def configured_symbols(self) -> Sequence[str]:
        """Return an optional quota-safe configured quote scope."""

        return ()

    def get_trading_calendar(
        self, symbols: Optional[Sequence[str]] = None
    ) -> Optional[TradingCalendar]:
        """Return a cache-only session calendar when the provider supports one.

        Providers without an exchange calendar return ``None``.  Runtime
        callers then retain the explicit weekday fallback; this hook never
        performs network I/O and therefore cannot consume a provider quota.
        """

        return None

    @property
    def supported_intervals(self) -> Optional[Sequence[str]]:
        """Return explicit bar intervals, or ``None`` when provider-defined."""

        return None

    async def fetch_instruments(self) -> List[Instrument]:
        """Return the provider's instrument catalogue when available."""

        return []

    async def close(self) -> None:
        """Release provider resources.  Stateless providers need no override."""

    async def __aenter__(self) -> "MarketDataProvider":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()
