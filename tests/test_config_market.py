"""Configuration, symbol and A-share session boundary tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from quant_monitor.config import Settings, load_settings
from quant_monitor.domain import SHANGHAI_TZ, ensure_market_time, normalize_symbol
from quant_monitor.market import expected_latest_daily_date, market_status


def test_environment_values_override_json_configuration(tmp_path):
    config_path = tmp_path / "settings.json"
    config_path.write_text(
        json.dumps(
            {
                "host": "0.0.0.0",
                "port": 7000,
                "database_path": "never-used.db",
                "poll_interval_seconds": 8,
                "watchlist": ["600000.SH"],
                "history_bar_count": 50,
                "mock_seed": 1,
            }
        ),
        encoding="utf-8",
    )
    environ = {
        "QUANT_MONITOR_HOST": "127.0.0.1",
        "QUANT_MONITOR_PORT": "9123",
        "QUANT_MONITOR_POLL_INTERVAL_SECONDS": "0.25",
        "QUANT_MONITOR_HISTORY_BAR_COUNT": "321",
        "QUANT_MONITOR_WATCHLIST": " 600519.sh,000001.sz ,, 430047.bj ",
        "QUANT_MONITOR_MOCK_SEED": "99",
        "QUANT_MONITOR_UNKNOWN_SETTING": "ignored",
        "OTHER_PORT": "1234",
    }

    settings = load_settings(str(config_path), environ=environ)

    assert settings.host == "127.0.0.1"
    assert settings.port == 9123
    assert settings.poll_interval_seconds == pytest.approx(0.25)
    assert settings.history_bar_count == 321
    assert settings.watchlist == ["600519.SH", "000001.SZ", "430047.BJ"]
    assert settings.mock_seed == 99
    assert settings.database_path == "never-used.db"
    assert "database_path" not in settings.public_dict()


def test_load_settings_rejects_non_object_json(tmp_path):
    config_path = tmp_path / "invalid.json"
    config_path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="JSON object"):
        load_settings(str(config_path), environ={})


def test_short_environment_aliases(tmp_path):
    settings = load_settings(
        environ={
            "QUANT_MONITOR_DB_PATH": str(tmp_path / "aliased.db"),
            "QUANT_MONITOR_POLL_INTERVAL": "7.5",
        }
    )

    assert settings.database_path == str(tmp_path / "aliased.db")
    assert settings.poll_interval_seconds == 7.5


def test_provider_only_override_uses_isolated_database_on_shipped_profile():
    settings = load_settings(
        environ={"QUANT_MONITOR_DATA_PROVIDER": "mock"}
    )

    assert settings.data_provider == "mock"
    assert settings.database_path == "data/quant_monitor_mock.db"


def test_direct_settings_derives_database_from_provider():
    assert Settings(data_provider="mock").database_path == "data/quant_monitor_mock.db"
    assert (
        Settings(data_provider="akshare").database_path
        == "data/quant_monitor_akshare.db"
    )


def test_custom_config_provider_without_database_derives_isolated_path(tmp_path):
    config_path = tmp_path / "settings.json"
    config_path.write_text(
        json.dumps({"data_provider": "mock", "watchlist": ["600000.SH"]}),
        encoding="utf-8",
    )

    settings = load_settings(str(config_path), environ={})

    assert settings.database_path == "data/quant_monitor_mock.db"


def test_provider_environment_override_on_custom_config_without_database_isolated(
    tmp_path,
):
    config_path = tmp_path / "settings.json"
    config_path.write_text(
        json.dumps({"watchlist": ["600000.SH"]}), encoding="utf-8"
    )

    settings = load_settings(
        str(config_path), environ={"QUANT_MONITOR_DATA_PROVIDER": "mock"}
    )

    assert settings.database_path == "data/quant_monitor_mock.db"


def test_explicit_database_override_wins_over_provider_isolation(tmp_path):
    selected = str(tmp_path / "intentional.sqlite3")
    settings = load_settings(
        environ={
            "QUANT_MONITOR_DATA_PROVIDER": "mock",
            "QUANT_MONITOR_DATABASE_PATH": selected,
        }
    )

    assert settings.data_provider == "mock"
    assert settings.database_path == selected


def test_custom_config_database_is_not_rewritten_by_provider_override(tmp_path):
    selected = tmp_path / "custom.sqlite3"
    config_path = tmp_path / "settings.json"
    config_path.write_text(
        json.dumps({"database_path": str(selected), "watchlist": ["600000.SH"]}),
        encoding="utf-8",
    )

    settings = load_settings(
        str(config_path), environ={"QUANT_MONITOR_DATA_PROVIDER": "mock"}
    )

    assert settings.database_path == str(selected)


def test_alltick_secret_is_environment_only_and_never_public(tmp_path):
    sentinel = "alltick-unit-test-secret"
    settings = load_settings(
        environ={
            "QUANT_MONITOR_ALLTICK_TOKEN": sentinel,
            "QUANT_MONITOR_ALLTICK_USE_WEBSOCKET": "false",
            "QUANT_MONITOR_ALLTICK_USE_ORDER_BOOK": "false",
            "QUANT_MONITOR_ALLTICK_BOOTSTRAP_DAILY_REFERENCE": "false",
            "QUANT_MONITOR_ALLTICK_FETCH_STATIC_INFO": "false",
            "QUANT_MONITOR_ALLTICK_BATCH_SIZE": "7",
            "QUANT_MONITOR_ALLTICK_KLINE_BATCH_SIZE": "10",
            "QUANT_MONITOR_ALLTICK_WEBSOCKET_SYMBOL_LIMIT": "5",
            "QUANT_MONITOR_ALLTICK_PERMISSION_RETRY_SECONDS": "123",
        }
    )

    assert settings.alltick_token == sentinel
    assert settings.alltick_use_websocket is False
    assert settings.alltick_use_order_book is False
    assert settings.alltick_bootstrap_daily_reference is False
    assert settings.alltick_fetch_static_info is False
    assert settings.alltick_batch_size == 7
    assert settings.alltick_kline_batch_size == 10
    assert settings.alltick_websocket_symbol_limit == 5
    assert settings.alltick_permission_retry_seconds == 123
    assert sentinel not in repr(settings)
    assert "alltick_token" not in settings.public_dict()
    assert sentinel not in json.dumps(settings.public_dict())

    config_path = tmp_path / "secret.json"
    config_path.write_text(json.dumps({"alltick_token": sentinel}), encoding="utf-8")
    with pytest.raises(ValueError, match="must use QUANT_MONITOR_ALLTICK_TOKEN"):
        load_settings(str(config_path), environ={})

    alias_path = tmp_path / "alltick-alias-secret.json"
    alias_path.write_text(json.dumps({"alltick_api_key": sentinel}), encoding="utf-8")
    with pytest.raises(ValueError, match="QUANT_MONITOR_ALLTICK_TOKEN"):
        load_settings(str(alias_path), environ={})


def test_asharehub_secret_and_quota_settings_are_safe(tmp_path):
    sentinel = "asharehub-unit-test-secret"
    settings = load_settings(
        environ={
            "QUANT_MONITOR_ASHAREHUB_API_KEY": sentinel,
            "QUANT_MONITOR_ASHAREHUB_QUOTE_CACHE_SECONDS": "600",
            "QUANT_MONITOR_ASHAREHUB_DAILY_SOFT_LIMIT": "80",
            "QUANT_MONITOR_ASHAREHUB_HISTORY_RESERVE_REQUESTS": "8",
            "QUANT_MONITOR_ASHAREHUB_HISTORY_REFRESH_SECONDS": "1200",
            "QUANT_MONITOR_ASHAREHUB_STRICT_SYMBOLS": "false",
        }
    )

    assert settings.asharehub_api_key == sentinel
    assert settings.asharehub_quote_cache_seconds == 600
    assert settings.asharehub_daily_soft_limit == 80
    assert settings.asharehub_history_reserve_requests == 8
    assert settings.asharehub_history_refresh_seconds == 1200
    assert settings.asharehub_strict_symbols is False
    assert sentinel not in repr(settings)
    assert "asharehub_api_key" not in settings.public_dict()
    assert sentinel not in json.dumps(settings.public_dict())

    config_path = tmp_path / "secret.json"
    config_path.write_text(json.dumps({"asharehub_api_key": sentinel}), encoding="utf-8")
    with pytest.raises(ValueError, match="QUANT_MONITOR_ASHAREHUB_API_KEY"):
        load_settings(str(config_path), environ={})


def test_default_configuration_uses_asharehub_daily_bars_and_soft_budget():
    settings = load_settings(environ={})

    assert settings.data_provider == "asharehub"
    assert settings.bar_interval == "1d"
    assert settings.asharehub_quote_cache_seconds == 300
    assert settings.asharehub_daily_soft_limit == 90
    assert settings.asharehub_history_reserve_requests == 10
    assert settings.asharehub_retries == 0


@pytest.mark.parametrize("field", ["asharehub_api_key", "asharehub_key", "asharehub_token"])
def test_asharehub_secret_aliases_are_rejected_in_json(tmp_path, field):
    config_path = tmp_path / "secret-alias.json"
    config_path.write_text(json.dumps({field: "must-not-load"}), encoding="utf-8")

    with pytest.raises(ValueError, match="QUANT_MONITOR_ASHAREHUB_API_KEY"):
        load_settings(str(config_path), environ={})


def test_asharehub_remote_base_url_requires_https():
    with pytest.raises(ValueError, match="HTTPS"):
        Settings(asharehub_base_url="http://example.com")

    assert Settings(asharehub_base_url="http://127.0.0.1:8765").asharehub_base_url


def test_asharehub_watchlist_honors_official_realtime_basket_limit():
    symbols = ["{:06d}.SH".format(index) for index in range(201)]

    with pytest.raises(ValueError, match="cannot exceed 200"):
        Settings(data_provider="asharehub", watchlist=symbols)


def test_explicit_missing_configuration_is_rejected(tmp_path):
    missing = tmp_path / "missing.json"

    with pytest.raises(FileNotFoundError, match="does not exist"):
        load_settings(str(missing), environ={})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"port": 0},
        {"data_provider": "unknown"},
        {"poll_interval_seconds": 0},
        {"history_bar_count": 0},
        {"watchlist": []},
        {"asharehub_history_reserve_requests": -1},
        {
            "asharehub_daily_soft_limit": 10,
            "asharehub_history_reserve_requests": 10,
        },
        {"asharehub_history_refresh_seconds": -1},
    ],
)
def test_settings_reject_invalid_runtime_values(kwargs):
    with pytest.raises(ValueError):
        Settings(**kwargs)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" 600519.sh ", "600519.SH"),
        ("000001.SZ", "000001.SZ"),
        ("430047.bj", "430047.BJ"),
    ],
)
def test_normalize_symbol(raw, expected):
    assert normalize_symbol(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["600519", "600519.HK", "SH.600519", "12345.SH", "abcdef.SZ", ""],
)
def test_normalize_symbol_rejects_invalid_values(raw):
    with pytest.raises(ValueError, match="CODE.EXCHANGE"):
        normalize_symbol(raw)


def test_market_time_requires_awareness_and_normalizes_to_shanghai():
    with pytest.raises(ValueError, match="timezone-aware"):
        ensure_market_time(datetime(2026, 8, 12, 1, 30))

    converted = ensure_market_time(datetime(2026, 8, 12, 1, 30, tzinfo=timezone.utc))

    assert converted.tzinfo == SHANGHAI_TZ
    assert converted.hour == 9
    assert converted.minute == 30
    assert converted.utcoffset().total_seconds() == 8 * 60 * 60


@pytest.mark.parametrize(
    ("hour", "minute", "phase", "is_trading"),
    [
        (9, 14, "closed", False),
        (9, 15, "opening_auction", False),
        (9, 25, "pre_open", False),
        (9, 30, "morning", True),
        (11, 30, "lunch_break", False),
        (13, 0, "afternoon", True),
        (14, 57, "closing_auction", True),
        (15, 0, "closed", False),
    ],
)
def test_market_status_regular_weekday_boundaries(hour, minute, phase, is_trading):
    at = datetime(2026, 8, 12, hour, minute, tzinfo=SHANGHAI_TZ)

    result = market_status(at)

    assert result["phase"] == phase
    assert result["is_trading"] is is_trading
    assert result["is_trading_day"] is True
    assert result["calendar"] == "weekday_fallback"
    assert result["timestamp"].endswith("+08:00")


def test_market_status_marks_weekend_closed():
    saturday = datetime(2026, 8, 15, 10, 0, tzinfo=SHANGHAI_TZ)

    result = market_status(saturday)

    assert result["phase"] == "closed"
    assert result["is_trading"] is False
    assert result["is_trading_day"] is False


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        ("2026-08-28T15:29:59+08:00", "2026-08-27"),
        ("2026-08-28T15:30:00+08:00", "2026-08-28"),
        ("2026-08-29T10:00:00+08:00", "2026-08-28"),
        ("2026-08-31T10:00:00+08:00", "2026-08-28"),
    ],
)
def test_expected_latest_daily_date_uses_publication_window(timestamp, expected):
    assert expected_latest_daily_date(datetime.fromisoformat(timestamp)).isoformat() == expected
