"""FastAPI application exposing local REST, WebSocket and dashboard endpoints."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import sqlite3
import statistics
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, time as datetime_time
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import unquote, urlsplit

from fastapi import Body, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from quant_monitor import __version__
from quant_monitor.backtesting import (
    BacktestConfig,
    BacktestEngine,
    BacktestValidationError,
    CapacityAssumptions,
    audit_backtest_payload_metrics,
    build_attribution_report_from_payload,
    StressScenario,
    audit_capacity_from_payload,
    build_bootstrap_robustness_report_from_payload,
    build_benchmark_report_from_payload,
    build_risk_report_from_payload,
    portfolio_snapshot_from_payload,
    PortfolioConstraintPolicy,
    evaluate_portfolio_constraints,
    run_stress_test_from_payload,
)
from quant_monitor.backtesting.jobs import (
    IdempotencyConflictError,
    ResearchBacktestJobManager,
)
from quant_monitor.backtesting.worker import run_cancellable_worker
from quant_monitor.config import Settings, load_settings
from quant_monitor.data_sources import MarketDataProviderError
from quant_monitor.domain import (
    AlertLevel,
    Adjustment,
    CorporateActionType,
    BarObservation,
    CollectionBatchStatus,
    ObservationQualityStatus,
    Quote,
    SignalAction,
    normalize_symbol,
    now_market_time,
    parse_market_time,
)
from quant_monitor.market import expected_latest_daily_date, market_status
from quant_monitor.market_treemap import build_market_treemap
from quant_monitor.runtime import (
    ApplicationRuntime,
    STOCK_NAMES,
    build_calendar_readiness,
    build_data_readiness,
)
from quant_monitor.research import (
    BANK_METRIC_SPECS,
    audit_observation_lineage,
    strategy_code_identity,
    DatasetDescriptor,
    ManifestValidationError,
    ResearchRunManifest,
    build_governance_report,
    build_bank_fundamental_factors,
    build_decision_readiness,
    verify_replay,
    verify_input_hash,
    strategy_code_version,
)
from quant_monitor.storage.migrations import SCHEMA_VERSION as STORAGE_SCHEMA_VERSION
from quant_monitor.serialization import (
    _safe_lineage_metadata,
    alert_dict,
    bar_dict,
    bar_observation_dict,
    backtest_result_dict,
    collection_batch_dict,
    corporate_action_dict,
    adjustment_factor_dict,
    jsonable,
    quote_dict,
    research_result_dict,
    research_run_dict,
    signal_dict,
    universe_snapshot_dict,
)
from quant_monitor.services import (
    DailyBarDataQualityService,
    MarketDataCollectionService,
    QuantitativeAnalysisService,
    QuoteCollectionResult,
)
from quant_monitor.services.analysis_inputs import (
    ANALYSIS_OBSERVATION_SCAN_LIMIT,
    select_analysis_bars,
)
from quant_monitor.strategies import (
    BankDailyMultifactorStrategy,
    CohortStrategyContext,
    StrategyContext,
    strategy_parameter_schema,
)
from quant_monitor.universes import fallback_bank_instruments, select_bank_instruments


LOGGER = logging.getLogger(__name__)
SUPPORTED_INTERVALS = {"1m", "5m", "15m", "30m", "60m", "1h", "1d", "1w"}
BANK_SECTOR_CACHE_SECONDS = 5.0
MARKET_TREEMAP_CACHE_SECONDS = 5.0
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
INSTRUMENT_SORTS = {
    "symbol",
    "name",
    "exchange",
    "security_type",
    "listed_at",
    "industry",
    "active",
}
LINEAGE_BATCH_MAX_LIMIT = 200
LINEAGE_OBSERVATION_MAX_LIMIT = 500
LINEAGE_MAX_OFFSET = 1_000_000
# Complete result JSON can be much larger than listing metadata. Keep an
# explicit response bound so a client cannot request hundreds of multi-MiB
# immutable artifacts in one HTTP response.
RESEARCH_RESULTS_EXPANDED_MAX_LIMIT = 20
# A provider outage is usually transient (network, upstream maintenance or a
# local rate/budget gate).  ``Retry-After`` is a minimum client backoff hint,
# not a promise that a quota will reset in five seconds.
PROVIDER_RETRY_AFTER_SECONDS = 5


def _filter_instrument_catalogue(
    items: Sequence[Any],
    *,
    search: Optional[str] = None,
    exchange: Optional[str] = None,
    security_type: Optional[str] = None,
    active: Optional[bool] = None,
    sort: str = "symbol",
    order: str = "asc",
) -> List[Any]:
    """Filter and sort cached security-master rows without provider I/O.

    Instrument catalogues are small enough for this read-side projection to
    run in memory, while keeping the repository contract stable for existing
    callers.  Provider metadata is intentionally treated as untrusted text:
    search only inspects a bounded set of display fields and never evaluates
    arbitrary JSON values.
    """

    normalized_exchange = str(exchange or "").strip().upper() or None
    if normalized_exchange is not None and normalized_exchange not in {"SH", "SZ", "BJ"}:
        raise ValueError("exchange must be one of: SH, SZ, BJ")
    normalized_type = str(security_type or "").strip().lower() or None
    allowed_types = {"stock", "etf", "index", "bond", "fund", "other"}
    if normalized_type is not None and normalized_type not in allowed_types:
        raise ValueError("security_type must be one of: {}".format(
            ", ".join(sorted(allowed_types))
        ))
    normalized_sort = str(sort or "symbol").strip().lower()
    if normalized_sort not in INSTRUMENT_SORTS:
        raise ValueError("sort must be one of: {}".format(
            ", ".join(sorted(INSTRUMENT_SORTS))
        ))
    normalized_order = str(order or "asc").strip().lower()
    if normalized_order not in {"asc", "desc"}:
        raise ValueError("order must be asc or desc")
    needle = str(search or "").strip().casefold()

    filtered: List[Any] = []
    for item in items:
        item_exchange = str(getattr(getattr(item, "exchange", None), "value", getattr(item, "exchange", ""))).upper()
        item_type = str(getattr(getattr(item, "security_type", None), "value", getattr(item, "security_type", ""))).lower()
        item_active = bool(getattr(item, "active", True))
        if normalized_exchange and item_exchange != normalized_exchange:
            continue
        if normalized_type and item_type != normalized_type:
            continue
        if active is not None and item_active is not bool(active):
            continue
        if needle:
            metadata = getattr(item, "metadata", {}) or {}
            searchable = [
                getattr(item, "symbol", ""),
                getattr(item, "name", ""),
                item_exchange,
                item_type,
                metadata.get("industry", ""),
                metadata.get("area", ""),
                metadata.get("market", ""),
                metadata.get("cnspell", ""),
                metadata.get("fullname", ""),
                metadata.get("enname", ""),
            ]
            if not any(needle in str(value or "").casefold() for value in searchable):
                continue
        filtered.append(item)

    def sort_key(item: Any):
        metadata = getattr(item, "metadata", {}) or {}
        if normalized_sort == "name":
            return str(getattr(item, "name", "")).casefold()
        if normalized_sort == "exchange":
            return str(getattr(getattr(item, "exchange", None), "value", getattr(item, "exchange", ""))).upper()
        if normalized_sort == "security_type":
            return str(getattr(getattr(item, "security_type", None), "value", getattr(item, "security_type", ""))).lower()
        if normalized_sort == "listed_at":
            listed_at = getattr(item, "listed_at", None)
            return listed_at.isoformat() if listed_at is not None else ""
        if normalized_sort == "industry":
            return str(metadata.get("industry", "")).casefold()
        if normalized_sort == "active":
            return int(bool(getattr(item, "active", True)))
        return str(getattr(item, "symbol", "")).upper()

    filtered.sort(key=sort_key, reverse=normalized_order == "desc")
    return filtered


def _bar_projection_provenance(
    repository: Any,
    bars_by_symbol: Dict[str, Sequence[Any]],
    *,
    interval: str,
) -> Dict[str, Any]:
    """Describe how much of a legacy bar projection has immutable lineage.

    ``bars`` predates the v3 observation ledger and intentionally remains a
    small compatibility projection.  A row can therefore be perfectly valid
    OHLC data while still lacking source response, availability and revision
    evidence.  This helper is deliberately conservative: it never calls a
    provider and never labels a projection authoritative.
    """

    served = sum(len(items) for items in bars_by_symbol.values())
    matched = 0
    content_mismatch = 0
    non_accepted = 0
    unavailable = 0
    scan_total = 0
    scan_count = 0
    scan_truncated = False
    duplicate_periods = 0
    mixed_sources = False
    quality_counts: Dict[str, int] = {}
    for symbol, bars in bars_by_symbol.items():
        projected = list(bars)
        if not projected:
            continue
        period_from = min(item.timestamp for item in projected)
        period_to = max(item.timestamp for item in projected)
        sources = {str(item.source).strip().casefold() for item in projected}
        mixed_sources = mixed_sources or len(sources) > 1
        observations: List[Any]
        total: int
        query = getattr(repository, "query_bar_observations", None)
        if callable(query):
            observations, total = query(
                symbol=symbol,
                interval=interval,
                source=next(iter(sources)) if len(sources) == 1 else None,
                adjustment=Adjustment.RAW,
                period_from=period_from,
                period_to=period_to,
                limit=ANALYSIS_OBSERVATION_SCAN_LIMIT,
            )
        else:
            # Keep the helper usable with small compatibility fakes and older
            # repository implementations, while the real SQLite repository
            # always takes the bounded, exact-window path above.
            observations = list(repository.get_bar_observations(
                symbol, interval, limit=ANALYSIS_OBSERVATION_SCAN_LIMIT
            ))
            total = len(observations)
        scan_total += total
        scan_count += len(observations)
        scan_truncated = scan_truncated or total != len(observations)
        by_key: Dict[Tuple[Any, str], List[Any]] = {}
        for item in observations:
            by_key.setdefault(
                (item.period_end, str(item.source).strip().casefold()), []
            ).append(item)
        seen_periods = set()
        for bar in projected:
            if bar.timestamp in seen_periods:
                duplicate_periods += 1
            seen_periods.add(bar.timestamp)
            candidates = by_key.get(
                (bar.timestamp, str(bar.source).strip().casefold())
            ) or []
            if not candidates:
                continue
            # The compatibility projection does not carry adjustment metadata,
            # so only raw observations with identical OHLCV can support it.
            # Inspect every revision in the bounded snapshot: filtering to an
            # accepted/latest row would hide conflicts and make certification
            # depend on query ordering.
            value_fingerprints = {
                (
                    item.open, item.high, item.low, item.close,
                    item.volume, item.turnover,
                    str(getattr(item.adjustment, "value", item.adjustment)),
                )
                for item in candidates
            }
            same_values = len(value_fingerprints) == 1 and next(iter(value_fingerprints)) == (
                bar.open, bar.high, bar.low, bar.close,
                bar.volume, bar.turnover, "raw",
            )
            if not same_values:
                content_mismatch += 1
                continue
            matched += 1
            statuses = {
                getattr(item.quality_status, "value", str(item.quality_status))
                for item in candidates
            }
            if any(status != "accepted" for status in statuses):
                non_accepted += 1
            for status in statuses:
                quality_counts[status] = quality_counts.get(status, 0) + 1
            # This compatibility helper has no explicit point-in-time cutoff,
            # so it cannot judge whether a record was received after a
            # historical decision.  It can still reject structurally
            # inconsistent evidence (or a missing availability timestamp).
            # The stricter ``select_analysis_bars(..., as_of=...)`` path is
            # used by generic analysis when a knowledge cutoff is available.
            if any(
                getattr(item, "available_at", None) is None
                or item.available_at > max(
                    getattr(item, "received_at", item.period_end),
                    getattr(item, "persisted_at", item.period_end),
                )
                for item in candidates
            ):
                unavailable += 1

    legacy = served - matched
    warnings = []
    if legacy:
        warnings.append("bar_projection_rows_without_immutable_observation")
    if content_mismatch:
        warnings.append("immutable_observation_content_mismatch")
    if non_accepted:
        warnings.append("immutable_observation_quality_not_accepted")
    if unavailable:
        warnings.append("immutable_observation_availability_inconsistent")
    if scan_truncated:
        warnings.append("immutable_observation_scan_truncated")
    if duplicate_periods:
        warnings.append("duplicate_projection_periods")
    if mixed_sources:
        warnings.append("mixed_projection_sources")
    if not served:
        warnings.append("no_bars_available")
    return {
        "projection": "sqlite_bars_v1",
        "authoritative": False,
        "served_item_count": served,
        "immutable_observation_count": matched,
        "legacy_item_count": legacy,
        "content_mismatch_count": content_mismatch,
        "lineage_complete": (
            bool(served)
            and legacy == 0
            and not scan_truncated
            and duplicate_periods == 0
            and not mixed_sources
        ),
        # A lineage row proves where/when a value was received, but does not
        # make a quarantined or calendar-unverified value suitable for model
        # inputs. Keep this separate from lineage completeness for callers
        # that need to distinguish provenance from usability.
        "quality_complete": (
            bool(served)
            and legacy == 0
            and non_accepted == 0
            and unavailable == 0
            and not scan_truncated
            and duplicate_periods == 0
            and not mixed_sources
        ),
        "non_accepted_observation_count": non_accepted,
        "unavailable_observation_count": unavailable,
        "quality_status_counts": quality_counts,
        "observation_scan_total": scan_total,
        "observation_scan_count": scan_count,
        "observation_scan_truncated": scan_truncated,
        "calculation_input": (
            "immutable_observations"
            if (
                bool(served)
                and legacy == 0
                and not scan_truncated
                and duplicate_periods == 0
                and not mixed_sources
            )
            else "legacy_bars_projection"
        ),
        "warnings": warnings,
    }


def _prometheus_label(value: Any) -> str:
    """Escape a value for a Prometheus label without exposing credentials."""

    text = str(value)
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _prometheus_metric(
    name: str,
    value: Any,
    *,
    help_text: str,
    metric_type: str = "gauge",
    labels: Optional[Dict[str, Any]] = None,
) -> str:
    """Render one numeric metric and its metadata in Prometheus format."""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number != number or number in (float("inf"), float("-inf")):
        return ""
    label_text = ""
    if labels:
        label_text = "{" + ",".join(
            '{}="{}"'.format(key, _prometheus_label(val))
            for key, val in sorted(labels.items())
        ) + "}"
    return "# HELP {0} {1}\n# TYPE {0} {2}\n{0}{3} {4:.12g}\n".format(
        name, help_text, metric_type, label_text, number
    )


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


def _error(
    status: int,
    code: str,
    message: str,
    request_id: str,
    details: Any = None,
    headers: Optional[Dict[str, str]] = None,
) -> JSONResponse:
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
        headers=headers,
    )


def _safe_http_detail(detail: Any) -> str:
    """Return a bounded HTTP error message without reflecting secrets.

    Route handlers often turn a validation/storage exception into an
    ``HTTPException(detail=str(exc))``.  Exception text is not a trusted
    public payload: a provider URL, API key, local path, or user-supplied
    value can be embedded in it.  Keep ordinary, actionable static messages
    unchanged, but replace any value rejected by the manifest/lineage
    privacy scanner with a stable generic message.  The handler deliberately
    returns a string so FastAPI never serializes an arbitrary exception object
    or mapping supplied as ``detail``.
    """

    if detail is None:
        return "请求失败"
    try:
        text = str(detail)
    except Exception:
        return "请求失败"
    if not text:
        return "请求失败"
    # Avoid turning an attacker-controlled exception into an unbounded public
    # response while retaining enough context for normal static diagnostics.
    text = text[:1024]
    try:
        safe = _safe_lineage_metadata(text)
    except Exception:
        safe = "[redacted]"
    if safe == "[redacted]" or not isinstance(safe, str):
        return "请求失败（错误信息已脱敏）"
    return safe


def _safe_request_path(path: Any) -> str:
    """Keep route names useful while redacting only unsafe path segments.

    The generic lineage scanner treats any string beginning with ``/`` as a
    filesystem path.  Applying it to an HTTP route therefore turned ordinary
    paths such as ``/live`` into the generic "redacted" message, destroying
    the value of request logs.  Scan each URL path segment independently:
    normal routes remain readable, while credential-like or oversized
    user-controlled segments are replaced with a stable marker.
    """

    try:
        raw = str(path or "/")[:2048]
    except Exception:
        return "/[redacted]"
    segments = []
    for segment in raw.split("/"):
        try:
            decoded = unquote(segment)
        except Exception:
            decoded = "[redacted]"
        if not decoded:
            segments.append("")
            continue
        candidate = decoded[:256]
        try:
            safe = _safe_lineage_metadata(candidate)
        except Exception:
            safe = "[redacted]"
        if safe == "[redacted]" or len(decoded) > 256:
            segments.append("[redacted]")
        else:
            # Prevent control characters from corrupting line-oriented logs.
            segments.append("".join(char if ord(char) >= 0x20 else "?" for char in candidate))
    result = "/".join(segments)
    return result if result.startswith("/") else "/" + result


def _safe_validation_details(errors: Sequence[Any]) -> List[Dict[str, Any]]:
    """Return bounded validation metadata without echoing submitted values.

    FastAPI/Pydantic validation records may contain an ``input`` member (and
    nested context) containing the complete value supplied by the caller.  A
    caller can accidentally put a credential in a query/body field, so
    returning ``exc.errors()`` verbatim would turn a normal 422 into a secret
    reflection endpoint.  Keep only stable schema metadata and bound both the
    number and size of entries for a predictable public error contract.
    """

    safe: List[Dict[str, Any]] = []
    for item in list(errors)[:20]:
        if not isinstance(item, dict):
            continue
        location = item.get("loc", ())
        if isinstance(location, (list, tuple)):
            safe_location = [str(part)[:128] for part in location[:16]]
        else:
            safe_location = [str(location)[:128]]
        error_type = str(item.get("type", "validation_error"))[:128]
        # Pydantic normally emits static messages, but custom validators can
        # embed submitted values in ``msg``.  Reuse the same scanner used by
        # HTTPException details before exposing it in the public envelope.
        message = _safe_http_detail(item.get("msg", "请求参数无效"))[:512]
        safe.append({"type": error_type, "loc": safe_location, "msg": message})
    return safe


def _parse_optional_time(value: Optional[str], field: str) -> Optional[datetime]:
    if value is None:
        return None
    try:
        return parse_market_time(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="{} must be timezone-aware ISO-8601: {}".format(field, exc))


def _parse_optional_date(value: Optional[str], field: str) -> Optional[date]:
    """Parse a strict calendar date used by point-in-time filters."""

    if value is None:
        return None
    text = str(value).strip()
    try:
        parsed = date.fromisoformat(text)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="{} must be YYYY-MM-DD: {}".format(field, exc))
    if parsed.isoformat() != text:
        raise HTTPException(status_code=422, detail="{} must be YYYY-MM-DD".format(field))
    return parsed


def _parse_research_tags(value: Optional[str]) -> Optional[Tuple[str, ...]]:
    """Parse a bounded comma-separated AND filter for manifest labels."""

    if value is None:
        return None
    parts = tuple(dict.fromkeys(item.strip().casefold() for item in str(value).split(",")))
    if not parts or any(not item for item in parts):
        raise HTTPException(status_code=422, detail="tags must contain non-empty labels")
    if any(len(item) > 64 for item in parts) or len(parts) > 32:
        raise HTTPException(status_code=422, detail="tags must contain at most 32 labels of 64 characters")
    return parts


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
        app.state.market_treemap_cache_lock = asyncio.Lock()
        app.state.market_treemap_cache_payload = None
        app.state.market_treemap_cache_expires_at = 0.0
        # Backtests are cache-only but can still outlive an HTTP request
        # timeout on a large universe.  Keep their bounded lifecycle separate
        # from the durable result artifact and stop it before SQLite closes.
        app.state.research_backtest_jobs = ResearchBacktestJobManager()
        runtime = ApplicationRuntime(configured)
        app.state.runtime = runtime
        try:
            await runtime.start()
            yield
        finally:
            await app.state.research_backtest_jobs.stop()
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
        # URL paths are user-controlled (for example, ``run_id``).  Keep
        # request-coordinate logs useful without allowing a credential-like
        # path segment to become a durable log leak.
        safe_request_path = _safe_request_path(request.url.path)
        # Reject oversized, explicitly sized API requests before Starlette
        # allocates/parses their body.  A missing Content-Length is common for
        # chunked and multipart clients; those requests intentionally retain
        # the endpoint's existing parser/validation behaviour.
        content_length = request.headers.get("content-length")
        content_type = request.headers.get("content-type", "").lower()
        if (
            request.url.path.startswith("/api/")
            and content_length is not None
            and not content_type.startswith("multipart/")
        ):
            try:
                declared_bytes = int(content_length)
            except (TypeError, ValueError):
                declared_bytes = None
            if (
                declared_bytes is not None
                and declared_bytes > int(configured.api_max_request_body_bytes)
            ):
                response = _error(
                    413,
                    "request_too_large",
                    "请求体超过服务器允许的大小",
                    request.state.request_id,
                    {"max_bytes": int(configured.api_max_request_body_bytes)},
                )
                duration_ms = (time.perf_counter() - started) * 1000.0
                response.headers["X-Request-ID"] = request.state.request_id
                response.headers["Server-Timing"] = "app;dur={:.1f}".format(duration_ms)
                response.headers["X-Content-Type-Options"] = "nosniff"
                response.headers["X-Frame-Options"] = "DENY"
                response.headers["Referrer-Policy"] = "no-referrer"
                response.headers["Cache-Control"] = "no-store"
                LOGGER.warning(
                    "HTTP request rejected request_id=%s method=%s path=%s reason=request_too_large declared_bytes=%s max_bytes=%s",
                    request.state.request_id,
                    request.method,
                    safe_request_path,
                    declared_bytes,
                    int(configured.api_max_request_body_bytes),
                )
                return response
        try:
            response = await call_next(request)
        except Exception as exc:
            # Do not emit a traceback here: provider/validation exceptions may
            # contain URLs, credentials, request bodies or local paths.  The
            # structured HTTP handler below still exposes only a safe error
            # envelope; logs retain the stable type and request coordinates.
            LOGGER.error(
                "HTTP request failed request_id=%s method=%s path=%s error_type=%s",
                request.state.request_id,
                request.method,
                safe_request_path,
                type(exc).__name__,
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
            safe_request_path,
            response.status_code,
            duration_ms,
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return _error(
            422,
            "validation_error",
            "请求参数无效",
            _request_id(request),
            _safe_validation_details(exc.errors()),
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return _error(
            exc.status_code,
            "http_error",
            _safe_http_detail(exc.detail),
            _request_id(request),
            headers={str(key): str(value) for key, value in (exc.headers or {}).items()},
        )

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
            # Provider exception text may contain a request URL, auth header,
            # or vendor response body.  Keep the public contract stable while
            # exposing only a type that is safe for diagnostics.  Provider
            # and collector layers may log richer details using their own
            # redaction policy, but they never belong in this HTTP payload.
            {"error_type": type(exc).__name__},
            headers={
                "Retry-After": str(PROVIDER_RETRY_AFTER_SECONDS),
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        LOGGER.error(
            "unhandled request error request_id=%s error_type=%s",
            _request_id(request),
            type(exc).__name__,
        )
        return _error(500, "internal_error", "本地服务发生内部错误", _request_id(request))

    def runtime() -> ApplicationRuntime:
        return app.state.runtime

    def current_market_status() -> Dict[str, Any]:
        """Return provider-aware session state without triggering I/O."""

        active_provider = getattr(runtime(), "provider", None)
        reader = getattr(active_provider, "current_market_status", None)
        if callable(reader):
            return dict(reader())
        return market_status()

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
            elif (
                expected == "asharehub"
                and source == "static_bank_fallback"
                and item.symbol in configured
            ):
                # Keep the visibly labelled bootstrap universe available only
                # for AShareHub, whose runtime explicitly owns this fallback.
                # If an operator intentionally reuses a SQLite file while
                # switching to AllTick/AKShare, exposing these rows would make
                # a static AShareHub bootstrap look like the new provider's
                # security master.
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
        # Health probes must stay responsive even if SQLite is briefly blocked
        # by an online backup, antivirus scan or another local process.
        status = await asyncio.to_thread(runtime().system_status)
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
    async def readiness(
        require_data: bool = Query(
            default=False,
            description=(
                "Also require the configured quote universe to be usable. "
                "During trading hours this requires fresh quotes; outside "
                "trading hours a cached last close is accepted."
            ),
        ),
        require_research: bool = Query(
            default=False,
            description="Also require the configured research history to be ready.",
        ),
        require_calendar: bool = Query(
            default=False,
            description=(
                "Also require an enabled, complete AShareHub SSE/SZSE calendar "
                "cache. This check never refreshes the provider."
            ),
        ),
    ):
        """Report serving, data and research readiness as distinct dimensions.

        The default remains a process/readiness probe for backwards
        compatibility.  Operators and Windows health checks can opt into the
        stronger ``require_data`` gate without causing any provider request:
        it is derived from the in-memory freshness snapshot only.
        """

        # Keep readiness polling independent of synchronous SQLite work.
        status = await asyncio.to_thread(runtime().system_status)
        startup = status["startup"]
        provider = status.get("provider") or {}
        freshness = provider.get("quote_freshness") or {}
        realtime = provider.get("realtime") or {}
        market = status.get("market") or {}
        data_readiness = build_data_readiness(
            configured_symbols=int(freshness.get("configured_symbols") or 0),
            received_symbols=int(freshness.get("received_symbols") or 0),
            freshness_state=str(freshness.get("state") or "unknown"),
            market_is_trading=bool(market.get("is_trading")),
        )
        data_ready = data_readiness["ready"]
        data_reason = data_readiness["reason"]
        calendar_readiness = build_calendar_readiness(
            provider_name=str(provider.get("name") or ""),
            calendar_enabled=bool(realtime.get("trade_calendar_enabled", False)),
            calendar_source=realtime.get("trade_calendar_source"),
            calendar_error_type=realtime.get("trade_calendar_error_type"),
            configured_symbols=int(freshness.get("configured_symbols") or 0),
        )
        serving_ready = bool(startup["serving_ready"])
        research_ready = bool(startup["research_ready"])
        ready = serving_ready and (research_ready if require_research else True) and (
            data_ready if require_data else True
        ) and (calendar_readiness["ready"] if require_calendar else True)
        payload = {
            "ready": ready,
            "serving_ready": serving_ready,
            "research_ready": research_ready,
            "data_ready": data_ready,
            "data_reason": data_reason,
            "data_gate_requested": require_data,
            "calendar_ready": calendar_readiness["ready"],
            "calendar_reason": calendar_readiness["reason"],
            "calendar_gate_requested": require_calendar,
            "calendar": calendar_readiness,
            "research_gate_requested": require_research,
            "feed": {
                "provider": provider.get("name"),
                "freshness": freshness,
                "market_is_trading": bool(market.get("is_trading")),
            },
            "phase": startup["phase"],
            "started_at": startup["started_at"],
            "ready_at": startup["ready_at"],
            "duration_ms": startup["duration_ms"],
            "error_type": startup["error_type"],
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
        return current_market_status()

    @app.get("/api/v1/instruments")
    async def instruments(
        symbols: Optional[str] = None,
        search: Optional[str] = Query(
            default=None,
            max_length=100,
            description="按证券代码、名称、行业、拼音或市场名称检索",
        ),
        exchange: Optional[str] = Query(
            default=None,
            description="交易所过滤：SH、SZ 或 BJ",
        ),
        security_type: Optional[str] = Query(
            default=None,
            description="证券类型：stock、etf、index、bond、fund 或 other",
        ),
        active: Optional[bool] = Query(
            default=None,
            description="是否只返回当前可交易/在市证券",
        ),
        active_only: Optional[bool] = Query(
            default=None,
            description="active 的兼容别名",
        ),
        limit: int = Query(default=5000, ge=1, le=5000),
        offset: int = Query(default=0, ge=0),
        sort: str = Query(default="symbol"),
        order: str = Query(default="asc"),
        http_response: Response = None,
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
            else await asyncio.to_thread(cached_provider_instruments)
        )
        if items is None:
            raise MarketDataProviderError("provider returned no instrument catalogue")
        # Keep catalogue coverage independent from the current symbol/search
        # view. This lets clients distinguish one filtered match from the
        # provider's complete security master without another request.
        all_catalogue_items = list(items)
        catalogue_total = len(all_catalogue_items)
        catalogue_active_count = sum(
            1 for item in all_catalogue_items if bool(getattr(item, "active", True))
        )
        if requested is not None:
            items = [item for item in items if item.symbol in requested]
        active_filter = active if active is not None else active_only
        try:
            items = _filter_instrument_catalogue(
                items,
                search=search,
                exchange=exchange,
                security_type=security_type,
                active=active_filter,
                sort=sort,
                order=order,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        total = len(items)
        page = items[offset : offset + limit]
        realtime = getattr(runtime().provider, "realtime_status", {}) or {}
        if not isinstance(realtime, Mapping):
            realtime = {}
        if http_response is not None:
            mark_legacy_refresh_response(http_response, refresh)
        return {
            "items": [jsonable(item) for item in page],
            "count": len(page),
            "total": total,
            "catalogue_total": catalogue_total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(page) < total,
            "provider": runtime().provider.name,
            "universe_scope": getattr(
                runtime().provider, "universe_scope", "provider_catalogue"
            ),
            "served_from": "provider_refresh" if refresh else "sqlite_cache",
            "refresh_requested": refresh,
            "as_of": realtime.get("last_catalogue_success_at"),
            "catalogue": {
                "source": runtime().provider.name,
                "as_of": realtime.get("last_catalogue_success_at"),
                "total": catalogue_total,
                "active": catalogue_active_count,
                "inactive": catalogue_total - catalogue_active_count,
                "served_from": "provider_refresh" if refresh else "sqlite_cache",
                "refresh_requested": refresh,
            },
            "filters": {
                "symbols": sorted(requested) if requested is not None else None,
                "search": search,
                "exchange": str(exchange).upper() if exchange else None,
                "security_type": str(security_type).lower() if security_type else None,
                "active": active_filter,
                "sort": str(sort).lower(),
                "order": str(order).lower(),
            },
        }

    def _parse_lineage_enum(value: Optional[str], enum_type, field: str):
        if value is None:
            return None
        normalized = str(value).strip().lower()
        if not normalized:
            raise HTTPException(status_code=422, detail="{} cannot be empty".format(field))
        try:
            return enum_type(normalized)
        except (TypeError, ValueError):
            allowed = ", ".join(item.value for item in enum_type)
            raise HTTPException(
                status_code=422,
                detail="{} must be one of: {}".format(field, allowed),
            )

    def _validate_lineage_offset(offset: int) -> int:
        if offset < 0 or offset > LINEAGE_MAX_OFFSET:
            raise HTTPException(
                status_code=422,
                detail="offset must be between 0 and {}".format(LINEAGE_MAX_OFFSET),
            )
        return offset

    def _lineage_page(
        items: Sequence[Any],
        total: int,
        limit: int,
        offset: int,
        *,
        item_key: str,
        **extra: Any,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            item_key: [
                collection_batch_dict(item)
                if item_key == "batches"
                else bar_observation_dict(item)
                for item in items
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(items) < total,
            "served_from": "sqlite_cache",
            "lineage_schema_version": 3,
        }
        payload.update(extra)
        return payload

    def _manifest_from_payload(payload: Any) -> ResearchRunManifest:
        """Build a manifest from the public JSON shape without dynamic imports."""

        if not isinstance(payload, dict):
            raise ValueError("manifest body must be a JSON object")
        strategy = payload.get("strategy")
        dataset = payload.get("dataset")
        versions = payload.get("versions")
        if not isinstance(strategy, dict):
            raise ValueError("manifest.strategy must be an object")
        if not isinstance(dataset, dict):
            raise ValueError("manifest.dataset must be an object")
        if not isinstance(versions, dict):
            raise ValueError("manifest.versions must be an object")
        required = (
            ("run_id", payload),
            ("input_hash", payload),
            ("name", strategy),
            ("code_version", strategy),
            ("name", dataset),
            ("schema_version", dataset),
            ("source", dataset),
            ("record_count", dataset),
            ("calendar", versions),
            ("universe", versions),
            ("adjustment", versions),
            ("constraints", versions),
        )
        missing = [name for name, container in required if name not in container]
        if missing:
            raise ValueError("manifest missing required field(s): {}".format(", ".join(sorted(set(missing)))))
        manifest = ResearchRunManifest(
            run_id=payload["run_id"],
            strategy_name=strategy["name"],
            strategy_parameters=strategy.get("parameters", {}),
            strategy_code_version=strategy["code_version"],
            dataset=DatasetDescriptor(
                name=dataset["name"],
                schema_version=dataset["schema_version"],
                source=dataset["source"],
                record_count=dataset["record_count"],
                attributes=dataset.get("attributes", {}),
            ),
            input_hash=payload["input_hash"],
            calendar_version=versions["calendar"],
            universe_version=versions["universe"],
            adjustment_version=versions["adjustment"],
            constraint_version=versions["constraints"],
            configuration=payload.get("configuration", {}),
            assumptions=payload.get("assumptions", {}),
            manifest_schema_version=payload.get("manifest_schema_version", "2"),
        )
        supplied_hash = payload.get("content_hash")
        if supplied_hash is not None and supplied_hash != manifest.content_hash:
            raise ValueError("manifest content_hash does not match its content")
        return manifest

    @app.post("/api/v1/research/runs", status_code=201)
    async def create_research_run(payload: Dict[str, Any] = Body(...)):
        """Register one validated research manifest in the local immutable store."""

        try:
            manifest = _manifest_from_payload(payload)
            # SQLite writes can wait on the collector's WAL transaction.  Keep
            # the ASGI loop responsive while preserving the immutable insert.
            record = await asyncio.to_thread(
                runtime().repository.create_research_run, manifest
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="research run_id already exists")
        except (TypeError, ValueError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return research_run_dict(record)

    @app.get("/api/v1/lineage/batches")
    async def lineage_batches(
        provider: Optional[str] = Query(default=None, max_length=64),
        status: Optional[str] = Query(default=None, max_length=32),
        request_kind: Optional[str] = Query(default=None, max_length=64),
        started_from: Optional[str] = Query(default=None, alias="from"),
        started_to: Optional[str] = Query(default=None, alias="to"),
        limit: int = Query(default=50, ge=1, le=LINEAGE_BATCH_MAX_LIMIT),
        offset: int = Query(default=0, ge=0, le=LINEAGE_MAX_OFFSET),
    ):
        """List immutable collection attempts without touching a provider."""

        started_from_time = _parse_optional_time(started_from, "from")
        started_to_time = _parse_optional_time(started_to, "to")
        if started_from_time and started_to_time and started_from_time > started_to_time:
            raise HTTPException(status_code=422, detail="from cannot be later than to")
        parsed_status = _parse_lineage_enum(status, CollectionBatchStatus, "status")
        try:
            # Collection lineage can grow without bound.  Keep its SQLite
            # query off the ASGI event loop so a locked/large WAL does not
            # starve health checks and WebSocket heartbeats.
            items, total = await asyncio.to_thread(
                runtime().repository.query_collection_batches,
                provider=provider,
                status=parsed_status,
                request_kind=request_kind,
                started_from=started_from_time,
                started_to=started_to_time,
                limit=limit,
                offset=_validate_lineage_offset(offset),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return _lineage_page(
            items,
            total,
            limit,
            offset,
            item_key="batches",
            provider_filter=(str(provider).strip().lower() if provider else None),
            status_filter=(parsed_status.value if parsed_status else None),
            request_kind_filter=(str(request_kind).strip().lower() if request_kind else None),
        )

    @app.get("/api/v1/lineage/batches/{batch_id}/observations")
    async def lineage_batch_observations(
        batch_id: str,
        symbol: Optional[str] = Query(default=None, max_length=16),
        interval: Optional[str] = Query(default=None, max_length=16),
        source: Optional[str] = Query(default=None, max_length=64),
        adjustment: Optional[str] = Query(default=None, max_length=8),
        quality_status: Optional[str] = Query(default=None, max_length=32),
        period_from: Optional[str] = Query(default=None, alias="from"),
        period_to: Optional[str] = Query(default=None, alias="to"),
        limit: int = Query(default=100, ge=1, le=LINEAGE_OBSERVATION_MAX_LIMIT),
        offset: int = Query(default=0, ge=0, le=LINEAGE_MAX_OFFSET),
    ):
        """Return observations attached to one collection batch."""

        normalized_batch_id = str(batch_id).strip()
        if not normalized_batch_id or len(normalized_batch_id) > 128:
            raise HTTPException(status_code=422, detail="invalid batch_id")
        period_from_time = _parse_optional_time(period_from, "from")
        period_to_time = _parse_optional_time(period_to, "to")
        if period_from_time and period_to_time and period_from_time > period_to_time:
            raise HTTPException(status_code=422, detail="from cannot be later than to")
        parsed_adjustment = _parse_lineage_enum(adjustment, Adjustment, "adjustment")
        parsed_quality = _parse_lineage_enum(
            quality_status, ObservationQualityStatus, "quality_status"
        )
        try:
            normalized_symbol = normalize_symbol(symbol) if symbol else None
            items, total = await asyncio.to_thread(
                runtime().repository.query_bar_observations,
                symbol=normalized_symbol,
                interval=interval,
                batch_id=normalized_batch_id,
                source=source,
                adjustment=parsed_adjustment,
                quality_status=parsed_quality,
                period_from=period_from_time,
                period_to=period_to_time,
                limit=limit,
                offset=_validate_lineage_offset(offset),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return _lineage_page(
            items,
            total,
            limit,
            offset,
            item_key="observations",
            batch_id=normalized_batch_id,
        )

    @app.get("/api/v1/lineage/batches/{batch_id}")
    async def lineage_batch_detail(batch_id: str):
        """Return one batch and its immutable observation cardinality."""

        normalized_batch_id = str(batch_id).strip()
        if not normalized_batch_id or len(normalized_batch_id) > 128:
            raise HTTPException(status_code=422, detail="invalid batch_id")
        def load_batch_detail():
            repo = runtime().repository
            # Read the row and its cardinality in one worker turn.  This
            # avoids blocking the loop and prevents a concurrent write from
            # producing a torn detail response.
            batch = repo.get_collection_batch(normalized_batch_id)
            if batch is None:
                return None
            return batch, repo.count_bar_observations(normalized_batch_id)

        batch_detail = await asyncio.to_thread(load_batch_detail)
        if batch_detail is None:
            raise HTTPException(status_code=404, detail="collection batch not found")
        batch, observation_count = batch_detail
        payload = collection_batch_dict(batch)
        payload.update(
            {
                "observation_count": observation_count,
                "lineage_schema_version": 3,
                "observations_url": "/api/v1/lineage/batches/{}/observations".format(
                    normalized_batch_id
                ),
            }
        )
        return payload

    @app.get("/api/v1/lineage/observations")
    async def lineage_observations(
        symbol: Optional[str] = Query(default=None, max_length=16),
        interval: Optional[str] = Query(default=None, max_length=16),
        batch_id: Optional[str] = Query(default=None, max_length=128),
        source: Optional[str] = Query(default=None, max_length=64),
        adjustment: Optional[str] = Query(default=None, max_length=8),
        quality_status: Optional[str] = Query(default=None, max_length=32),
        period_from: Optional[str] = Query(default=None, alias="from"),
        period_to: Optional[str] = Query(default=None, alias="to"),
        limit: int = Query(default=100, ge=1, le=LINEAGE_OBSERVATION_MAX_LIMIT),
        offset: int = Query(default=0, ge=0, le=LINEAGE_MAX_OFFSET),
    ):
        """Query immutable K-line observations from SQLite only.

        ``from`` and ``to`` filter the market period end (not receive time),
        while ``available_at``/``received_at`` in each row expose temporal
        knowledge semantics for backtests and data-quality checks.
        """

        period_from_time = _parse_optional_time(period_from, "from")
        period_to_time = _parse_optional_time(period_to, "to")
        if period_from_time and period_to_time and period_from_time > period_to_time:
            raise HTTPException(status_code=422, detail="from cannot be later than to")
        parsed_adjustment = _parse_lineage_enum(adjustment, Adjustment, "adjustment")
        parsed_quality = _parse_lineage_enum(
            quality_status, ObservationQualityStatus, "quality_status"
        )
        try:
            normalized_symbol = normalize_symbol(symbol) if symbol else None
            normalized_batch_id = str(batch_id).strip() if batch_id is not None else None
            if normalized_batch_id == "":
                raise ValueError("batch_id cannot be empty")
            items, total = await asyncio.to_thread(
                runtime().repository.query_bar_observations,
                symbol=normalized_symbol,
                interval=interval,
                batch_id=normalized_batch_id,
                source=source,
                adjustment=parsed_adjustment,
                quality_status=parsed_quality,
                period_from=period_from_time,
                period_to=period_to_time,
                limit=limit,
                offset=_validate_lineage_offset(offset),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return _lineage_page(
            items,
            total,
            limit,
            offset,
            item_key="observations",
            symbol_filter=normalized_symbol,
            interval_filter=(str(interval).strip().lower() if interval else None),
            batch_id_filter=(str(batch_id).strip() if batch_id else None),
            source_filter=(str(source).strip().lower() if source else None),
            adjustment_filter=(parsed_adjustment.value if parsed_adjustment else None),
            quality_status_filter=(parsed_quality.value if parsed_quality else None),
        )

    @app.get("/api/v1/fundamentals/as-of")
    async def fundamentals_as_of_endpoint(
        as_of: str = Query(
            ...,
            description=(
                "Required timezone-aware decision timestamp.  Only facts whose "
                "available_at is no later than this instant are visible."
            ),
        ),
        symbol: Optional[str] = Query(default=None, max_length=16),
        metric: Optional[str] = Query(default=None, max_length=64),
        period_start: Optional[str] = Query(default=None, max_length=10),
        period_end: Optional[str] = Query(default=None, max_length=10),
        unit: Optional[str] = Query(default=None, max_length=32),
        scope: Optional[str] = Query(default=None, max_length=16),
        source: Optional[str] = Query(default=None, max_length=128),
    ):
        """Resolve persisted fundamentals at a point in time.

        This route is deliberately provider-independent and cache-only.  It
        reads append-only SQLite observations and never refreshes a catalogue,
        calls a market-data provider, or spends an upstream request budget.
        Incomparable dimensions or same-time conflicting revisions fail closed
        with a validation response rather than silently merging facts.
        """

        decision_at = _parse_optional_time(as_of, "as_of")
        # ``as_of`` is required, therefore this is only a defensive guard for
        # static type checkers and future refactors.
        if decision_at is None:
            raise HTTPException(status_code=422, detail="as_of is required")
        parsed_period_start = _parse_optional_date(period_start, "period_start")
        parsed_period_end = _parse_optional_date(period_end, "period_end")
        try:
            normalized_symbol = normalize_symbol(symbol) if symbol else None
            items, candidate_count = await asyncio.to_thread(
                runtime().repository.query_fundamentals_as_of,
                decision_at=decision_at,
                symbol=normalized_symbol,
                metric=metric,
                period_start=parsed_period_start,
                period_end=parsed_period_end,
                unit=unit,
                scope=scope,
                source=source,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        payload_items = [item.to_dict() for item in items]
        missing_values = sum(item.value is None for item in items)
        sources = sorted({item.source for item in items})
        if not items:
            quality_status = "empty"
        elif missing_values:
            quality_status = "missing_value"
        else:
            quality_status = "ok"
        return {
            "items": payload_items,
            "count": len(payload_items),
            "candidate_count": candidate_count,
            "as_of": decision_at.isoformat(),
            "selection": "fundamentals_as_of_v1",
            "source": sources[0] if len(sources) == 1 else ("mixed" if sources else None),
            "sources": sources,
            "served_from": "sqlite_cache",
            "provider_operations": 0,
            "quality": {
                "status": quality_status,
                "missing_value_count": missing_values,
                "selected_count": len(payload_items),
                "candidate_count": candidate_count,
            },
        }

    def _cached_bank_fundamental_report(decision_at, symbols=None):
        observations, candidate_count = runtime().repository.query_fundamentals_as_of(
            decision_at=decision_at,
            symbols=symbols,
            metrics=tuple(spec.name for spec in BANK_METRIC_SPECS),
        )
        payload = build_bank_fundamental_factors(
            observations, decision_at=decision_at, symbols=symbols,
        ).to_dict()
        payload.update({
            "candidate_count": candidate_count,
            "served_from": "sqlite_cache",
            "provider_operations": 0,
            "upstream_requests": 0,
            "score_usage": "parallel_research_only",
            "strategy_integration": False,
            "knowledge_mode": "published_as_of",
            "notice": (
                "基本面仅使用本地已持久化事实，按公开可用时间截断；"
                "不代表本机当时已经收到，也未验证数据新鲜度。"
                "该评分未纳入价格策略，不构成投资建议或交易指令。"
            ),
        })
        return payload

    @app.get("/api/v1/fundamentals/factors/as-of")
    async def bank_fundamental_factors_as_of_endpoint(
        as_of: str = Query(
            ...,
            description=(
                "Required timezone-aware decision timestamp. Factors only use "
                "financial facts available by this instant."
            ),
        ),
        symbols: Optional[str] = Query(
            default=None,
            max_length=4000,
            description="Optional comma-separated bank symbols; missing members remain visible.",
        ),
    ):
        """Build conservative bank factors from persisted fundamentals only.

        This endpoint deliberately does not call AShareHub or any other
        provider.  It exposes an explicit empty/partial state while the
        configured provider has no point-in-time fundamentals feed; missing
        values are never turned into zeros or a directional score.
        """

        decision_at = _parse_optional_time(as_of, "as_of")
        if decision_at is None:
            raise HTTPException(status_code=422, detail="as_of is required")
        requested_symbols: Optional[Tuple[str, ...]] = None
        if symbols is not None:
            raw_symbols = tuple(item.strip() for item in symbols.split(",") if item.strip())
            try:
                requested_symbols = tuple(dict.fromkeys(normalize_symbol(item) for item in raw_symbols))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
            if not requested_symbols:
                raise HTTPException(status_code=422, detail="symbols must contain at least one symbol")

        try:
            payload = await asyncio.to_thread(
                _cached_bank_fundamental_report, decision_at, requested_symbols,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return payload

    @app.get("/api/v1/research/universes/as-of")
    @app.get("/api/v1/universes/as-of", include_in_schema=False)
    async def point_in_time_universes_as_of_endpoint(
        as_of: str = Query(
            ...,
            description=(
                "Required timezone-aware decision timestamp. Only persisted "
                "universe snapshots whose knowledge_cutoff and recorded_at "
                "are no later than this instant are visible."
            ),
        ),
        universe: Optional[str] = Query(default=None, max_length=128),
        session_date: Optional[str] = Query(default=None, max_length=10),
        version: Optional[str] = Query(default=None, max_length=128),
        source: Optional[str] = Query(default=None, max_length=128),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0, le=LINEAGE_MAX_OFFSET),
    ):
        """Return persisted PIT universes without consulting live catalogues.

        A current ``instruments`` catalogue is deliberately not a fallback:
        an empty result means that no auditable historical snapshot has been
        persisted for the requested decision time.
        """

        decision_at = _parse_optional_time(as_of, "as_of")
        if decision_at is None:  # defensive; ``as_of`` is required
            raise HTTPException(status_code=422, detail="as_of is required")
        parsed_session_date = _parse_optional_date(session_date, "session_date")
        try:
            records, total = await asyncio.to_thread(
                runtime().repository.query_universe_snapshot_records_as_of,
                as_of=decision_at,
                universe=universe,
                session_date=parsed_session_date,
                version=version,
                source=source,
                limit=limit,
                offset=_validate_lineage_offset(offset),
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {
            "items": [
                {
                    **universe_snapshot_dict(record["snapshot"]),
                    "snapshot_key": record["snapshot_key"],
                    "source": record["source"],
                    "recorded_at": record["recorded_at"].isoformat(),
                    "metadata": _safe_lineage_metadata(record["metadata"]),
                }
                for record in records
            ],
            "count": len(records),
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(records) < total,
            "as_of": decision_at.isoformat(),
            "selection": "universe_snapshot_knowledge_cutoff_as_of_v1",
            "served_from": "sqlite_cache",
            "provider_operations": 0,
            "filters": {
                "universe": universe,
                "session_date": (
                    parsed_session_date.isoformat() if parsed_session_date else None
                ),
                "version": version,
                "source": source,
            },
        }

    @app.get("/api/v1/corporate-actions")
    async def corporate_actions_endpoint(
        as_of: Optional[str] = Query(
            default=None,
            description="Optional receipt-time cutoff; actions received later are hidden.",
        ),
        symbol: Optional[str] = Query(default=None, max_length=16),
        action_type: Optional[str] = Query(default=None, max_length=32),
        ex_from: Optional[str] = Query(default=None, max_length=10),
        ex_to: Optional[str] = Query(default=None, max_length=10),
        source: Optional[str] = Query(default=None, max_length=128),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0, le=LINEAGE_MAX_OFFSET),
    ):
        """List immutable corporate actions from SQLite only.

        This endpoint intentionally never infers an action from an OHLC gap
        and never contacts a provider.  A revised action must arrive with a
        distinct immutable identity; callers can use ``as_of`` to reproduce
        what the local process knew at a historical decision time.
        """

        decision_at = _parse_optional_time(as_of, "as_of")
        parsed_ex_from = _parse_optional_date(ex_from, "ex_from")
        parsed_ex_to = _parse_optional_date(ex_to, "ex_to")
        parsed_type = _parse_lineage_enum(action_type, CorporateActionType, "action_type")
        try:
            normalized_symbol = normalize_symbol(symbol) if symbol else None
            items, total = await asyncio.to_thread(
                runtime().repository.query_corporate_actions,
                as_of=decision_at,
                symbol=normalized_symbol,
                action_type=parsed_type,
                ex_from=parsed_ex_from,
                ex_to=parsed_ex_to,
                source=source,
                limit=limit,
                offset=_validate_lineage_offset(offset),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {
            "items": [corporate_action_dict(item) for item in items],
            "count": len(items),
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(items) < total,
            "as_of": decision_at.isoformat() if decision_at else None,
            "served_from": "sqlite_cache",
            "provider_operations": 0,
            "filters": {
                "symbol": normalized_symbol,
                "action_type": parsed_type.value if parsed_type else None,
                "ex_from": parsed_ex_from.isoformat() if parsed_ex_from else None,
                "ex_to": parsed_ex_to.isoformat() if parsed_ex_to else None,
                "source": source,
            },
        }

    @app.get("/api/v1/adjustment-factors")
    async def adjustment_factors_endpoint(
        as_of: Optional[str] = Query(
            default=None,
            description="Optional computed-time cutoff; later factors are hidden.",
        ),
        symbol: Optional[str] = Query(default=None, max_length=16),
        adjustment: Optional[str] = Query(default=None, max_length=8),
        session_from: Optional[str] = Query(default=None, max_length=10),
        session_to: Optional[str] = Query(default=None, max_length=10),
        factor_version: Optional[str] = Query(default=None, max_length=128),
        limit: int = Query(default=200, ge=1, le=500),
        offset: int = Query(default=0, ge=0, le=LINEAGE_MAX_OFFSET),
    ):
        """List versioned factors without mutating or adjusting raw bars."""

        decision_at = _parse_optional_time(as_of, "as_of")
        parsed_session_from = _parse_optional_date(session_from, "session_from")
        parsed_session_to = _parse_optional_date(session_to, "session_to")
        parsed_adjustment = _parse_lineage_enum(adjustment, Adjustment, "adjustment")
        try:
            normalized_symbol = normalize_symbol(symbol) if symbol else None
            items, total = await asyncio.to_thread(
                runtime().repository.query_adjustment_factors,
                as_of=decision_at,
                symbol=normalized_symbol,
                adjustment=parsed_adjustment,
                session_from=parsed_session_from,
                session_to=parsed_session_to,
                factor_version=factor_version,
                limit=limit,
                offset=_validate_lineage_offset(offset),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {
            "items": [adjustment_factor_dict(item) for item in items],
            "count": len(items),
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(items) < total,
            "as_of": decision_at.isoformat() if decision_at else None,
            "served_from": "sqlite_cache",
            "provider_operations": 0,
            "filters": {
                "symbol": normalized_symbol,
                "adjustment": parsed_adjustment.value if parsed_adjustment else None,
                "session_from": parsed_session_from.isoformat() if parsed_session_from else None,
                "session_to": parsed_session_to.isoformat() if parsed_session_to else None,
                "factor_version": factor_version,
            },
        }

    @app.get("/api/v1/research/runs")
    async def research_runs(
        strategy_name: Optional[str] = Query(default=None, max_length=128),
        dataset_name: Optional[str] = Query(default=None, max_length=128),
        tags: Optional[str] = Query(default=None, max_length=2048),
        created_from: Optional[str] = Query(default=None, alias="from"),
        created_to: Optional[str] = Query(default=None, alias="to"),
        sort_by: str = Query(default="created_at", alias="sort", max_length=32),
        sort_order: str = Query(default="desc", alias="order", max_length=8),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0, le=LINEAGE_MAX_OFFSET),
    ):
        """List immutable, persisted research manifests from SQLite only."""

        created_from_time = _parse_optional_time(created_from, "from")
        created_to_time = _parse_optional_time(created_to, "to")
        if created_from_time and created_to_time and created_from_time > created_to_time:
            raise HTTPException(status_code=422, detail="from cannot be later than to")
        parsed_tags = _parse_research_tags(tags)
        def load_runs_page():
            # Query the page and result-presence projection under one worker
            # turn.  This avoids two event-loop-blocking calls and prevents a
            # concurrent result insert from producing a torn listing.
            repo = runtime().repository
            items, total = repo.query_research_runs(
                strategy_name=strategy_name,
                dataset_name=dataset_name,
                created_from=created_from_time,
                created_to=created_to_time,
                tags=parsed_tags,
                sort_by=sort_by,
                sort_order=sort_order,
                limit=limit,
                offset=_validate_lineage_offset(offset),
            )
            result_ids = repo.research_result_presence(
                [item.run_id for item in items]
            )
            return items, total, result_ids

        try:
            items, total, result_ids = await asyncio.to_thread(load_runs_page)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        runs = []
        for item in items:
            payload = research_run_dict(item)
            payload["result_available"] = item.run_id in result_ids
            manifest = payload.get("manifest")
            assumptions = manifest.get("assumptions") if isinstance(manifest, dict) else None
            raw_tags = assumptions.get("tags") if isinstance(assumptions, dict) else None
            if isinstance(raw_tags, str):
                raw_tags = raw_tags.split(",")
            payload["tags"] = sorted(
                {tag.strip() for tag in raw_tags if isinstance(tag, str) and tag.strip()}
            ) if isinstance(raw_tags, (list, tuple, set, frozenset)) else []
            runs.append(payload)
        return {
            "runs": runs,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(items) < total,
            "filters": {
                "strategy_name": strategy_name,
                "dataset_name": dataset_name,
                "tags": list(parsed_tags) if parsed_tags is not None else [],
                "sort": sort_by,
                "order": sort_order,
            },
            "served_from": "sqlite_cache",
            "manifest_schema_version": 2,
            "storage_schema_version": STORAGE_SCHEMA_VERSION,
        }

    @app.get("/api/v1/research/runs/compare")
    async def compare_research_runs(
        run_ids: str = Query(
            ...,
            description="Comma-separated persisted research run IDs (2-20)",
            min_length=3,
            max_length=4096,
        )
    ):
        """Compare immutable research artifacts using local SQLite only."""

        identifiers = tuple(dict.fromkeys(item.strip() for item in str(run_ids).split(",")))
        if len(identifiers) < 2 or len(identifiers) > 20 or any(
            not item or len(item) > 128 for item in identifiers
        ):
            raise HTTPException(status_code=422, detail="run_ids must contain 2-20 valid IDs")

        def load_comparison():
            rows = []
            for identifier in identifiers:
                manifest = runtime().repository.get_research_run(identifier)
                if manifest is None:
                    raise KeyError(identifier)
                result = runtime().repository.get_research_result(identifier)
                result_payload = None
                if result is not None:
                    result_payload = json.loads(result.result_json)
                manifest_payload = json.loads(manifest.manifest_json)
                assumptions_payload = manifest_payload.get("assumptions", {})
                raw_tags = assumptions_payload.get("tags") if isinstance(assumptions_payload, dict) else None
                if isinstance(raw_tags, str):
                    raw_tags = raw_tags.split(",")
                tags = sorted(
                    {tag.strip() for tag in raw_tags if isinstance(tag, str) and tag.strip()}
                ) if isinstance(raw_tags, (list, tuple, set, frozenset)) else []
                metrics = result_payload.get("metrics", {}) if isinstance(result_payload, dict) else {}
                diagnostics = result_payload.get("diagnostics", {}) if isinstance(result_payload, dict) else {}
                declared_grade = diagnostics.get("decision_grade") if isinstance(diagnostics, dict) else None
                declared_failures = (
                    [value for value in diagnostics.get("decision_grade_failures", ()) if isinstance(value, str)]
                    if isinstance(diagnostics, dict)
                    and isinstance(diagnostics.get("decision_grade_failures", ()), (list, tuple))
                    else []
                )
                rows.append(
                    {
                        "run_id": identifier,
                        "strategy_name": manifest.strategy_name,
                        "strategy_code_version": manifest.strategy_code_version,
                        "dataset_name": manifest.dataset_name,
                        "dataset_source": manifest.dataset_source,
                        "manifest_hash": manifest.content_hash,
                        "input_hash": manifest.input_hash,
                        "calendar_version": manifest.calendar_version,
                        "universe_version": manifest.universe_version,
                        "adjustment_version": manifest.adjustment_version,
                        "constraint_version": manifest.constraint_version,
                        "created_at": manifest.created_at.isoformat(),
                        "tags": tags,
                        "result_available": result is not None,
                        "result_hash": result.result_hash if result is not None else None,
                        "metrics": {
                            key: metrics.get(key)
                            for key in (
                                "total_return",
                                "annualized_return",
                                "annualized_volatility",
                                "sharpe_ratio",
                                "sortino_ratio",
                                "calmar_ratio",
                                "maximum_drawdown",
                                "turnover",
                                "trade_count",
                                "session_count",
                                "closed_trade_count",
                                "winning_trade_count",
                                "losing_trade_count",
                                "break_even_trade_count",
                                "win_rate",
                                "gross_profit",
                                "gross_loss",
                                "realized_pnl",
                                "profit_factor",
                                "expectancy_per_closed_trade",
                                "trade_statistics_status",
                                "profit_factor_status",
                            )
                            if key in metrics
                        },
                        # Results may be uploaded by clients. A valid hash
                        # preserves the declaration; it does not authenticate
                        # decision-grade evidence. Keep comparison badges as
                        # conservative as the dedicated governance endpoint.
                        "decision_grade": False if result is not None else None,
                        "declared_decision_grade": (
                            declared_grade if type(declared_grade) is bool else None
                        ),
                        "decision_grade_failures": (
                            list(dict.fromkeys(["decision_grade_unattested"] + declared_failures))
                            if result is not None else []
                        ),
                    }
                )
            return rows

        try:
            rows = await asyncio.to_thread(load_comparison)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="research run not found: {}".format(exc.args[0]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail="invalid persisted research result: {}".format(exc))
        return {
            "runs": rows,
            "count": len(rows),
            "comparison_fields": [
                "total_return",
                "annualized_return",
                "annualized_volatility",
                "sharpe_ratio",
                "sortino_ratio",
                "calmar_ratio",
                "maximum_drawdown",
                "turnover",
            ],
            "served_from": "sqlite_cache",
            "upstream_requests": 0,
            "notice": "运行比较仅用于研究，不构成投资建议或交易指令",
        }

    @app.get("/api/v1/research/runs/{run_id}")
    async def research_run_detail(run_id: str):
        """Return one immutable manifest and verify its JSON hash on read."""

        normalized_run_id = str(run_id).strip()
        if not normalized_run_id or len(normalized_run_id) > 128:
            raise HTTPException(status_code=422, detail="invalid run_id")
        try:
            record = await asyncio.to_thread(
                runtime().repository.get_research_run, normalized_run_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if record is None:
            raise HTTPException(status_code=404, detail="research run not found")
        return research_run_dict(record)

    @app.get("/api/v1/research/runs/{run_id}/lineage")
    async def research_run_lineage(
        run_id: str,
        decision_at: Optional[str] = Query(default=None, max_length=64),
    ):
        """Audit persisted observation evidence for a research manifest.

        This is a read-only, fail-closed audit.  It never calls a provider and
        deliberately does not mutate the backtest result's decision grade.
        ``decision_at`` must be supplied to attest point-in-time availability.
        """

        normalized_run_id = str(run_id).strip()
        if not normalized_run_id or len(normalized_run_id) > 128:
            raise HTTPException(status_code=422, detail="invalid run_id")

        def load_lineage() -> Optional[Dict[str, Any]]:
            """Read and audit the bounded SQLite evidence off the event loop.

            Lineage scans may inspect up to 10,000 observations.  Keeping all
            repository I/O and pure validation in a worker prevents a slow
            SQLite read from starving WebSocket heartbeats and other API
            requests on the ASGI loop.
            """

            record = runtime().repository.get_research_run(normalized_run_id)
            if record is None:
                return None
            parsed = json.loads(record.manifest_json)
            dataset = parsed.get("dataset", {})
            if not isinstance(dataset, dict):
                raise ValueError("manifest dataset is invalid")
            decision_timestamp = (
                parse_market_time(decision_at) if decision_at is not None else None
            )
            source = str(dataset.get("source", "")).strip()
            declared_count = dataset.get("record_count")
            attributes = dataset.get("attributes", {})
            if not isinstance(attributes, dict):
                raise ValueError("manifest dataset attributes are invalid")
            declared_batch_id = attributes.get("batch_id") or parsed.get("batch_id")
            if declared_batch_id is not None:
                declared_batch_id = str(declared_batch_id).strip()
                if not declared_batch_id or len(declared_batch_id) > 128:
                    raise ValueError("manifest batch_id is invalid")
            # A source-wide scan is not evidence for one run: another run may
            # have identical symbols and a different revision.  Require the
            # manifest to bind a concrete collection batch; absent binding is
            # intentionally reported as an unverified, empty audit.
            if declared_batch_id:
                scan_limit = min(
                    max(int(declared_count) if type(declared_count) is int else 1, 1),
                    10000,
                )
                observations, total = runtime().repository.query_bar_observations(
                    batch_id=declared_batch_id,
                    source=source or None,
                    interval="1d",
                    limit=scan_limit,
                )
                batch = runtime().repository.get_collection_batch(declared_batch_id)
                batches = [batch] if batch is not None else []
            else:
                observations, total, batches = [], 0, []
            report = audit_observation_lineage(
                observations,
                expected_input_hash=record.input_hash,
                expected_record_count=declared_count
                if type(declared_count) is int
                else None,
                expected_source=source or None,
                decision_at=decision_timestamp,
                batches=batches,
            )
            return {
                "run_id": normalized_run_id,
                "verified": report.verified,
                "checks": dict(report.checks),
                "failures": list(report.failures),
                "observation_count": len(observations),
                "observation_total": total,
                "scan_truncated": total > len(observations),
                "batch_id": declared_batch_id,
                "batch_binding_present": bool(declared_batch_id),
                "decision_at": decision_timestamp.isoformat() if decision_timestamp else None,
                "served_from": "sqlite_lineage",
                "upstream_requests": 0,
                "notice": "血缘审计仅证明本地观察记录满足结构性条件，不会将结果自动提升为交易决策或投资建议",
            }

        try:
            response = await asyncio.to_thread(load_lineage)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if response is None:
            raise HTTPException(status_code=404, detail="research run not found")
        return response

    @app.post("/api/v1/research/runs/{run_id}/result", status_code=201)
    async def create_research_result(run_id: str, payload: Dict[str, Any] = Body(...)):
        """Persist one validated, immutable result artifact for a research run."""

        normalized_run_id = str(run_id).strip()
        if not normalized_run_id or len(normalized_run_id) > 128:
            raise HTTPException(status_code=422, detail="invalid run_id")
        if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
            raise HTTPException(status_code=422, detail="result must be a JSON object")
        result = dict(payload["result"])
        # The result hash is part of the signed projection and is therefore
        # required; clients should obtain it from backtest_result_dict().
        if "result_hash" not in result:
            raise HTTPException(status_code=422, detail="result.result_hash is required")
        result_json = json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        try:
            record = await asyncio.to_thread(
                runtime().repository.create_research_result,
                normalized_run_id,
                result_json,
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="research result already exists")
        except (TypeError, ValueError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return research_result_dict(record)

    @app.get("/api/v1/research/results")
    async def research_results(
        run_id: Optional[str] = Query(default=None, max_length=128),
        # Keep the default safe for the larger complete-result representation;
        # compact metadata listings may still request up to 200 explicitly.
        limit: int = Query(default=20, ge=1, le=200),
        offset: int = Query(default=0, ge=0, le=LINEAGE_MAX_OFFSET),
        include_result: bool = Query(
            default=False,
            description="Include complete result payloads; disabled by default for compact, safer listings",
        ),
    ):
        """List immutable result artifacts from SQLite only.

        Listings return metadata and integrity hashes by default.  Request
        ``include_result=true`` only when the complete artifact is needed;
        detail endpoints always return the full integrity-checked payload.
        """

        if include_result and limit > RESEARCH_RESULTS_EXPANDED_MAX_LIMIT:
            raise HTTPException(
                status_code=422,
                detail=(
                    "limit must be between 1 and {} when include_result=true"
                ).format(RESEARCH_RESULTS_EXPANDED_MAX_LIMIT),
            )

        def load_results_page():
            return runtime().repository.query_research_results(
                run_id=run_id,
                limit=limit,
                offset=_validate_lineage_offset(offset),
            )

        try:
            items, total = await asyncio.to_thread(load_results_page)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {
            "results": [
                research_result_dict(item, include_result=include_result)
                for item in items
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(items) < total,
            "served_from": "sqlite_cache",
            "include_result": include_result,
            "storage_schema_version": STORAGE_SCHEMA_VERSION,
        }

    @app.post("/api/v1/research/runs/{run_id}/backtest", status_code=201)
    async def run_cached_backtest(run_id: str, payload: Dict[str, Any] = Body(default_factory=dict)):
        """Run the bank research strategy using cached daily bars only.

        This endpoint deliberately never calls a provider.  A caller must
        refresh data explicitly through the bars/market endpoints first, then
        bind the resulting offline run to an immutable result artifact.
        """

        normalized_run_id = str(run_id).strip()
        if not normalized_run_id or len(normalized_run_id) > 128:
            raise HTTPException(status_code=422, detail="invalid run_id")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="JSON object required")
        verify_manifest = payload.get("verify_manifest", False)
        if type(verify_manifest) is not bool:
            raise HTTPException(status_code=422, detail="verify_manifest must be a boolean")
        # A cross-sectional backtest must not silently shrink its requested
        # universe when one or more symbols have no cached bars.  Exploratory
        # callers may opt in explicitly, but the default is fail-closed so a
        # partial result cannot be mistaken for a full-universe replay.
        allow_partial_symbols = payload.get("allow_partial_symbols", False)
        if type(allow_partial_symbols) is not bool:
            raise HTTPException(
                status_code=422,
                detail="allow_partial_symbols must be a boolean",
            )
        if verify_manifest and allow_partial_symbols:
            raise HTTPException(
                status_code=422,
                detail="strict manifest verification does not allow partial symbols",
            )
        requested_strategy = str(
            payload.get("strategy", "bank_daily_multifactor_v1")
        ).strip()
        if requested_strategy != "bank_daily_multifactor_v1":
            raise HTTPException(status_code=422, detail="unsupported backtest strategy")
        raw_symbols = payload.get("symbols", runtime().symbols)
        if not isinstance(raw_symbols, (list, tuple)) or not raw_symbols:
            raise HTTPException(status_code=422, detail="symbols must be a non-empty array")
        try:
            symbols = tuple(dict.fromkeys(normalize_symbol(item) for item in raw_symbols))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        raw_limit = payload.get("limit", configured.history_bar_count)
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int) or not 1 <= raw_limit <= 2000:
            raise HTTPException(status_code=422, detail="limit must be an integer between 1 and 2000")
        raw_fundamental_reports = payload.get("fundamental_factor_reports")
        if raw_fundamental_reports is not None and not isinstance(raw_fundamental_reports, dict):
            raise HTTPException(
                status_code=422,
                detail="fundamental_factor_reports must be an object keyed by session date",
            )

        def execute(checkpoint: Callable[[], None]) -> Tuple[str, Dict[str, Any]]:
            checkpoint()
            manifest_record = runtime().repository.get_research_run(normalized_run_id)
            if manifest_record is None:
                raise LookupError("research run not found")
            if manifest_record.strategy_name != requested_strategy:
                raise ValueError(
                    "run manifest strategy does not match requested strategy"
                )
            bars_by_symbol = {}
            for symbol in symbols:
                checkpoint()
                bars_by_symbol[symbol] = runtime().repository.get_bars(
                    symbol,
                    "1d",
                    limit=raw_limit,
                    source_prefix=runtime().provider.name,
                )
            missing_symbols = tuple(
                symbol for symbol in symbols if not bars_by_symbol.get(symbol)
            )
            if (
                missing_symbols
                and len(missing_symbols) < len(symbols)
                and not allow_partial_symbols
            ):
                raise ValueError(
                    "cached bars are missing requested symbols: {}; "
                    "pass allow_partial_symbols=true only for exploratory runs".format(
                        ", ".join(missing_symbols)
                    )
                )
            bars_by_symbol = {symbol: bars for symbol, bars in bars_by_symbol.items() if bars}
            if not bars_by_symbol:
                raise ValueError("no cached 1d bars available for requested symbols")

            # The compatibility ``bars`` table is only a projection.  It does
            # not carry batch/revision/availability metadata, and rows written
            # by older versions (or by ``insert_bars``) have no immutable
            # observation at all.  Surface that fact to research callers so a
            # successful cache-only backtest cannot be mistaken for an
            # independently reproducible data set.
            checkpoint()
            data_provenance = _bar_projection_provenance(
                runtime().repository, bars_by_symbol, interval="1d"
            )
            binding = {
                "verified": False,
                "warnings": list(data_provenance["warnings"]),
                "data_provenance": data_provenance,
                "requested_symbols": list(symbols),
                "served_symbols": sorted(bars_by_symbol),
                "missing_symbols": list(missing_symbols),
                "partial_symbol_input": bool(missing_symbols),
            }
            if missing_symbols:
                binding["warnings"].append("requested_symbols_missing")
            selected_calendar = runtime().current_trading_calendar()
            calendar_metadata = getattr(selected_calendar, "metadata", None)
            binding["calendar"] = {
                "identifier": getattr(calendar_metadata, "identifier", "unknown"),
                "source": getattr(calendar_metadata, "source", "unknown"),
                "version": getattr(calendar_metadata, "version", "unknown"),
                "fingerprint": getattr(selected_calendar, "fingerprint", None),
                "verified": False,
            }
            strict_strategy_parameters = None
            strict_backtest_config = None
            declared_strategy = None
            expected_fundamental_hashes = None
            if verify_manifest:
                # Strict mode binds this offline execution to the immutable
                # manifest.  The default remains backwards compatible for
                # legacy runs whose dataset hash was only descriptive.
                try:
                    # The legacy ``bars`` table is only a projection.  A
                    # strict research run must prove that every served row
                    # has an unambiguous raw observation with identical OHLCV
                    # content; otherwise a direct/old projection write could
                    # pass the manifest hash checks without point-in-time
                    # lineage.  Observation quality is audited separately by
                    # the governance gate, so degraded provider evidence can
                    # remain reproducible without being promoted to
                    # decision-ready.
                    if not data_provenance.get("lineage_complete"):
                        raise ManifestValidationError(
                            "strict manifest binding requires immutable observations"
                        )
                    parsed_manifest = json.loads(manifest_record.manifest_json)
                    declared_strategy = parsed_manifest.get("strategy", {})
                    if not isinstance(declared_strategy, dict):
                        raise ManifestValidationError("manifest strategy is invalid")
                    declared_parameters = declared_strategy.get("parameters", {})
                    if not isinstance(declared_parameters, dict):
                        raise ManifestValidationError("manifest strategy parameters are invalid")
                    strategy_parameter_types = {
                        "min_history": int,
                        "min_universe": int,
                        "entry_percentile": (int, float),
                        "exit_percentile": (int, float),
                        "minimum_breadth": (int, float),
                        "maximum_annualized_volatility": (int, float),
                        "maximum_drawdown": (int, float),
                        "liquidity_floor_percentile": (int, float),
                        "corporate_action_gap": (int, float),
                        "fundamental_weight": (int, float),
                        "trend_exit_days": int,
                        "valid_sessions": int,
                    }
                    unknown_parameters = set(declared_parameters) - set(strategy_parameter_types)
                    if unknown_parameters:
                        raise ManifestValidationError("manifest strategy parameters contain unsupported fields")
                    for key, value in declared_parameters.items():
                        expected_type = strategy_parameter_types[key]
                        if isinstance(value, bool) or not isinstance(value, expected_type):
                            raise ManifestValidationError("manifest strategy parameter has invalid type")
                    strict_strategy_parameters = dict(declared_parameters)
                    declared_code_version = declared_strategy.get("code_version")
                    # Strict cached execution must never produce a result
                    # under a stale/human strategy label.  The manifest may
                    # legitimately be an imported historical artifact, but
                    # replaying it with the current implementation is only
                    # safe when its content-addressed source identity agrees.
                    if declared_code_version != strategy_code_version(requested_strategy):
                        raise ManifestValidationError(
                            "strategy code version does not match current implementation"
                        )
                    dataset = parsed_manifest.get("dataset", {})
                    if not isinstance(dataset, dict):
                        raise ManifestValidationError("manifest dataset is invalid")
                    # A PIT universe declaration is not self-authenticating:
                    # the cached backtest endpoint has no resolver/snapshot
                    # input with which to prove membership at the manifest
                    # timestamp.  Refuse strict execution instead of
                    # treating a descriptive universe label/hash as evidence.
                    dataset_attributes = dataset.get("attributes", {})
                    if isinstance(dataset_attributes, dict) and "universe_snapshot_hashes" in dataset_attributes:
                        raise ManifestValidationError(
                            "strict manifest binding requires point-in-time universe evidence"
                        )
                    declared_fundamental_weight = float(
                        strict_strategy_parameters.get("fundamental_weight", 0.0)
                    )
                    declared_fundamental_hashes = (
                        dataset_attributes.get("fundamental_factor_report_hashes")
                        if isinstance(dataset_attributes, dict)
                        else None
                    )
                    if declared_fundamental_weight > 0.0:
                        if not isinstance(declared_fundamental_hashes, dict):
                            raise ManifestValidationError(
                                "strict manifest with fundamental_weight requires "
                                "dataset.attributes.fundamental_factor_report_hashes"
                            )
                        if raw_fundamental_reports is None:
                            raise ManifestValidationError(
                                "strict manifest with fundamental_weight requires "
                                "fundamental_factor_reports"
                            )
                        expected_fundamental_hashes = {
                            str(session): str(report_hash)
                            for session, report_hash in declared_fundamental_hashes.items()
                        }
                    declared_configuration = parsed_manifest.get("configuration", {})
                    if not isinstance(declared_configuration, dict):
                        raise ManifestValidationError("manifest backtest configuration is invalid")
                    config_fields = {field.name for field in dataclasses.fields(BacktestConfig) if field.init}
                    if not set(declared_configuration).issubset(config_fields):
                        raise ManifestValidationError("manifest backtest configuration contains unsupported fields")
                    config_kwargs = dict(declared_configuration)
                    for key in ("daily_evaluation_time", "daily_execution_time"):
                        value = config_kwargs.get(key)
                        if isinstance(value, str):
                            try:
                                config_kwargs[key] = datetime_time.fromisoformat(value)
                            except ValueError as exc:
                                raise ManifestValidationError("manifest backtest time is invalid") from exc
                    try:
                        strict_backtest_config = BacktestConfig(**config_kwargs)
                    except (TypeError, ValueError) as exc:
                        raise ManifestValidationError("manifest backtest configuration is invalid") from exc
                    missing_symbols = tuple(
                        symbol for symbol in symbols if not bars_by_symbol.get(symbol)
                    )
                    if missing_symbols:
                        raise ManifestValidationError(
                            "cached bars are missing one or more requested symbols"
                        )
                    declared_count = dataset.get("record_count")
                    actual_records = [
                        bar_dict(bar)
                        for symbol in symbols
                        for bar in bars_by_symbol.get(symbol, ())
                    ]
                    if type(declared_count) is not int or declared_count != len(actual_records):
                        raise ManifestValidationError(
                            "cached bars count does not match manifest dataset"
                        )
                    declared_source = str(dataset.get("source", "")).strip().casefold()
                    if not declared_source:
                        raise ManifestValidationError("manifest dataset source is empty")
                    sources = {str(bar.source).strip().casefold() for item in bars_by_symbol.values() for bar in item}
                    if any(source != declared_source and not source.startswith(declared_source + ":") for source in sources):
                        raise ManifestValidationError(
                            "cached bars source does not match manifest dataset source"
                        )
                    verify_input_hash(manifest_record.input_hash, actual_records)
                    assumptions = parsed_manifest.get("assumptions", {})
                    if isinstance(assumptions, dict) and "symbols" in assumptions:
                        declared_symbols = tuple(
                            dict.fromkeys(normalize_symbol(item) for item in assumptions["symbols"])
                        )
                        if declared_symbols != symbols:
                            raise ManifestValidationError(
                                "requested symbols do not match manifest assumptions"
                            )
                    binding["verified"] = True
                except (ManifestValidationError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError("manifest binding verification failed: {}".format(exc))
            else:
                binding["warnings"].append("manifest_input_hash_not_verified")
            if verify_manifest:
                strategy_kwargs = dict(strict_strategy_parameters or {})
                # A legacy manifest that omitted the optional fundamental
                # parameter is explicitly price-only.  Never let the current
                # runtime default silently add a new data dependency during
                # strict replay.
                strategy_kwargs.setdefault("fundamental_weight", 0.0)
                strategy_kwargs["strategy_name"] = requested_strategy
                strategy = BankDailyMultifactorStrategy(**strategy_kwargs)
                execution_config = strict_backtest_config or BacktestConfig()
            else:
                strategy = BankDailyMultifactorStrategy(
                    min_history=configured.bank_strategy_min_history,
                    min_universe=configured.bank_strategy_min_universe,
                    entry_percentile=configured.bank_strategy_entry_percentile,
                    exit_percentile=configured.bank_strategy_exit_percentile,
                    maximum_annualized_volatility=configured.bank_strategy_max_volatility,
                    maximum_drawdown=configured.bank_strategy_max_drawdown,
                    corporate_action_gap=configured.bank_strategy_corporate_action_gap,
                    liquidity_floor_percentile=configured.bank_strategy_min_liquidity_percentile,
                    fundamental_weight=configured.bank_strategy_fundamental_weight,
                )
                execution_config = BacktestConfig()
            # Bind the replay to the same cache-only exchange calendar used
            # during collection and quality auditing.  AShareHub returns a
            # finite VersionedSessionCalendar when its rows are loaded;
            # otherwise this is the explicit weekday fallback and the result
            # remains visibly unverified in diagnostics.
            result = BacktestEngine(strategy, execution_config).run(
                bars_by_symbol,
                trading_calendar=selected_calendar,
                fundamental_factor_reports=raw_fundamental_reports,
                cancellation_check=checkpoint,
            )
            if verify_manifest and expected_fundamental_hashes is not None:
                actual_binding = result.diagnostics.assumptions.get(
                    "fundamental_factor_binding", {}
                )
                actual_hashes = (
                    actual_binding.get("report_hashes")
                    if isinstance(actual_binding, Mapping)
                    else None
                )
                if (
                    not isinstance(actual_binding, Mapping)
                    or actual_binding.get("status") != "bound_unattested"
                    or actual_binding.get("complete") is not True
                    or actual_hashes != expected_fundamental_hashes
                ):
                    raise ValueError(
                        "manifest binding verification failed: fundamental factor reports "
                        "do not match the immutable manifest"
                    )
                binding["fundamental_factor_binding"] = {
                    "verified": True,
                    "report_count": actual_binding.get("report_count"),
                    "session_count": actual_binding.get("session_count"),
                    "report_hashes": dict(actual_hashes),
                }
            checkpoint()
            result_payload = backtest_result_dict(result)
            result_json = json.dumps(
                result_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            )
            return result_json, binding

        def persist(computed: Tuple[str, Dict[str, Any]]) -> Dict[str, Any]:
            result_json, binding = computed
            record = runtime().repository.create_research_result(
                normalized_run_id, result_json
            )
            response = research_result_dict(record)
            response["manifest_binding"] = binding
            return response

        try:
            return await run_cancellable_worker(execute, persist)
        except LookupError:
            raise HTTPException(status_code=404, detail="research run not found")
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="research result already exists")
        except (BacktestValidationError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.post("/api/v1/research/runs/{run_id}/backtest/jobs", status_code=202)
    async def submit_cached_backtest_job(
        run_id: str,
        request: Request,
        payload: Dict[str, Any] = Body(default_factory=dict),
    ):
        """Queue a cache-only backtest and return before the engine finishes.

        This is an in-process convenience for local deployments.  The
        resulting artifact is durable in SQLite; the job lifecycle itself is
        intentionally ephemeral and is never replayed after a restart.
        """

        normalized_run_id = str(run_id).strip()
        if not normalized_run_id or len(normalized_run_id) > 128:
            raise HTTPException(status_code=422, detail="invalid run_id")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="JSON object required")
        manager = app.state.research_backtest_jobs

        async def execute() -> Dict[str, Any]:
            return await run_cached_backtest(normalized_run_id, payload)

        # Bind an idempotency key to the complete request body.  This keeps a
        # retry safe while rejecting accidental reuse of the same key for a
        # different backtest configuration.
        request_fingerprint = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()

        try:
            job = await manager.submit(
                normalized_run_id,
                execute,
                idempotency_key=request.headers.get("Idempotency-Key"),
                request_fingerprint=request_fingerprint,
            )
        except IdempotencyConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(
                status_code=409,
                detail=str(exc),
                headers={"Retry-After": "1"},
            )
        return JSONResponse(
            status_code=202,
            content=jsonable(
                {
                    **job,
                    "served_from": "in_process_job_manager",
                    "upstream_requests": 0,
                }
            ),
        )

    @app.get("/api/v1/research/runs/{run_id}/backtest/jobs")
    async def list_cached_backtest_jobs(
        run_id: str,
        limit: int = Query(default=20, ge=1, le=100),
    ):
        """List in-process backtest jobs for one run without provider I/O."""

        normalized_run_id = str(run_id).strip()
        if not normalized_run_id or len(normalized_run_id) > 128:
            raise HTTPException(status_code=422, detail="invalid run_id")
        payload = await app.state.research_backtest_jobs.list(
            limit, run_id=normalized_run_id
        )
        payload["run_id"] = normalized_run_id
        payload["served_from"] = "in_process_job_manager"
        payload["upstream_requests"] = 0
        return jsonable(payload)

    @app.get("/api/v1/research/runs/{run_id}/backtest/jobs/{job_id}")
    async def get_cached_backtest_job(run_id: str, job_id: str):
        """Return one asynchronous backtest job snapshot."""

        normalized_run_id = str(run_id).strip()
        if not normalized_run_id or len(normalized_run_id) > 128:
            raise HTTPException(status_code=422, detail="invalid run_id")
        job = await app.state.research_backtest_jobs.get(job_id)
        if job is None or job.get("run_id") != normalized_run_id:
            raise HTTPException(status_code=404, detail="research backtest job not found")
        return jsonable(
            {**job, "served_from": "in_process_job_manager", "upstream_requests": 0}
        )

    @app.post("/api/v1/research/runs/{run_id}/backtest/jobs/{job_id}/cancel")
    async def cancel_cached_backtest_job(run_id: str, job_id: str):
        """Cancel a queued/running in-process backtest when possible."""

        normalized_run_id = str(run_id).strip()
        if not normalized_run_id or len(normalized_run_id) > 128:
            raise HTTPException(status_code=422, detail="invalid run_id")
        job = await app.state.research_backtest_jobs.get(job_id)
        if job is None or job.get("run_id") != normalized_run_id:
            raise HTTPException(status_code=404, detail="research backtest job not found")
        cancelled = await app.state.research_backtest_jobs.cancel(job_id)
        return jsonable(
            {**(cancelled or job), "served_from": "in_process_job_manager", "upstream_requests": 0}
        )

    @app.get("/api/v1/research/runs/{run_id}/result")
    async def research_result_detail(run_id: str):
        """Return one immutable result artifact without touching a provider."""

        normalized_run_id = str(run_id).strip()
        if not normalized_run_id or len(normalized_run_id) > 128:
            raise HTTPException(status_code=422, detail="invalid run_id")
        try:
            record = await asyncio.to_thread(
                runtime().repository.get_research_result, normalized_run_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if record is None:
            raise HTTPException(status_code=404, detail="research result not found")
        return research_result_dict(record)

    @app.get("/api/v1/research/runs/{run_id}/result/download")
    async def research_result_download(run_id: str):
        """Download the exact immutable result JSON for offline replay.

        The body is the canonical JSON text stored in SQLite, byte-for-byte;
        clients can verify it with ``X-Result-JSON-Hash`` before archiving or
        replaying it.  This route is cache-only and never contacts a provider.
        """

        normalized_run_id = str(run_id).strip()
        if not normalized_run_id or len(normalized_run_id) > 128:
            raise HTTPException(status_code=422, detail="invalid run_id")
        try:
            record = await asyncio.to_thread(
                runtime().repository.get_research_result, normalized_run_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if record is None:
            raise HTTPException(status_code=404, detail="research result not found")
        return Response(
            content=record.result_json,
            media_type="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="research-result.json"',
                "X-Research-Run-ID": record.run_id,
                "X-Result-Hash": record.result_hash,
                "X-Result-JSON-Hash": record.result_json_hash,
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/v1/research/runs/{run_id}/export")
    async def export_research_run(run_id: str):
        """Export one immutable run and its result as a portable JSON bundle.

        The bundle is assembled exclusively from integrity-checked SQLite
        records.  It deliberately contains no live quotes, provider calls,
        credentials or machine-local paths, so it can be saved and verified
        while offline.  A run without a result remains exportable and is
        marked ``result_available=false`` rather than fabricating one.
        """

        normalized_run_id = str(run_id).strip()
        if not normalized_run_id or len(normalized_run_id) > 128:
            raise HTTPException(status_code=422, detail="invalid run_id")

        def load_bundle():
            repo = runtime().repository
            run_record = repo.get_research_run(normalized_run_id)
            if run_record is None:
                return None
            result_record = repo.get_research_result(normalized_run_id)
            return run_record, result_record

        try:
            records = await asyncio.to_thread(load_bundle)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if records is None:
            raise HTTPException(status_code=404, detail="research run not found")
        run_record, result_record = records
        run_payload = research_run_dict(run_record)
        result_payload = (
            research_result_dict(result_record) if result_record is not None else None
        )
        bundle = {
            "bundle_schema_version": 1,
            "run": run_payload,
            "result": result_payload,
            "result_available": result_payload is not None,
            "integrity": {
                "run_content_hash": run_record.content_hash,
                "manifest_json_hash": run_record.manifest_json_hash,
                "input_hash": run_record.input_hash,
                "result_hash": (
                    result_record.result_hash if result_record is not None else None
                ),
                "result_json_hash": (
                    result_record.result_json_hash if result_record is not None else None
                ),
            },
            "served_from": "sqlite_cache",
            "upstream_requests": 0,
            "notice": "研究制品仅供离线复核，不构成投资建议或交易指令",
        }
        # run_id has already been restricted to a bounded identifier.  Keep
        # the filename conservative for browsers and Windows path handling.
        safe_name = "".join(
            char if char.isalnum() or char in {"-", "_", "."} else "_"
            for char in normalized_run_id
        )
        return JSONResponse(
            content=bundle,
            headers={
                "Content-Disposition": 'attachment; filename="research-{}.json"'.format(
                    safe_name
                )
            },
        )

    @app.get("/api/v1/research/runs/{run_id}/portfolio")
    async def research_portfolio_snapshot(
        run_id: str,
        benchmark_name: Optional[str] = Query(default=None, max_length=128),
    ):
        """Return an accounting-checked portfolio snapshot from a saved result."""

        normalized_run_id = str(run_id).strip()
        if not normalized_run_id or len(normalized_run_id) > 128:
            raise HTTPException(status_code=422, detail="invalid run_id")
        try:
            record = await asyncio.to_thread(
                runtime().repository.get_research_result, normalized_run_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if record is None:
            raise HTTPException(status_code=404, detail="research result not found")
        try:
            result_payload = json.loads(record.result_json)
            snapshot = await asyncio.to_thread(
                portfolio_snapshot_from_payload,
                result_payload,
                run_id=normalized_run_id,
                benchmark_name=benchmark_name,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=422,
                detail="invalid persisted research result: {}".format(exc),
            )
        return {
            "run_id": normalized_run_id,
            "result_hash": record.result_hash,
            "result_created_at": record.created_at.isoformat(),
            "portfolio": snapshot.to_dict(),
            "constraints_endpoint": "/api/v1/research/runs/{}/portfolio/constraints".format(
                normalized_run_id
            ),
            "served_from": "sqlite_cache",
            "upstream_requests": 0,
            "notice": "组合快照仅用于研究，不构成投资建议或交易指令",
        }

    @app.get("/api/v1/research/runs/{run_id}/portfolio/constraints")
    async def research_portfolio_constraints(
        run_id: str,
        max_holding_weight: Optional[float] = Query(default=None, ge=0.0, le=1.0),
        max_concentration_hhi: Optional[float] = Query(default=None, ge=0.0, le=1.0),
        min_cash_weight: Optional[float] = Query(default=None, ge=0.0, le=1.0),
        max_gross_exposure: Optional[float] = Query(default=None, ge=0.0),
        max_positions: Optional[int] = Query(default=None, ge=1, le=100000),
        require_complete_valuation: bool = Query(default=True),
        disable_default_limits: bool = Query(default=False),
    ):
        """Evaluate explicit portfolio limits from a cached research result.

        This endpoint is deliberately separate from the portfolio snapshot:
        it makes policy values visible and never turns a partial valuation into
        a pass.  It is read-only and performs no provider requests.
        """

        normalized_run_id = str(run_id).strip()
        if not normalized_run_id or len(normalized_run_id) > 128:
            raise HTTPException(status_code=422, detail="invalid run_id")
        try:
            record = await asyncio.to_thread(
                runtime().repository.get_research_result, normalized_run_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if record is None:
            raise HTTPException(status_code=404, detail="research result not found")
        try:
            result_payload = json.loads(record.result_json)
            snapshot = await asyncio.to_thread(
                portfolio_snapshot_from_payload,
                result_payload,
                run_id=normalized_run_id,
            )
            requested_policy_values = {
                name: value
                for name, value in {
                    "max_holding_weight": max_holding_weight,
                    "max_concentration_hhi": max_concentration_hhi,
                    "min_cash_weight": min_cash_weight,
                    "max_gross_exposure": max_gross_exposure,
                    "max_positions": max_positions,
                    "require_complete_valuation": require_complete_valuation,
                }.items()
            }
            policy_values = (
                requested_policy_values
                if disable_default_limits
                else {name: value for name, value in requested_policy_values.items() if value is not None}
            )
            policy = PortfolioConstraintPolicy(**policy_values)
            report = await asyncio.to_thread(
                evaluate_portfolio_constraints,
                snapshot,
                policy=policy,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=422,
                detail="invalid persisted research result: {}".format(exc),
            )
        return {
            "run_id": normalized_run_id,
            "result_hash": record.result_hash,
            "result_created_at": record.created_at.isoformat(),
            "constraints": report.to_dict(),
            "served_from": "sqlite_cache",
            "upstream_requests": 0,
            "notice": "组合约束仅用于研究审计，不构成投资建议或交易指令",
        }

    @app.get("/api/v1/research/runs/{run_id}/risk")
    async def research_risk_report(
        run_id: str,
        confidence: str = Query(
            default="0.95,0.99",
            description="Comma-separated historical tail confidence levels",
        ),
        minimum_observations: int = Query(default=20, ge=1, le=2000),
    ):
        """Compute risk analytics from a persisted result without provider calls."""

        normalized_run_id = str(run_id).strip()
        if not normalized_run_id or len(normalized_run_id) > 128:
            raise HTTPException(status_code=422, detail="invalid run_id")
        try:
            levels = tuple(
                float(item.strip())
                for item in str(confidence).split(",")
                if item.strip()
            )
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="confidence must be comma-separated numbers")
        try:
            record = await asyncio.to_thread(
                runtime().repository.get_research_result, normalized_run_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if record is None:
            raise HTTPException(status_code=404, detail="research result not found")
        try:
            result_payload = json.loads(record.result_json)
            report = await asyncio.to_thread(
                build_risk_report_from_payload,
                result_payload,
                confidence_levels=levels,
                minimum_observations=minimum_observations,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail="invalid persisted research result: {}".format(exc))
        return {
            "run_id": normalized_run_id,
            "result_hash": record.result_hash,
            "result_created_at": record.created_at.isoformat(),
            "risk": report.to_dict(),
            "served_from": "sqlite_cache",
            "upstream_requests": 0,
            "notice": "风险统计仅用于研究，不构成投资建议或交易指令",
        }

    @app.get("/api/v1/research/runs/{run_id}/integrity")
    async def research_result_integrity(
        run_id: str,
        tolerance: float = Query(
            default=1e-9,
            ge=0.0,
            le=1e-3,
            description="Relative/absolute tolerance for floating-point metric checks",
        ),
    ):
        """Audit stored performance metrics against the stored equity curve.

        This is a read-only, provider-independent audit.  A result hash proves
        immutability, while this endpoint additionally verifies that reported
        returns, drawdowns, risk ratios and ledger totals can be recomputed
        from the artifact itself.
        """

        normalized_run_id = str(run_id).strip()
        if not normalized_run_id or len(normalized_run_id) > 128:
            raise HTTPException(status_code=422, detail="invalid run_id")
        try:
            record = await asyncio.to_thread(
                runtime().repository.get_research_result, normalized_run_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if record is None:
            raise HTTPException(status_code=404, detail="research result not found")
        try:
            result_payload = json.loads(record.result_json)
            report = await asyncio.to_thread(
                audit_backtest_payload_metrics,
                result_payload,
                tolerance=tolerance,
            )
        except (TypeError, ValueError, ArithmeticError, statistics.StatisticsError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=422,
                detail="invalid persisted research result: {}".format(exc),
            )
        return {
            "run_id": normalized_run_id,
            "result_hash": record.result_hash,
            "result_created_at": record.created_at.isoformat(),
            "integrity": report.to_dict(),
            "served_from": "sqlite_cache",
            "upstream_requests": 0,
            "notice": "指标一致性审计仅用于研究，不构成投资建议或交易指令",
        }

    @app.get("/api/v1/research/runs/{run_id}/governance")
    async def research_governance_report(
        run_id: str,
        decision_at: Optional[str] = Query(
            default=None,
            max_length=64,
            description="Timezone-aware timestamp used to attest point-in-time availability",
        ),
    ):
        """Return one fail-closed readiness gate for a persisted research run.

        The endpoint composes immutable manifest/result validation, metric
        integrity, strategy binding and observation lineage.  It is read-only
        and never calls a provider.  Missing result or lineage evidence is
        represented as a failed check instead of being treated as a pass.
        """

        normalized_run_id = str(run_id).strip()
        if not normalized_run_id or len(normalized_run_id) > 128:
            raise HTTPException(status_code=422, detail="invalid run_id")
        decision_timestamp = _parse_optional_time(decision_at, "decision_at")

        def load_governance() -> Optional[Dict[str, Any]]:
            repository = runtime().repository
            manifest_record = repository.get_research_run(normalized_run_id)
            if manifest_record is None:
                return None
            result_record = repository.get_research_result(normalized_run_id)
            warnings: List[str] = []
            result_integrity = result_record is not None
            result_metrics_integrity = False
            metric_audit = None
            strategy_matches = False
            replay_audit = None
            replay_verified = False
            # PIT universe declarations are not evidence by themselves.  Keep
            # an explicit cache-backed audit projection so callers can see
            # which immutable v9 snapshots were actually visible at the
            # decision time (and why a declaration was rejected).  This is
            # intentionally separate from ``decision_grade``: finding a
            # persisted snapshot does not authenticate the rest of a run.
            universe_binding: Dict[str, Any] = {
                "required": False,
                "verified": False,
                "status": "not_declared",
                "declared_version": None,
                "declared_snapshot_hashes": [],
                "declared_snapshot_hash_map": {},
                "matched_snapshot_hashes": [],
                "missing_snapshot_hashes": [],
                "missing_snapshot_sessions": [],
                "snapshots": [],
                "candidate_count": 0,
                "scan_truncated": False,
                "decision_at": decision_timestamp.isoformat()
                if decision_timestamp is not None
                else None,
            }

            def _is_snapshot_hash(value: Any) -> bool:
                if not isinstance(value, str) or len(value) != 71:
                    return False
                prefix, separator, digest = value.partition(":")
                if prefix != "sha256" or separator != ":" or len(digest) != 64:
                    return False
                return all(char in "0123456789abcdef" for char in digest)

            def _audit_persisted_universe(parsed_manifest: Any) -> Dict[str, Any]:
                """Resolve declared PIT hashes against SQLite v9 evidence.

                The repository query applies both knowledge-cutoff and local
                receipt-time predicates.  We deliberately do not consult the
                provider catalogue here: a missing or late-imported snapshot
                must remain a failed/indeterminate governance result.
                """

                result = dict(universe_binding)
                if not isinstance(parsed_manifest, dict):
                    result["status"] = "manifest_invalid"
                    return result
                versions = parsed_manifest.get("versions", {})
                dataset = parsed_manifest.get("dataset", {})
                if not isinstance(versions, dict) or not isinstance(dataset, dict):
                    result["status"] = "manifest_invalid"
                    return result
                declared_version = versions.get("universe")
                result["declared_version"] = declared_version
                attributes = dataset.get("attributes", {})
                if not isinstance(attributes, dict):
                    result["status"] = "manifest_invalid"
                    return result
                raw_hashes = attributes.get("universe_snapshot_hashes")
                # A content-addressed ``versions.universe`` is also a valid
                # PIT declaration emitted by the research manifest contract.
                # Human labels (for example ``banks-v1``) remain legacy,
                # non-authenticating declarations.
                if raw_hashes is None and _is_snapshot_hash(declared_version):
                    raw_hashes = [declared_version]
                if raw_hashes is None:
                    return result
                result["required"] = True
                hash_map = None
                if isinstance(raw_hashes, dict):
                    hash_map = {}
                    for raw_session, raw_hash in raw_hashes.items():
                        if not isinstance(raw_session, str) or not isinstance(raw_hash, str):
                            result["status"] = "invalid_declaration"
                            return result
                        try:
                            session = date.fromisoformat(raw_session)
                        except (TypeError, ValueError):
                            result["status"] = "invalid_declaration"
                            return result
                        if session.isoformat() != raw_session:
                            result["status"] = "invalid_declaration"
                            return result
                        hash_map[raw_session] = raw_hash
                    if not hash_map:
                        result["status"] = "invalid_declaration"
                        return result
                    hashes = list(hash_map.values())
                    result["declared_snapshot_hash_map"] = dict(hash_map)
                elif isinstance(raw_hashes, (list, tuple)) and raw_hashes:
                    hashes = list(raw_hashes)
                else:
                    result["status"] = "invalid_declaration"
                    return result
                result["declared_snapshot_hashes"] = hashes
                if (
                    any(not _is_snapshot_hash(item) for item in hashes)
                    or len(set(hashes)) != len(hashes)
                ):
                    result["status"] = "invalid_declaration"
                    return result
                if decision_timestamp is None:
                    result["status"] = "decision_at_required"
                    return result
                try:
                    records, total = repository.query_universe_snapshot_records_as_of(
                        as_of=decision_timestamp,
                        limit=500,
                        offset=0,
                    )
                except (TypeError, ValueError, sqlite3.Error):
                    result["status"] = "query_error"
                    return result
                result["candidate_count"] = total
                result["scan_truncated"] = total > len(records)
                if hash_map is not None:
                    matched = [
                        record for record in records
                        if hash_map.get(record["snapshot"].session_date.isoformat())
                        == record["snapshot"].content_hash
                    ]
                    matched_keys = {
                        record["snapshot"].session_date.isoformat()
                        for record in matched
                    }
                    missing_sessions = [
                        session for session in hash_map
                        if session not in matched_keys
                    ]
                    missing = [hash_map[session] for session in missing_sessions]
                    result["missing_snapshot_sessions"] = missing_sessions
                else:
                    matched = [
                        record for record in records
                        if record["snapshot"].content_hash in hashes
                    ]
                    matched_keys = None
                    missing = None
                    result["missing_snapshot_sessions"] = []
                matched_hashes = sorted({record["snapshot"].content_hash for record in matched})
                if missing is None:
                    missing = [item for item in hashes if item not in matched_hashes]
                result["matched_snapshot_hashes"] = matched_hashes
                result["missing_snapshot_hashes"] = missing
                result["snapshots"] = [
                    {
                        "content_hash": record["snapshot"].content_hash,
                        "universe": record["snapshot"].universe,
                        "session_date": record["snapshot"].session_date.isoformat(),
                        "knowledge_cutoff": record["snapshot"].knowledge_cutoff.isoformat(),
                        "version": record["snapshot"].version,
                        "source": record["source"],
                        "recorded_at": record["recorded_at"].isoformat(),
                    }
                    for record in matched
                ]
                # A descriptive manifest version must agree with every
                # resolved snapshot.  A hash version is already checked by
                # membership in ``hashes`` and does not carry a second label.
                observed_versions = {
                    row["version"] for row in result["snapshots"]
                }
                version_mismatch = False
                if not _is_snapshot_hash(declared_version) and isinstance(declared_version, str):
                    version_mismatch = (
                        declared_version == "mixed"
                        and len(observed_versions) < 2
                    ) or (
                        declared_version != "mixed"
                        and any(row["version"] != declared_version for row in result["snapshots"])
                    )
                result["version_mismatch"] = version_mismatch
                if result["scan_truncated"]:
                    result["status"] = "scan_truncated"
                elif missing:
                    result["status"] = "missing"
                elif version_mismatch:
                    result["status"] = "version_mismatch"
                else:
                    result["verified"] = True
                    result["status"] = "verified"
                return result

            parsed_manifest_for_universe = None
            try:
                parsed_manifest_for_universe = json.loads(manifest_record.manifest_json)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            universe_binding = _audit_persisted_universe(parsed_manifest_for_universe)
            if universe_binding["required"] and not universe_binding["verified"]:
                warnings.append("universe_snapshot_evidence_missing")
            # Uploaded diagnostics are claims, not independently authenticated
            # data/calendar/execution evidence. The current engine deliberately
            # cannot attest decision grade; hashing a forged flag changes none
            # of that. Likewise this read-only audit does not replay a strategy.
            declared_decision_grade = None
            warnings.append("decision_grade_unattested")
            if result_record is None:
                warnings.append("result_artifact_missing")
            else:
                try:
                    result_payload = json.loads(result_record.result_json)
                    strategy_matches = (
                        result_payload.get("strategy_name") == manifest_record.strategy_name
                    )
                    if not strategy_matches:
                        warnings.append("strategy_manifest_mismatch")
                    diagnostics = result_payload.get("diagnostics", {})
                    if isinstance(diagnostics, dict):
                        claim = diagnostics.get("decision_grade")
                        declared_decision_grade = claim if type(claim) is bool else None
                        if declared_decision_grade is not True:
                            warnings.append("result_not_decision_grade")
                        else:
                            warnings.append("caller_decision_grade_ignored")
                    else:
                        warnings.append("diagnostics_missing")
                    metric_report = audit_backtest_payload_metrics(result_payload)
                    metric_audit = metric_report.to_dict()
                    # Legacy artifacts may pass only the checks for which
                    # fields exist. Keep that report visible, but do not
                    # describe incomplete coverage as full consistency.
                    result_metrics_integrity = (
                        metric_report.verified and not metric_report.unavailable_checks
                    )
                    if metric_report.unavailable_checks:
                        warnings.append("result_metrics_coverage_incomplete")
                    if not metric_report.verified:
                        warnings.append("result_metrics_integrity_failed")
                except (TypeError, ValueError, ArithmeticError, statistics.StatisticsError, json.JSONDecodeError):
                    result_integrity = False
                    warnings.append("result_artifact_invalid")

            # A manifest's batch binding is necessary but not sufficient: the
            # same exact lineage gate used by the dedicated endpoint must
            # attest source, count, hash, quality, session and availability.
            lineage_verified = False
            lineage_count = 0
            lineage_total = 0
            lineage_observations = ()
            lineage_batch = None
            # PIT universe evidence is loaded only from immutable SQLite
            # snapshots already visible at the requested decision time.  A
            # manifest hash/version is a declaration, not proof; the replay
            # verifier below still checks the exact session-to-snapshot
            # mapping and content hashes.
            persisted_universe_evidence = None
            universe_binding_declared = False
            try:
                parsed = json.loads(manifest_record.manifest_json)
                dataset = parsed.get("dataset", {})
                attributes = dataset.get("attributes", {}) if isinstance(dataset, dict) else {}
                # Reuse the v9 evidence audit above so a content-addressed
                # ``versions.universe`` hash is treated exactly like an
                # explicit ``dataset.attributes.universe_snapshot_hashes``
                # declaration. Human labels remain backwards-compatible.
                declared_snapshot_hashes = (
                    universe_binding.get("declared_snapshot_hashes")
                    if universe_binding.get("required")
                    else None
                )
                universe_binding_declared = bool(universe_binding.get("required"))
                batch_id = attributes.get("batch_id") if isinstance(attributes, dict) else None
                declared_count = dataset.get("record_count") if isinstance(dataset, dict) else None
                source = str(dataset.get("source", "")).strip() if isinstance(dataset, dict) else ""
                if batch_id and decision_timestamp is not None:
                    if not isinstance(batch_id, str) or not batch_id.strip() or len(batch_id) > 128:
                        raise ValueError("invalid manifest batch binding")
                    scan_limit = min(max(int(declared_count) if type(declared_count) is int else 1, 1), 10000)
                    observations, lineage_total = repository.query_bar_observations(
                        batch_id=str(batch_id).strip(), source=source or None, interval="1d", limit=scan_limit
                    )
                    lineage_count = len(observations)
                    batch = repository.get_collection_batch(str(batch_id).strip())
                    lineage_observations = tuple(observations)
                    lineage_batch = batch
                    lineage_verified = audit_observation_lineage(
                        observations,
                        expected_input_hash=manifest_record.input_hash,
                        expected_record_count=declared_count if type(declared_count) is int else None,
                        expected_source=source or None,
                        decision_at=decision_timestamp,
                        batches=[batch] if batch is not None else [],
                    ).verified and lineage_total == lineage_count
                    if lineage_total != lineage_count:
                        warnings.append("lineage_scan_truncated")
                else:
                    warnings.append("lineage_attestation_missing")
            except (TypeError, ValueError, json.JSONDecodeError):
                warnings.append("lineage_attestation_invalid")

            if universe_binding_declared:
                if decision_timestamp is None or not lineage_observations:
                    warnings.append("universe_snapshot_evidence_missing")
                else:
                    try:
                        versions = parsed.get("versions", {})
                        declared_version = (
                            versions.get("universe")
                            if isinstance(versions, dict)
                            else None
                        )
                        declared_hash_map = universe_binding.get(
                            "declared_snapshot_hash_map", {}
                        )
                        sessions = sorted({
                            item.session_date for item in lineage_observations
                        })
                        selected = {}
                        for session in sessions:
                            snapshots, _ = repository.query_universe_snapshots_as_of(
                                as_of=decision_timestamp,
                                session_date=session,
                                limit=200,
                            )
                            expected_hash = None
                            if isinstance(declared_hash_map, dict) and declared_hash_map:
                                expected_hash = declared_hash_map.get(session.isoformat())
                            elif isinstance(declared_snapshot_hashes, (list, tuple)):
                                expected_hashes = set(declared_snapshot_hashes)
                                candidates = [
                                    item for item in snapshots
                                    if item.content_hash in expected_hashes
                                ]
                                if len(candidates) == 1:
                                    selected[session] = candidates[0]
                                    continue
                            if not isinstance(expected_hash, str):
                                raise ValueError("universe snapshot hash declaration is invalid")
                            candidates = [
                                item for item in snapshots
                                if item.content_hash == expected_hash
                            ]
                            if len(candidates) != 1:
                                raise ValueError("universe snapshot evidence is missing or ambiguous")
                            selected[session] = candidates[0]
                        if len(selected) == len(sessions):
                            # Version is validated by verify_replay; retain the
                            # local read here only to make malformed/missing
                            # manifest versions fail closed before replay.
                            if not isinstance(declared_version, str) or not declared_version.strip():
                                raise ValueError("universe version declaration is missing")
                            persisted_universe_evidence = selected
                    except (TypeError, ValueError, KeyError):
                        warnings.append("universe_snapshot_evidence_invalid")

            # Independent replay is deliberately narrower than the lineage
            # audit: it only runs when the complete immutable observation set
            # and a valid decision timestamp are present.  The verifier is
            # pure and receives no provider or repository handle.
            if (
                result_record is not None
                and metric_audit is not None
                and lineage_verified
                and lineage_observations
                and lineage_batch is not None
                and decision_timestamp is not None
            ):
                try:
                    replay_audit = verify_replay(
                        manifest_record,
                        parsed,
                        result_payload,
                        lineage_observations,
                        (lineage_batch,),
                        decision_timestamp,
                        point_in_time_universe=persisted_universe_evidence,
                        require_universe_binding=universe_binding_declared,
                        require_calendar_binding=True,
                    )
                    replay_verified = replay_audit.verified
                    if not replay_verified:
                        warnings.extend(("independent_replay_failed", "replay_evidence_missing"))
                except (TypeError, ValueError, KeyError) as exc:
                    warnings.append("independent_replay_invalid")
                    replay_audit = {"verified": False, "error": type(exc).__name__}
            else:
                warnings.extend(("independent_replay_evidence_missing", "replay_evidence_missing"))

            report = build_governance_report(
                normalized_run_id,
                manifest_integrity=True,
                result_integrity=result_integrity,
                result_metrics_integrity=result_metrics_integrity,
                strategy_matches_manifest=strategy_matches,
                lineage_verified=lineage_verified,
                decision_grade=False,
                replay_verified=replay_verified,
                warnings=tuple(dict.fromkeys(warnings)),
            )
            payload = report.to_dict()
            payload.update(
                {
                    "manifest_hash": manifest_record.content_hash,
                    "result_hash": result_record.result_hash if result_record is not None else None,
                    "declared_decision_grade": declared_decision_grade,
                    "metric_audit": metric_audit,
                    "replay_audit": (
                        replay_audit.to_dict()
                        if hasattr(replay_audit, "to_dict")
                        else replay_audit
                    ),
                    "decision_at": decision_timestamp.isoformat() if decision_timestamp else None,
                    "lineage_observation_count": lineage_count,
                    "lineage_observation_total": lineage_total,
                    "lineage_scan_truncated": lineage_count != lineage_total,
                    # This is an evidence report only.  In particular,
                    # ``verified`` here never changes the intentionally
                    # fail-closed decision-grade/replay checks above.
                    "universe_binding": universe_binding,
                    "served_from": "sqlite_cache",
                    "upstream_requests": 0,
                    "notice": "制品一致不等于已验证重放；仅在 replay_audit 全部通过时授予可复现状态。当前没有决策级数据认证，不授予决策就绪等级",
                }
            )
            return payload

        try:
            response = await asyncio.to_thread(load_governance)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if response is None:
            raise HTTPException(status_code=404, detail="research run not found")
        return response

    @app.get("/api/v1/research/runs/{run_id}/robustness")
    async def research_robustness_report(
        run_id: str,
        confidence: float = Query(
            default=0.95,
            gt=0.0,
            lt=1.0,
            description="Bootstrap confidence level",
        ),
        resamples: int = Query(
            default=2000,
            ge=1,
            le=100000,
            description="Number of deterministic bootstrap resamples",
        ),
        seed: int = Query(default=0, ge=0, description="Reproducibility seed"),
        minimum_observations: int = Query(default=30, ge=1, le=2000),
    ):
        """Estimate return/drawdown stability from a cached result only.

        This endpoint performs deterministic IID bootstrap resampling of the
        persisted equity curve.  It never fetches bars or calls a provider;
        short or incomplete samples return explicit unavailable intervals.
        """

        normalized_run_id = str(run_id).strip()
        if not normalized_run_id or len(normalized_run_id) > 128:
            raise HTTPException(status_code=422, detail="invalid run_id")
        try:
            record = await asyncio.to_thread(
                runtime().repository.get_research_result, normalized_run_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if record is None:
            raise HTTPException(status_code=404, detail="research result not found")
        try:
            result_payload = json.loads(record.result_json)
            report = await asyncio.to_thread(
                build_bootstrap_robustness_report_from_payload,
                result_payload,
                confidence=confidence,
                resamples=resamples,
                seed=seed,
                minimum_observations=minimum_observations,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=422,
                detail="invalid persisted research result: {}".format(exc),
            )
        return {
            "run_id": normalized_run_id,
            "result_hash": record.result_hash,
            "result_created_at": record.created_at.isoformat(),
            "robustness": report.to_dict(),
            "served_from": "sqlite_cache",
            "upstream_requests": 0,
            "notice": "bootstrap 稳健性区间仅用于研究，不构成投资建议或交易指令",
        }

    @app.post("/api/v1/research/runs/{run_id}/benchmark")
    async def research_benchmark_report(
        run_id: str, payload: Dict[str, Any] = Body(...)
    ):
        """Compare a persisted backtest with caller-supplied benchmark returns.

        Benchmark observations are supplied explicitly by the research client;
        this endpoint never fetches an index or calls a provider.  The
        comparison is therefore reproducible and safe under the daily API
        budget.
        """

        normalized_run_id = str(run_id).strip()
        if not normalized_run_id or len(normalized_run_id) > 128:
            raise HTTPException(status_code=422, detail="invalid run_id")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="JSON object required")
        if "benchmark_returns" not in payload:
            raise HTTPException(status_code=422, detail="benchmark_returns is required")
        benchmark_returns = payload.get("benchmark_returns")
        benchmark_name = payload.get("benchmark_name", "benchmark")
        minimum_observations = payload.get("minimum_observations", 20)
        annualization_sessions = payload.get("annualization_sessions", 252)
        annual_risk_free_rate = payload.get("annual_risk_free_rate", 0.0)
        try:
            record = await asyncio.to_thread(
                runtime().repository.get_research_result, normalized_run_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if record is None:
            raise HTTPException(status_code=404, detail="research result not found")
        try:
            result_payload = json.loads(record.result_json)
            report = await asyncio.to_thread(
                build_benchmark_report_from_payload,
                result_payload,
                benchmark_returns,
                benchmark_name=benchmark_name,
                minimum_observations=minimum_observations,
                annualization_sessions=annualization_sessions,
                annual_risk_free_rate=annual_risk_free_rate,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {
            "run_id": normalized_run_id,
            "result_hash": record.result_hash,
            "benchmark": report.to_dict(),
            "served_from": "sqlite_cache",
            "upstream_requests": 0,
            "notice": "基准比较仅用于研究，不构成投资建议或交易指令",
        }

    @app.post("/api/v1/research/runs/{run_id}/attribution")
    async def research_attribution_report(
        run_id: str, payload: Dict[str, Any] = Body(...)
    ):
        """Decompose a cached result into benchmark, cash and active return.

        Benchmark and optional cash returns are supplied by the research
        client.  This cache-only route never fetches an index, assumes a cash
        yield, or reruns the strategy; missing observations remain explicit
        and prevent a complete attribution status.
        """

        normalized_run_id = str(run_id).strip()
        if not normalized_run_id or len(normalized_run_id) > 128:
            raise HTTPException(status_code=422, detail="invalid run_id")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="JSON object required")
        if "benchmark_returns" not in payload:
            raise HTTPException(status_code=422, detail="benchmark_returns is required")
        try:
            record = await asyncio.to_thread(
                runtime().repository.get_research_result, normalized_run_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if record is None:
            raise HTTPException(status_code=404, detail="research result not found")
        try:
            result_payload = json.loads(record.result_json)
            report = await asyncio.to_thread(
                build_attribution_report_from_payload,
                result_payload,
                payload.get("benchmark_returns"),
                cash_returns=payload.get("cash_returns"),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {
            "run_id": normalized_run_id,
            "result_hash": record.result_hash,
            "attribution": report.to_dict(),
            "served_from": "sqlite_cache",
            "upstream_requests": 0,
            "notice": "组合归因仅用于研究，不构成投资建议或交易指令",
        }

    @app.post("/api/v1/research/runs/{run_id}/stress")
    async def research_stress_report(
        run_id: str, payload: Dict[str, Any] = Body(...)
    ):
        """Run explicit deterministic stress scenarios on a cached result.

        Scenarios are supplied by the research client and applied to the
        persisted backtest artifact only.  No strategy is rerun and no
        provider request is made, so this endpoint is safe under the daily
        AShareHub budget.
        """

        normalized_run_id = str(run_id).strip()
        if not normalized_run_id or len(normalized_run_id) > 128:
            raise HTTPException(status_code=422, detail="invalid run_id")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="JSON object required")
        raw_scenarios = payload.get("scenarios")
        if not isinstance(raw_scenarios, list):
            raise HTTPException(status_code=422, detail="scenarios must be an array")
        try:
            scenarios = tuple(
                StressScenario(
                    name=item.get("name"),
                    price_shock=item.get("price_shock", 0),
                    cost_multiplier=item.get("cost_multiplier", 1),
                )
                for item in raw_scenarios
                if isinstance(item, dict)
            )
            if len(scenarios) != len(raw_scenarios):
                raise TypeError("each scenario must be an object")
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        try:
            record = await asyncio.to_thread(
                runtime().repository.get_research_result, normalized_run_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if record is None:
            raise HTTPException(status_code=404, detail="research result not found")
        try:
            result_payload = json.loads(record.result_json)
            report = await asyncio.to_thread(
                run_stress_test_from_payload,
                result_payload,
                scenarios,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {
            "run_id": normalized_run_id,
            "result_hash": record.result_hash,
            "stress": report.to_dict(),
            "served_from": "sqlite_cache",
            "upstream_requests": 0,
            "notice": "压力测试仅用于研究，不构成投资建议或交易指令",
        }

    @app.post("/api/v1/research/runs/{run_id}/capacity")
    async def research_capacity_report(
        run_id: str, payload: Dict[str, Any] = Body(default_factory=dict)
    ):
        """Audit recorded fills against cached execution-bar volume.

        This is deliberately an audit rather than a fill simulator.  It reads
        only the immutable result and local 1d bars; missing volume remains
        unassessed and never becomes an assumed executable fill.
        """

        normalized_run_id = str(run_id).strip()
        if not normalized_run_id or len(normalized_run_id) > 128:
            raise HTTPException(status_code=422, detail="invalid run_id")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="JSON object required")
        try:
            assumptions = CapacityAssumptions(
                max_participation_rate=payload.get("max_participation_rate", "0.10")
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        try:
            record = await asyncio.to_thread(
                runtime().repository.get_research_result, normalized_run_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if record is None:
            raise HTTPException(status_code=404, detail="research result not found")
        try:
            result_payload = json.loads(record.result_json)
            raw_trades = result_payload.get("trades", [])
            if not isinstance(raw_trades, list):
                raise ValueError("persisted research result trades must be an array")
            symbols = sorted(
                {
                    normalize_symbol(item["symbol"])
                    for item in raw_trades
                    if isinstance(item, dict) and item.get("symbol")
                }
            )
            # Capacity is a property of the execution bars used by this
            # immutable result, not of whichever provider happens to be
            # configured when the report is requested.  Prefer the source
            # captured in the result diagnostics; older artifacts fall back
            # to the manifest source and explicitly disclose that downgrade.
            manifest_record = await asyncio.to_thread(
                runtime().repository.get_research_run, normalized_run_id
            )
            manifest_source = (
                str(manifest_record.dataset_source).strip()
                if manifest_record is not None
                else ""
            )
            diagnostics = result_payload.get("diagnostics", {})
            result_assumptions = (
                diagnostics.get("assumptions", {})
                if isinstance(diagnostics, dict)
                else {}
            )
            raw_bar_sources = (
                result_assumptions.get("bar_sources")
                if isinstance(result_assumptions, dict)
                else None
            )
            bar_sources = tuple(
                str(item).strip()
                for item in raw_bar_sources
                if str(item).strip()
            ) if isinstance(raw_bar_sources, (list, tuple)) else ()
            if len(bar_sources) == 1:
                source_prefix = bar_sources[0]
                source_origin = "result_diagnostics"
            elif manifest_source:
                # A mixed-source replay is not silently collapsed to the
                # current provider.  The manifest namespace is the narrowest
                # durable fallback available for legacy/mixed artifacts.
                source_prefix = manifest_source
                source_origin = "research_manifest"
            else:
                source_prefix = None
                source_origin = "unavailable"

            def build_capacity() -> Any:
                bars_by_symbol = {
                    symbol: runtime().repository.get_bars(
                        symbol,
                        interval="1d",
                        limit=5000,
                        source_prefix=source_prefix,
                    )
                    for symbol in symbols
                }
                return audit_capacity_from_payload(
                    result_payload,
                    bars_by_symbol,
                    assumptions,
                )

            report = await asyncio.to_thread(build_capacity)
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {
            "run_id": normalized_run_id,
            "result_hash": record.result_hash,
            "capacity": report.to_dict(),
            "data_provenance": {
                "bar_source": source_prefix,
                "bar_source_origin": source_origin,
                "bar_sources": list(bar_sources),
                "manifest_source": manifest_source or None,
            },
            "served_from": "sqlite_cache",
            "upstream_requests": 0,
            "notice": "成交容量审计仅用于研究，不构成投资建议或交易指令",
        }

    @app.get("/api/v1/overview")
    async def overview():
        active_runtime = runtime()

        def load_overview_cache():
            """Read the bounded overview snapshot off the ASGI event loop."""

            repo = active_runtime.repository
            quotes = current_provider_items(
                repo.get_latest_quotes(
                    active_runtime.symbols,
                    source_prefix=active_runtime.provider.name,
                )
            )
            items = [
                quote_payload(item, active_runtime.instrument_name(item.symbol))
                for item in quotes[:3]
            ]
            alerts = repo.list_alerts(limit=100)
            signals = repo.list_signals(limit=100)
            provider = active_runtime.system_status()["provider"]
            return quotes, items, alerts, signals, provider

        quotes, items, alerts, signals, provider = await asyncio.to_thread(
            load_overview_cache
        )
        return {
            "market_status": current_market_status(),
            "provider": provider,
            "counts": {
                "watchlist": len(quotes),
                "configured_watchlist": len(active_runtime.symbols),
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
        active_runtime = runtime()

        def load_watchlist_cache():
            """Read the configured bank basket without blocking the event loop."""

            quotes = current_provider_items(
                active_runtime.repository.get_latest_quotes(
                    active_runtime.symbols,
                    source_prefix=active_runtime.provider.name,
                )
            )
            items = [
                quote_payload(item, active_runtime.instrument_name(item.symbol))
                for item in quotes
            ]
            return quotes, items

        quotes, items = await asyncio.to_thread(load_watchlist_cache)
        received_symbols = {item.symbol for item in quotes}
        missing_symbols = [
            symbol for symbol in active_runtime.symbols if symbol not in received_symbols
        ]
        status = await asyncio.to_thread(active_runtime.system_status)
        return {
            "items": items,
            "count": len(quotes),
            "configured_count": len(active_runtime.symbols),
            "missing_symbols": missing_symbols,
            "provider": active_runtime.provider.name,
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
        bar_provenance: Dict[str, Dict[str, Any]] = {}
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
            # ``bars`` is a compatibility projection.  Compute provenance per
            # symbol so a strategy result cannot report a generic ``ok`` when
            # its inputs have no immutable observation/response evidence.
            bar_provenance[symbol] = _bar_projection_provenance(
                repo, {symbol: cached_bars}, interval="1d"
            )
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
                fundamental_weight=configured.bank_strategy_fundamental_weight,
            ),
        )
        cohort_timestamp = max(
            (item.timestamp for item in contexts.values()),
            default=now_market_time(),
        )
        # A complete factor window is still exploratory unless the decision
        # instant is bound to an attested exchange calendar and an immutable
        # point-in-time security universe.  This lookup is cache-only: a
        # missing/ambiguous snapshot fails closed and never refreshes a
        # provider or spends an upstream request.
        decision_snapshot = None
        try:
            snapshots, snapshot_total = repo.query_universe_snapshots_as_of(
                as_of=cohort_timestamp,
                universe="a_share_banks",
                session_date=cohort_timestamp.date(),
                limit=2,
                offset=0,
            )
            if snapshot_total == 1 and len(snapshots) == 1:
                decision_snapshot = snapshots[0]
        except (RuntimeError, TypeError, ValueError):
            snapshots = ()
            snapshot_total = 0
        decision_readiness = build_decision_readiness(
            calendar=active_runtime.current_trading_calendar(),
            universe_source=active_runtime.universe_source,
            universe_size=len(symbols),
            minimum_universe=bank_strategy.min_universe,
            point_in_time_snapshot=decision_snapshot,
            as_of=cohort_timestamp,
            expected_symbols=symbols,
            session_date=cohort_timestamp.date(),
        )
        try:
            # Bind fundamentals to the same decision instant as the price
            # cohort. A wall-clock snapshot taken later could leak a newly
            # published fact into an older cached bar decision.
            fundamental_report = _cached_bank_fundamental_report(
                cohort_timestamp, symbols
            )
        except ValueError:
            # Ambiguous financial revisions must not disable price/history
            # diagnostics. Only the independent fundamental view fails closed.
            fundamental_report = build_bank_fundamental_factors(
                (), decision_at=cohort_timestamp, symbols=symbols,
            ).to_dict()
            fundamental_report.update({
                "quality": {"status": "invalid", "complete_count": 0},
                "warnings": ["fundamental_selection_ambiguous"],
                "candidate_count": None,
                "served_from": "sqlite_cache",
                "provider_operations": 0,
                "upstream_requests": 0,
                "score_usage": "parallel_research_only",
                "strategy_integration": False,
                "knowledge_mode": "published_as_of",
            })
        fundamentals_by_symbol = {}
        for record in fundamental_report["items"]:
            evidence = dict(record)
            evidence.update({
                "warnings": list(fundamental_report["warnings"]),
                "score_usage": "parallel_research_only",
                "strategy_integration": True,
            })
            fundamentals_by_symbol[record["symbol"]] = evidence
        research = bank_strategy.analyze(
            CohortStrategyContext(
                timestamp=cohort_timestamp,
                members=contexts,
                cohort="a_share_banks",
                metadata={
                    "served_from": "sqlite_cache",
                    "expected_bar_date": expected_latest_daily_date().isoformat(),
                    "calendar": "weekday_fallback",
                    "fundamental_factor_report": fundamental_report,
                },
            )
        )
        research_by_symbol = {
            str(item["symbol"]): item for item in research.get("items", ())
        }
        strategy_fundamental_overlay = research.get("fundamental_overlay", {})
        fundamental_used_by_strategy = bool(
            isinstance(strategy_fundamental_overlay, Mapping)
            and strategy_fundamental_overlay.get("enabled")
        )
        fundamental_report["strategy_usage"] = {
            "enabled": fundamental_used_by_strategy,
            "status": strategy_fundamental_overlay.get("status")
            if isinstance(strategy_fundamental_overlay, Mapping)
            else "unavailable",
            "reason": strategy_fundamental_overlay.get("reason")
            if isinstance(strategy_fundamental_overlay, Mapping)
            else "fundamental_report_unavailable",
            "weight": bank_strategy.fundamental_weight,
            "decision_at": strategy_fundamental_overlay.get("decision_at")
            if isinstance(strategy_fundamental_overlay, Mapping)
            else None,
        }
        for evidence in fundamentals_by_symbol.values():
            evidence["strategy_integration"] = fundamental_used_by_strategy

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
                # Keep factor rankings visible for diagnosis, but never expose
                # a BUY/SELL conclusion when the bars are only a legacy
                # projection or carry non-accepted quality.  Previously the
                # entry gate was false while ``recommendation.action`` still
                # said BUY/SELL, which was easy for a client to misread as a
                # tradeable risk conclusion.
                decision_ready = bool(
                    bar_provenance[symbol]["quality_complete"]
                    and decision_readiness.decision_ready
                )
                raw_action = str(model["action"])
                action = raw_action if decision_ready else "insufficient_data"
                label = (
                    str(model["label"])
                    if decision_ready
                    else "数据质量不足"
                )
                reason = (
                    str(model["reason"])
                    if decision_ready
                    else "数据血缘或质量证据未达到研究决策门槛"
                )
                risk_value = max(
                    float(factors["annualized_volatility_60"])
                    / bank_strategy.maximum_annualized_volatility,
                    abs(float(factors["max_drawdown_60"]))
                    / bank_strategy.maximum_drawdown,
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
                    "连续{}个交易日收盘低于60日均线".format(
                        bank_strategy.trend_exit_days
                    ),
                    "周度同业分位低于 {:.0%}".format(
                        model["score_thresholds"]["exit"]
                    ),
                    "年化波动高于 {:.0%} 或60日回撤达到 -{:.0%}".format(
                        bank_strategy.maximum_annualized_volatility,
                        bank_strategy.maximum_drawdown,
                    ),
                ]
                # A mathematically valid factor score is not an actionable
                # research entry when its compatibility bars lack complete,
                # accepted immutable observation evidence.  Keep the raw
                # ranking/action in ``model_action`` for diagnosis, but fail
                # closed for the user-facing recommendation and entry gate.
                provenance_gate_failures = list(model.get("gate_failures", ()))
                entry_failure_reasons = list(
                    model.get("entry_failure_reasons", ())
                )
                if not bar_provenance[symbol]["quality_complete"]:
                    if "data_lineage_incomplete" not in provenance_gate_failures:
                        provenance_gate_failures.append("data_lineage_incomplete")
                    if "data_lineage_incomplete" not in entry_failure_reasons:
                        entry_failure_reasons.append("data_lineage_incomplete")
                entry_eligible = bool(model.get("entry_eligible", False)) and bool(
                    bar_provenance[symbol]["quality_complete"]
                    and decision_readiness.decision_ready
                )
                # A valid factor calculation is separate from a
                # decision-grade result.  The former means the strategy had
                # enough mathematically valid history; the latter additionally
                # requires accepted immutable observation evidence.
                analysis_ready = True
                if not bar_provenance[symbol]["quality_complete"]:
                    decision_ready_reason = (
                        bar_provenance[symbol]["warnings"][0]
                        if bar_provenance[symbol]["warnings"]
                        else "data_quality_not_decision_ready"
                    )
                elif not decision_readiness.decision_ready:
                    decision_ready_reason = (
                        decision_readiness.failures[0]
                        if decision_readiness.failures
                        else "decision_evidence_unverified"
                    )
                else:
                    decision_ready_reason = "ok"
                analysis = {
                    "symbol": symbol,
                    "name": instrument.name if instrument else symbol,
                    "model_version": bank_strategy.MODEL_VERSION,
                    "model_variant": model.get("model_variant", "price_only_v1"),
                    "fundamental_factors": fundamentals_by_symbol.get(symbol),
                    "fundamental_overlay": model.get("fundamental_overlay") if model else None,
                    "interval": "1d",
                    "price": model["price"],
                    "price_score": model.get("price_score"),
                    "bars_used": bars_count[symbol],
                    "timestamp": model["bar_as_of"],
                    "indicators": {
                        # The bank model has different lookbacks from the
                        # generic analysis endpoint. Keep unavailable legacy
                        # fields null rather than aliasing unlike measures.
                        # Ratios below use fractions, never percentage points.
                        "return_5": None,
                        "return_20": None,
                        "annualized_volatility_20": None,
                        "momentum_60_5": factors["momentum_60_5"],
                        "momentum_120_20": factors["momentum_120_20"],
                        "annualized_volatility_60": factors["annualized_volatility_60"],
                        "price_to_sma_120": factors["price_to_sma_120"],
                        "sma_20_to_60": factors["sma_20_to_60"],
                        "max_drawdown_60": factors["max_drawdown_60"],
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
                        "label": label,
                        "score": model["score"],
                        "reason": reason,
                        "model_action": raw_action,
                        "evidence": evidence,
                        "risks": risks,
                        "invalidation_conditions": invalidations,
                        "entry_eligible": entry_eligible,
                        "gate_failures": provenance_gate_failures,
                        "entry_failure_reasons": entry_failure_reasons,
                        "exit_reasons": list(model.get("exit_reasons", ())),
                    },
                    "data_quality": {
                        # A complete strategy window is not equivalent to a
                        # complete data lineage.  ``partial`` deliberately
                        # maps to the dashboard's cache-analysis state.
                        "status": (
                            "ok"
                            if bar_provenance[symbol]["quality_complete"]
                            else "partial"
                        ),
                        "bars_used": bars_count[symbol],
                        "bars_required": bank_strategy.min_history,
                        "bar_as_of": model["bar_as_of"],
                        "price_basis": "raw_unadjusted",
                        "lineage_complete": bar_provenance[symbol][
                            "lineage_complete"
                        ],
                        "quality_complete": bar_provenance[symbol][
                            "quality_complete"
                        ],
                        "analysis_ready": analysis_ready,
                        "decision_ready": decision_ready,
                        "decision_ready_reason": decision_ready_reason,
                        "decision_gate": decision_readiness.to_dict(),
                        "authoritative": bar_provenance[symbol]["authoritative"],
                        "immutable_observation_count": bar_provenance[symbol][
                            "immutable_observation_count"
                        ],
                        "legacy_item_count": bar_provenance[symbol][
                            "legacy_item_count"
                        ],
                        "non_accepted_observation_count": bar_provenance[symbol][
                            "non_accepted_observation_count"
                        ],
                        "quality_status_counts": dict(
                            bar_provenance[symbol]["quality_status_counts"]
                        ),
                        "calculation_input": bar_provenance[symbol][
                            "calculation_input"
                        ],
                        "warnings": list(bar_provenance[symbol]["warnings"]),
                    },
                    # Keep the bank-sector response on the same explicit
                    # provenance contract as the generic analysis endpoint.
                    # The dashboard uses this top-level object to label the
                    # calculation input; putting the field only inside
                    # ``data_quality`` made an otherwise valid factor model
                    # appear to have an unknown source in the UI.
                    "data_provenance": dict(bar_provenance[symbol]),
                    "factors": factors,
                    "factor_weights": dict(model["factor_weights"]),
                    "factor_contributions": dict(model["factor_contributions"]),
                    "score_thresholds": dict(model["score_thresholds"]),
                    "factor_percentiles": factor_percentiles,
                    "gates": model["gates"],
                    "gate_failures": provenance_gate_failures,
                    "entry_eligible": entry_eligible,
                    "entry_failure_reasons": entry_failure_reasons,
                    "analysis_ready": analysis_ready,
                    "decision_ready": decision_ready,
                    "decision_ready_reason": decision_ready_reason,
                    "model_action": raw_action,
                    "exit_reasons": list(model.get("exit_reasons", ())),
                    "rank": model["rank"],
                    "universe_size": model["universe_size"],
                }
                # ``quantMonitorAnalysisReadiness`` intentionally requires a
                # typed list.  Without it, bank rows were rendered as
                # "research conditions not met" even when all immutable
                # observations were accepted.  Derive this list from the
                # same quality gate used for the user-facing action.
                readiness_failures = []
                if not decision_ready:
                    readiness_failures.append("immutable_observation_lineage_incomplete")
                if bar_provenance[symbol]["warnings"]:
                    readiness_failures.extend(
                        "analysis_{}".format(str(item))
                        for item in bar_provenance[symbol]["warnings"]
                        if "analysis_{}".format(str(item)) not in readiness_failures
                    )
                analysis["data_quality"]["analysis_readiness_failures"] = list(
                    dict.fromkeys(readiness_failures)
                )
            items.append(
                {
                    "symbol": symbol,
                    "name": instrument.name if instrument else active_runtime.instrument_name(symbol),
                    "exchange": symbol.rsplit(".", 1)[1],
                    "instrument": jsonable(instrument) if instrument else None,
                    "quote": quote_data,
                    "analysis": analysis,
                    "fundamental_factors": fundamentals_by_symbol.get(symbol),
                    "fundamental_overlay": model.get("fundamental_overlay") if model else None,
                    "research_score": model["score"] if model else None,
                    # ``model`` may have enough rows for diagnostics while
                    # lacking accepted immutable evidence.  Surface the
                    # fail-closed decision state consistently at the item
                    # level as well as inside ``analysis``.
                    "research_action": action if model else "insufficient_data",
                    "research_label": label if model else "数据不足",
                    "data_quality": (
                        analysis["data_quality"]
                        if analysis
                        else {
                            "status": "insufficient_data",
                            "analysis_ready": False,
                            "decision_ready": False,
                             "decision_ready_reason": research.get("excluded", {}).get(symbol)
                             or research.get("reason")
                             or "insufficient_or_invalid_history",
                             "decision_gate": decision_readiness.to_dict(),
                             "bars_used": bars_count[symbol],
                            "bars_required": bank_strategy.min_history,
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
                        count >= bank_strategy.min_history
                        for count in bars_count.values()
                    ),
                    "analysis_ready": len(research_by_symbol),
                    # Keep the legacy ``analysis_ready`` count (the pure
                    # factor model produced a result) but expose the
                    # stricter evidence gate explicitly.  Consumers must not
                    # infer decision-grade readiness from a non-zero model
                    # count when the endpoint is serving compatibility bars.
                    "quality_ready": sum(
                        1
                        for symbol in research_by_symbol
                        if bar_provenance.get(symbol, {}).get("quality_complete")
                    ),
                     "decision_ready": sum(
                         1
                         for symbol in research_by_symbol
                         if bar_provenance.get(symbol, {}).get("quality_complete")
                         and decision_readiness.decision_ready
                     ),
                     "decision_gate_ready": decision_readiness.decision_ready,
                     "decision_gate_failures": list(decision_readiness.failures),
                    "common_as_of_ready": bool(research.get("ready")),
                    "analysis_reason": research.get("reason"),
                    "minimum_analysis_universe": bank_strategy.min_universe,
                },
                "decision_readiness": decision_readiness.to_dict(),
                "summary": summary,
                "fundamental_factors": fundamental_report,
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
                "market_status": current_market_status(),
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

    def _build_market_treemap_snapshot(
        max_industries: int = 200,
        max_stocks_per_industry: int = 500,
    ):
        """Build the cache-only industry → stock visualization payload.

        The treemap must remain safe to refresh from a browser every few
        seconds.  It reads only the active provider's already persisted
        catalogue and quote snapshot; unlike ``refresh=true`` market routes,
        this function never calls a provider or consumes AShareHub quota.
        """

        active_runtime = runtime()
        instruments = cached_provider_instruments()
        # Read all latest rows for this provider in one SQLite query instead
        # of constructing a very large ``IN (...)`` clause.  The catalogue
        # join/filter below still controls which securities can become tiles;
        # this remains cache-only and avoids SQLite bind-variable limits when
        # an authorized full-market catalogue is present.
        quotes = current_provider_items(
            active_runtime.repository.get_latest_quotes(
                None,
                source_prefix=active_runtime.provider.name,
            )
        )
        payload = build_market_treemap(
            instruments,
            quotes,
            max_industries=max_industries,
            max_stocks_per_industry=max_stocks_per_industry,
            source=active_runtime.provider.name,
        )
        # Expose quote age separately from the request time.  A local
        # cache hit is useful for the dashboard, but it must not be mistaken
        # for a fresh provider tick or a provider WebSocket stream.
        checked_at = now_market_time()
        as_of = payload.get("as_of")
        quote_age_seconds = None
        freshness_state = "missing"
        if as_of:
            try:
                quote_age_seconds = max(
                    0.0,
                    (checked_at - parse_market_time(as_of)).total_seconds(),
                )
                threshold = active_runtime.quote_stale_after_seconds
                freshness_state = (
                    "fresh"
                    if threshold is None or quote_age_seconds <= float(threshold)
                    else "stale"
                )
            except (TypeError, ValueError, OverflowError):
                freshness_state = "unknown"
        payload["freshness"] = {
            "state": freshness_state,
            "as_of": as_of,
            "checked_at": checked_at.isoformat(),
            "age_seconds": quote_age_seconds,
            "stale_after_seconds": active_runtime.quote_stale_after_seconds,
            "source": "latest_persisted_quote",
        }
        payload["market_status"] = current_market_status()
        payload["provider"] = active_runtime.provider.name
        payload["universe_scope"] = "cached_catalogue"
        payload["updated_at"] = now_market_time()
        return jsonable(payload)

    app.state.market_treemap_snapshot_builder = _build_market_treemap_snapshot

    @app.get("/api/v1/market/treemap")
    @app.get("/api/v1/market/electronic-cloud", include_in_schema=False)
    @app.get("/api/v1/sectors/treemap", include_in_schema=False)
    async def market_treemap(
        max_industries: int = Query(default=200, ge=1, le=2000),
        max_stocks_per_industry: int = Query(default=500, ge=1, le=5000),
    ):
        """Return a cache-only electronic-cloud industry → stock hierarchy.

        The result is intentionally bounded and short-lived cached.  Querying
        this route is always local SQLite work (``upstream_requests=0``), so a
        dashboard reload cannot spend the provider's daily request budget.
        ``value``/``size`` is turnover-oriented, not market capitalisation;
        clients should display the accompanying ``size_semantics`` label.  The
        ``electronic-cloud`` alias makes the visualization contract explicit,
        while ``treemap`` remains the stable compatibility path.
        """

        now = time.monotonic()
        cached = app.state.market_treemap_cache_payload
        # Query bounds are part of the cache key.  Keep a tiny per-process
        # single-entry cache to avoid retaining arbitrary user input.
        cache_key = (int(max_industries), int(max_stocks_per_industry))
        if (
            cached is not None
            and cached.get("_cache_key") == cache_key
            and now < app.state.market_treemap_cache_expires_at
        ):
            return {key: value for key, value in cached.items() if key != "_cache_key"}

        async with app.state.market_treemap_cache_lock:
            now = time.monotonic()
            cached = app.state.market_treemap_cache_payload
            if (
                cached is not None
                and cached.get("_cache_key") == cache_key
                and now < app.state.market_treemap_cache_expires_at
            ):
                return {key: value for key, value in cached.items() if key != "_cache_key"}
            payload = await asyncio.to_thread(
                app.state.market_treemap_snapshot_builder,
                int(max_industries),
                int(max_stocks_per_industry),
            )
            payload["_cache_key"] = cache_key
            app.state.market_treemap_cache_payload = payload
            app.state.market_treemap_cache_expires_at = (
                time.monotonic() + MARKET_TREEMAP_CACHE_SECONDS
            )
            return {key: value for key, value in payload.items() if key != "_cache_key"}

    def quote_refresh_metadata(
        result: Optional[QuoteCollectionResult],
        items: Sequence[Quote],
        requested: bool,
        catalogue_operations: int = 0,
    ) -> Dict[str, Any]:
        """Describe the refresh attempt separately from the durable view.

        No provider exposes request-local HTTP receipts here. Global budget
        deltas include concurrent history/catalogue requests and must not be
        presented as the cost of this operation.
        """

        quote_operations = result.provider_operations if result is not None else 0
        operations = quote_operations + catalogue_operations
        metadata = {
            "served_from": "sqlite_cache",
            "refresh_requested": requested,
            "provider_operations": operations,
            "quote_provider_operations": quote_operations,
            "catalogue_provider_operations": catalogue_operations,
            "upstream_requests": (
                0 if not operations or runtime().provider.name == "mock" else None
            ),
            "upstream_requests_scope": "request_local_http_attempts",
            "refresh_result": None,
        }
        if result is not None:
            accepted = {item.symbol for item in result.quotes}
            received = set(result.received_symbols)
            missing = [symbol for symbol in result.requested_symbols if symbol not in received]
            fallback = [item.symbol for item in items if item.symbol not in accepted]
            metadata["refresh_result"] = {
                "status": (
                    "empty" if not accepted else
                    "partial" if missing or result.stale_symbols else "complete"
                ),
                "requested_symbols": list(result.requested_symbols),
                "received_symbols": list(result.received_symbols),
                "accepted_symbols": [item.symbol for item in result.quotes],
                "missing_symbols": missing,
                "stale_symbols": list(result.stale_symbols),
                "cache_fallback_symbols": fallback,
            }
        return metadata

    def mark_legacy_refresh_response(response: Response, requested: bool) -> None:
        """Mark synchronous ``GET ...?refresh=true`` as compatibility-only.

        The old query flag is intentionally retained for existing dashboards,
        but a GET with side effects is not a durable workflow.  Advertise the
        asynchronous quote-refresh resource through standard response headers
        so new clients can migrate without breaking older clients.
        """

        if not requested:
            return
        response.headers["Deprecation"] = "true"
        response.headers["Link"] = (
            '</api/v1/quote-refresh/jobs>; rel="alternate"; '
            'title="asynchronous quote refresh job"'
        )

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
        http_response: Response = None,
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
                else await asyncio.to_thread(cached_provider_instruments)
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
        refresh_result = None
        if symbols and refresh:
            refresh_result = await runtime().monitor.collector.collect_quote_snapshot(
                symbols, relaxed=True
            )
        if symbols:
            quotes = current_provider_items(
                await asyncio.to_thread(
                    runtime().repository.get_latest_quotes,
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
            **quote_refresh_metadata(
                refresh_result, quotes, refresh,
                catalogue_operations=int(refresh and quote_scope != "configured_symbols"),
            ),
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
            response["market_status"] = current_market_status()
        # Expose cache-only provider response-shape diagnostics alongside the
        # paginated payload.  This is especially useful after an explicit
        # ``refresh=true``: callers can tell whether an upstream response was
        # partial or rows were rejected by validation without inspecting the
        # process-wide system endpoint.  Keep this aggregate symbol-safe and
        # avoid triggering another provider request.
        provider_status_reader = getattr(provider, "realtime_status", None)
        if provider.name == "asharehub" and provider_status_reader is not None:
            realtime_snapshot = dict(provider_status_reader)
            missing_symbols = realtime_snapshot.get("last_quote_missing_symbols") or []
            # The configured basket can be larger than the last request (for
            # example the collector may refresh a bounded slice while the
            # dashboard asks for one symbol).  Use the provider's per-request
            # count when available; the configured-basket size is only a
            # compatibility fallback for older providers.
            requested_count = realtime_snapshot.get("last_quote_requested_symbols")
            if requested_count is None:
                requested_count = realtime_snapshot.get("requested_symbols")
            parsed_count = realtime_snapshot.get("last_quote_parsed_symbols")
            try:
                coverage_ratio = (
                    round(float(parsed_count) / float(requested_count), 6)
                    if parsed_count is not None and float(requested_count or 0) > 0
                    else None
                )
            except (TypeError, ValueError):
                coverage_ratio = None
            response["provider_diagnostics"] = {
                "quote_response_quality": {
                    "response_rows": realtime_snapshot.get("last_quote_response_rows"),
                    "parsed_symbols": realtime_snapshot.get("last_quote_parsed_symbols"),
                    "dropped_rows": realtime_snapshot.get("last_quote_dropped_rows"),
                    "invalid_rows": realtime_snapshot.get("last_quote_invalid_rows"),
                    "unexpected_rows": realtime_snapshot.get("last_quote_unexpected_rows"),
                    "duplicate_rows": realtime_snapshot.get("last_quote_duplicate_rows"),
                    "conflict_rows": realtime_snapshot.get("last_quote_conflict_rows"),
                    "missing_symbols": list(missing_symbols)
                    if isinstance(missing_symbols, (list, tuple))
                    else [],
                    "missing_count": len(missing_symbols)
                    if isinstance(missing_symbols, (list, tuple))
                    else 0,
                    "requested_symbols": requested_count,
                    "coverage_ratio": coverage_ratio,
                    "as_of": realtime_snapshot.get("last_quote_success_at"),
                }
            }
        if http_response is not None:
            mark_legacy_refresh_response(http_response, refresh)
        return response

    @app.get("/api/v1/quotes")
    async def quotes(
        symbols: Optional[str] = None,
        http_response: Response = None,
        refresh: bool = Query(
            default=False,
            description="允许调用 provider；仍遵守其缓存和预算，不保证一次 HTTP 请求",
        ),
    ):
        requested = runtime().symbols if not symbols else [item.strip() for item in symbols.split(",") if item.strip()]
        try:
            normalized = [normalize_symbol(symbol) for symbol in requested]
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        active_runtime = runtime()

        def load_quote_cache():
            # Batch quote reads are on the dashboard hot path.  SQLite is
            # synchronous by design, so dispatch the complete cache lookup to
            # a worker and keep health/WebSocket traffic responsive while a
            # WAL checkpoint or cold disk is busy.
            return current_provider_items(
                active_runtime.repository.get_latest_quotes(
                    normalized, source_prefix=active_runtime.provider.name
                )
            )

        refresh_result = None
        if refresh and normalized:
            refresh_result = await active_runtime.monitor.collector.collect_quote_snapshot(
                normalized, relaxed=True
            )
        items = await asyncio.to_thread(load_quote_cache)
        payload_items = [
            quote_payload(item, active_runtime.instrument_name(item.symbol))
            for item in items
        ]
        received_symbols = tuple(item.symbol for item in items)
        received_set = set(received_symbols)
        snapshot_as_of = max((item.timestamp for item in items), default=None)
        stale_count = sum(
            bool(item.get("stale")) and not bool(item.get("future_timestamp"))
            for item in payload_items
        )
        future_timestamp_count = sum(
            bool(item.get("future_timestamp")) for item in payload_items
        )
        if http_response is not None:
            mark_legacy_refresh_response(http_response, refresh)
        return {
            "items": payload_items,
            "count": len(payload_items),
            "requested_count": len(dict.fromkeys(normalized)),
            "fresh_count": len(payload_items) - stale_count - future_timestamp_count,
            "stale_count": stale_count,
            "future_timestamp_count": future_timestamp_count,
            "requested_symbols": list(dict.fromkeys(normalized)),
            "received_symbols": list(received_symbols),
            "missing_symbols": [
                symbol for symbol in dict.fromkeys(normalized) if symbol not in received_set
            ],
            "as_of": snapshot_as_of.isoformat() if snapshot_as_of is not None else None,
            "source": active_runtime.provider.name,
            **quote_refresh_metadata(refresh_result, items, refresh),
            "market_status": current_market_status(),
        }

    @app.get("/api/v1/quotes/{symbol}")
    async def quote(
        symbol: str,
        http_response: Response = None,
        refresh: bool = Query(
            default=False,
            description="显式允许向行情供应商请求该股票的最新快照",
        ),
    ):
        try:
            normalized = normalize_symbol(symbol)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        active_runtime = runtime()

        def load_quote_cache():
            """Read the single quote off the ASGI event loop.

            SQLite is local, but a busy WAL checkpoint or a cold disk can
            still block long enough to delay health checks and WebSocket
            heartbeats.  Detail reads are cache-only, so dispatch the whole
            repository operation to a worker just like the batch endpoint's
            cache path.
            """

            items = current_provider_items(
                active_runtime.repository.get_latest_quotes(
                    [normalized], source_prefix=active_runtime.provider.name
                )
            )
            return items[0] if items else None

        refresh_result = None
        if refresh:
            refresh_result = await active_runtime.monitor.collector.collect_quote_snapshot(
                [normalized], relaxed=True
            )
        item = await asyncio.to_thread(load_quote_cache)
        if item is None:
            raise HTTPException(status_code=404, detail="quote not found")
        payload = quote_payload(item, active_runtime.instrument_name(normalized))
        # Keep the historical flat quote fields for compatibility, while
        # making the detail response contract as explicit as the batch route:
        # callers can distinguish a fresh cache hit from an old-but-valid
        # observation without inferring semantics from ``updated_at``.
        payload.update(
            {
                "requested_symbol": normalized,
                "as_of": item.timestamp.isoformat(),
                # quote_dict() classifies provider timestamps consistently with
                # runtime.quote_freshness, including future-dated rows.
                "freshness": payload.get("freshness")
                or ("stale" if payload.get("stale") else "fresh"),
                **quote_refresh_metadata(refresh_result, [item], refresh),
                "market_status": current_market_status(),
            }
        )
        if http_response is not None:
            mark_legacy_refresh_response(http_response, refresh)
        return payload

    @app.get("/api/v1/bars/{symbol}")
    async def bars(
        symbol: str,
        http_response: Response = None,
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
        served_from = "sqlite_cache"
        collection_batch_id = None
        collection_transport = None
        if len(items) < limit and (
            runtime().provider.name == "mock" or refresh
        ):
            # Real providers are cache-first by default: an ordinary chart GET
            # must not spend scarce quota.  Callers must opt into refresh=true.
            # Route explicit refreshes through the same collection service used
            # by the background monitor.  This keeps the compatibility `bars`
            # projection and the v3 collection-batch/observation ledger in one
            # atomic write, preserving transport evidence for audit/research.
            collector = MarketDataCollectionService(
                provider=runtime().provider,
                repository=repo,
                event_bus=runtime().event_bus,
                bar_interval=selected_interval,
                bar_limit=limit,
                collect_bars=True,
            )
            refresh_result = await collector.collect_bar_batch(
                (normalized,),
                publish_events=False,
                provider_options={
                    "start": start_time,
                    "end": end_time,
                },
            )
            collection_batch_id = refresh_result.batch_id
            collection_transport = (
                refresh_result.transport.value
                if refresh_result.transport is not None
                else None
            )
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
            served_from = "provider_refresh"
        # Keep the legacy bar projection for compatibility, but expose how
        # much immutable observation evidence backs this response.  Matching
        # is by period and source (not merely by row count), so unrelated
        # revisions cannot make a stale projection look replayable.
        data_provenance = _bar_projection_provenance(
            repo, {normalized: items}, interval=selected_interval
        )
        if http_response is not None:
            mark_legacy_refresh_response(http_response, refresh)
        return {
            "symbol": normalized,
            "interval": selected_interval,
            "items": [bar_dict(item) for item in items],
            "count": len(items),
            "served_from": served_from,
            "refresh_requested": refresh,
            "collection_batch_id": collection_batch_id,
            "collection_transport": collection_transport,
            "observation_count": data_provenance["immutable_observation_count"],
            "observation_total": data_provenance["immutable_observation_count"],
            "observation_authority": (
                "immutable_observations"
                if data_provenance["lineage_complete"]
                else "legacy_bars_projection"
            ),
            "data_provenance": data_provenance,
        }

    async def build_analysis(symbol: str, selected_interval: str, limit: int) -> Dict[str, Any]:
        """Read cached bars and lazily fill the requested analysis window."""

        repo = runtime().repository
        items = current_provider_items(
            await asyncio.to_thread(
                repo.get_bars,
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
                await asyncio.to_thread(repo.insert_bars, generated)
                items = current_provider_items(
                    await asyncio.to_thread(
                        repo.get_bars,
                        symbol,
                        selected_interval,
                        limit=limit,
                        source_prefix=runtime().provider.name,
                    )
                )
        quotes = current_provider_items(
            await asyncio.to_thread(
                repo.get_latest_quotes,
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
        analysis_time = now_market_time()
        # Load once: validation, conversion and provenance are derived from
        # the same immutable observation objects, with one knowledge cutoff.
        items, provenance = await asyncio.to_thread(
            select_analysis_bars, repo, symbol, selected_interval, items,
            as_of=analysis_time,
        )
        snapshot = await asyncio.to_thread(
            analysis_service.analyze,
            symbol,
            items,
            quote_item,
            name=runtime().instrument_name(symbol),
            interval=selected_interval,
            now=analysis_time,
            stale_after_seconds=runtime().quote_stale_after_seconds,
        )
        payload = jsonable(snapshot)
        # Generic analysis historically consumed the compatibility ``bars``
        # projection without exposing whether each row had immutable lineage.
        # Keep the analysis values backward compatible, but make research
        # readiness explicit so a complete-looking indicator set cannot be
        # mistaken for reproducible evidence.
        payload["data_provenance"] = provenance
        quality = payload.get("data_quality")
        if isinstance(quality, dict):
            quality["observation_authority"] = provenance["calculation_input"]
            quality["lineage_complete"] = provenance["lineage_complete"]
            quality["quality_complete"] = provenance["quality_complete"]
            failures = []
            if not provenance["quality_complete"]:
                failures.append("immutable_observation_lineage_incomplete")
            if quality.get("status") != "ok":
                failures.append("analysis_quality_{}".format(quality.get("status", "unknown")))
            if quality.get("stale"):
                failures.append("analysis_data_stale")
            if quality.get("missing_indicators"):
                failures.append("analysis_indicators_incomplete")
            if quality.get("future_timestamp"):
                failures.append("analysis_future_timestamp")
            if quality.get("calendar_out_of_coverage"):
                failures.append("analysis_calendar_out_of_coverage")
            quality["analysis_ready"] = not failures
            quality["analysis_readiness_failures"] = failures
            # Locally accepted inputs permit diagnostic research, not a
            # decision-grade claim about calendar, PIT universe or adjustment.
            quality["decision_ready"] = False
            quality["decision_ready_reason"] = (
                failures[0] if failures else "decision_evidence_unverified"
            )
            if failures:
                recommendation = payload.get("recommendation")
                if isinstance(recommendation, dict):
                    # Preserve the raw rule output for audit, but never expose
                    # it as the research action when the underlying bars are
                    # not fully backed by accepted immutable observations.
                    raw_action = recommendation.get("action")
                    recommendation["model_action"] = raw_action
                    recommendation["action"] = "insufficient_data"
                    recommendation["label"] = "研究数据条件未满足"
                    reasons = recommendation.get("reason")
                    if not isinstance(reasons, list):
                        reasons = []
                    recommendation["reason"] = list(reasons) + [
                        "数据血缘、时效或指标条件未满足，暂不形成方向性研究动作"
                    ]
                payload["research_action"] = "insufficient_data"
            else:
                payload["research_action"] = (
                    payload.get("recommendation", {}).get("action")
                    if isinstance(payload.get("recommendation"), dict)
                    else "insufficient_data"
                )
        return payload

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
        offset: int = Query(default=0, ge=0, le=1_000_000),
    ):
        try:
            normalized = normalize_symbol(symbol) if symbol else None
            alert_level = AlertLevel(level) if level else None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        try:
            # Alert pages can scan a large SQLite table and may contend with
            # the collector's WAL writer.  Keep the synchronous repository
            # snapshot off the ASGI event loop so health/WebSocket traffic
            # remains responsive under load.
            items, total = await asyncio.to_thread(
                runtime().repository.query_alerts,
                normalized,
                alert_level,
                acknowledged,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {
            "items": [alert_dict(item) for item in items],
            "count": len(items),
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(items) < total,
        }

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
        # This is a synchronous SQLite write; never block the request loop on
        # the database lock while the collector is persisting a batch.
        acknowledged = await asyncio.to_thread(
            runtime().repository.acknowledge_alert, alert_id, value
        )
        if not acknowledged:
            raise HTTPException(status_code=404, detail="alert not found")
        return {"id": alert_id, "acknowledged": value}

    @app.get("/api/v1/signals")
    async def signals(
        symbol: Optional[str] = None,
        strategy: Optional[str] = None,
        action: Optional[str] = None,
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0, le=1_000_000),
    ):
        try:
            normalized = normalize_symbol(symbol) if symbol else None
            signal_action = SignalAction(action) if action else None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        try:
            items, total = await asyncio.to_thread(
                runtime().repository.query_signals,
                symbol=normalized,
                strategy=strategy,
                action=signal_action,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {
            "items": [signal_dict(item) for item in items],
            "count": len(items),
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(items) < total,
        }

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

        def strategy_parameters(strategy: Any) -> Dict[str, Any]:
            """Expose read-only effective thresholds without leaking internals.

            The registry is deliberately static (no dynamic imports).  This
            projection lets a research client reproduce the configured rule
            gates while keeping provider credentials and implementation
            details out of the public API.
            """

            fields = (
                "MODEL_VERSION",
                "interval",
                "fast_window",
                "slow_window",
                "min_history",
                "min_universe",
                "entry_percentile",
                "exit_percentile",
                "minimum_breadth",
                "maximum_annualized_volatility",
                "maximum_drawdown",
                "liquidity_floor_percentile",
                "corporate_action_gap",
                "trend_exit_days",
                "valid_sessions",
            )
            return {
                name.lower() if name.isupper() else name: getattr(strategy, name)
                for name in fields
                if hasattr(strategy, name)
            }

        def strategy_capabilities(strategy: Any) -> Dict[str, Any]:
            """Expose stable execution boundaries for research clients."""

            return {
                "signal_only": True,
                "orders_supported": False,
                "network_access": False,
                "parameter_schema_version": "1",
                "parameter_schema": list(strategy_parameter_schema(strategy)),
            }

        def strategy_identity(strategy: Any) -> Dict[str, Any]:
            """Expose a content-addressed implementation identity.

            This is intentionally a read-only descriptor containing package
            relative source units only.  A failure to hash a built-in source
            is disclosed as unavailable rather than replaced with its human
            model label, since labels cannot prove replay equivalence.
            """

            try:
                value = strategy_code_identity(strategy.name)
            except (FileNotFoundError, ValueError, OSError):
                return {"status": "unavailable"}
            return dict(value)

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
                    "parameters": strategy_parameters(strategy),
                    "code_identity": strategy_identity(strategy),
                    "capabilities": strategy_capabilities(strategy),
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
            bars_by_symbol: Dict[str, Sequence[Any]] = {}
            for symbol in active_runtime.symbols:
                symbol_bars = active_runtime.repository.get_bars(
                    symbol,
                    interval="1d",
                    limit=required,
                    source_prefix=active_runtime.provider.name,
                )
                bars_by_symbol[symbol] = symbol_bars
                cached_bars.extend(symbol_bars)
            payload = DailyBarDataQualityService(
                required,
                calendar=active_runtime.current_trading_calendar(),
            ).audit(
                cached_bars,
                expected_symbols=active_runtime.symbols,
                as_of=now_market_time(),
            ).to_dict()
            # The quality service audits the compatibility ``bars`` projection.
            # Pair it with an explicit lineage report so ``healthy=true`` is
            # never mistaken for independently reproducible/authoritative
            # research data.  This remains cache-only and bounded by the same
            # per-symbol window as the audit itself.
            provenance = _bar_projection_provenance(
                active_runtime.repository,
                bars_by_symbol,
                interval="1d",
            )
            payload["data_provenance"] = provenance
            payload["quality_assurance"] = {
                "structural_checks": True,
                "immutable_lineage_complete": bool(
                    provenance.get("lineage_complete")
                ),
                "immutable_quality_complete": bool(
                    provenance.get("quality_complete")
                ),
                "authoritative": False,
                "decision_ready": False,
                "notice": (
                    "日线质量报告校验 SQLite bars 投影；仅当独立 immutable observation "
                    "血缘完整且质量可接受时，才可用于可复现研究。"
                ),
            }
            return payload

        payload = await asyncio.to_thread(build_quality_snapshot)
        payload["served_from"] = "sqlite_cache"
        payload["upstream_requests"] = 0
        return payload

    @app.get("/api/v1/system/status")
    async def system_status():
        return await asyncio.to_thread(runtime().system_status)

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics():
        """Expose a small Prometheus-compatible local health snapshot.

        This endpoint is deliberately derived from ``system_status`` only:
        scraping it never invokes a market-data provider and never includes
        symbols, API keys, or other potentially sensitive payloads.
        """

        status = await asyncio.to_thread(runtime().system_status)
        provider = status.get("provider") or {}
        startup = status.get("startup") or {}
        data_readiness = status.get("data_readiness") or {}
        collector = status.get("collector") or {}
        cycle_stats = collector.get("cycle_stats") or {}
        history = startup.get("history_bootstrap") or {}
        jobs = collector.get("jobs") or {}
        job_statuses = jobs.get("by_status") or {}
        # Quote freshness is an operator-facing SLO signal.  Keep this
        # aggregate intentionally symbol-free so Prometheus cardinality and
        # data licensing exposure do not grow with the watchlist.  It is
        # derived from ``system_status`` and therefore remains cache-only.
        quote_freshness = provider.get("quote_freshness") or {}
        quote_response_quality = provider.get("quote_response_quality") or {}
        lines = [
            _prometheus_metric(
                "quant_monitor_up",
                1 if status.get("healthy") else 0,
                help_text="Whether the market monitor is healthy (1/0).",
            ),
            _prometheus_metric(
                "quant_monitor_startup_ready",
                1 if startup.get("serving_ready") else 0,
                help_text="Whether the HTTP service has completed startup.",
            ),
            _prometheus_metric(
                "quant_monitor_research_ready",
                1 if startup.get("research_ready") else 0,
                help_text="Whether the configured research history is ready.",
            ),
            _prometheus_metric(
                "quant_monitor_data_ready",
                1 if data_readiness.get("ready") else 0,
                help_text="Whether the configured market-data universe is ready for the current market phase.",
            ),
            _prometheus_metric(
                "quant_monitor_collector_running",
                1 if collector.get("running") else 0,
                help_text="Whether the background collector is running.",
            ),
            _prometheus_metric(
                "quant_monitor_collector_iterations_total",
                collector.get("iteration", 0),
                help_text="Number of completed collector iterations since process start.",
                metric_type="counter",
            ),
            _prometheus_metric(
                "quant_monitor_collector_cycles_total",
                cycle_stats.get("total", 0),
                help_text="Number of collector cycles started since process start.",
                metric_type="counter",
            ),
            _prometheus_metric(
                "quant_monitor_collector_cycles_succeeded_total",
                cycle_stats.get("succeeded", 0),
                help_text="Number of collector cycles completed successfully since process start.",
                metric_type="counter",
            ),
            _prometheus_metric(
                "quant_monitor_collector_cycles_failed_total",
                cycle_stats.get("failed", 0),
                help_text="Number of collector cycles that failed since process start.",
                metric_type="counter",
            ),
            _prometheus_metric(
                "quant_monitor_collector_cycles_partial_total",
                cycle_stats.get("partial", 0),
                help_text="Number of collector cycles with partial feed issues since process start.",
                metric_type="counter",
            ),
            _prometheus_metric(
                "quant_monitor_collector_cycles_cancelled_total",
                cycle_stats.get("cancelled", 0),
                help_text="Number of collector cycles cancelled before completion since process start.",
                metric_type="counter",
            ),
            _prometheus_metric(
                "quant_monitor_collector_jobs_active",
                jobs.get("active", 0),
                help_text="Number of non-terminal manual collection jobs.",
            ),
            _prometheus_metric(
                "quant_monitor_collector_jobs_total",
                jobs.get("total", 0),
                help_text="Number of retained manual collection jobs.",
            ),
            _prometheus_metric(
                "quant_monitor_collector_jobs_queued",
                job_statuses.get("queued", 0),
                help_text="Number of queued manual collection jobs.",
            ),
            _prometheus_metric(
                "quant_monitor_collector_jobs_running",
                job_statuses.get("running", 0),
                help_text="Number of running manual collection jobs.",
            ),
            _prometheus_metric(
                "quant_monitor_collector_jobs_failed",
                job_statuses.get("failed", 0),
                help_text="Number of failed manual collection jobs retained.",
            ),
            _prometheus_metric(
                "quant_monitor_collector_jobs_succeeded",
                job_statuses.get("succeeded", 0),
                help_text="Number of succeeded manual collection jobs retained.",
            ),
            _prometheus_metric(
                "quant_monitor_collector_jobs_cancelled",
                job_statuses.get("cancelled", 0),
                help_text="Number of cancelled manual collection jobs retained.",
            ),
            _prometheus_metric(
                "quant_monitor_uptime_seconds",
                status.get("uptime_seconds", 0),
                help_text="Process uptime in seconds.",
            ),
            _prometheus_metric(
                "quant_monitor_last_collection_age_seconds",
                (float(status["last_collection_age_ms"]) / 1000.0)
                if status.get("last_collection_age_ms") is not None
                else -1,
                help_text="Age of the latest collection in seconds; -1 when unavailable.",
            ),
            _prometheus_metric(
                "quant_monitor_quote_fresh_symbols",
                quote_freshness.get("fresh_symbols"),
                help_text="Number of configured symbols with a quote within the freshness threshold.",
            ),
            _prometheus_metric(
                "quant_monitor_quote_stale_symbols",
                quote_freshness.get("stale_symbols"),
                help_text="Number of configured symbols whose latest quote exceeds the freshness threshold.",
            ),
            _prometheus_metric(
                "quant_monitor_quote_missing_symbols",
                quote_freshness.get("missing_symbols"),
                help_text="Number of configured symbols without a latest quote.",
            ),
            _prometheus_metric(
                "quant_monitor_quote_future_timestamp_symbols",
                quote_freshness.get("future_timestamp_symbols"),
                help_text="Number of configured symbols with a future-dated quote timestamp.",
            ),
            _prometheus_metric(
                "quant_monitor_quote_oldest_age_seconds",
                quote_freshness.get("oldest_age_seconds")
                if quote_freshness.get("oldest_age_seconds") is not None
                else -1,
                help_text="Age of the oldest in-memory quote in seconds; -1 when unavailable.",
            ),
            _prometheus_metric(
                "quant_monitor_subscriptions",
                status.get("subscriptions", 0),
                help_text="Number of configured symbols (not exported individually).",
            ),
            _prometheus_metric(
                "quant_monitor_websocket_clients",
                status.get("websocket_clients", 0),
                help_text="Number of connected local WebSocket clients.",
            ),
            _prometheus_metric(
                "quant_monitor_history_bootstrap_ready",
                history.get("ready", 0),
                help_text="Number of symbols with ready historical bootstrap data.",
            ),
            _prometheus_metric(
                "quant_monitor_history_bootstrap_total",
                history.get("total", 0),
                help_text="Number of symbols requested for historical bootstrap.",
            ),
            _prometheus_metric(
                "quant_monitor_history_bootstrap_failed",
                len(history.get("failed") or {}),
                help_text="Number of historical bootstrap failures.",
            ),
        ]
        # AShareHub returns a top-level array whose rows are validated before
        # entering the domain model.  Export only aggregate counts/coverage;
        # per-symbol values belong in the local JSON status and would create
        # high-cardinality or licensing-sensitive Prometheus labels.
        if isinstance(quote_response_quality, dict):
            lines.extend(
                [
                    _prometheus_metric(
                        "quant_monitor_quote_response_rows",
                        quote_response_quality.get("response_rows")
                        if quote_response_quality.get("response_rows") is not None
                        else -1,
                        help_text="Rows returned by the latest provider quote response; -1 when unavailable.",
                    ),
                    _prometheus_metric(
                        "quant_monitor_quote_parsed_symbols",
                        quote_response_quality.get("parsed_symbols")
                        if quote_response_quality.get("parsed_symbols") is not None
                        else -1,
                        help_text="Symbols parsed from the latest provider quote response; -1 when unavailable.",
                    ),
                    _prometheus_metric(
                        "quant_monitor_quote_dropped_rows",
                        quote_response_quality.get("dropped_rows")
                        if quote_response_quality.get("dropped_rows") is not None
                        else -1,
                        help_text="Rows dropped during provider quote validation; -1 when unavailable.",
                    ),
                    _prometheus_metric(
                        "quant_monitor_quote_invalid_rows",
                        quote_response_quality.get("invalid_rows")
                        if quote_response_quality.get("invalid_rows") is not None
                        else -1,
                        help_text="Requested-symbol rows rejected during provider quote validation; -1 when unavailable.",
                    ),
                    _prometheus_metric(
                        "quant_monitor_quote_unexpected_rows",
                        quote_response_quality.get("unexpected_rows")
                        if quote_response_quality.get("unexpected_rows") is not None
                        else -1,
                        help_text="Valid provider quote rows for symbols outside the requested basket; -1 when unavailable.",
                    ),
                    _prometheus_metric(
                        "quant_monitor_quote_duplicate_rows",
                        quote_response_quality.get("duplicate_rows")
                        if quote_response_quality.get("duplicate_rows") is not None
                        else -1,
                        help_text="Duplicate symbol rows in the latest provider quote response; -1 when unavailable.",
                    ),
                    _prometheus_metric(
                        "quant_monitor_quote_conflict_rows",
                        quote_response_quality.get("conflict_rows")
                        if quote_response_quality.get("conflict_rows") is not None
                        else -1,
                        help_text="Same-timestamp conflicting quote rows rejected by the provider; -1 when unavailable.",
                    ),
                    _prometheus_metric(
                        "quant_monitor_quote_response_missing_symbols",
                        quote_response_quality.get("missing_count")
                        if quote_response_quality.get("missing_count") is not None
                        else -1,
                        help_text="Requested symbols absent from the latest provider quote response; -1 when unavailable.",
                    ),
                    _prometheus_metric(
                        "quant_monitor_quote_response_coverage_ratio",
                        quote_response_quality.get("coverage_ratio")
                        if quote_response_quality.get("coverage_ratio") is not None
                        else -1,
                        help_text="Parsed/requested symbol coverage ratio for the latest provider quote response; -1 when unavailable.",
                    ),
                ]
            )
        labels = {"provider": provider.get("name", "unknown")}
        lines.append(
            _prometheus_metric(
                "quant_monitor_provider_info",
                1,
                help_text="Active market-data provider (labelled by provider name).",
                metric_type="gauge",
                labels={**labels, "state": provider.get("state", "unknown")},
            )
        )
        freshness_state = quote_freshness.get("state")
        if freshness_state:
            lines.append(
                _prometheus_metric(
                    "quant_monitor_quote_freshness_info",
                    1,
                    help_text="Current aggregate quote freshness state (labelled by provider and state).",
                    labels={**labels, "state": freshness_state},
                )
            )
        data_ready_reason = data_readiness.get("reason")
        if data_ready_reason:
            lines.append(
                _prometheus_metric(
                    "quant_monitor_data_readiness_info",
                    1,
                    help_text="Current aggregate market-data readiness reason.",
                    labels={**labels, "reason": data_ready_reason},
                )
            )
        quota = provider.get("quota")
        # Providers without a quota contract (for example mock/AKShare) must
        # not be represented as having a known, non-exhausted quota.
        if isinstance(quota, dict):
            lines.extend(
                [
                    _prometheus_metric(
                        "quant_monitor_provider_quota_used",
                        quota.get("used"),
                        help_text="Estimated provider requests used during the local quota day.",
                        labels=labels,
                    ),
                    _prometheus_metric(
                        "quant_monitor_provider_quota_remaining",
                        quota.get("remaining_soft"),
                        help_text="Estimated provider requests remaining under the local soft budget.",
                        labels=labels,
                    ),
                    _prometheus_metric(
                        "quant_monitor_provider_quota_exhausted",
                        1 if quota.get("exhausted") else 0,
                        help_text="Whether the provider quota is exhausted (1/0).",
                        labels=labels,
                    ),
                ]
            )
        body = "".join(item for item in lines if item)
        return PlainTextResponse(
            body,
            media_type="text/plain; version=0.0.4",
            headers={"Cache-Control": "no-cache"},
        )

    async def _quote_refresh_symbols_from_request(request: Request) -> Tuple[str, ...]:
        """Parse the bounded quote-refresh basket without contacting a provider."""

        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="request body must be a JSON object")
        raw = payload.get("symbols")
        if raw is None:
            values = list(runtime().symbols)
        elif isinstance(raw, str):
            values = [item.strip() for item in raw.split(",") if item.strip()]
        elif isinstance(raw, (list, tuple)):
            values = [str(item).strip() for item in raw if str(item).strip()]
        else:
            raise HTTPException(status_code=422, detail="symbols must be a comma-separated string or array")
        if not values:
            raise HTTPException(status_code=422, detail="at least one symbol is required")
        if len(values) > 5000:
            raise HTTPException(status_code=422, detail="at most 5000 symbols may be refreshed")
        try:
            return tuple(dict.fromkeys(normalize_symbol(item) for item in values))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.post("/api/v1/quote-refresh/jobs", status_code=202)
    async def submit_quote_refresh_job(request: Request):
        """Queue a quote-only provider refresh without blocking the GET path."""

        symbols = await _quote_refresh_symbols_from_request(request)
        try:
            job = await runtime().monitor.submit_quote_refresh_job(
                symbols,
                request.headers.get("Idempotency-Key"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(
                status_code=409,
                detail=str(exc),
                headers={"Retry-After": "1"},
            )
        return JSONResponse(
            status_code=202,
            content=jsonable(
                {
                    **(job or {}),
                    "served_from": "in_process_job_manager",
                    "upstream_requests": "provider_dependent",
                }
            ),
        )

    @app.get("/api/v1/quote-refresh/jobs")
    async def list_quote_refresh_jobs(
        limit: int = Query(default=20, ge=1, le=100),
    ):
        payload = await asyncio.to_thread(
            runtime().monitor.collection_jobs, limit, "quote_refresh"
        )
        payload["served_from"] = "in_process_job_manager"
        return jsonable(payload)

    @app.get("/api/v1/quote-refresh/jobs/{job_id}")
    async def get_quote_refresh_job(job_id: str):
        job = await asyncio.to_thread(runtime().monitor.collection_job, job_id)
        if job is None or job.get("operation") != "quote_refresh":
            raise HTTPException(status_code=404, detail="quote refresh job not found")
        return jsonable({**job, "served_from": "in_process_job_manager"})

    @app.post("/api/v1/quote-refresh/jobs/{job_id}/cancel")
    async def cancel_quote_refresh_job(job_id: str):
        job = await runtime().monitor.cancel_collection_job(job_id)
        if job is None or job.get("operation") != "quote_refresh":
            raise HTTPException(status_code=404, detail="quote refresh job not found")
        return jsonable({**job, "served_from": "in_process_job_manager"})

    @app.post("/api/v1/collector/jobs", status_code=202)
    async def submit_collection_job(request: Request):
        """Queue one explicit monitoring cycle.

        This endpoint is intentionally opt-in: normal startup polling remains
        the default, while a manual refresh is observable and cancellable.
        The returned job only uses the active in-process provider and may
        consume an upstream request when the provider cache is stale.
        """

        idempotency_key = request.headers.get("Idempotency-Key")
        try:
            job = await runtime().monitor.submit_collection_job(idempotency_key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            # A concurrent manual collection is expected back-pressure, not
            # an opaque server failure.  Tell automation when it is safe to
            # retry so repeated clicks do not create a request storm.
            raise HTTPException(
                status_code=409,
                detail=str(exc),
                headers={"Retry-After": "1"},
            )
        return JSONResponse(
            status_code=202,
            content=jsonable(
                {
                    **(job or {}),
                    "served_from": "in_process_job_manager",
                    "upstream_requests": "provider_dependent",
                }
            ),
        )

    @app.get("/api/v1/collector/jobs")
    async def list_collection_jobs(
        limit: int = Query(default=20, ge=1, le=100),
    ):
        """List recent explicit collection jobs without provider I/O."""

        # Job snapshots are read-mostly, but a cold process may restore up to
        # 1,000 durable rows and terminalise interrupted jobs.  The restore
        # path uses SQLite's synchronous connection, so keep it off the ASGI
        # event loop just like the other read-only research endpoints.  This
        # prevents a locked database or a large job ledger from delaying
        # health checks and WebSocket heartbeats.
        payload = await asyncio.to_thread(runtime().monitor.collection_jobs, limit)
        return jsonable(payload)

    @app.get("/api/v1/collector/jobs/{job_id}")
    async def get_collection_job(job_id: str):
        # ``collection_job`` may hydrate a missing in-memory entry from
        # SQLite; do not perform that synchronous lookup on the event loop.
        job = await asyncio.to_thread(runtime().monitor.collection_job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="collection job not found")
        return jsonable(job)

    @app.post("/api/v1/collector/jobs/{job_id}/cancel")
    async def cancel_collection_job(job_id: str):
        job = await runtime().monitor.cancel_collection_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="collection job not found")
        return jsonable(job)

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
            def load_websocket_snapshot() -> Dict[str, Any]:
                """Build the initial snapshot away from the ASGI event loop.

                The first WebSocket frame used to perform several synchronous
                SQLite reads plus ``system_status`` inline.  A cold WAL
                checkpoint or a large persisted alert/signal ledger could then
                delay heartbeats and unrelated HTTP requests while a client
                was only connecting.  Keep the WebSocket handshake itself on
                the loop, but execute the complete cache-only snapshot in one
                worker so its reads and status aggregation share the same
                consistent runtime view.
                """

                active_runtime = runtime()
                return {
                    "overview": {"market_status": current_market_status()},
                    "watchlist": {
                        "items": [
                            quote_payload(item, active_runtime.instrument_name(item.symbol))
                            for item in current_provider_items(
                                active_runtime.repository.get_latest_quotes(
                                    active_runtime.symbols,
                                    source_prefix=active_runtime.provider.name,
                                )
                            )
                        ]
                    },
                    "alerts": {
                        "items": [
                            alert_dict(item)
                            for item in active_runtime.repository.list_alerts(limit=20)
                        ]
                    },
                    "signals": {
                        "items": [
                            signal_dict(item)
                            for item in active_runtime.repository.list_signals(limit=20)
                        ]
                    },
                    "system": active_runtime.system_status(),
                }

            snapshot_data = await asyncio.to_thread(load_websocket_snapshot)
            await websocket.send_json(
                {
                    "type": "snapshot",
                    "timestamp": now_market_time().isoformat(),
                    "data": snapshot_data,
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
