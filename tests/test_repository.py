"""SQLite durability and domain round-trip tests."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from decimal import Decimal

from quant_monitor.domain import (
    Alert,
    AlertLevel,
    Bar,
    Exchange,
    Instrument,
    Metric,
    Quote,
    SecurityType,
    Signal,
    SignalAction,
    parse_market_time,
)
from quant_monitor.storage import SQLiteRepository


def test_sqlite_round_trip_and_wal_mode_use_tmp_database(tmp_path):
    database_path = tmp_path / "monitor.sqlite3"
    timestamp = parse_market_time("2026-08-12T10:01:02.123456+08:00")
    repository = SQLiteRepository(database_path)
    repository.initialize()
    try:
        instrument = Instrument(
            symbol="600519.SH",
            name="贵州茅台",
            exchange=Exchange.SH,
            security_type=SecurityType.STOCK,
            listed_at=date(2001, 8, 27),
            metadata={"board": "main"},
        )
        quote = Quote(
            symbol="600519.SH",
            timestamp=timestamp,
            last=Decimal("1499.87"),
            open=Decimal("1490.00"),
            high=Decimal("1501.23"),
            low=Decimal("1488.88"),
            previous_close=Decimal("1489.99"),
            bid=Decimal("1499.86"),
            ask=Decimal("1499.88"),
            bid_size=200,
            ask_size=300,
            volume=123456,
            turnover=Decimal("185123456.78"),
            source="fixture",
        )
        bar = Bar(
            symbol="600519.SH",
            interval="1m",
            timestamp=timestamp,
            open=Decimal("1499.00"),
            high=Decimal("1500.00"),
            low=Decimal("1498.00"),
            close=Decimal("1499.87"),
            volume=900,
            turnover=Decimal("1349883.00"),
            source="fixture",
        )
        metric = Metric(
            symbol="600519.SH",
            name="sma_5",
            timestamp=timestamp,
            value=1498.75,
            window=5,
            metadata={"interval": "1m"},
        )
        alert = Alert(
            symbol="600519.SH",
            timestamp=timestamp,
            level=AlertLevel.WARNING,
            title="数据延迟",
            message="行情超过阈值",
            category="quality",
            metadata={"delay_ms": 1200},
        )
        signal = Signal(
            strategy="test_strategy",
            symbol="600519.SH",
            timestamp=timestamp,
            action=SignalAction.BUY,
            strength=0.75,
            price=Decimal("1499.87"),
            reason="fixture",
            metadata={"version": 1},
        )

        assert repository.upsert_instruments([instrument]) == 1
        assert repository.upsert_quotes([quote]) == 1
        assert repository.insert_bars([bar]) == 1
        assert repository.insert_metrics([metric]) == 1
        alert_id = repository.insert_alert(alert)
        signal_id = repository.insert_signal(signal)

        assert repository.list_instruments() == [instrument]
        assert repository.get_instrument("600519.sh") == instrument
        assert repository.get_instrument("600036.SH") is None
        assert repository.get_latest_quotes(["600519.SH"]) == [quote]
        assert repository.get_quotes("600519.SH") == [quote]
        assert repository.get_bars("600519.SH", "1M") == [bar]
        assert repository.get_metrics("600519.SH", "sma_5") == [metric]

        stored_alert = repository.list_alerts(symbol="600519.SH")[0]
        assert stored_alert.id == alert_id
        assert stored_alert.title == alert.title
        assert stored_alert.metadata == alert.metadata
        assert repository.acknowledge_alert(alert_id) is True
        assert repository.list_alerts(acknowledged=True)[0].acknowledged is True
        assert repository.acknowledge_alert(999999) is False

        stored_signal = repository.list_signals(strategy="test_strategy")[0]
        assert stored_signal.id == signal_id
        assert stored_signal.action == SignalAction.BUY
        assert stored_signal.price == Decimal("1499.87")
        assert stored_signal.metadata == {"version": 1}

        # The composite keys make collector retries idempotent.
        assert repository.upsert_quotes([quote]) == 1
        assert repository.insert_bars([bar]) == 1
        assert repository.get_quotes("600519.SH") == [quote]
        assert repository.get_bars("600519.SH") == [bar]

        with sqlite3.connect(str(database_path)) as inspection:
            assert inspection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            assert inspection.execute("PRAGMA user_version").fetchone()[0] == 3
            migrations = inspection.execute(
                "SELECT version, checksum, applied_at FROM schema_migrations ORDER BY version"
            ).fetchall()
            assert [row[0] for row in migrations] == [1, 2, 3]
    finally:
        repository.close()

    assert database_path.exists()
    assert repository.initialized is False

    reopened = SQLiteRepository(database_path)
    reopened.initialize()
    try:
        assert reopened.get_latest_quotes(["600519.SH"]) == [quote]
        assert reopened.get_bars("600519.SH", "1m") == [bar]
        assert reopened.list_alerts()[0].acknowledged is True
        assert reopened.list_signals()[0].price == Decimal("1499.87")
    finally:
        reopened.close()


def test_repository_can_replace_cached_instrument_catalogue(tmp_path):
    repository = SQLiteRepository(tmp_path / "instruments.sqlite3")
    repository.initialize()
    try:
        first = Instrument("600519.SH", "贵州茅台", Exchange.SH)
        second = Instrument("000001.SZ", "平安银行", Exchange.SZ)
        repository.upsert_instruments([first])

        assert repository.replace_instruments([second]) == 1

        assert [item.symbol for item in repository.list_instruments()] == ["000001.SZ"]
    finally:
        repository.close()


def test_repository_rejects_empty_instrument_replacement(tmp_path):
    repository = SQLiteRepository(tmp_path / "instruments-empty.sqlite3")
    repository.initialize()
    try:
        original = Instrument("600519.SH", "贵州茅台", Exchange.SH)
        repository.upsert_instruments([original])

        import pytest

        with pytest.raises(ValueError, match="cannot be empty"):
            repository.replace_instruments([])
        assert repository.list_instruments() == [original]
    finally:
        repository.close()


def test_repository_can_isolate_rows_by_provider_source(tmp_path):
    repository = SQLiteRepository(tmp_path / "sources.sqlite3")
    repository.initialize()
    first_time = parse_market_time("2026-08-12T10:00:00+08:00")
    second_time = first_time + timedelta(minutes=1)
    alltick_quote = Quote(
        symbol="600519.SH",
        timestamp=first_time,
        last=Decimal("10"),
        source="alltick",
    )
    akshare_quote = Quote(
        symbol="600519.SH",
        timestamp=second_time,
        last=Decimal("11"),
        source="akshare:sina",
    )
    alltick_bar = Bar(
        symbol="600519.SH",
        interval="1m",
        timestamp=first_time,
        open=Decimal("10"),
        high=Decimal("10"),
        low=Decimal("10"),
        close=Decimal("10"),
        source="alltick",
    )
    akshare_bar = Bar(
        symbol="600519.SH",
        interval="1m",
        timestamp=second_time,
        open=Decimal("11"),
        high=Decimal("11"),
        low=Decimal("11"),
        close=Decimal("11"),
        source="akshare:sina",
    )
    try:
        repository.upsert_quotes([alltick_quote, akshare_quote])
        repository.insert_bars([alltick_bar, akshare_bar])

        assert repository.get_latest_quotes(
            ["600519.SH"], source_prefix="alltick"
        ) == [alltick_quote]
        assert repository.get_latest_quotes(
            ["600519.SH"], source_prefix="akshare"
        ) == [akshare_quote]
        assert repository.get_bars(
            "600519.SH", "1m", source_prefix="alltick"
        ) == [alltick_bar]
    finally:
        repository.close()


def test_repository_documents_same_key_cross_provider_overwrite_until_v3(tmp_path):
    """Guard the reason launchers must use a separate DB per provider.

    Schema v2 intentionally preserves legacy keys without ``source``.  This
    regression test must change when a future migration makes sources coexist.
    """

    repository = SQLiteRepository(tmp_path / "legacy-source-key.sqlite3")
    repository.initialize()
    timestamp = parse_market_time("2026-08-12T15:30:00+08:00")
    vendor = Bar(
        symbol="600519.SH",
        interval="1d",
        timestamp=timestamp,
        open="10",
        high="10",
        low="10",
        close="10",
        source="asharehub",
    )
    simulated = Bar(
        symbol="600519.SH",
        interval="1d",
        timestamp=timestamp,
        open="20",
        high="20",
        low="20",
        close="20",
        source="mock",
    )
    try:
        repository.insert_bars([vendor])
        repository.insert_bars([simulated])

        assert repository.get_bars("600519.SH", "1d") == [simulated]
        assert repository.get_bars(
            "600519.SH", "1d", source_prefix="asharehub"
        ) == []
    finally:
        repository.close()
