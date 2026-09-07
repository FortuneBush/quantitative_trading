"""FastAPI application exposing local REST, WebSocket and dashboard endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
import time
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from quant_monitor import __version__
from quant_monitor.config import Settings, load_settings
from quant_monitor.data_sources import MarketDataProviderError
from quant_monitor.domain import (
    AlertLevel,
    Quote,
    SignalAction,
    normalize_symbol,
    now_market_time,
    parse_market_time,
)
from quant_monitor.market import expected_latest_daily_date, market_status
from quant_monitor.runtime import ApplicationRuntime, STOCK_NAMES
from quant_monitor.serialization import alert_dict, bar_dict, jsonable, quote_dict, signal_dict
from quant_monitor.services import DailyBarDataQualityService, QuantitativeAnalysisService
from quant_monitor.strategies import (
    BankDailyMultifactorStrategy,
    CohortStrategyContext,
    StrategyContext,
)
from quant_monitor.universes import fallback_bank_instruments, select_bank_instruments


LOGGER = logging.getLogger(__name__)
SUPPORTED_INTERVALS = {"1m", "5m", "15m", "30m", "60m", "1h", "1d", "1w"}
BANK_SECTOR_CACHE_SECONDS = 5.0
BATCH_QUOTE_SORTS = {
    "symbol",
    "name",
    "last",
    "price",
    "pct_change",
    "change_pct",
    "change",
    "volume",
    "turnover",
    "amount",
    "timestamp",
    "updated_at",
}


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


def _error(status: int, code: str, message: str, request_id: str, details: Any = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details,
                "request_id": request_id,
            }
        },
    )


def _parse_optional_time(value: Optional[str], field: str) -> Optional[datetime]:
    if value is None:
        return None
    try:
        return parse_market_time(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="{} must be timezone-aware ISO-8601: {}".format(field, exc))


def _quote_percentage(quote: Quote) -> Optional[Decimal]:
    """Return a quote's percentage move using its previous close."""

    previous = quote.previous_close
    if previous is None or previous == 0:
        return None
    return (quote.last - previous) / previous * Decimal("100")


def _batch_quote_sort_key(record: Tuple[Quote, str], sort: str) -> Tuple[bool, Any]:
    """Build a deterministic key while keeping missing values at the end."""

    quote, name = record
    normalized = str(sort).strip().lower()
    if normalized in {"name"}:
        value: Any = str(name or quote.symbol).casefold()
    elif normalized in {"last", "price"}:
        value = quote.last
    elif normalized in {"pct_change", "change_pct", "change"}:
        value = _quote_percentage(quote)
    elif normalized == "volume":
        value = quote.volume
    elif normalized in {"turnover", "amount"}:
        value = quote.turnover
    elif normalized in {"timestamp", "updated_at"}:
        value = quote.timestamp
    else:
        value = quote.symbol
    return (value is None, value if value is not None else 0)


def _batch_quote_summary(
    records: Sequence[Tuple[Quote, str]],
    payloads: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compute compact market-breadth figures for the filtered universe.

    The summary intentionally uses only normalized domain values.  It is a
    research aid for the dashboard, not a trading decision or an index
    calculation; missing previous closes are excluded from percentage-based
    figures instead of being treated as zero.
    """

    percentages: List[Decimal] = []
    advancers = decliners = unchanged = 0
    total_volume = 0
    total_turnover = Decimal("0")
    for quote, _ in records:
        percentage = _quote_percentage(quote)
        if percentage is not None:
            percentages.append(percentage)
            if percentage > 0:
                advancers += 1
            elif percentage < 0:
                decliners += 1
            else:
                unchanged += 1
        total_volume += int(quote.volume)
        total_turnover += quote.turnover

    ranked = [
        item
        for item in payloads
        if item.get("pct_change") is not None
    ]
    gainers = sorted(
        (item for item in ranked if float(item["pct_change"]) > 0),
        key=lambda item: float(item["pct_change"]),
        reverse=True,
    )[:5]
    losers = sorted(
        (item for item in ranked if float(item["pct_change"]) < 0),
        key=lambda item: float(item["pct_change"]),
    )[:5]

    def compact(item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "symbol": item.get("symbol"),
            "name": item.get("name"),
            "price": item.get("price"),
            "last": item.get("last"),
            "pct_change": item.get("pct_change"),
            "change_pct": item.get("pct_change"),
        }

    average_pct_change = (
        round(float(sum(percentages) / Decimal(len(percentages))), 6)
        if percentages
        else None
    )
    return {
        "count": len(records),
        "total": len(records),
        "universe": len(records),
        "matched": len(records),
        "priced": len(percentages),
        "advancers": advancers,
        "advancing": advancers,
        "decliners": decliners,
        "declining": decliners,
        "unchanged": unchanged,
        "total_volume": total_volume,
        "total_turnover": str(total_turnover),
        "average_pct_change": average_pct_change,
        "average_change_pct": average_pct_change,
        "top_gainers": [compact(item) for item in gainers],
        "top_losers": [compact(item) for item in losers],
    }


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    configured = settings or load_settings()
    web_dir = Path(__file__).resolve().parent / "web"
    analysis_service = QuantitativeAnalysisService()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # asyncio primitives are created inside the serving loop.  Keeping the
        # cache on the application instance also prevents data leaking between
        # independently configured applications in tests or embedded use.
        app.state.bank_sector_cache_lock = asyncio.Lock()
        app.state.bank_sector_cache_payload = None
        app.state.bank_sector_cache_expires_at = 0.0
        runtime = ApplicationRuntime(configured)
        app.state.runtime = runtime
        try:
            await runtime.start()
            yield
        finally:
            await runtime.stop()

    app = FastAPI(
        title=configured.app_name,
        version=__version__,
        description="Local-first China A-share quantitative market-data monitor",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
    )
    app.state.settings = configured
    app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        import uuid

        request.state.request_id = uuid.uuid4().hex
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            LOGGER.exception(
                "HTTP request failed request_id=%s method=%s path=%s",
                request.state.request_id,
                request.method,
                request.url.path,
            )
            raise
        duration_ms = (time.perf_counter() - started) * 1000.0
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Server-Timing"] = "app;dur={:.1f}".format(duration_ms)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
        LOGGER.info(
            "HTTP request request_id=%s method=%s path=%s status=%s duration_ms=%.1f",
            request.state.request_id,
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return _error(422, "validation_error", "请求参数无效", _request_id(request), exc.errors())

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return _error(exc.status_code, "http_error", str(exc.detail), _request_id(request))

    @app.exception_handler(MarketDataProviderError)
    async def provider_error(request: Request, exc: MarketDataProviderError):
        # An upstream outage is expected to be recoverable and should not look
        # like an internal application bug to API clients.  The collector still
        # records the error and marks `/health` degraded; this handler covers
        # on-demand routes such as `/api/v1/bars/{symbol}`.
        return _error(
            503,
            "provider_unavailable",
            "Market-data provider is temporarily unavailable",
            _request_id(request),
            {"error_type": type(exc).__name__, "message": str(exc)},
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        LOGGER.exception("unhandled request error")
        return _error(500, "internal_error", "本地服务发生内部错误", _request_id(request))

    def runtime() -> ApplicationRuntime:
        return app.state.runtime

    def validate_provider_interval(selected_interval: str) -> None:
        supported = getattr(runtime().provider, "supported_intervals", None)
        if supported is not None and selected_interval not in {
            str(item).strip().lower() for item in supported
        }:
            raise HTTPException(
                status_code=422,
                detail="current provider does not support interval {}".format(
                    selected_interval
                ),
            )

    def current_provider_items(items):
        """Hide rows left by another provider in a previously reused DB."""

        expected = str(runtime().provider.name).strip().lower()
        prefix = expected + ":"
        return [
            item
            for item in items
            if str(getattr(item, "source", "")).strip().lower() == expected
            or str(getattr(item, "source", "")).strip().lower().startswith(prefix)
        ]

    def cached_provider_instruments():
        """Read the active provider's security master without upstream I/O."""

        active_runtime = runtime()
        expected = str(active_runtime.provider.name).strip().lower()
        prefix = expected + ":"
        configured = set(active_runtime.symbols)
        items = []
        for item in active_runtime.repository.list_instruments(active_only=False):
            metadata = getattr(item, "metadata", {}) or {}
            source = str(metadata.get("source") or "").strip().lower()
            if source == expected or source.startswith(prefix):
                items.append(item)
            elif expected == "mock" and not source:
                # The deterministic mock catalogue predates provider lineage
                # metadata. Provider-specific databases keep these rows safe.
                items.append(item)
            elif source == "static_bank_fallback" and item.symbol in configured:
                # Keep the visibly labelled bootstrap universe available while
                # AShareHub's reference catalogue is repaired in the background.
                items.append(item)
        return items

    def quote_payload(item, name: str = None):
        payload = quote_dict(
            item,
            name,
            stale_after_seconds=runtime().quote_stale_after_seconds,
        )
        source = str(getattr(item, "source", ""))
        is_alltick = source.startswith("alltick")
        is_asharehub = source.startswith("asharehub")
        if is_alltick:
            payload["timestamp_semantics"] = "provider_trade_event"
            payload["volume_semantics"] = "latest_trade"
        elif is_asharehub:
            payload["timestamp_semantics"] = "provider_snapshot_time"
            payload["volume_semantics"] = "session_cumulative_shares"
        else:
            payload["timestamp_semantics"] = "provider_or_receive_time"
            payload["volume_semantics"] = "provider_reported"
        day_values = (item.open, item.high, item.low, item.previous_close)
        available_day_values = sum(value is not None for value in day_values)
        if available_day_values == len(day_values):
            day_reference = "complete"
        elif available_day_values:
            day_reference = "partial"
        else:
            day_reference = "unavailable"
        if item.bid is not None and item.ask is not None:
            order_book = "two_sided"
        elif item.bid is not None or item.ask is not None:
            order_book = "one_sided"
        else:
            order_book = "unavailable"
        if is_alltick and payload.get("stale"):
            if available_day_values:
                day_reference = "stale"
            if item.bid is not None or item.ask is not None:
                order_book = "stale"
            # A persisted AllTick Quote can outlive the in-memory depth TTL.
            # Do not return an old book as if it were still executable depth.
            payload["bid"] = None
            payload["ask"] = None
            payload["bid_size"] = 0
            payload["ask_size"] = 0
        payload["field_availability"] = {
            "daily_reference": day_reference,
            "order_book": order_book,
        }
        return payload

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(str(web_dir / "index.html"), media_type="text/html; charset=utf-8")

    @app.get("/health")
    async def health():
        status = runtime().system_status()
        return {
            "status": status["status"],
            "version": __version__,
            "server_time": status["server_time"],
            "database": status["database"],
            "feed": status["provider"],
            "collector": status["collector"],
        }

    @app.get("/live")
    async def liveness():
        """Process liveness; intentionally independent of upstream data."""

        return {
            "live": True,
            "version": __version__,
            "server_time": now_market_time().isoformat(),
        }

    @app.get("/ready")
    async def readiness():
        """Report serving and research readiness as distinct dimensions."""

        status = runtime().system_status()
        startup = status["startup"]
        payload = {
            "ready": bool(startup["serving_ready"]),
            "serving_ready": bool(startup["serving_ready"]),
            "research_ready": bool(startup["research_ready"]),
            "history_bootstrap": startup["history_bootstrap"],
            "database": status["database"],
            "server_time": status["server_time"],
        }
        return JSONResponse(
            status_code=200 if payload["ready"] else 503,
            content=jsonable(payload),
        )

    @app.get("/api/v1/market/status")
    async def get_market_status():
        return market_status()

    @app.get("/api/v1/instruments")
    async def instruments(
        symbols: Optional[str] = None,
        refresh: bool = Query(
            default=False,
            description="Explicitly allow one upstream security-master refresh",
        ),
    ):
        """Return the current provider's known security master records."""

        requested = None
        if symbols:
            try:
                requested = {
                    normalize_symbol(item.strip())
                    for item in symbols.split(",")
                    if item.strip()
                }
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
        items = (
            await runtime().instruments()
            if refresh
            else cached_provider_instruments()
        )
        if items is None:
            raise MarketDataProviderError("provider returned no instrument catalogue")
        if requested is not None:
            items = [item for item in items if item.symbol in requested]
        return {
            "items": [jsonable(item) for item in items],
            "count": len(items),
            "provider": runtime().provider.name,
            "universe_scope": getattr(
                runtime().provider, "universe_scope", "provider_catalogue"
            ),
            "served_from": "provider_refresh" if refresh else "sqlite_cache",
            "refresh_requested": refresh,
        }

    @app.get("/api/v1/overview")
    async def overview():
        repo = runtime().repository
        quotes = current_provider_items(
            repo.get_latest_quotes(
                runtime().symbols, source_prefix=runtime().provider.name
            )
        )
        items = [quote_payload(item, runtime().instrument_name(item.symbol)) for item in quotes[:3]]
        alerts = repo.list_alerts(limit=100)
        signals = repo.list_signals(limit=100)
        return {
            "market_status": market_status(),
            "provider": runtime().system_status()["provider"],
            "counts": {
                "watchlist": len(quotes),
                "configured_watchlist": len(runtime().symbols),
                "alerts": len(alerts),
                "signals": len(signals),
            },
            "indices": items,
            "updated_at": now_market_time().isoformat(),
            "data_notice": runtime().data_notice(),
            "notice": runtime().data_notice(),
        }

    @app.get("/api/v1/watchlist")
    async def watchlist():
        quotes = current_provider_items(
            runtime().repository.get_latest_quotes(
                runtime().symbols, source_prefix=runtime().provider.name
            )
        )
        received_symbols = {item.symbol for item in quotes}
        missing_symbols = [
            symbol for symbol in runtime().symbols if symbol not in received_symbols
        ]
        status = runtime().system_status()
        return {
            "items": [quote_payload(item, runtime().instrument_name(item.symbol)) for item in quotes],
            "count": len(quotes),
            "configured_count": len(runtime().symbols),
            "missing_symbols": missing_symbols,
            "provider": runtime().provider.name,
            "provider_status": status["provider"],
            "collector_error": status["collector"]["last_error"],
            "updated_at": now_market_time().isoformat(),
        }

    def _build_bank_sector_snapshot():
        """Build an all-bank dashboard from SQLite on a worker thread.

        Repository reads, domain-model construction and cross-sectional factor
        analysis are deliberately synchronous here.  The async route below
        always dispatches this complete unit through ``asyncio.to_thread`` so a
        cold dashboard refresh cannot stall health checks or WebSocket traffic.
        """

        active_runtime = runtime()
        repo = active_runtime.repository
        configured_symbols = tuple(active_runtime.symbols)
        cached_catalogue = repo.list_instruments(active_only=False)
        selected = select_bank_instruments(cached_catalogue)
        selected_by_symbol = {item.symbol: item for item in selected}
        # The runtime basket remains authoritative for collection.  Attach a
        # fallback instrument if a provider catalogue row is temporarily absent.
        fallback_by_symbol = {
            item.symbol: item for item in fallback_bank_instruments()
        }
        symbols = configured_symbols
        quotes = current_provider_items(
            repo.get_latest_quotes(
                symbols, source_prefix=active_runtime.provider.name
            )
        )
        quotes_by_symbol = {item.symbol: item for item in quotes}

        contexts: Dict[str, StrategyContext] = {}
        bars_count: Dict[str, int] = {}
        latest_bar_at: Dict[str, Optional[datetime]] = {}
        for symbol in symbols:
            cached_bars = current_provider_items(
                repo.get_bars(
                    symbol,
                    "1d",
                    limit=configured.history_bar_count,
                    source_prefix=active_runtime.provider.name,
                )
            )
            bars_count[symbol] = len(cached_bars)
            latest_bar_at[symbol] = cached_bars[-1].timestamp if cached_bars else None
            quote_item = quotes_by_symbol.get(symbol)
            timestamp = (
                cached_bars[-1].timestamp
                if cached_bars
                else (quote_item.timestamp if quote_item else now_market_time())
            )
            contexts[symbol] = StrategyContext(
                symbol=symbol,
                timestamp=timestamp,
                quote=quote_item,
                bars=tuple(cached_bars),
            )

        bank_strategy = next(
            (
                item
                for item in active_runtime.monitor.cohort_strategies
                if isinstance(item, BankDailyMultifactorStrategy)
            ),
            BankDailyMultifactorStrategy(
                min_history=configured.bank_strategy_min_history,
                min_universe=configured.bank_strategy_min_universe,
                entry_percentile=configured.bank_strategy_entry_percentile,
                exit_percentile=configured.bank_strategy_exit_percentile,
                maximum_annualized_volatility=(
                    configured.bank_strategy_max_volatility
                ),
                maximum_drawdown=configured.bank_strategy_max_drawdown,
                liquidity_floor_percentile=(
                    configured.bank_strategy_min_liquidity_percentile
                ),
                corporate_action_gap=(
                    configured.bank_strategy_corporate_action_gap
                ),
            ),
        )
        cohort_timestamp = max(
            (item.timestamp for item in contexts.values()),
            default=now_market_time(),
        )
        research = bank_strategy.analyze(
            CohortStrategyContext(
                timestamp=cohort_timestamp,
                members=contexts,
                cohort="a_share_banks",
                metadata={
                    "served_from": "sqlite_cache",
                    "expected_bar_date": expected_latest_daily_date().isoformat(),
                    "calendar": "weekday_fallback",
                },
            )
        )
        research_by_symbol = {
            str(item["symbol"]): item for item in research.get("items", ())
        }

        items: List[Dict[str, Any]] = []
        for symbol in symbols:
            instrument = selected_by_symbol.get(symbol) or fallback_by_symbol.get(symbol)
            quote_item = quotes_by_symbol.get(symbol)
            quote_data = (
                quote_payload(quote_item, instrument.name if instrument else None)
                if quote_item is not None
                else None
            )
            model = research_by_symbol.get(symbol)
            analysis = None
            if model is not None:
                factors = dict(model["factors"])
                factor_percentiles = dict(model["factor_percentiles"])
                action = str(model["action"])
                risk_value = max(
                    float(factors["annualized_volatility_60"])
                    / configured.bank_strategy_max_volatility,
                    abs(float(factors["max_drawdown_60"]))
                    / configured.bank_strategy_max_drawdown,
                )
                evidence = [
                    "同业综合排名 {}/{}，分位得分 {:.1f}".format(
                        model["rank"], model["universe_size"], model["score"] * 100
                    ),
                    "价格/120日均线 {:.2%}，20/60日均线差 {:.2%}".format(
                        factors["price_to_sma_120"], factors["sma_20_to_60"]
                    ),
                    "60-5日动量 {:.2%}，120-20日动量 {:.2%}".format(
                        factors["momentum_60_5"], factors["momentum_120_20"]
                    ),
                ]
                risks = [
                    "60日年化波动 {:.2%}，60日最大回撤 {:.2%}".format(
                        factors["annualized_volatility_60"],
                        factors["max_drawdown_60"],
                    ),
                    "当前为未复权日线；银行分红除息可能影响趋势与动量",
                ]
                invalidations = [
                    "连续2个交易日收盘低于60日均线",
                    "周度同业分位低于 {:.0%}".format(
                        configured.bank_strategy_exit_percentile
                    ),
                    "年化波动高于 {:.0%} 或60日回撤达到 -{:.0%}".format(
                        configured.bank_strategy_max_volatility,
                        configured.bank_strategy_max_drawdown,
                    ),
                ]
                analysis = {
                    "symbol": symbol,
                    "name": instrument.name if instrument else symbol,
                    "price": model["price"],
                    "bars_used": bars_count[symbol],
                    "timestamp": model["bar_as_of"],
                    "indicators": {
                        "return_5": None,
                        "return_20": factors["momentum_60_5"] * 100,
                        "annualized_volatility_20": factors[
                            "annualized_volatility_60"
                        ],
                    },
                    "trend": {
                        "score": model["score"],
                        "description": evidence[1],
                    },
                    "momentum": {
                        "score": factor_percentiles["momentum_60_5"],
                        "description": evidence[2],
                    },
                    "risk": {
                        "score": min(1.0, risk_value),
                        "label": (
                            "high" if risk_value >= 1 else "medium" if risk_value >= 0.65 else "low"
                        ),
                        "description": risks[0],
                    },
                    "recommendation": {
                        "action": action,
                        "label": model["label"],
                        "score": model["score"],
                        "reason": model["reason"],
                        "evidence": evidence,
                        "risks": risks,
                        "invalidation_conditions": invalidations,
                    },
                    "data_quality": {
                        "status": "ok",
                        "bars_used": bars_count[symbol],
                        "bars_required": configured.bank_strategy_min_history,
                        "bar_as_of": model["bar_as_of"],
                        "price_basis": "raw_unadjusted",
                    },
                    "factors": factors,
                    "factor_percentiles": factor_percentiles,
                    "gates": model["gates"],
                    "rank": model["rank"],
                    "universe_size": model["universe_size"],
                }
            items.append(
                {
                    "symbol": symbol,
                    "name": instrument.name if instrument else active_runtime.instrument_name(symbol),
                    "exchange": symbol.rsplit(".", 1)[1],
                    "instrument": jsonable(instrument) if instrument else None,
                    "quote": quote_data,
                    "analysis": analysis,
                    "research_score": model["score"] if model else None,
                    "research_action": model["action"] if model else "insufficient_data",
                    "research_label": model["label"] if model else "数据不足",
                    "data_quality": (
                        analysis["data_quality"]
                        if analysis
                        else {
                            "status": "insufficient_data",
                            "bars_used": bars_count[symbol],
                            "bars_required": configured.bank_strategy_min_history,
                            "bar_as_of": (
                                latest_bar_at[symbol].isoformat()
                                if latest_bar_at[symbol]
                                else None
                            ),
                            "reason": research.get("excluded", {}).get(symbol),
                        }
                    ),
                }
            )

        quote_payloads = [item["quote"] for item in items if item["quote"]]
        records = [
            (quotes_by_symbol[item["symbol"]], item["name"])
            for item in items
            if item["symbol"] in quotes_by_symbol
        ]
        summary = _batch_quote_summary(records, quote_payloads)
        percentages = sorted(
            float(item["quote"]["pct_change"])
            for item in items
            if item["quote"] and item["quote"].get("pct_change") is not None
        )
        summary["median_pct_change"] = (
            statistics.median(percentages) if percentages else None
        )

        def ranking(key, reverse=True, require_analysis=False):
            candidates = [
                item
                for item in items
                if (not require_analysis or item["analysis"] is not None)
                and key(item) is not None
            ]
            candidates.sort(
                key=lambda item: (key(item), item["symbol"]), reverse=reverse
            )
            return candidates[:5]

        status = active_runtime.system_status()
        quota = status.get("provider", {}).get("quota", {})
        as_of = max(
            (quote.timestamp for quote in quotes),
            default=max(
                (value for value in latest_bar_at.values() if value is not None),
                default=None,
            ),
        )
        return jsonable(
            {
                "sector": "银行",
                "universe": {
                    "mode": "bank",
                    "total": len(symbols),
                    "classification": active_runtime.universe_source,
                    "industry": configured.universe_industry,
                    "exchanges": ["SH", "SZ"],
                    "as_of": as_of,
                },
                "coverage": {
                    "configured": len(symbols),
                    "quotes_received": len(quotes),
                    "missing_quotes": len(symbols) - len(quotes),
                    "history_ready": sum(
                        count >= configured.bank_strategy_min_history
                        for count in bars_count.values()
                    ),
                    "analysis_ready": len(research_by_symbol),
                    "common_as_of_ready": bool(research.get("ready")),
                    "analysis_reason": research.get("reason"),
                    "minimum_analysis_universe": configured.bank_strategy_min_universe,
                },
                "summary": summary,
                "rankings": {
                    "gainers": ranking(
                        lambda item: item["quote"].get("pct_change") if item["quote"] else None
                    ),
                    "losers": ranking(
                        lambda item: item["quote"].get("pct_change") if item["quote"] else None,
                        reverse=False,
                    ),
                    "turnover": ranking(
                        lambda item: Decimal(item["quote"]["turnover"])
                        if item["quote"]
                        else None
                    ),
                    "quant_score_cached": ranking(
                        lambda item: item["research_score"], require_analysis=True
                    ),
                },
                "items": items,
                "quota": quota,
                "market_status": market_status(),
                "provider": active_runtime.provider.name,
                "served_from": "sqlite_cache",
                "upstream_requests": 0,
                "as_of": as_of,
                "updated_at": now_market_time(),
                "notice": (
                    "银行分类、行情、日线与量化排名均读取本地 SQLite 缓存；"
                    "本接口不调用上游。研究模型使用未复权日线，不构成投资建议或交易指令。"
                ),
            }
        )

    # The indirection is intentionally application-local: it keeps the
    # production builder private while allowing offline tests to prove that
    # concurrent callers are coalesced and executed outside the event loop.
    app.state.bank_sector_snapshot_builder = _build_bank_sector_snapshot

    @app.get("/api/v1/sectors/banks")
    async def bank_sector():
        """Return a short-lived, single-flight cached bank-sector snapshot.

        The payload itself remains sourced only from SQLite and never calls a
        market-data provider.  A five-second cache coalesces simultaneous page
        loads without materially delaying newly persisted local data.
        """

        now = time.monotonic()
        cached = app.state.bank_sector_cache_payload
        if cached is not None and now < app.state.bank_sector_cache_expires_at:
            return cached

        async with app.state.bank_sector_cache_lock:
            now = time.monotonic()
            cached = app.state.bank_sector_cache_payload
            if cached is not None and now < app.state.bank_sector_cache_expires_at:
                return cached
            payload = await asyncio.to_thread(
                app.state.bank_sector_snapshot_builder
            )
            app.state.bank_sector_cache_payload = payload
            app.state.bank_sector_cache_expires_at = (
                time.monotonic() + BANK_SECTOR_CACHE_SECONDS
            )
            return payload

    @app.get("/api/v1/market/quotes")
    @app.get("/api/v1/quotes/batch", include_in_schema=True)
    async def market_quotes_batch(
        limit: int = Query(default=100, ge=1, le=5000),
        offset: int = Query(default=0, ge=0, le=1000000),
        sort: str = Query(default="symbol"),
        order: str = Query(default="asc"),
        search: Optional[str] = Query(default=None),
        include_status: bool = Query(default=True),
        include_source: bool = Query(default=True),
        refresh: bool = Query(
            default=False,
            description="Explicitly allow upstream catalogue/quote requests",
        ),
    ):
        """Return a paginated snapshot of the provider's Shanghai-A scope.

        Ordinary GETs are SQLite-only so pagination, sorting and browser reloads
        cannot spend a metered provider quota. ``refresh=true`` explicitly opts
        into upstream catalogue/quote work. Provider exceptions reach the common
        503 handler; an empty scope/filter is a valid 200.
        """

        selected_sort = str(sort or "symbol").strip().lower()
        if selected_sort not in BATCH_QUOTE_SORTS:
            raise HTTPException(
                status_code=422,
                detail="unsupported sort; choose one of {}".format(
                    ", ".join(sorted(BATCH_QUOTE_SORTS))
                ),
            )
        selected_order = str(order or "asc").strip().lower()
        if selected_order not in {"asc", "desc"}:
            raise HTTPException(status_code=422, detail="order must be asc or desc")

        # Resolve the provider-declared scope. Configured baskets need no
        # catalogue request; full catalogues use SQLite unless explicitly
        # refreshed by the caller.
        provider = runtime().provider
        quote_scope = getattr(provider, "quote_universe_scope", provider.universe_scope)
        name_by_symbol: Dict[str, str] = {}
        symbols: List[str] = []
        if quote_scope == "configured_symbols":
            configured_symbols = tuple(
                getattr(provider, "configured_symbols", ()) or runtime().symbols
            )
            instruments = []
            for raw_symbol in configured_symbols:
                try:
                    normalized = normalize_symbol(raw_symbol)
                except (TypeError, ValueError):
                    continue
                if normalized.endswith(".SH"):
                    symbols.append(normalized)
                    name_by_symbol[normalized] = STOCK_NAMES.get(normalized, normalized)
            symbols = list(dict.fromkeys(symbols))
        else:
            instruments = (
                await runtime().instruments()
                if refresh
                else cached_provider_instruments()
            )
        if instruments is None:
            # ``[]`` is a valid empty universe; ``None`` violates the
            # provider contract and normally indicates an unavailable source.
            raise MarketDataProviderError("provider returned no instrument catalogue")
        for instrument in instruments or []:
            if getattr(instrument, "active", True) is False:
                continue
            security_type = getattr(instrument, "security_type", None)
            if security_type is not None and str(getattr(security_type, "value", security_type)) != "stock":
                continue
            try:
                normalized = normalize_symbol(instrument.symbol)
            except (TypeError, ValueError):
                LOGGER.warning("provider returned invalid instrument symbol: %r", instrument)
                continue
            if not normalized.endswith(".SH"):
                continue
            symbols.append(normalized)
            name = str(getattr(instrument, "name", "") or "").strip()
            name_by_symbol[normalized] = name or STOCK_NAMES.get(normalized, normalized)
        # Keep the request deterministic even if a vendor catalogue contains
        # duplicate rows.
        symbols = list(dict.fromkeys(symbols))

        quotes: List[Quote] = []
        if symbols and refresh:
            # Provider scopes can contain suspended/unpriced rows. Providers
            # with a relaxed hook return the usable subset;
            # the base implementation preserves strict behavior for simpler
            # providers and existing watchlist collection.
            relaxed_fetcher = getattr(provider, "fetch_quotes_relaxed", None)
            if relaxed_fetcher is None:
                relaxed_fetcher = provider.fetch_quotes
            fetched = await relaxed_fetcher(symbols)
            if fetched is None:
                raise MarketDataProviderError("provider returned no batch quote payload")
            # A malformed/over-broad provider response must not leak SZ/BJ rows
            # through a Shanghai-only endpoint.  De-duplicate by symbol and
            # retain the newest timestamp if a provider repeats a row.
            latest_by_symbol: Dict[str, Quote] = {}
            for item in fetched or []:
                if not isinstance(item, Quote):
                    LOGGER.warning("provider returned non-Quote batch item: %r", item)
                    continue
                try:
                    normalized = normalize_symbol(item.symbol)
                except (TypeError, ValueError):
                    continue
                if not normalized.endswith(".SH"):
                    continue
                previous = latest_by_symbol.get(normalized)
                if previous is None or item.timestamp >= previous.timestamp:
                    latest_by_symbol[normalized] = item
            quotes = list(latest_by_symbol.values())
            if quotes:
                runtime().repository.upsert_quotes(quotes)
        elif symbols:
            quotes = current_provider_items(
                runtime().repository.get_latest_quotes(
                    symbols,
                    source_prefix=provider.name,
                )
            )

        snapshot_as_of = max((item.timestamp for item in quotes), default=None)
        records: List[Tuple[Quote, str]] = [
            (item, name_by_symbol.get(item.symbol, STOCK_NAMES.get(item.symbol, item.symbol)))
            for item in quotes
        ]
        search_text = str(search or "").strip().casefold()
        if search_text:
            records = [
                record
                for record in records
                if search_text in record[0].symbol.casefold()
                or search_text in str(record[1]).casefold()
            ]

        if selected_order == "desc":
            # ``reverse=True`` would move missing values to the front because
            # the key deliberately marks them with ``True``.  Keep unknown
            # prices/changes at the end for either direction.
            known = [
                record
                for record in records
                if not _batch_quote_sort_key(record, selected_sort)[0]
            ]
            missing = [
                record
                for record in records
                if _batch_quote_sort_key(record, selected_sort)[0]
            ]
            known.sort(
                key=lambda record: _batch_quote_sort_key(record, selected_sort)[1],
                reverse=True,
            )
            records = known + missing
        else:
            records.sort(key=lambda record: _batch_quote_sort_key(record, selected_sort))
        payloads = [quote_payload(item, name) for item, name in records]
        summary = _batch_quote_summary(records, payloads)
        catalogue_total = len(symbols)
        unpriced_count = max(0, catalogue_total - len(quotes))
        summary["catalogue_total"] = catalogue_total
        summary["unpriced"] = unpriced_count
        total = len(records)
        page = payloads[offset : offset + limit]
        unique_sources = sorted({str(item.source) for item, _ in records if item.source})

        response: Dict[str, Any] = {
            "items": page,
            "total": total,
            "universe_total": catalogue_total,
            "unpriced_count": unpriced_count,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(page) < total,
            "as_of": snapshot_as_of.isoformat() if snapshot_as_of is not None else None,
            "summary": summary,
            "universe_scope": quote_scope,
            "served_from": "provider_refresh" if refresh else "sqlite_cache",
            "refresh_requested": refresh,
        }
        if include_source:
            if len(unique_sources) == 1:
                response["source"] = unique_sources[0]
            elif unique_sources:
                response["source"] = "mixed"
                response["sources"] = unique_sources
            else:
                response["source"] = runtime().provider.name
        if include_status:
            response["market_status"] = market_status()
        return response

    @app.get("/api/v1/quotes")
    async def quotes(symbols: Optional[str] = None):
        requested = runtime().symbols if not symbols else [item.strip() for item in symbols.split(",") if item.strip()]
        try:
            normalized = [normalize_symbol(symbol) for symbol in requested]
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        items = current_provider_items(
            runtime().repository.get_latest_quotes(
                normalized, source_prefix=runtime().provider.name
            )
        )
        return {"items": [quote_payload(item, runtime().instrument_name(item.symbol)) for item in items]}

    @app.get("/api/v1/quotes/{symbol}")
    async def quote(symbol: str):
        try:
            normalized = normalize_symbol(symbol)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        items = current_provider_items(
            runtime().repository.get_latest_quotes(
                [normalized], source_prefix=runtime().provider.name
            )
        )
        if not items:
            raise HTTPException(status_code=404, detail="quote not found")
        return quote_payload(items[0], runtime().instrument_name(normalized))

    @app.get("/api/v1/bars/{symbol}")
    async def bars(
        symbol: str,
        interval: str = Query(default=None),
        period: str = Query(default=None, description="Dashboard-compatible interval alias"),
        start: str = Query(default=None, alias="from"),
        end: str = Query(default=None, alias="to"),
        limit: int = Query(default=240, ge=1, le=2000),
        refresh: bool = Query(
            default=False,
            description="Explicitly allow an upstream request when cache is short",
        ),
    ):
        try:
            normalized = normalize_symbol(symbol)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        selected_interval = str(interval or period or configured.bar_interval).strip().lower()
        if selected_interval not in SUPPORTED_INTERVALS:
            raise HTTPException(status_code=422, detail="unsupported interval")
        validate_provider_interval(selected_interval)
        start_time = _parse_optional_time(start, "from")
        end_time = _parse_optional_time(end, "to")
        if start_time and end_time and start_time > end_time:
            raise HTTPException(status_code=422, detail="from cannot be later than to")

        repo = runtime().repository
        items = current_provider_items(
            repo.get_bars(
                normalized,
                selected_interval,
                start_time,
                end_time,
                limit,
                source_prefix=runtime().provider.name,
            )
        )
        if len(items) < limit and (
            runtime().provider.name == "mock" or refresh
        ):
            # Real providers are cache-first by default: an ordinary chart GET
            # must not spend scarce quota.  Callers must opt into refresh=true.
            generated = await runtime().provider.fetch_bars(
                normalized,
                interval=selected_interval,
                start=start_time,
                end=end_time,
                limit=limit,
            )
            repo.insert_bars(generated)
            items = current_provider_items(
                repo.get_bars(
                    normalized,
                    selected_interval,
                    start_time,
                    end_time,
                    limit,
                    source_prefix=runtime().provider.name,
                )
            )
        return {
            "symbol": normalized,
            "interval": selected_interval,
            "items": [bar_dict(item) for item in items],
            "count": len(items),
            "served_from": "sqlite_cache",
            "refresh_requested": refresh,
        }

    async def build_analysis(symbol: str, selected_interval: str, limit: int) -> Dict[str, Any]:
        """Read cached bars and lazily fill the requested analysis window."""

        repo = runtime().repository
        items = current_provider_items(
            repo.get_bars(
                symbol,
                selected_interval,
                limit=limit,
                source_prefix=runtime().provider.name,
            )
        )
        # Once the longest analysis lookback is ready, avoid an unnecessary
        # upstream request merely because the caller asked for a larger chart
        # window.  This keeps a cached, complete analysis available during a
        # temporary provider outage while still filling genuinely short data.
        if (
            runtime().provider.name == "mock"
            and len(items) < limit
            and len(items) < analysis_service.required_bars
        ):
            try:
                generated = await runtime().provider.fetch_bars(
                    symbol,
                    interval=selected_interval,
                    limit=limit,
                )
            except MarketDataProviderError:
                # A cached partial history is still useful for a clearly
                # labelled, possibly stale analysis.  If there is no history
                # at all, preserve the provider-unavailable 503 contract.
                if not items:
                    raise
                generated = []
            if generated:
                repo.insert_bars(generated)
                items = current_provider_items(
                    repo.get_bars(
                        symbol,
                        selected_interval,
                        limit=limit,
                        source_prefix=runtime().provider.name,
                    )
                )
        quotes = current_provider_items(
            repo.get_latest_quotes(
                [symbol], source_prefix=runtime().provider.name
            )
        )
        quote_item = quotes[0] if quotes else None
        if quote_item is None and runtime().provider.name == "mock":
            # A caller can request an explicitly named symbol even when it is
            # not in the configured watchlist during deterministic demo mode.
            # Real-provider analysis GETs stay cache-only to protect quotas.
            try:
                quote_fetcher = getattr(runtime().provider, "fetch_quotes_relaxed", None)
                if quote_fetcher is None:
                    quote_fetcher = runtime().provider.fetch_quotes
                fetched_quotes = await quote_fetcher([symbol])
                if fetched_quotes:
                    quote_item = fetched_quotes[0]
            except (MarketDataProviderError, ValueError):
                quote_item = None
        snapshot = analysis_service.analyze(
            symbol,
            items,
            quote_item,
            name=runtime().instrument_name(symbol),
            interval=selected_interval,
            stale_after_seconds=runtime().quote_stale_after_seconds,
        )
        return jsonable(snapshot)

    @app.get("/api/v1/analysis")
    async def analyses(
        symbols: Optional[str] = None,
        interval: str = Query(default=None),
        period: str = Query(default=None, description="Dashboard-compatible interval alias"),
        limit: int = Query(default=240, ge=1, le=2000),
    ):
        requested = runtime().symbols if not symbols else [
            item.strip() for item in symbols.split(",") if item.strip()
        ]
        try:
            normalized_symbols = list(dict.fromkeys(normalize_symbol(item) for item in requested))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        selected_interval = str(interval or period or "1d").strip().lower()
        if selected_interval not in SUPPORTED_INTERVALS:
            raise HTTPException(status_code=422, detail="unsupported interval")
        validate_provider_interval(selected_interval)
        items = [
            await build_analysis(symbol, selected_interval, limit)
            for symbol in normalized_symbols
        ]
        return {
            "items": items,
            "count": len(items),
            "interval": selected_interval,
            "provider": runtime().provider.name,
            "updated_at": now_market_time().isoformat(),
            "notice": "分析结果仅用于研究和展示，不构成投资建议或交易指令",
        }

    @app.get("/api/v1/analysis/{symbol}")
    async def analysis(
        symbol: str,
        interval: str = Query(default=None),
        period: str = Query(default=None, description="Dashboard-compatible interval alias"),
        limit: int = Query(default=240, ge=1, le=2000),
    ):
        try:
            normalized = normalize_symbol(symbol)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        selected_interval = str(interval or period or "1d").strip().lower()
        if selected_interval not in SUPPORTED_INTERVALS:
            raise HTTPException(status_code=422, detail="unsupported interval")
        validate_provider_interval(selected_interval)
        return await build_analysis(normalized, selected_interval, limit)

    @app.get("/api/v1/alerts")
    async def alerts(
        symbol: Optional[str] = None,
        level: Optional[str] = None,
        acknowledged: Optional[bool] = None,
        limit: int = Query(default=50, ge=1, le=500),
    ):
        try:
            normalized = normalize_symbol(symbol) if symbol else None
            alert_level = AlertLevel(level) if level else None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        items = runtime().repository.list_alerts(normalized, alert_level, acknowledged, limit)
        return {"items": [alert_dict(item) for item in items], "count": len(items)}

    @app.patch("/api/v1/alerts/{alert_id}")
    async def acknowledge_alert(alert_id: int, request: Request):
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(status_code=422, detail="JSON body required")
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="JSON object required")
        value = body.get("acknowledged", True)
        if not isinstance(value, bool):
            raise HTTPException(status_code=422, detail="acknowledged must be a JSON boolean")
        if not runtime().repository.acknowledge_alert(alert_id, value):
            raise HTTPException(status_code=404, detail="alert not found")
        return {"id": alert_id, "acknowledged": value}

    @app.get("/api/v1/signals")
    async def signals(
        symbol: Optional[str] = None,
        strategy: Optional[str] = None,
        limit: int = Query(default=50, ge=1, le=500),
    ):
        try:
            normalized = normalize_symbol(symbol) if symbol else None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        items = runtime().repository.list_signals(
            symbol=normalized,
            strategy=strategy,
            limit=limit,
        )
        return {"items": [signal_dict(item) for item in items], "count": len(items)}

    @app.get("/api/v1/strategies")
    async def strategies():
        ordinary = [
            (strategy, "single_symbol")
            for strategy in runtime().monitor.strategies
        ]
        cohort = [
            (strategy, "cross_sectional")
            for strategy in runtime().monitor.cohort_strategies
        ]
        return {
            "items": [
                {
                    "id": strategy.name,
                    "enabled": True,
                    "type": type(strategy).__name__,
                    "mode": "signal_only",
                    "evaluation_scope": evaluation_scope,
                    "price_basis": (
                        getattr(strategy, "PRICE_BASIS", None)
                    ),
                }
                for strategy, evaluation_scope in ordinary + cohort
            ],
            "notice": "策略仅生成研究信号，不会提交订单。",
        }

    @app.get("/api/v1/data-quality/daily")
    async def daily_data_quality(
        required_history_bars: int = Query(default=None, ge=1, le=2000),
    ):
        """Audit cached daily histories without contacting the provider."""

        active_runtime = runtime()
        required = required_history_bars or configured.bank_strategy_min_history

        def build_quality_snapshot():
            cached_bars = []
            for symbol in active_runtime.symbols:
                cached_bars.extend(
                    active_runtime.repository.get_bars(
                        symbol,
                        interval="1d",
                        limit=required,
                        source_prefix=active_runtime.provider.name,
                    )
                )
            return DailyBarDataQualityService(required).audit(
                cached_bars,
                expected_symbols=active_runtime.symbols,
                as_of=now_market_time(),
            ).to_dict()

        payload = await asyncio.to_thread(build_quality_snapshot)
        payload["served_from"] = "sqlite_cache"
        payload["upstream_requests"] = 0
        return payload

    @app.get("/api/v1/system/status")
    async def system_status():
        return runtime().system_status()

    @app.get("/api/v1/config/public")
    async def public_config():
        return configured.public_dict()

    @app.websocket("/ws/market")
    async def market_stream(websocket: WebSocket):
        origin = websocket.headers.get("origin")
        host = websocket.headers.get("host")
        if origin:
            parsed_origin = urlsplit(origin)
            if parsed_origin.scheme not in {"http", "https"} or parsed_origin.netloc != host:
                await websocket.close(code=1008, reason="cross-origin websocket is not allowed")
                return
        hub = runtime().hub
        await hub.connect(websocket)
        try:
            await websocket.send_json(
                {
                    "type": "snapshot",
                    "timestamp": now_market_time().isoformat(),
                    "data": {
                        "overview": {"market_status": market_status()},
                        "watchlist": {
                            "items": [
                                quote_payload(item, runtime().instrument_name(item.symbol))
                                for item in current_provider_items(
                                    runtime().repository.get_latest_quotes(
                                        runtime().symbols,
                                        source_prefix=runtime().provider.name,
                                    )
                                )
                            ]
                        },
                        "alerts": {"items": [alert_dict(item) for item in runtime().repository.list_alerts(limit=20)]},
                        "signals": {"items": [signal_dict(item) for item in runtime().repository.list_signals(limit=20)]},
                        "system": runtime().system_status(),
                    },
                }
            )
            while True:
                try:
                    message = await asyncio.wait_for(websocket.receive_json(), timeout=20.0)
                    kind = str(message.get("type") or message.get("action") or "").lower()
                    if kind in {"ping", "pong"}:
                        await websocket.send_json({"type": "heartbeat", "timestamp": now_market_time().isoformat()})
                    elif kind in {"subscribe", "unsubscribe"}:
                        await websocket.send_json({"type": "subscribed", "symbols": runtime().symbols})
                except asyncio.TimeoutError:
                    await websocket.send_json({"type": "heartbeat", "timestamp": now_market_time().isoformat()})
        except WebSocketDisconnect:
            pass
        finally:
            await hub.disconnect(websocket)

    return app


app = create_app()
