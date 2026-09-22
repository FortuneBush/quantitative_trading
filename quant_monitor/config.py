"""Application configuration with JSON defaults and environment overrides."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from quant_monitor.domain import normalize_symbol
from quant_monitor.universes import BANK_STOCK_SYMBOLS


ENV_PREFIX = "QUANT_MONITOR_"
DEFAULT_DATABASE_PATH = "data/quant_monitor_{provider}.db"


@dataclass(frozen=True)
class Settings:
    app_name: str = "A股量化数据监控台"
    host: str = "127.0.0.1"
    port: int = 8000
    # Empty means "derive the safe default from data_provider".  This avoids
    # direct Settings(data_provider="mock") construction ever inheriting the
    # AShareHub database. Explicit caller paths remain authoritative.
    database_path: str = ""
    # Bound SQLite initialization so a stale lock cannot make ASGI startup
    # appear to hang forever.  The runtime emits a phase heartbeat while it
    # waits and raises a safe, actionable error when this deadline expires.
    startup_database_timeout_seconds: float = 30.0
    # Upper bound for declared HTTP request bodies.  This protects the local
    # API from accidentally posting an unbounded JSON/backtest payload while
    # keeping the limit high enough for normal research artifacts.  Requests
    # without Content-Length (including chunked/multipart uploads) are left to
    # the endpoint's own parser so this guard remains backwards compatible.
    api_max_request_body_bytes: int = 8 * 1024 * 1024
    # AShareHub is the primary real feed. AllTick and AKShare remain available
    # as compatibility providers and mock remains the deterministic offline source.
    data_provider: str = "asharehub"
    poll_interval_seconds: float = 60.0
    bar_interval: str = "1d"
    history_bar_count: int = 240
    watchlist: List[str] = field(
        default_factory=lambda: list(BANK_STOCK_SYMBOLS)
    )
    # Direct ``Settings(...)`` construction keeps an explicit watchlist.  The
    # shipped JSON profile opts into the dynamic bank universe.
    universe_mode: str = "watchlist"
    universe_industry: str = "银行"
    log_level: str = "INFO"
    market_timezone: str = "Asia/Shanghai"
    mock_seed: int = 20260812
    # Secrets are accepted only through QUANT_MONITOR_ASHAREHUB_API_KEY.
    asharehub_api_key: str = field(default="", repr=False, compare=False)
    asharehub_base_url: str = "https://asharehub.com"
    asharehub_request_timeout_seconds: float = 15.0
    asharehub_quote_cache_seconds: float = 300.0
    asharehub_bars_cache_seconds: float = 86400.0
    asharehub_instruments_cache_seconds: float = 604800.0
    asharehub_retries: int = 0
    asharehub_retry_backoff_seconds: float = 0.5
    asharehub_daily_soft_limit: int = 90
    # Keep part of the local allowance available for live basket quotes and
    # explicit user actions while background daily-history work is running.
    asharehub_history_reserve_requests: int = 10
    asharehub_history_refresh_seconds: float = 900.0
    asharehub_budget_path: str = "data/asharehub_budget.json"
    asharehub_strict_symbols: bool = False
    # AShareHub exposes an exchange calendar; keep a week-long local snapshot
    # so holiday workdays do not trigger needless quote requests.
    # Direct Settings(...) construction remains conservative/offline by
    # default; the shipped config profile explicitly enables the documented
    # AShareHub calendar.  This keeps embedded/tests callers from acquiring a
    # second upstream request unexpectedly while preserving the production
    # launcher behaviour.
    asharehub_trade_calendar_enabled: bool = False
    asharehub_trade_calendar_cache_seconds: float = 604800.0
    asharehub_trade_calendar_path: str = "data/asharehub_trade_calendar.json"
    strategy_profile: str = "bank_daily_multifactor_v1"
    bank_strategy_min_history: int = 130
    bank_strategy_min_universe: int = 15
    bank_strategy_entry_percentile: float = 0.80
    bank_strategy_exit_percentile: float = 0.50
    bank_strategy_max_volatility: float = 0.45
    bank_strategy_max_drawdown: float = 0.18
    bank_strategy_corporate_action_gap: float = 0.06
    bank_strategy_min_liquidity_percentile: float = 0.10
    # Optional point-in-time fundamental overlay. Zero keeps the price-only
    # model; incomplete or temporally invalid evidence still fails closed.
    bank_strategy_fundamental_weight: float = 0.20
    # Secrets are accepted only through QUANT_MONITOR_ALLTICK_TOKEN.  repr=False
    # prevents accidental disclosure in diagnostics; public_dict also omits it.
    alltick_token: str = field(default="", repr=False, compare=False)
    alltick_request_timeout_seconds: float = 15.0
    alltick_http_min_interval_seconds: float = 10.0
    alltick_quote_cache_seconds: float = 10.0
    alltick_bars_cache_seconds: float = 120.0
    alltick_retries: int = 2
    alltick_retry_backoff_seconds: float = 0.5
    alltick_batch_size: int = 5
    alltick_kline_batch_size: int = 10
    alltick_use_websocket: bool = True
    alltick_use_order_book: bool = True
    alltick_bootstrap_daily_reference: bool = True
    alltick_fetch_static_info: bool = True
    alltick_websocket_symbol_limit: int = 5
    alltick_websocket_subscription_gap_seconds: float = 1.05
    alltick_permission_retry_seconds: float = 300.0
    akshare_quote_cache_seconds: float = 15.0
    akshare_bars_cache_seconds: float = 30.0
    akshare_retries: int = 2
    akshare_retry_backoff_seconds: float = 0.5
    akshare_strict_symbols: bool = True
    akshare_use_spot_fallback: bool = False

    def __post_init__(self) -> None:
        if not str(self.app_name).strip():
            raise ValueError("app_name cannot be empty")
        if not str(self.host).strip():
            raise ValueError("host cannot be empty")
        if not 1 <= int(self.port) <= 65535:
            raise ValueError("port must be between 1 and 65535")
        provider_name = str(self.data_provider).strip().lower()
        if provider_name not in {"asharehub", "alltick", "akshare", "mock"}:
            raise ValueError(
                "data_provider must be one of: asharehub, alltick, akshare, mock"
            )
        object.__setattr__(self, "data_provider", provider_name)
        database_path = str(self.database_path).strip()
        if not database_path:
            database_path = DEFAULT_DATABASE_PATH.format(provider=provider_name)
        object.__setattr__(self, "database_path", database_path)
        timeout_seconds = float(self.startup_database_timeout_seconds)
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("startup_database_timeout_seconds must be positive")
        max_body_bytes = int(self.api_max_request_body_bytes)
        if max_body_bytes < 64 * 1024 or max_body_bytes > 64 * 1024 * 1024:
            raise ValueError("api_max_request_body_bytes must be between 65536 and 67108864")
        if float(self.poll_interval_seconds) <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if int(self.history_bar_count) <= 0:
            raise ValueError("history_bar_count must be positive")
        if not self.watchlist:
            raise ValueError("watchlist cannot be empty")
        normalized_watchlist = [normalize_symbol(item) for item in self.watchlist]
        if len(normalized_watchlist) != len(set(normalized_watchlist)):
            raise ValueError("watchlist cannot contain duplicate symbols")
        object.__setattr__(self, "watchlist", normalized_watchlist)
        if str(self.data_provider).strip().lower() == "asharehub" and len(
            normalized_watchlist
        ) > 200:
            raise ValueError("AShareHub watchlist cannot exceed 200 symbols")
        universe_mode = str(self.universe_mode).strip().lower()
        if universe_mode not in {"watchlist", "bank"}:
            raise ValueError("universe_mode must be 'watchlist' or 'bank'")
        object.__setattr__(self, "universe_mode", universe_mode)
        if not str(self.universe_industry).strip():
            raise ValueError("universe_industry cannot be empty")
        strategy_profile = str(self.strategy_profile).strip().lower()
        if strategy_profile not in {"moving_average_cross", "bank_daily_multifactor_v1"}:
            raise ValueError("unsupported strategy_profile")
        object.__setattr__(self, "strategy_profile", strategy_profile)
        if int(self.bank_strategy_min_history) < 121:
            raise ValueError("bank_strategy_min_history must be at least 121")
        if int(self.bank_strategy_min_universe) <= 1:
            raise ValueError("bank_strategy_min_universe must exceed 1")
        for name in (
            "bank_strategy_entry_percentile",
            "bank_strategy_exit_percentile",
            "bank_strategy_max_volatility",
            "bank_strategy_max_drawdown",
            "bank_strategy_corporate_action_gap",
            "bank_strategy_min_liquidity_percentile",
        ):
            if not 0 < float(getattr(self, name)) < 1:
                raise ValueError("{} must be between 0 and 1".format(name))
        fundamental_weight = float(self.bank_strategy_fundamental_weight)
        if not math.isfinite(fundamental_weight) or not 0.0 <= fundamental_weight < 1.0:
            raise ValueError("bank_strategy_fundamental_weight must be between 0 and 1")
        if self.bank_strategy_exit_percentile >= self.bank_strategy_entry_percentile:
            raise ValueError("bank strategy exit percentile must be below entry percentile")
        if float(self.asharehub_request_timeout_seconds) <= 0:
            raise ValueError("asharehub_request_timeout_seconds must be positive")
        for name in (
            "asharehub_quote_cache_seconds",
            "asharehub_bars_cache_seconds",
            "asharehub_instruments_cache_seconds",
            "asharehub_retry_backoff_seconds",
            "asharehub_history_refresh_seconds",
            "asharehub_trade_calendar_cache_seconds",
        ):
            if float(getattr(self, name)) < 0:
                raise ValueError("{} cannot be negative".format(name))
        if int(self.asharehub_retries) < 0:
            raise ValueError("asharehub_retries cannot be negative")
        if not 1 <= int(self.asharehub_daily_soft_limit) <= 100:
            raise ValueError("asharehub_daily_soft_limit must be between 1 and 100")
        if not 0 <= int(self.asharehub_history_reserve_requests) < int(
            self.asharehub_daily_soft_limit
        ):
            raise ValueError(
                "asharehub_history_reserve_requests must be non-negative and below the soft limit"
            )
        if not str(self.asharehub_budget_path).strip():
            raise ValueError("asharehub_budget_path cannot be empty")
        if not str(self.asharehub_trade_calendar_path).strip():
            raise ValueError("asharehub_trade_calendar_path cannot be empty")
        base_url = str(self.asharehub_base_url).strip().lower()
        if not base_url.startswith("https://"):
            if not (
                base_url.startswith("http://127.0.0.1")
                or base_url.startswith("http://localhost")
                or base_url.startswith("http://[::1]")
            ):
                raise ValueError("asharehub_base_url must use HTTPS")
        if float(self.akshare_quote_cache_seconds) < 0:
            raise ValueError("akshare_quote_cache_seconds cannot be negative")
        if float(self.akshare_bars_cache_seconds) < 0:
            raise ValueError("akshare_bars_cache_seconds cannot be negative")
        if int(self.akshare_retries) < 0:
            raise ValueError("akshare_retries cannot be negative")
        if float(self.akshare_retry_backoff_seconds) < 0:
            raise ValueError("akshare_retry_backoff_seconds cannot be negative")
        if float(self.alltick_request_timeout_seconds) <= 0:
            raise ValueError("alltick_request_timeout_seconds must be positive")
        for name in (
            "alltick_http_min_interval_seconds",
            "alltick_quote_cache_seconds",
            "alltick_bars_cache_seconds",
            "alltick_retry_backoff_seconds",
            "alltick_permission_retry_seconds",
        ):
            if float(getattr(self, name)) < 0:
                raise ValueError("{} cannot be negative".format(name))
        if int(self.alltick_retries) < 0:
            raise ValueError("alltick_retries cannot be negative")
        if int(self.alltick_batch_size) <= 0:
            raise ValueError("alltick_batch_size must be positive")
        if int(self.alltick_kline_batch_size) <= 0:
            raise ValueError("alltick_kline_batch_size must be positive")
        if int(self.alltick_websocket_symbol_limit) <= 0:
            raise ValueError("alltick_websocket_symbol_limit must be positive")
        if self.alltick_use_websocket and self.alltick_use_order_book and float(
            self.alltick_websocket_subscription_gap_seconds
        ) < 1.0:
            raise ValueError(
                "alltick_websocket_subscription_gap_seconds must be at least 1"
            )

    def public_dict(self) -> Dict[str, Any]:
        """Return values safe to expose to the local dashboard."""
        return {
            "app_name": self.app_name,
            "data_provider": self.data_provider,
            "poll_interval_seconds": self.poll_interval_seconds,
            "bar_interval": self.bar_interval,
            "history_bar_count": self.history_bar_count,
            "watchlist": list(self.watchlist),
            "universe_mode": self.universe_mode,
            "universe_industry": self.universe_industry,
            "strategy_profile": self.strategy_profile,
            "bank_strategy": {
                "min_history": self.bank_strategy_min_history,
                "min_universe": self.bank_strategy_min_universe,
                "entry_percentile": self.bank_strategy_entry_percentile,
                "exit_percentile": self.bank_strategy_exit_percentile,
                "max_volatility": self.bank_strategy_max_volatility,
                "max_drawdown": self.bank_strategy_max_drawdown,
                "corporate_action_gap": self.bank_strategy_corporate_action_gap,
                "min_liquidity_percentile": self.bank_strategy_min_liquidity_percentile,
                "fundamental_weight": self.bank_strategy_fundamental_weight,
                "price_basis": "raw_unadjusted",
                "signal_only": True,
            },
            "market_timezone": self.market_timezone,
            "startup_database_timeout_seconds": self.startup_database_timeout_seconds,
            "api_max_request_body_bytes": self.api_max_request_body_bytes,
            "asharehub_request_timeout_seconds": self.asharehub_request_timeout_seconds,
            "asharehub_quote_cache_seconds": self.asharehub_quote_cache_seconds,
            "asharehub_bars_cache_seconds": self.asharehub_bars_cache_seconds,
            "asharehub_instruments_cache_seconds": self.asharehub_instruments_cache_seconds,
            "asharehub_daily_soft_limit": self.asharehub_daily_soft_limit,
            "asharehub_history_reserve_requests": self.asharehub_history_reserve_requests,
            "asharehub_history_refresh_seconds": self.asharehub_history_refresh_seconds,
            "asharehub_trade_calendar_enabled": self.asharehub_trade_calendar_enabled,
            "asharehub_trade_calendar_cache_seconds": self.asharehub_trade_calendar_cache_seconds,
            "alltick_request_timeout_seconds": self.alltick_request_timeout_seconds,
            "alltick_http_min_interval_seconds": self.alltick_http_min_interval_seconds,
            "alltick_quote_cache_seconds": self.alltick_quote_cache_seconds,
            "alltick_bars_cache_seconds": self.alltick_bars_cache_seconds,
            "alltick_batch_size": self.alltick_batch_size,
            "alltick_kline_batch_size": self.alltick_kline_batch_size,
            "alltick_use_websocket": self.alltick_use_websocket,
            "alltick_use_order_book": self.alltick_use_order_book,
            "alltick_bootstrap_daily_reference": self.alltick_bootstrap_daily_reference,
            "alltick_fetch_static_info": self.alltick_fetch_static_info,
            "alltick_websocket_symbol_limit": self.alltick_websocket_symbol_limit,
            "alltick_permission_retry_seconds": self.alltick_permission_retry_seconds,
            "akshare_quote_cache_seconds": self.akshare_quote_cache_seconds,
            "akshare_bars_cache_seconds": self.akshare_bars_cache_seconds,
        }


_COERCERS = {
    "port": int,
    "startup_database_timeout_seconds": float,
    "api_max_request_body_bytes": int,
    "poll_interval_seconds": float,
    "history_bar_count": int,
    "mock_seed": int,
    "asharehub_request_timeout_seconds": float,
    "asharehub_quote_cache_seconds": float,
    "asharehub_bars_cache_seconds": float,
    "asharehub_instruments_cache_seconds": float,
    "asharehub_retries": int,
    "asharehub_retry_backoff_seconds": float,
    "asharehub_daily_soft_limit": int,
    "asharehub_history_reserve_requests": int,
    "asharehub_history_refresh_seconds": float,
    "asharehub_trade_calendar_cache_seconds": float,
    "bank_strategy_min_history": int,
    "bank_strategy_min_universe": int,
    "bank_strategy_entry_percentile": float,
    "bank_strategy_exit_percentile": float,
    "bank_strategy_max_volatility": float,
    "bank_strategy_max_drawdown": float,
    "bank_strategy_corporate_action_gap": float,
    "bank_strategy_min_liquidity_percentile": float,
    "bank_strategy_fundamental_weight": float,
    "alltick_request_timeout_seconds": float,
    "alltick_http_min_interval_seconds": float,
    "alltick_quote_cache_seconds": float,
    "alltick_bars_cache_seconds": float,
    "alltick_retries": int,
    "alltick_retry_backoff_seconds": float,
    "alltick_batch_size": int,
    "alltick_kline_batch_size": int,
    "alltick_websocket_symbol_limit": int,
    "alltick_websocket_subscription_gap_seconds": float,
    "alltick_permission_retry_seconds": float,
    "akshare_quote_cache_seconds": float,
    "akshare_bars_cache_seconds": float,
    "akshare_retries": int,
    "akshare_retry_backoff_seconds": float,
    "watchlist": lambda value: [item.strip().upper() for item in value.split(",") if item.strip()],
}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("expected a boolean value")

_ENV_ALIASES = {
    # Short aliases retained for the user-facing .env.example and shell usage.
    "db_path": "database_path",
    "poll_interval": "poll_interval_seconds",
    "asharehub_key": "asharehub_api_key",
    "asharehub_token": "asharehub_api_key",
    "alltick_api_key": "alltick_token",
}


def _env_overrides(environ: Mapping[str, str]) -> Dict[str, Any]:
    valid_fields = set(Settings.__dataclass_fields__)
    result: Dict[str, Any] = {}
    for key, value in environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        field_name = key[len(ENV_PREFIX) :].lower()
        field_name = _ENV_ALIASES.get(field_name, field_name)
        if field_name not in valid_fields:
            continue
        coercer = _COERCERS.get(field_name, str)
        if field_name == "akshare_strict_symbols":
            coercer = _as_bool
        if field_name == "akshare_use_spot_fallback":
            coercer = _as_bool
        if field_name == "asharehub_strict_symbols":
            coercer = _as_bool
        if field_name == "asharehub_trade_calendar_enabled":
            coercer = _as_bool
        if field_name in {
            "alltick_use_websocket",
            "alltick_use_order_book",
            "alltick_bootstrap_daily_reference",
            "alltick_fetch_static_info",
        }:
            coercer = _as_bool
        result[field_name] = coercer(value)
    return result


def load_settings(
    config_path: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Settings:
    """Load defaults from JSON, then apply ``QUANT_MONITOR_*`` environment values."""
    source_environ = os.environ if environ is None else environ
    project_root = Path(__file__).resolve().parent.parent
    selected_path = config_path or source_environ.get(ENV_PREFIX + "CONFIG_PATH")
    if selected_path:
        path = Path(selected_path)
        if not path.exists():
            raise FileNotFoundError("configuration file does not exist: {}".format(path))
    else:
        source_default = project_root / "config" / "default.json"
        path = source_default if source_default.exists() else Path(__file__).resolve().parent / "default_config.json"
    payload: Dict[str, Any] = {}
    loaded_database_path_explicitly = False
    if path.exists():
        with path.open("r", encoding="utf-8") as stream:
            loaded = json.load(stream)
        if not isinstance(loaded, dict):
            raise ValueError("configuration root must be a JSON object")
        forbidden_alltick_secrets = {
            "alltick_token",
            "alltick_api_key",
        }.intersection(loaded)
        if forbidden_alltick_secrets:
            raise ValueError(
                "alltick_token is secret and must use QUANT_MONITOR_ALLTICK_TOKEN"
            )
        forbidden_asharehub_secrets = {
            "asharehub_api_key",
            "asharehub_key",
            "asharehub_token",
        }.intersection(loaded)
        if forbidden_asharehub_secrets:
            raise ValueError(
                "asharehub_api_key is secret and must use QUANT_MONITOR_ASHAREHUB_API_KEY"
            )
        payload.update(loaded)
        loaded_database_path_explicitly = (
            bool(selected_path) and "database_path" in loaded
        )

    # The shipped profile spells out its documented path, but a custom profile
    # that omits database_path should derive it from that profile's provider.
    if not loaded_database_path_explicitly:
        provider_for_default = str(payload.get("data_provider") or "asharehub").strip().lower()
        payload["database_path"] = DEFAULT_DATABASE_PATH.format(
            provider=provider_for_default
        )

    settings = Settings(**payload)
    overrides = _env_overrides(source_environ)
    # The legacy v2 primary keys do not include ``source``. When callers
    # switch only the provider, select a provider-specific database so
    # mock/compatibility data cannot overwrite AShareHub rows. A database path
    # explicitly supplied by a custom config or environment remains
    # authoritative; the path in the shipped profile is a default, not an
    # explicit cross-provider choice.
    provider_override = overrides.get("data_provider")
    explicit_database_override = "database_path" in overrides
    custom_database_path = loaded_database_path_explicitly
    if (
        provider_override is not None
        and not explicit_database_override
        and not custom_database_path
        and str(provider_override) != settings.data_provider
    ):
        overrides["database_path"] = DEFAULT_DATABASE_PATH.format(
            provider=str(provider_override).strip().lower()
        )
    return replace(settings, **overrides) if overrides else settings
