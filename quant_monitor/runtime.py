"""Application composition root and process-local runtime state."""

from __future__ import annotations

import asyncio
import logging
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
from quant_monitor.domain import EventType, MarketEvent, Quote, now_market_time
from quant_monitor.market import market_status
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
        self.repository = SQLiteRepository(db_path)
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
            self.provider = AShareHubMarketDataProvider(
                api_key=settings.asharehub_api_key,
                symbols=settings.watchlist,
                instrument_names=STOCK_NAMES,
                base_url=settings.asharehub_base_url,
                request_timeout_seconds=settings.asharehub_request_timeout_seconds,
                quote_cache_seconds=settings.asharehub_quote_cache_seconds,
                bars_cache_seconds=settings.asharehub_bars_cache_seconds,
                instruments_cache_seconds=settings.asharehub_instruments_cache_seconds,
                daily_soft_limit=settings.asharehub_daily_soft_limit,
                budget_path=budget_path,
                retries=settings.asharehub_retries,
                retry_backoff_seconds=settings.asharehub_retry_backoff_seconds,
                strict_symbols=settings.asharehub_strict_symbols,
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
        if settings.strategy_profile == "moving_average_cross":
            ordinary_strategies = (
                MovingAverageCrossStrategy(interval=settings.bar_interval),
            )
        else:
            cohort_strategies = (
                BankDailyMultifactorStrategy(
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
        )
        self.hub = WebSocketHub()
        self.subscription: Subscription = None
        self.started_at: datetime = None
        self._instrument_task = None
        self._catalogue_refresh_task: Optional["asyncio.Task[None]"] = None
        self._history_bootstrap_task: Optional["asyncio.Task[None]"] = None
        self._startup_completed_at: Optional[datetime] = None
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
        """Resolve the bank universe without network I/O during ASGI startup."""

        if self.settings.universe_mode != "bank":
            return
        catalogue = [
            item
            for item in self.repository.list_instruments(active_only=False)
            if str(item.metadata.get("source") or "") == self.provider.name
        ]
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
            self.repository.replace_instruments(instruments)
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
            self.repository.upsert_instruments(instruments)
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
            self.repository.replace_instruments(items)
        else:
            self.repository.upsert_instruments(items)
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
            for symbol in self.symbols:
                if freshness_check(
                    symbol,
                    interval=self.settings.bar_interval,
                    minimum_rows=minimum,
                ):
                    ready.add(symbol)
            return ready
        for symbol in self.symbols:
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
        for symbol in self.symbols:
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
            self.symbols,
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
                    symbol for symbol in self.symbols if symbol not in ready_before
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
        self.repository.initialize()
        try:
            # A process can terminate after recording the start of a provider
            # request but before committing its terminal batch.  Such rows
            # must never remain indistinguishable from work still in flight.
            abandoned_batches = self.repository.abandon_running_collection_batches(
                now_market_time()
            )
            if abandoned_batches:
                LOGGER.warning(
                    "marked %s interrupted collection batch(es) as abandoned",
                    abandoned_batches,
                )
            if self.provider.name == "asharehub":
                cached_instruments = [
                    item
                    for item in self.repository.list_instruments(active_only=False)
                    if str(item.metadata.get("source") or "") == "asharehub"
                ]
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
            self._configure_research_universe_from_cache()
            seed_quotes = getattr(self.provider, "seed_quotes", None)
            if seed_quotes is not None:
                cached_quotes = self.repository.get_latest_quotes(
                    self.symbols,
                    source_prefix=self.provider.name,
                )
                seed_quotes(cached_quotes)
            seed_bars = getattr(self.provider, "seed_bars", None)
            if seed_bars is not None:
                cached_bars = []
                intervals = tuple(dict.fromkeys((self.settings.bar_interval, "1d")))
                for symbol in self.symbols:
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
                seed_bars(cached_bars)
            # AShareHub history is filled by a separate one-symbol-at-a-time
            # background job.  The normal monitor starts quote-only so the two
            # jobs cannot duplicate daily requests during application startup.
            if self.provider.name == "asharehub":
                self._history_bootstrap_total = len(self.symbols)
                self._history_bootstrap_ready = len(self._history_ready_symbols())
                self._history_bootstrap_state = "scheduled"
                self.monitor.collector.collect_bars_enabled = False
            start_realtime = getattr(self.provider, "start_realtime", None)
            if start_realtime is not None:
                await start_realtime()
            if self.provider.name not in {"alltick", "asharehub"}:
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
            LOGGER.info(
                "application runtime ready; initial %s collection continues in background",
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
            if self.provider.name == "alltick":
                # Static info is optional enrichment and the free plan is
                # globally rate-limited. Do not block FastAPI startup or the
                # core tick subscription while permission isolation completes.
                self._instrument_task = asyncio.create_task(
                    self._refresh_instruments(),
                    name="alltick-static-instruments",
                )
        except BaseException:
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
            await self.provider.close()
            self.repository.close()
            raise

    async def stop(self) -> None:
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

    async def _on_event(self, event: MarketEvent) -> None:
        payload = event.payload
        if isinstance(payload, Quote):
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
        provider_issues = []
        provider_status: Dict[str, Any] = {
            "name": self.provider.name,
            "state": "running" if running else "starting",
        }
        realtime_status = getattr(self.provider, "realtime_status", None)
        if realtime_status is not None:
            realtime = dict(realtime_status)
            provider_status["realtime"] = realtime
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
                    "limit": 100,
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
        serving_ready = self._startup_completed_at is not None
        if running and degraded:
            provider_status["state"] = "degraded"
        elif error:
            provider_status["state"] = "error"
        provider_status["issues"] = provider_issues
        return {
            "app": self.settings.app_name,
            "version": "0.1.0",
            "status": "degraded" if degraded else ("ok" if running else "starting"),
            "healthy": running and not degraded,
            "provider": provider_status,
            "database": {"type": "sqlite", "state": "connected"},
            "collector": {
                "running": running,
                "iteration": snapshot.get("iteration", 0),
                "last_run": jsonable(last),
                "last_duration_ms": snapshot.get("last_collection_duration_ms"),
                "last_error": error,
            },
            "startup": {
                "ready": serving_ready,
                "serving_ready": serving_ready,
                "research_ready": research_ready,
                "ready_at": jsonable(self._startup_completed_at),
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
            },
            "websocket_clients": self.hub.count,
            "market": market_status(now),
            "settings": self.settings.public_dict(),
            "server_time": now.isoformat(),
        }

    def data_notice(self) -> str:
        if self.provider.name == "asharehub":
            return (
                "当前使用 AShareHub 覆盖沪深银行板块：批量行情快照与未复权日线。上游没有官方 WebSocket、"
                "五档盘口或分钟 K 线；页面 WebSocket 仅推送本地轮询后的增量。非交易时段可能"
                "显示上一交易日收盘快照。本机按上海自然日执行 90 次软预算，平台官方免费额度为"
                "每日 100 次，剩余额度为本地估算。银行股排名只读 SQLite；多因子结果仅为研究信号。"
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
