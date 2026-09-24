"""Application composition root and process-local runtime state."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

from quant_monitor.config import Settings
from quant_monitor.data_sources import (
    AShareHubAuthenticationError,
    AShareHubBudgetError,
    AShareHubMarketDataProvider,
    AShareHubPermissionError,
    AShareHubRateLimitError,
    AkShareMarketDataProvider,
    AllTickMarketDataProvider,
    MockMarketDataProvider,
)
from quant_monitor.domain import (
    EventType,
    Exchange,
    Instrument,
    MarketEvent,
    Quote,
    SecurityType,
    now_market_time,
)
from quant_monitor.market import market_status
from quant_monitor.trading_calendar import DEFAULT_TRADING_CALENDAR, TradingCalendar
from quant_monitor.serialization import alert_dict, jsonable, quote_dict, signal_dict
from quant_monitor.services import (
    CollectionResult,
    EventBus,
    IndicatorCalculator,
    MonitoringService,
    Subscription,
)
from quant_monitor.storage import SQLiteRepository
from quant_monitor.strategies import (
    BankDailyMultifactorStrategy,
    MovingAverageCrossStrategy,
    StrategyRegistry,
)
from quant_monitor.universes import (
    BANK_STOCK_FALLBACK,
    fallback_bank_instruments,
    is_active_bank_stock,
    select_bank_instruments,
)


STOCK_NAMES = {
    "000001.SZ": "平安银行",
    "000858.SZ": "五粮液",
    "300750.SZ": "宁德时代",
    "600036.SH": "招商银行",
    "600519.SH": "贵州茅台",
    "601318.SH": "中国平安",
}
STOCK_NAMES.update(dict(BANK_STOCK_FALLBACK))
LOGGER = logging.getLogger(__name__)
MINIMUM_AUTHORITATIVE_BANK_CATALOGUE = len(BANK_STOCK_FALLBACK) - 2
MINIMUM_AUTHORITATIVE_STOCK_CATALOGUE = 1_000


def build_data_readiness(
    *,
    configured_symbols: int,
    received_symbols: int,
    freshness_state: str,
    market_is_trading: bool,
) -> Dict[str, Any]:
    """Derive the cache-only market-data readiness contract.

    Keep this rule in one place so ``system_status`` and ``/ready`` cannot
    drift.  A closed market may use prior-close snapshots, but every
    configured symbol must still be represented; a partial universe is not a
    usable data set merely because the exchange is closed.
    """

    configured = max(0, int(configured_symbols))
    received = max(0, int(received_symbols))
    state = str(freshness_state or "unknown")
    if configured == 0:
        ready = True
        reason = "no_symbols_configured"
    elif market_is_trading:
        ready = state in {"fresh", "untracked"} and received >= configured
        reason = "fresh_quotes_required"
    else:
        ready = received >= configured and state not in {"missing", "future_timestamp"}
        reason = "market_closed_cached_close_allowed"
    return {
        "ready": bool(ready),
        "reason": reason,
        "configured_symbols": configured,
        "received_symbols": received,
        "freshness_state": state,
        "market_is_trading": bool(market_is_trading),
    }


def build_calendar_readiness(
    *,
    provider_name: str,
    calendar_enabled: bool,
    calendar_source: Optional[str],
    calendar_error_type: Optional[str],
    configured_symbols: int,
) -> Dict[str, Any]:
    """Derive the cache-only trading-calendar readiness contract.

    This gate deliberately checks only the provider's already-loaded calendar
    state; it never refreshes a snapshot or spends an upstream request.  The
    exchange cache is considered usable only when both SSE and SZSE rows were
    loaded (the provider reports this as ``asharehub`` or
    ``asharehub_cache``).  A partial, expired, corrupt, or budget-protected
    cache remains an explicit fallback and must not satisfy an opt-in
    ``require_calendar`` readiness probe.
    """

    configured = max(0, int(configured_symbols))
    provider = str(provider_name or "").strip().lower()
    source = str(calendar_source or "weekday_fallback")
    error = str(calendar_error_type).strip() if calendar_error_type else None
    if provider != "asharehub" or configured == 0:
        return {
            "ready": True,
            "required": False,
            "reason": "not_required" if provider != "asharehub" else "no_symbols_configured",
            "provider": provider,
            "enabled": bool(calendar_enabled),
            "source": source,
            "error_type": error,
            "configured_symbols": configured,
        }
    if not calendar_enabled:
        reason = "calendar_disabled"
        ready = False
    elif source in {"asharehub", "asharehub_cache"} and not error:
        reason = "exchange_calendar_loaded"
        ready = True
    else:
        reason = "exchange_calendar_unavailable"
        ready = False
    return {
        "ready": ready,
        "required": True,
        "reason": reason,
        "provider": provider,
        "enabled": bool(calendar_enabled),
        "source": source,
        "error_type": error,
        "configured_symbols": configured,
    }


_ERROR_TYPE_PATTERN = re.compile(r"\b[A-Za-z][A-Za-z0-9_]*(?:Error|Exception)\b")


def _public_error_type(error: Any) -> Optional[str]:
    """Extract only a safe exception class name for public health payloads.

    ``MonitoringService`` keeps a richer, process-local message for logs and
    alert records.  That message can contain provider response text, URLs, or
    other sensitive material, so the HTTP status projection must never echo it.
    """

    if not error:
        return None
    match = _ERROR_TYPE_PATTERN.search(str(error))
    return match.group(0) if match else "CollectionError"


class WebSocketHub:
    """Fan out domain events to connected local dashboard clients."""

    def __init__(self) -> None:
        self._clients: Set[Any] = set()
        self._lock: asyncio.Lock = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @property
    def count(self) -> int:
        return len(self._clients)

    async def connect(self, websocket: Any) -> None:
        await websocket.accept()
        async with self._get_lock():
            self._clients.add(websocket)

    async def disconnect(self, websocket: Any) -> None:
        async with self._get_lock():
            self._clients.discard(websocket)

    async def broadcast(self, message: Dict[str, Any]) -> None:
        async with self._get_lock():
            clients = tuple(self._clients)
        failed = []
        for client in clients:
            try:
                await asyncio.wait_for(client.send_json(message), timeout=1.0)
            except Exception:
                failed.append(client)
        if failed:
            async with self._get_lock():
                for client in failed:
                    self._clients.discard(client)


class ApplicationRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.symbols: Tuple[str, ...] = tuple(settings.watchlist)
        self.universe_source = "configured_watchlist"
        db_path = Path(settings.database_path)
        if not db_path.is_absolute():
            db_path = Path(__file__).resolve().parent.parent / db_path
        # Pass the lifecycle deadline into SQLite itself.  ``asyncio.to_thread``
        # keeps migrations off the event loop, but cancelling that future cannot
        # interrupt a native sqlite call; without an inner deadline a timed-out
        # startup could still hold the repository lock while shutdown tries to
        # close it.  The repository therefore stops waiting at the same bound.
        self.repository = SQLiteRepository(
            db_path,
            initialization_timeout_seconds=settings.startup_database_timeout_seconds,
        )
        provider_name = str(settings.data_provider).strip().lower()
        if provider_name == "mock":
            self.provider = MockMarketDataProvider(
                seed=settings.mock_seed,
                clock=now_market_time,
            )
        elif provider_name == "asharehub":
            budget_path = Path(settings.asharehub_budget_path)
            if not budget_path.is_absolute():
                budget_path = Path(__file__).resolve().parent.parent / budget_path
            trade_calendar_path = Path(settings.asharehub_trade_calendar_path)
            if not trade_calendar_path.is_absolute():
                trade_calendar_path = Path(__file__).resolve().parent.parent / trade_calendar_path
            self.provider = AShareHubMarketDataProvider(
                api_key=settings.asharehub_api_key,
                symbols=settings.watchlist,
                instrument_names=STOCK_NAMES,
                base_url=settings.asharehub_base_url,
                request_timeout_seconds=settings.asharehub_request_timeout_seconds,
                quote_cache_seconds=(
                    settings.asharehub_market_quote_cache_seconds
                    if settings.universe_mode == "market"
                    else settings.asharehub_quote_cache_seconds
                ),
                bars_cache_seconds=settings.asharehub_bars_cache_seconds,
                instruments_cache_seconds=settings.asharehub_instruments_cache_seconds,
                daily_soft_limit=settings.asharehub_daily_soft_limit,
                official_daily_limit=settings.asharehub_official_daily_limit,
                budget_path=budget_path,
                retries=settings.asharehub_retries,
                retry_backoff_seconds=settings.asharehub_retry_backoff_seconds,
                strict_symbols=settings.asharehub_strict_symbols,
                trade_calendar_enabled=settings.asharehub_trade_calendar_enabled,
                trade_calendar_cache_seconds=settings.asharehub_trade_calendar_cache_seconds,
                trade_calendar_path=trade_calendar_path,
            )
        elif provider_name == "alltick":
            self.provider = AllTickMarketDataProvider(
                token=settings.alltick_token,
                symbols=settings.watchlist,
                instrument_names=STOCK_NAMES,
                request_timeout_seconds=settings.alltick_request_timeout_seconds,
                http_min_interval_seconds=settings.alltick_http_min_interval_seconds,
                quote_cache_seconds=settings.alltick_quote_cache_seconds,
                bars_cache_seconds=settings.alltick_bars_cache_seconds,
                retries=settings.alltick_retries,
                retry_backoff_seconds=settings.alltick_retry_backoff_seconds,
                batch_size=settings.alltick_batch_size,
                kline_batch_size=settings.alltick_kline_batch_size,
                use_websocket=settings.alltick_use_websocket,
                use_order_book=settings.alltick_use_order_book,
                bootstrap_daily_reference=settings.alltick_bootstrap_daily_reference,
                fetch_static_info=settings.alltick_fetch_static_info,
                websocket_symbol_limit=settings.alltick_websocket_symbol_limit,
                websocket_subscription_gap_seconds=(
                    settings.alltick_websocket_subscription_gap_seconds
                ),
                permission_retry_seconds=settings.alltick_permission_retry_seconds,
            )
        elif provider_name == "akshare":
            self.provider = AkShareMarketDataProvider(
                quote_cache_seconds=settings.akshare_quote_cache_seconds,
                bars_cache_seconds=settings.akshare_bars_cache_seconds,
                retries=settings.akshare_retries,
                retry_backoff_seconds=settings.akshare_retry_backoff_seconds,
                strict_symbols=settings.akshare_strict_symbols,
                use_spot_fallback=settings.akshare_use_spot_fallback,
            )
        else:
            raise ValueError(
                "unsupported data provider {!r}; choose 'asharehub', 'alltick', 'akshare' or 'mock'".format(
                    settings.data_provider
                )
            )
        # Providers may expose a cache-only, finite exchange calendar.  Start
        # on the explicit weekday fallback and refresh this view after a
        # provider cache is hydrated; no network request is made here.
        self.trading_calendar: TradingCalendar = DEFAULT_TRADING_CALENDAR
        self.quote_stale_after_seconds = (
            max(
                30.0,
                (
                    settings.asharehub_quote_cache_seconds
                    if self.provider.name == "asharehub"
                    else (
                        settings.alltick_quote_cache_seconds
                        if self.provider.name == "alltick"
                        else settings.akshare_quote_cache_seconds
                    )
                )
                * 2.0,
                settings.poll_interval_seconds * 3.0,
            )
            if self.provider.name in {"asharehub", "alltick", "akshare"}
            else None
        )
        self.event_bus = EventBus()
        ordinary_strategies = ()
        cohort_strategies = ()
        strategy_registry = StrategyRegistry()
        if settings.strategy_profile == "moving_average_cross":
            # Keep the runtime on the same allow-listed construction path as
            # configuration/API catalogue consumers.  The registry accepts
            # concrete built-ins only; no import path or eval is involved.
            strategy_registry.register(
                "moving_average_cross", MovingAverageCrossStrategy
            )
            ordinary_strategies = (
                strategy_registry.create(
                    "moving_average_cross", interval=settings.bar_interval
                ),
            )
        else:
            strategy_registry.register(
                "bank_daily_multifactor_v1", BankDailyMultifactorStrategy
            )
            cohort_strategies = (
                strategy_registry.create(
                    "bank_daily_multifactor_v1",
                    min_history=settings.bank_strategy_min_history,
                    min_universe=settings.bank_strategy_min_universe,
                    entry_percentile=settings.bank_strategy_entry_percentile,
                    exit_percentile=settings.bank_strategy_exit_percentile,
                    maximum_annualized_volatility=(
                        settings.bank_strategy_max_volatility
                    ),
                    maximum_drawdown=settings.bank_strategy_max_drawdown,
                    liquidity_floor_percentile=(
                        settings.bank_strategy_min_liquidity_percentile
                    ),
                    corporate_action_gap=(
                        settings.bank_strategy_corporate_action_gap
                    ),
                    fundamental_weight=settings.bank_strategy_fundamental_weight,
                ),
            )
        self.monitor = MonitoringService(
            provider=self.provider,
            repository=self.repository,
            symbols=settings.watchlist,
            event_bus=self.event_bus,
            strategies=ordinary_strategies,
            cohort_strategies=cohort_strategies,
            poll_interval=settings.poll_interval_seconds,
            bar_interval=settings.bar_interval,
            bar_limit=settings.history_bar_count,
            trading_calendar=self.trading_calendar,
        )
        self.hub = WebSocketHub()
        self.subscription: Subscription = None
        self.started_at: datetime = None
        self._instrument_task = None
        self._catalogue_refresh_task: Optional["asyncio.Task[None]"] = None
        self._history_bootstrap_task: Optional["asyncio.Task[None]"] = None
        self._startup_started_at: Optional[datetime] = None
        self._startup_completed_at: Optional[datetime] = None
        self._startup_duration_ms: Optional[float] = None
        self._startup_phase = "not_started"
        self._startup_error_type: Optional[str] = None
        self._history_bootstrap_total = 0
        self._history_bootstrap_ready = 0
        self._history_bootstrap_deferred = False
        self._history_bootstrap_state = "not_required"
        self._history_bootstrap_current_symbol: Optional[str] = None
        self._history_bootstrap_attempted = 0
        self._history_bootstrap_failed: Dict[str, str] = {}
        self._history_bootstrap_started_at: Optional[datetime] = None
        self._history_bootstrap_completed_at: Optional[datetime] = None
        self._history_bootstrap_last_check_at: Optional[datetime] = None
        self._last_research_signature: Optional[Tuple[Tuple[str, str, int], ...]] = None
        self._refresh_strategy_profile()

    def current_trading_calendar(self) -> TradingCalendar:
        """Return the provider calendar view used by quality and research.

        AShareHub loads its finite calendar lazily and may refresh it while
        the monitor is already running.  The optional provider hook is
        intentionally cache-only; providers without one stay on the explicit
        weekday fallback.  Keep the collector bound to the same object so
        ingestion, data-quality reports and backtests share one identity.
        """

        getter = getattr(self.provider, "get_trading_calendar", None)
        selected = None
        if callable(getter):
            try:
                selected = getter(self.symbols)
            except Exception:
                selected = None
        if selected is None:
            selected = DEFAULT_TRADING_CALENDAR
        self.trading_calendar = selected
        collector = getattr(getattr(self, "monitor", None), "collector", None)
        if collector is not None:
            collector.trading_calendar = selected
        return selected

    def _cached_provider_instruments(self):
        """Load the provider-owned security master without blocking the event loop."""

        return [
            item
            for item in self.repository.list_instruments(active_only=False)
            if str(item.metadata.get("source") or "") == self.provider.name
        ]

    def _research_symbols(self) -> Tuple[str, ...]:
        """Return the bounded historical/strategy universe.

        Market mode expands only the realtime quote basket.  Daily history
        remains an explicit research scope (the configured watchlist) so a
        full-market snapshot cannot silently turn into thousands of daily
        requests or an unbounded strategy evaluation.
        """

        if self.settings.universe_mode == "market":
            return tuple(self.settings.watchlist)
        return tuple(self.symbols)

    def _cached_bars_for_startup(self):
        """Read the bounded startup bar cache in one worker-thread operation."""

        cached_bars = []
        intervals = tuple(dict.fromkeys((self.settings.bar_interval, "1d")))
        for symbol in self._research_symbols():
            for interval in intervals:
                limit = (
                    self.settings.history_bar_count
                    if interval == self.settings.bar_interval
                    else 2
                )
                cached_bars.extend(
                    bar
                    for bar in self.repository.get_bars(
                        symbol,
                        interval=interval,
                        limit=limit,
                        source_prefix=self.provider.name,
                    )
                    if str(bar.source).startswith(self.provider.name)
                )
        return cached_bars

    def _set_startup_phase(self, phase: str) -> float:
        self._startup_phase = phase
        LOGGER.info(
            "application startup phase=%s status=starting provider=%s",
            phase,
            self.provider.name,
        )
        return time.perf_counter()

    def _complete_startup_phase(self, phase: str, started: float, **details: Any) -> None:
        fields = " ".join("{}={}".format(key, value) for key, value in details.items())
        LOGGER.info(
            "application startup phase=%s status=complete duration_ms=%.1f%s",
            phase,
            (time.perf_counter() - started) * 1000.0,
            " " + fields if fields else "",
        )

    async def _initialize_repository_with_progress(self) -> None:
        """Initialize SQLite with lock-wait heartbeats and a hard deadline.

        SQLite's busy timeout only bounds an individual connection attempt;
        the repository may retry several times.  A separate runtime deadline
        keeps an abandoned writer lock from holding the ASGI lifespan open
        indefinitely, while preserving the one-second progress heartbeat.
        """

        task = asyncio.create_task(
            asyncio.to_thread(self.repository.initialize),
            name="sqlite-initialize",
        )
        started = time.perf_counter()
        timeout_seconds = float(self.settings.startup_database_timeout_seconds)
        deadline = started + timeout_seconds
        try:
            while True:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    LOGGER.error(
                        "application startup phase=database_initialize status=timeout "
                        "duration_ms=%.1f timeout_seconds=%.3f database=%s; "
                        "close other processes using this database and retry",
                        (time.perf_counter() - started) * 1000.0,
                        timeout_seconds,
                        str(Path(self.repository.path).resolve()),
                    )
                    raise RuntimeError(
                        "SQLite initialization timed out after {:.3f} seconds; "
                        "another process may hold the database lock".format(
                            timeout_seconds
                        )
                    )
                done, _ = await asyncio.wait(
                    (task,), timeout=min(1.0, remaining)
                )
                if done:
                    await task
                    return
                LOGGER.warning(
                    "application startup phase=database_initialize status=waiting "
                    "duration_ms=%.1f database=%s; another process may hold the SQLite lock",
                    (time.perf_counter() - started) * 1000.0,
                    str(Path(self.repository.path).resolve()),
                )
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    def _refresh_strategy_profile(self) -> None:
        """Enable the configured strategy only when its universe is meaningful."""

        if self.settings.strategy_profile == "bank_daily_multifactor_v1":
            self.monitor.cohort_strategies = (
                BankDailyMultifactorStrategy(
                    min_history=self.settings.bank_strategy_min_history,
                    min_universe=self.settings.bank_strategy_min_universe,
                    entry_percentile=self.settings.bank_strategy_entry_percentile,
                    exit_percentile=self.settings.bank_strategy_exit_percentile,
                    maximum_annualized_volatility=(
                        self.settings.bank_strategy_max_volatility
                    ),
                    maximum_drawdown=self.settings.bank_strategy_max_drawdown,
                    liquidity_floor_percentile=(
                        self.settings.bank_strategy_min_liquidity_percentile
                    ),
                    corporate_action_gap=(
                        self.settings.bank_strategy_corporate_action_gap
                    ),
                    fundamental_weight=self.settings.bank_strategy_fundamental_weight,
                ),
            )

    def instrument_name(self, symbol: str) -> str:
        """Resolve a provider catalogue name with a deterministic fallback."""

        if self.repository.initialized:
            try:
                item = self.repository.get_instrument(symbol)
                if item is not None and item.name:
                    return item.name
            except RuntimeError:
                pass
        return STOCK_NAMES.get(symbol, symbol)

    def _configure_research_universe_from_cache(self) -> None:
        """Resolve the quote and bounded research universes from local cache."""

        catalogue = [
            item
            for item in self.repository.list_instruments(active_only=False)
            if str(item.metadata.get("source") or "") == self.provider.name
        ]
        if self.settings.universe_mode == "market":
            selected = tuple(
                sorted(
                    (
                        item
                        for item in catalogue
                        if item.active
                        and item.security_type.value == "stock"
                        and item.exchange.value in {"SH", "SZ", "BJ"}
                    ),
                    key=lambda item: item.symbol,
                )
            )
            # A market basket is only authoritative when it came from a
            # complete provider catalogue.  A partial cache must remain
            # visible as a watchlist fallback rather than being presented as
            # full-market coverage.
            if len(catalogue) < MINIMUM_AUTHORITATIVE_STOCK_CATALOGUE or not selected:
                selected = tuple(
                    item
                    for item in self.repository.list_instruments(active_only=False)
                    if item.symbol in self.settings.watchlist
                ) or tuple(
                    Instrument(
                        symbol=symbol,
                        name=STOCK_NAMES.get(symbol, symbol),
                        exchange=Exchange(symbol.rsplit(".", 1)[1]),
                        security_type=SecurityType.STOCK,
                        active=True,
                        metadata={"source": "configured_watchlist"},
                    )
                    for symbol in self.settings.watchlist
                )
                self.universe_source = "configured_watchlist_fallback"
            else:
                self.universe_source = "asharehub_market_catalogue"
            self.symbols = tuple(item.symbol for item in selected)
            provider_configure = getattr(self.provider, "configure_symbols", None)
            if provider_configure is not None:
                try:
                    provider_configure(
                        self.symbols,
                        allow_batched=self.provider.name == "asharehub",
                    )
                except TypeError:
                    provider_configure(self.symbols)
            self.monitor.configure_symbols(self.symbols)
            self._refresh_strategy_profile()
            return
        if self.settings.universe_mode != "bank":
            return
        dynamic_banks = tuple(
            sorted(
                (item for item in catalogue if is_active_bank_stock(item)),
                key=lambda item: item.symbol,
            )
        )
        # Cross-sectional research must not silently shrink to a handful of
        # classified rows from an incomplete security master. Keep the full,
        # visibly labelled fallback until the provider catalogue meets the
        # conservative catalogue-completeness thresholds.
        selected = (
            dynamic_banks
            if len(catalogue) >= MINIMUM_AUTHORITATIVE_STOCK_CATALOGUE
            and len(dynamic_banks) >= MINIMUM_AUTHORITATIVE_BANK_CATALOGUE
            else fallback_bank_instruments()
        )
        self.symbols = tuple(item.symbol for item in selected)
        self.universe_source = (
            "asharehub_industry"
            if selected
            and all(str(item.metadata.get("source") or "") == "asharehub" for item in selected)
            else "static_bank_fallback"
        )
        # Persist fallback names without clearing a healthy provider catalogue.
        if self.universe_source == "static_bank_fallback":
            self.repository.upsert_instruments(fallback_bank_instruments())
        provider_configure = getattr(self.provider, "configure_symbols", None)
        if provider_configure is not None:
            try:
                provider_configure(
                    self.symbols,
                    allow_batched=self.settings.universe_mode == "market",
                )
            except TypeError:
                provider_configure(self.symbols)
        self.monitor.configure_symbols(self.symbols)
        self._refresh_strategy_profile()

    async def _refresh_bank_catalogue(self) -> None:
        """Refresh the provider catalogue for the next process start.

        The running basket is deliberately not mutated after monitoring has
        started.  This keeps one collection cycle coherent and avoids a race
        with the provider cache; the new catalogue is authoritative on the
        next restart.
        """

        try:
            instruments = await self.provider.fetch_instruments()
            if not instruments:
                LOGGER.warning("bank catalogue refresh returned no instruments")
                return
            dynamic_banks = tuple(
                item for item in instruments if is_active_bank_stock(item)
            )
            if (
                len(instruments) < MINIMUM_AUTHORITATIVE_STOCK_CATALOGUE
                or len(dynamic_banks) < MINIMUM_AUTHORITATIVE_BANK_CATALOGUE
                or str(
                    dynamic_banks[0].metadata.get("source") or ""
                ) != self.provider.name
            ):
                LOGGER.warning(
                    "bank catalogue refresh is incomplete: %s instruments, %s classified banks",
                    len(instruments),
                    len(dynamic_banks),
                )
                return
            if any(
                str(item.metadata.get("source") or "") != self.provider.name
                for item in instruments
            ):
                LOGGER.warning("bank catalogue refresh contains mixed source metadata")
                return
            await asyncio.to_thread(self.repository.replace_instruments, instruments)
            LOGGER.info(
                "bank catalogue refreshed in background: %s active bank symbols",
                len(dynamic_banks),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.warning("background bank catalogue refresh unavailable: %s", exc)

    async def _refresh_instruments(self) -> None:
        try:
            instruments = await self.provider.fetch_instruments()
            await asyncio.to_thread(self.repository.upsert_instruments, instruments)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.warning("instrument catalogue unavailable: %s", exc)

    async def instruments(self):
        """Return a cached security master and persist explicit refreshes."""

        try:
            items = await self.provider.fetch_instruments()
        except Exception:
            cached = [
                item
                for item in self.repository.list_instruments()
                if str(item.metadata.get("source") or "") == self.provider.name
            ]
            if self.provider.name == "asharehub" and cached:
                return cached
            raise
        if self.provider.name == "asharehub" and items:
            dynamic_banks = tuple(item for item in items if is_active_bank_stock(item))
            if self.settings.universe_mode == "bank" and (
                len(items) < MINIMUM_AUTHORITATIVE_STOCK_CATALOGUE
                or len(dynamic_banks) < MINIMUM_AUTHORITATIVE_BANK_CATALOGUE
            ):
                raise ValueError("AShareHub instrument catalogue is incomplete")
            if any(
                str(item.metadata.get("source") or "") != self.provider.name
                for item in items
            ):
                raise ValueError("AShareHub instrument catalogue has mixed sources")
            await asyncio.to_thread(self.repository.replace_instruments, items)
        else:
            await asyncio.to_thread(self.repository.upsert_instruments, items)
        return items

    def _history_ready_symbols(self) -> Set[str]:
        """Return symbols with enough durable history for the active strategy."""

        minimum = min(
            self.settings.history_bar_count,
            self.settings.bank_strategy_min_history,
        )
        ready: Set[str] = set()
        freshness_check = getattr(self.provider, "has_current_bar_data", None)
        if freshness_check is not None:
            for symbol in self._research_symbols():
                if freshness_check(
                    symbol,
                    interval=self.settings.bar_interval,
                    minimum_rows=minimum,
                ):
                    ready.add(symbol)
            return ready
        for symbol in self._research_symbols():
            enough_rows = len(
                self.repository.get_bars(
                    symbol,
                    interval=self.settings.bar_interval,
                    limit=minimum,
                    source_prefix=self.provider.name,
                )
            ) >= minimum
            if enough_rows:
                ready.add(symbol)
        return ready

    def _cached_research_collection(
        self,
    ) -> Tuple[CollectionResult, Tuple[Tuple[str, str, int], ...]]:
        """Build one coherent research input on a worker thread."""

        bars = []
        metrics = []
        signature = []
        for symbol in self._research_symbols():
            symbol_bars = self.repository.get_bars(
                symbol,
                interval=self.settings.bar_interval,
                limit=self.settings.history_bar_count,
                source_prefix=self.provider.name,
            )
            bars.extend(symbol_bars)
            metrics.extend(IndicatorCalculator.metrics_from_bars(symbol_bars))
            signature.append(
                (
                    symbol,
                    symbol_bars[-1].timestamp.isoformat() if symbol_bars else "",
                    len(symbol_bars),
                )
            )
        quotes = self.repository.get_latest_quotes(
            self._research_symbols(),
            source_prefix=self.provider.name,
        )
        return (
            CollectionResult(tuple(quotes), tuple(bars), tuple(metrics)),
            tuple(signature),
        )

    async def _evaluate_cached_research(self) -> None:
        """Evaluate strategies once when the durable history snapshot changes."""

        collection, signature = await asyncio.to_thread(
            self._cached_research_collection
        )
        if signature == self._last_research_signature:
            return
        await self.monitor.evaluate_cached_collection(
            collection
        )
        self._last_research_signature = signature

    async def _bootstrap_asharehub_history(self) -> None:
        """Fill daily history incrementally after the ASGI app is available.

        Each symbol is persisted independently.  This provides a durable resume
        point if Windows closes the process, a request fails, or the local soft
        budget is reached part-way through the bank universe.
        """

        self._history_bootstrap_state = "running"
        self._history_bootstrap_started_at = now_market_time()
        self._history_bootstrap_completed_at = None
        LOGGER.info(
            "AShareHub history bootstrap started: %s/%s symbols ready",
            self._history_bootstrap_ready,
            self._history_bootstrap_total,
        )
        fatal_errors = (
            AShareHubAuthenticationError,
            AShareHubPermissionError,
            AShareHubRateLimitError,
            AShareHubBudgetError,
        )
        try:
            while True:
                self._history_bootstrap_last_check_at = now_market_time()
                # The quota-sensitive bootstrap is driven by the same fresh,
                # minimum-history predicate exposed as research readiness.
                # A newly listed security with 130 valid rows must not burn one
                # request every 15 minutes merely because the display target is
                # 240 rows; later sessions will grow its cache incrementally.
                ready_before = self._history_ready_symbols()
                pending = [
                    symbol
                    for symbol in self._research_symbols()
                    if symbol not in ready_before
                ]
                self._history_bootstrap_deferred = False
                for symbol in pending:
                    remaining = int(
                        self.provider.realtime_status.get("requests_remaining_soft")
                        or 0
                    )
                    if remaining <= self.settings.asharehub_history_reserve_requests:
                        self._history_bootstrap_deferred = True
                        self._history_bootstrap_state = "deferred_budget"
                        LOGGER.warning(
                            "AShareHub history bootstrap paused at %s/%s ready; "
                            "%s requests remain and %s are reserved",
                            self._history_bootstrap_ready,
                            self._history_bootstrap_total,
                            remaining,
                            self.settings.asharehub_history_reserve_requests,
                        )
                        break
                    self._history_bootstrap_current_symbol = symbol
                    self._history_bootstrap_attempted += 1
                    started = asyncio.get_running_loop().time()
                    try:
                        bar_result = await self.monitor.collector.collect_bar_batch(
                            (symbol,),
                            publish_events=False,
                            provider_options={
                                "reserve_requests": (
                                    self.settings.asharehub_history_reserve_requests
                                )
                            },
                        )
                        bars = bar_result.bars
                        ready_after = self._history_ready_symbols()
                        self._history_bootstrap_ready = len(ready_after)
                        if symbol in ready_after:
                            self._history_bootstrap_failed.pop(symbol, None)
                        else:
                            self._history_bootstrap_failed[symbol] = (
                                "stale_or_insufficient_history"
                            )
                        LOGGER.info(
                            "AShareHub history progress: %s/%s ready; symbol=%s rows=%s elapsed=%.2fs remaining_soft=%s",
                            self._history_bootstrap_ready,
                            self._history_bootstrap_total,
                            symbol,
                            len(bars),
                            asyncio.get_running_loop().time() - started,
                            self.provider.realtime_status.get("requests_remaining_soft"),
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self._history_bootstrap_failed[symbol] = type(exc).__name__
                        LOGGER.warning(
                            "AShareHub history unavailable for %s: %s",
                            symbol,
                            type(exc).__name__,
                        )
                        if isinstance(exc, AShareHubBudgetError) and getattr(
                            exc, "reserved", False
                        ):
                            self._history_bootstrap_deferred = True
                            self._history_bootstrap_state = "deferred_budget"
                            break
                        if isinstance(exc, fatal_errors):
                            self._history_bootstrap_state = "failed"
                            return
                    finally:
                        self._history_bootstrap_current_symbol = None
                self._history_bootstrap_ready = len(self._history_ready_symbols())
                if self._history_bootstrap_deferred:
                    # The persisted budget resets by Shanghai calendar date.
                    # Rechecking is local-only until the reserve is available.
                    await asyncio.sleep(self.settings.asharehub_history_refresh_seconds)
                    continue
                if self._history_bootstrap_failed:
                    self._history_bootstrap_state = "partial"
                elif self._history_bootstrap_ready >= self._history_bootstrap_total:
                    self._history_bootstrap_state = "complete"
                else:
                    self._history_bootstrap_state = "partial"
                if self._history_bootstrap_ready >= min(
                    self._history_bootstrap_total,
                    self.settings.bank_strategy_min_universe,
                ):
                    await self._evaluate_cached_research()
                # Keep the task alive so a long-running process notices the
                # next expected daily close without a per-symbol HTTP poll.
                await asyncio.sleep(self.settings.asharehub_history_refresh_seconds)
                self._history_bootstrap_state = "checking"
        finally:
            self._history_bootstrap_current_symbol = None
            self._history_bootstrap_completed_at = now_market_time()
            LOGGER.info(
                "AShareHub history bootstrap finished: state=%s ready=%s/%s failed=%s",
                self._history_bootstrap_state,
                self._history_bootstrap_ready,
                self._history_bootstrap_total,
                len(self._history_bootstrap_failed),
            )

    async def start(self) -> None:
        startup_timer = time.perf_counter()
        self._startup_started_at = now_market_time()
        self._startup_completed_at = None
        self._startup_duration_ms = None
        self._startup_error_type = None
        phase_timer = self._set_startup_phase("database_initialize")
        try:
            # SQLite migration and contract validation are synchronous. Run
            # them off-loop so a slow disk, antivirus hook or lock wait does
            # not freeze the ASGI event loop without an actionable phase log.
            await self._initialize_repository_with_progress()
            self._complete_startup_phase(
                "database_initialize",
                phase_timer,
                schema_version=self.repository.SCHEMA_VERSION,
            )
            restored_jobs = await asyncio.to_thread(
                self.monitor.restore_collection_jobs
            )
            if restored_jobs:
                LOGGER.info("restored %s durable collector job(s)", restored_jobs)
            phase_timer = self._set_startup_phase("cache_hydration")
            # A process can terminate after recording the start of a provider
            # request but before committing its terminal batch.  Such rows
            # must never remain indistinguishable from work still in flight.
            abandoned_batches = await asyncio.to_thread(
                self.repository.abandon_running_collection_batches,
                now_market_time(),
            )
            if abandoned_batches:
                LOGGER.warning(
                    "marked %s interrupted collection batch(es) as abandoned",
                    abandoned_batches,
                )
            if self.provider.name == "asharehub":
                cached_instruments = await asyncio.to_thread(
                    self._cached_provider_instruments
                )
                # A partially corrupted or incomplete security master must not
                # be promoted into a fresh seven-day provider cache. In bank
                # mode, only seed a catalogue that meets both conservative
                # whole-market and bank-sector completeness thresholds;
                # otherwise keep the static labelled fallback for this run and
                # let the background reference refresh repair the cache with
                # one budgeted request.
                if self.settings.universe_mode == "bank":
                    classified_bank_count = sum(
                        is_active_bank_stock(item) for item in cached_instruments
                    )
                    if (
                        len(cached_instruments)
                        < MINIMUM_AUTHORITATIVE_STOCK_CATALOGUE
                        or classified_bank_count
                        < MINIMUM_AUTHORITATIVE_BANK_CATALOGUE
                    ):
                        cached_instruments = []
                seed_instruments = getattr(self.provider, "seed_instruments", None)
                if seed_instruments is not None:
                    seed_instruments(cached_instruments)
                if (
                    self.settings.universe_mode == "market"
                    and len(cached_instruments) < MINIMUM_AUTHORITATIVE_STOCK_CATALOGUE
                ):
                    # Market mode explicitly opts into one startup reference
                    # request when the local catalogue is incomplete.  The
                    # resulting catalogue is persisted before the quote
                    # monitor starts, so every subsequent cycle can build a
                    # deterministic, batched market basket without repeating
                    # this request.
                    try:
                        refreshed = await self.provider.fetch_instruments()
                        if len(refreshed) >= MINIMUM_AUTHORITATIVE_STOCK_CATALOGUE:
                            await asyncio.to_thread(
                                self.repository.replace_instruments, refreshed
                            )
                            cached_instruments = list(refreshed)
                            if seed_instruments is not None:
                                seed_instruments(cached_instruments)
                            LOGGER.info(
                                "market catalogue loaded during startup: %s instruments",
                                len(cached_instruments),
                            )
                        else:
                            LOGGER.warning(
                                "market catalogue is incomplete: %s instruments",
                                len(refreshed),
                            )
                    except Exception as exc:
                        LOGGER.warning(
                            "market catalogue startup refresh unavailable: %s",
                            type(exc).__name__,
                        )
            await asyncio.to_thread(self._configure_research_universe_from_cache)
            seed_quotes = getattr(self.provider, "seed_quotes", None)
            cached_quotes = []
            if seed_quotes is not None:
                cached_quotes = await asyncio.to_thread(
                    self.repository.get_latest_quotes,
                    self.symbols,
                    source_prefix=self.provider.name,
                )
                seed_quotes(cached_quotes)
                self.monitor.remember_quotes(cached_quotes)
            seed_bars = getattr(self.provider, "seed_bars", None)
            cached_bars = []
            if seed_bars is not None:
                cached_bars = await asyncio.to_thread(self._cached_bars_for_startup)
                seed_bars(cached_bars)
            # AShareHub history is filled by a separate one-symbol-at-a-time
            # background job.  The normal monitor starts quote-only so the two
            # jobs cannot duplicate daily requests during application startup.
            if self.provider.name == "asharehub":
                self._history_bootstrap_total = len(self._research_symbols())
                self._history_bootstrap_ready = len(self._history_ready_symbols())
                self._history_bootstrap_state = "scheduled"
                self.monitor.collector.collect_bars_enabled = False
            self._complete_startup_phase(
                "cache_hydration",
                phase_timer,
                symbols=len(self.symbols),
                quotes=len(cached_quotes),
                bars=len(cached_bars),
                universe_source=self.universe_source,
            )
            phase_timer = self._set_startup_phase("service_activation")
            start_realtime = getattr(self.provider, "start_realtime", None)
            if start_realtime is not None:
                await start_realtime()
            if self.provider.name == "mock":
                await self._refresh_instruments()
            self.subscription = await self.event_bus.subscribe(None, self._on_event)
            self.started_at = now_market_time()
            # Deterministic offline mock data is primed synchronously for tests
            # and local demos.  Real provider network work never holds the
            # FastAPI lifespan open.
            if self.provider.name == "mock":
                await self.monitor.collect_once()
                await self.monitor.start(collect_immediately=False)
            else:
                await self.monitor.start(collect_immediately=True)
            self._startup_completed_at = now_market_time()
            self._startup_phase = "ready"
            self._startup_duration_ms = round(
                (time.perf_counter() - startup_timer) * 1000.0,
                3,
            )
            self._complete_startup_phase(
                "service_activation",
                phase_timer,
                monitor_running=self.monitor.running,
            )
            LOGGER.info(
                "application runtime ready duration_ms=%.1f; initial %s collection continues in background",
                self._startup_duration_ms,
                self.provider.name,
            )
            if self.provider.name == "asharehub":
                self._history_bootstrap_task = asyncio.create_task(
                    self._bootstrap_asharehub_history(),
                    name="asharehub-history-bootstrap",
                )
            if (
                self.provider.name == "asharehub"
                and self.settings.universe_mode == "bank"
                and self.universe_source == "static_bank_fallback"
            ):
                self._catalogue_refresh_task = asyncio.create_task(
                    self._refresh_bank_catalogue(),
                    name="asharehub-bank-catalogue-refresh",
                )
            if self.provider.name in {"alltick", "akshare"}:
                # Static info is optional enrichment and the free plan is
                # rate-limited or network-bound. Do not block FastAPI startup
                # while provider discovery completes.
                self._instrument_task = asyncio.create_task(
                    self._refresh_instruments(),
                    name="{}-static-instruments".format(self.provider.name),
                )
        except BaseException as exc:
            failed_phase = self._startup_phase
            self._startup_phase = "failed"
            self._startup_completed_at = None
            self._startup_error_type = type(exc).__name__
            self._startup_duration_ms = round(
                (time.perf_counter() - startup_timer) * 1000.0,
                3,
            )
            LOGGER.error(
                "application startup failed phase=%s duration_ms=%.1f error_type=%s",
                failed_phase,
                self._startup_duration_ms,
                self._startup_error_type,
            )
            history_task = self._history_bootstrap_task
            self._history_bootstrap_task = None
            if history_task is not None:
                history_task.cancel()
                await asyncio.gather(history_task, return_exceptions=True)
            catalogue_task = self._catalogue_refresh_task
            self._catalogue_refresh_task = None
            if catalogue_task is not None:
                catalogue_task.cancel()
                await asyncio.gather(catalogue_task, return_exceptions=True)
            instrument_task = self._instrument_task
            self._instrument_task = None
            if instrument_task is not None:
                instrument_task.cancel()
                try:
                    await instrument_task
                except asyncio.CancelledError:
                    pass
            if self.subscription is not None:
                await self.event_bus.unsubscribe(self.subscription)
                self.subscription = None
            # Activation can fail after monitor.start() has scheduled the
            # collector. Stop that task before closing its provider/database;
            # otherwise a failed lifespan can leave writes against a closed DB.
            await self.monitor.stop()
            await self.provider.close()
            self.repository.close()
            raise

    async def stop(self) -> None:
        # Revoke readiness before the first await so a concurrent probe cannot
        # report an accepting service while its dependencies are being closed.
        self._startup_phase = "stopping"
        history_task = self._history_bootstrap_task
        self._history_bootstrap_task = None
        if history_task is not None:
            history_task.cancel()
            await asyncio.gather(history_task, return_exceptions=True)
        catalogue_task = self._catalogue_refresh_task
        self._catalogue_refresh_task = None
        if catalogue_task is not None:
            catalogue_task.cancel()
            try:
                await catalogue_task
            except asyncio.CancelledError:
                pass
        instrument_task = self._instrument_task
        self._instrument_task = None
        if instrument_task is not None:
            instrument_task.cancel()
            try:
                await instrument_task
            except asyncio.CancelledError:
                pass
        await self.monitor.stop(close_provider=True)
        if self.subscription is not None:
            await self.event_bus.unsubscribe(self.subscription)
            self.subscription = None
        self.repository.close()
        self._startup_phase = "stopped"

    async def _on_event(self, event: MarketEvent) -> None:
        payload = event.payload
        if isinstance(payload, Quote):
            self.monitor.remember_quotes([payload])
            data = quote_dict(
                payload,
                self.instrument_name(payload.symbol),
                stale_after_seconds=self.quote_stale_after_seconds,
            )
        elif event.type == EventType.ALERT:
            data = alert_dict(payload)
        elif event.type == EventType.SIGNAL:
            data = signal_dict(payload)
        elif event.type in {EventType.BAR, EventType.METRIC}:
            # REST serves historical series. Broadcasting every bar and metric
            # in the polling batch would flood each browser on every cycle.
            return
        else:
            data = jsonable(payload)
        await self.hub.broadcast(
            {
                "type": event.type.value,
                "timestamp": event.timestamp.isoformat(),
                "data": data,
            }
        )

    def system_status(self) -> Dict[str, Any]:
        snapshot = self.monitor.current_snapshot()
        now = now_market_time()
        last = snapshot.get("last_collection_at")
        latency = max(0.0, (now - last).total_seconds() * 1000) if last else None
        running = bool(snapshot.get("running"))
        error = snapshot.get("last_error")
        public_error_type = _public_error_type(error)
        # A successful collector iteration does not prove that every quote is
        # current: providers may return a partial snapshot or replay a cached
        # timestamp.  Expose per-universe freshness counts so operators can
        # distinguish a healthy process from a healthy market feed.  This is
        # deliberately derived from in-memory normalized Quote objects and
        # never triggers provider I/O.
        monitored_symbols = tuple(self.symbols)
        latest_quotes = snapshot.get("quotes") or {}
        freshness_threshold = self.quote_stale_after_seconds
        fresh_count = stale_count = missing_count = future_count = 0
        ages = []
        quote_timestamps = []
        for symbol in monitored_symbols:
            quote = latest_quotes.get(symbol)
            if quote is None:
                missing_count += 1
                continue
            age_seconds = (now - quote.timestamp).total_seconds()
            quote_timestamps.append(quote.timestamp)
            if age_seconds < -5.0:
                future_count += 1
            elif freshness_threshold is not None and age_seconds > float(freshness_threshold):
                stale_count += 1
            else:
                fresh_count += 1
            ages.append(max(0.0, age_seconds))
        received_count = fresh_count + stale_count + future_count
        if not monitored_symbols:
            freshness_state = "unknown"
        elif received_count == 0:
            freshness_state = "missing"
        elif future_count:
            freshness_state = "future_timestamp"
        elif stale_count and fresh_count == 0:
            freshness_state = "stale"
        elif stale_count or missing_count:
            freshness_state = "partial"
        elif freshness_threshold is None:
            freshness_state = "untracked"
        else:
            freshness_state = "fresh"
        quote_freshness = {
            "state": freshness_state,
            "configured_symbols": len(monitored_symbols),
            "received_symbols": received_count,
            "fresh_symbols": fresh_count,
            "stale_symbols": stale_count,
            "missing_symbols": missing_count,
            "future_timestamp_symbols": future_count,
            "threshold_seconds": freshness_threshold,
            "oldest_age_seconds": round(max(ages), 3) if ages else None,
            "newest_timestamp": max(quote_timestamps).isoformat() if quote_timestamps else None,
            "as_of": now.isoformat(),
        }
        realtime_status = getattr(self.provider, "realtime_status", None)
        realtime = dict(realtime_status) if realtime_status is not None else {}
        calendar_readiness = build_calendar_readiness(
            provider_name=self.provider.name,
            calendar_enabled=bool(realtime.get("trade_calendar_enabled", False)),
            calendar_source=realtime.get("trade_calendar_source"),
            calendar_error_type=realtime.get("trade_calendar_error_type"),
            configured_symbols=len(monitored_symbols),
        )
        provider_issues = []
        provider_status: Dict[str, Any] = {
            "name": self.provider.name,
            "state": "running" if running else "starting",
            "quote_freshness": quote_freshness,
        }
        if realtime_status is not None:
            provider_status["realtime"] = realtime
            # Preserve the provider's response-shape diagnostics as a stable
            # aggregate in the public runtime contract.  The raw realtime
            # status remains available for backwards compatibility, while
            # this explicit object lets API/metrics consumers distinguish an
            # empty/partial upstream response from a transport failure.  Do
            # not expose per-row payloads or credentials here; the missing
            # symbol list is already normalized by the provider and is useful
            # for the local operator-facing status endpoint only.
            if self.provider.name == "asharehub":
                response_rows = realtime.get("last_quote_response_rows")
                parsed_symbols = realtime.get("last_quote_parsed_symbols")
                dropped_rows = realtime.get("last_quote_dropped_rows")
                invalid_rows = realtime.get("last_quote_invalid_rows")
                unexpected_rows = realtime.get("last_quote_unexpected_rows")
                duplicate_rows = realtime.get("last_quote_duplicate_rows")
                conflict_rows = realtime.get("last_quote_conflict_rows")
                missing_symbols = realtime.get("last_quote_missing_symbols") or []
                try:
                    requested_rows = int(
                        realtime.get("last_quote_requested_symbols")
                        or realtime.get("requested_symbols")
                        or 0
                    )
                except (TypeError, ValueError):
                    requested_rows = 0
                try:
                    parsed_count = int(parsed_symbols) if parsed_symbols is not None else None
                except (TypeError, ValueError):
                    parsed_count = None
                response_quality = {
                    "response_rows": response_rows,
                    "parsed_symbols": parsed_symbols,
                    "dropped_rows": dropped_rows,
                    "invalid_rows": invalid_rows,
                    "unexpected_rows": unexpected_rows,
                    "duplicate_rows": duplicate_rows,
                    "conflict_rows": conflict_rows,
                    "missing_symbols": list(missing_symbols)
                    if isinstance(missing_symbols, (list, tuple))
                    else [],
                    "missing_count": len(missing_symbols)
                    if isinstance(missing_symbols, (list, tuple))
                    else 0,
                    "requested_symbols": requested_rows,
                    "coverage_ratio": (
                        round(parsed_count / requested_rows, 6)
                        if parsed_count is not None and requested_rows > 0
                        else None
                    ),
                    "as_of": realtime.get("last_quote_success_at"),
                }
                provider_status["quote_response_quality"] = response_quality
            provider_status["mode"] = realtime.get("mode") or (
                "websocket" if self.provider.name == "alltick" else "polling"
            )
            if self.provider.name == "asharehub":
                used = int(realtime.get("requests_used_today") or 0)
                soft_limit = int(
                    realtime.get("daily_soft_limit")
                    or self.settings.asharehub_daily_soft_limit
                )
                provider_status["quota"] = {
                    "limit": int(
                        realtime.get("official_daily_limit")
                        or self.settings.asharehub_official_daily_limit
                    ),
                    "soft_limit": soft_limit,
                    "used": used,
                    "remaining_soft": max(
                        0,
                        int(realtime.get("requests_remaining_soft") or 0),
                    ),
                    "estimated": True,
                    "scope": "local_shanghai_calendar_day",
                    "budget_date": realtime.get("budget_date"),
                    "local_exhausted": bool(
                        realtime.get("local_budget_exhausted")
                    ),
                    "upstream_exhausted": bool(
                        realtime.get("upstream_quota_exhausted")
                    ),
                    "exhausted": bool(
                        realtime.get("local_budget_exhausted")
                        or realtime.get("upstream_quota_exhausted")
                    ),
                }
                provider_status["cache"] = {
                    "quote_ttl_seconds": realtime.get("quote_cache_seconds"),
                    "bars_ttl_seconds": self.settings.asharehub_bars_cache_seconds,
                    "last_quote_fetch_at": realtime.get("last_quote_success_at"),
                    "last_bar_fetch_at": realtime.get("last_bar_success_at"),
                    "last_catalogue_fetch_at": realtime.get(
                        "last_catalogue_success_at"
                    ),
                    "next_quote_refresh_at": realtime.get("next_refresh_at"),
                }
                provider_status["capabilities"] = {
                    "realtime_snapshot": True,
                    "order_book": False,
                    "official_websocket": False,
                    "intraday_bars": False,
                    "daily_bars": True,
                    "bar_intervals": ["1d"],
                    "instrument_catalogue": True,
                }
            normal_realtime_states = {"running"}
            if self.provider.name == "asharehub":
                normal_realtime_states.update({"idle", "http_polling", "closed"})
            if realtime.get("enabled") and realtime.get("state") not in normal_realtime_states:
                provider_issues.append(
                    "realtime_{}".format(realtime.get("state") or "unavailable")
                )
            if int(realtime.get("limited_symbols") or 0) > 0:
                provider_issues.append("realtime_subscription_limit")
            if realtime.get("quote_permission_denied_symbols"):
                provider_issues.append("quote_code_permission_denied")
            if realtime.get("bar_permission_denied_symbols"):
                provider_issues.append("bar_code_permission_denied")
            if realtime.get("order_book_permission_denied_symbols"):
                provider_issues.append("order_book_code_permission_denied")
            if (
                realtime.get("order_book_enabled")
                and (
                    realtime.get("order_book_snapshot_error_type")
                    or realtime.get("order_book_realtime_error_type")
                )
            ):
                provider_issues.append("order_book_unavailable")
            if (
                realtime.get("daily_reference_enabled")
                and realtime.get("daily_reference_error_type")
            ):
                provider_issues.append("daily_reference_unavailable")
            if (
                realtime.get("static_info_enabled")
                and realtime.get("static_info_error_type")
            ):
                provider_issues.append("static_info_unavailable")
        if error:
            provider_issues.append("collector_error")
        if self._startup_phase == "ready":
            if not running:
                provider_issues.append("collector_stopped")
            if not self.repository.initialized:
                provider_issues.append("database_disconnected")
        if self.provider.name == "asharehub" and self.repository.initialized and not snapshot.get("quotes"):
            try:
                cached_quotes = self.repository.get_latest_quotes(
                    self.symbols,
                    source_prefix=self.provider.name,
                )
            except RuntimeError:
                cached_quotes = []
            if not cached_quotes:
                provider_issues.append("no_quote_data")
        # A timestamp from the future is always an integrity failure: it can
        # indicate a clock/unit conversion bug or an upstream replay that is
        # not yet observable.  This remains actionable outside trading hours
        # too, unlike stale/missing quotes where the prior close is expected
        # while the exchange is closed.
        if self.provider.name in {"asharehub", "alltick", "akshare"} and future_count:
            provider_issues.append("quote_timestamp_future")
        # During an active A-share session stale/missing quotes are actionable
        # feed failures.  Outside trading hours the last close is expected and
        # must not make the process appear unhealthy.
        provider_market_status = getattr(self.provider, "current_market_status", None)
        market = (
            provider_market_status()
            if callable(provider_market_status)
            else market_status(now)
        )
        # Keep the data-readiness decision in the cache-only runtime status so
        # health probes, Prometheus scrapers and the dashboard share the same
        # semantics.  A running process is not necessarily serving usable
        # market data: during an open session every configured symbol must be
        # represented by a fresh quote, while outside trading hours a cached
        # prior close is a valid observation (future timestamps remain invalid
        # in either case).
        configured_count = len(monitored_symbols)
        data_readiness = build_data_readiness(
            configured_symbols=configured_count,
            received_symbols=received_count,
            freshness_state=freshness_state,
            market_is_trading=bool(market.get("is_trading")),
        )
        if self.provider.name in {"asharehub", "alltick", "akshare"} and market.get("is_trading"):
            if stale_count:
                provider_issues.append("quote_data_stale")
            if missing_count:
                provider_issues.append("quote_data_missing")
        if self.provider.name == "asharehub":
            remaining_soft = int(
                provider_status.get("quota", {}).get("remaining_soft") or 0
            )
            if 0 < remaining_soft <= 10:
                provider_issues.append("quota_low")
            if provider_status.get("quota", {}).get("exhausted"):
                provider_issues.append("quota_exhausted")
            if provider_status.get("quota", {}).get("upstream_exhausted"):
                provider_issues.append("upstream_quota_exhausted")
        provider_issues = list(dict.fromkeys(provider_issues))
        degraded = bool(provider_issues)
        database_state = "connected" if self.repository.initialized else "disconnected"
        history_required = (
            self.provider.name == "asharehub"
            and bool(self.monitor.cohort_strategies or self.monitor.strategies)
        )
        research_ready = (
            not history_required
            or self._history_bootstrap_ready
            >= min(
                self._history_bootstrap_total,
                self.settings.bank_strategy_min_universe,
            )
        )
        if not history_required:
            research_ready_reason = "not_required"
        elif research_ready:
            research_ready_reason = "minimum_history_coverage_met"
        elif self._history_bootstrap_deferred:
            research_ready_reason = "history_bootstrap_deferred_for_budget"
        elif self._history_bootstrap_failed:
            research_ready_reason = "history_bootstrap_partial_failure"
        elif self._history_bootstrap_state in {"running", "pending", "scheduled"}:
            research_ready_reason = "history_bootstrap_in_progress"
        else:
            research_ready_reason = "history_bootstrap_not_ready"
        serving_ready = (
            self._startup_completed_at is not None
            and self._startup_phase == "ready"
            and running
            and self.repository.initialized
        )
        if running and degraded:
            provider_status["state"] = "degraded"
        elif error:
            provider_status["state"] = "error"
        lifecycle_status = {
            "failed": "error",
            "stopping": "stopping",
            "stopped": "stopped",
        }.get(self._startup_phase)
        if lifecycle_status is not None:
            provider_status["state"] = lifecycle_status
        elif self._startup_phase == "ready" and not running:
            provider_status["state"] = "error"
        provider_status["issues"] = provider_issues
        return {
            "app": self.settings.app_name,
            "version": "0.1.0",
            "status": lifecycle_status or (
                "degraded" if degraded else ("ok" if running else "starting")
            ),
            "healthy": running and not degraded and lifecycle_status is None,
            "provider": provider_status,
            "data_readiness": data_readiness,
            "calendar_readiness": calendar_readiness,
            "database": {"type": "sqlite", "state": database_state},
            "collector": {
                "running": running,
                "iteration": snapshot.get("iteration", 0),
                "last_run": jsonable(last),
                "last_duration_ms": snapshot.get("last_collection_duration_ms"),
                # Keep the public contract useful for operators without
                # reflecting provider exception text (which may contain a
                # request URL, response body, or credential).
                "last_error": (
                    "collection failed ({})".format(public_error_type)
                    if public_error_type
                    else None
                ),
                "last_error_type": public_error_type,
                "cycle_stats": snapshot.get(
                    "cycle_stats",
                    {
                        "total": 0,
                        "succeeded": 0,
                        "failed": 0,
                        "partial": 0,
                        "cancelled": 0,
                        "last_outcome": None,
                    },
                ),
                "jobs": snapshot.get("jobs", {"active": 0, "recent": []}),
            },
            "startup": {
                "ready": serving_ready,
                "serving_ready": serving_ready,
                "research_ready": research_ready,
                "research_ready_reason": research_ready_reason,
                "phase": self._startup_phase,
                "started_at": jsonable(self._startup_started_at),
                "ready_at": jsonable(self._startup_completed_at),
                "duration_ms": self._startup_duration_ms,
                "error_type": self._startup_error_type,
                "collection_mode": "background",
                "history_bootstrap": {
                    "state": self._history_bootstrap_state,
                    "ready": self._history_bootstrap_ready,
                    "total": self._history_bootstrap_total,
                    "remaining": max(
                        0,
                        self._history_bootstrap_total
                        - self._history_bootstrap_ready,
                    ),
                    "attempted": self._history_bootstrap_attempted,
                    "failed": dict(self._history_bootstrap_failed),
                    "current_symbol": self._history_bootstrap_current_symbol,
                    "started_at": jsonable(self._history_bootstrap_started_at),
                    "last_check_at": jsonable(
                        self._history_bootstrap_last_check_at
                    ),
                    "completed_at": jsonable(self._history_bootstrap_completed_at),
                    "deferred_for_budget": self._history_bootstrap_deferred,
                },
            },
            "latency_ms": round(latency, 1) if latency is not None else None,
            "last_collection_age_ms": round(latency, 1) if latency is not None else None,
            "uptime_seconds": max(0, int((now - self.started_at).total_seconds())) if self.started_at else 0,
            "subscriptions": len(self.symbols),
            "universe": {
                "mode": self.settings.universe_mode,
                "industry": self.settings.universe_industry,
                "source": self.universe_source,
                "symbols": len(self.symbols),
                "quote_batch_size": realtime.get("subscription_limit"),
                "quote_batch_count": realtime.get("batch_count"),
                "quote_batched": bool(realtime.get("batched_quotes", False)),
                "quote_coverage_ratio": round(
                    received_count / len(self.symbols), 6
                )
                if self.symbols
                else None,
            },
            "websocket_clients": self.hub.count,
            "market": market,
            "settings": self.settings.public_dict(),
            "server_time": now.isoformat(),
        }

    def data_notice(self) -> str:
        if self.provider.name == "asharehub":
            if self.settings.universe_mode == "market":
                return (
                    "当前使用 AShareHub 全市场股票目录：沪深北 active stock 按每批最多 200 只分批获取"
                    " HTTP 实时快照，目录与行情覆盖率在系统状态中单独披露。上游没有官方 WebSocket、"
                    "五档盘口或分钟 K 线；本地 WebSocket 仅推送轮询后的增量。全市场模式使用较长报价缓存"
                    "以控制每日额度，停牌/无成交证券可能在覆盖率中缺失。平台只产生研究信号，不执行交易。"
                )
            return (
                "当前使用 AShareHub 覆盖沪深银行板块：批量行情快照与未复权日线。上游没有官方 WebSocket、"
                "五档盘口或分钟 K 线；页面 WebSocket 仅推送本地轮询后的增量。非交易时段可能"
                "显示上一交易日收盘快照。本机按上海自然日执行配置的软预算，套餐上限由"
                "asharehub_official_daily_limit 披露，剩余额度为本地估算。银行股排名只读 SQLite；"
                "多因子结果仅为研究信号。"
            )
        if self.provider.name == "alltick":
            return (
                "当前使用 AllTick：WebSocket 提供逐笔成交与盘口，HTTP 提供历史/批量 K 线和"
                "基础信息；日内开高低与昨收由最新两根日 K 补齐。逐笔 volume/turnover 不是"
                "当日累计量额，供应商未提供的字段保持为空。"
            )
        if self.provider.name == "akshare":
            return (
                "当前使用 AKShare 沪 A 实时快照；时间戳为数据接收时间，"
                "上游接口可能延迟或暂时不可用，不代表交易所级实时行情。"
            )
        return "当前使用离线模拟行情，仅用于开发和界面验证。"
