import json
from dataclasses import FrozenInstanceError
from datetime import datetime
from decimal import Decimal

import pytest

from quant_monitor.domain import Bar, SHANGHAI_TZ
from quant_monitor.services import DailyBarDataQualityService


def bar(symbol, day, *, hour=15, minute=0, source="fixture"):
    return Bar(
        symbol=symbol,
        interval="1d",
        timestamp=datetime.fromisoformat(day).replace(
            hour=hour, minute=minute, tzinfo=SHANGHAI_TZ
        ),
        open=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("9"),
        close=Decimal("10.5"),
        volume=100,
        turnover=Decimal("1050"),
        source=source,
    )


def item_for(summary, symbol):
    return next(item for item in summary.symbols if item.symbol == symbol)


def test_complete_aligned_daily_history_is_healthy():
    bars = [
        bar(symbol, day)
        for symbol in ("600000.SH", "000001.SZ")
        for day in ("2026-01-05", "2026-01-06", "2026-01-07")
    ]

    summary = DailyBarDataQualityService(3).audit(
        bars,
        expected_symbols=("000001.sz", "600000.sh"),
        as_of=datetime(2026, 1, 7, 16, tzinfo=SHANGHAI_TZ),
    )

    assert summary.status == "ok"
    assert summary.healthy is True
    assert summary.expected_latest_date == "2026-01-07"
    assert summary.expected_date_coverage_ratio == 1.0
    assert summary.common_dates == ("2026-01-05", "2026-01-06", "2026-01-07")
    assert summary.latest_common_date == "2026-01-07"
    assert summary.sources == ("fixture",)
    assert summary.mixed_source_symbols == ()
    assert summary.source_consistent is True
    assert all(item.status == "ok" for item in summary.symbols)


def test_mixed_source_within_symbol_degrades_even_complete_history():
    summary = DailyBarDataQualityService(3).audit(
        [
            bar("600000.SH", "2026-01-05", source="asharehub"),
            bar("600000.SH", "2026-01-06", source="mock"),
            bar("600000.SH", "2026-01-07", source="asharehub"),
        ],
        expected_symbols=("600000.SH",),
        as_of=datetime(2026, 1, 7, 16, tzinfo=SHANGHAI_TZ),
    )

    item = summary.symbols[0]
    assert summary.status == "degraded"
    assert summary.healthy is False
    assert summary.sources == ("asharehub", "mock")
    assert summary.mixed_source_symbols == ("600000.SH",)
    assert summary.source_consistent is False
    assert item.status == "warning"
    assert item.sources == ("asharehub", "mock")
    assert item.source_consistent is False
    assert item.history_shortfall == 0


def test_different_sources_across_symbols_degrade_cross_section():
    summary = DailyBarDataQualityService(1).audit(
        [
            bar("600000.SH", "2026-01-07", source="asharehub"),
            bar("000001.SZ", "2026-01-07", source="mock"),
        ],
        expected_symbols=("600000.SH", "000001.SZ"),
        as_of=datetime(2026, 1, 7, 16, tzinfo=SHANGHAI_TZ),
    )

    assert all(item.status == "ok" for item in summary.symbols)
    assert summary.healthy_symbol_count == 2
    assert summary.status == "degraded"
    assert summary.source_consistent is False
    assert summary.mixed_source_symbols == ()
    assert summary.sources == ("asharehub", "mock")


def test_empty_source_is_exposed_not_relabelled_and_degrades():
    summary = DailyBarDataQualityService(1).audit(
        [bar("600000.SH", "2026-01-07", source="")],
        as_of=datetime(2026, 1, 7, 16, tzinfo=SHANGHAI_TZ),
    )

    item = summary.symbols[0]
    assert item.sources == ("",)
    assert item.source_complete is False
    assert item.missing_source_row_count == 1
    assert item.status == "warning"
    assert summary.sources == ("",)
    assert summary.missing_source_symbols == ("600000.SH",)
    assert summary.source_consistent is False
    assert summary.to_dict()["sources"] == [""]


def test_duplicate_out_of_order_and_gap_are_reported_after_normalization():
    summary = DailyBarDataQualityService(3).audit(
        [
            bar("600000.SH", "2026-01-07"),
            bar("000001.SZ", "2026-01-05"),
            bar("600000.SH", "2026-01-05"),
            bar("600000.SH", "2026-01-07", hour=14),
            bar("000001.SZ", "2026-01-07"),
        ],
        expected_symbols=("600000.SH", "000001.SZ"),
        as_of=datetime(2026, 1, 7, 16, tzinfo=SHANGHAI_TZ),
    )

    first = item_for(summary, "600000.SH")
    assert first.duplicate_row_count == 1
    assert first.duplicate_dates == ("2026-01-07",)
    assert first.out_of_order_count == 2
    assert first.missing_dates == ("2026-01-06",)
    assert first.available_history_bars == 2
    assert first.history_coverage_ratio == pytest.approx(2 / 3, abs=1e-6)
    assert first.status == "insufficient"
    assert summary.common_dates == ("2026-01-05", "2026-01-07")
    assert summary.status == "degraded"


def test_expected_latest_date_honors_publication_window_and_weekend():
    service = DailyBarDataQualityService(1)
    before_close = service.audit(
        [bar("600000.SH", "2026-01-09")],
        as_of=datetime(2026, 1, 12, 14, tzinfo=SHANGHAI_TZ),
    )
    weekend = service.audit(
        [bar("600000.SH", "2026-01-09")],
        as_of=datetime(2026, 1, 11, 18, tzinfo=SHANGHAI_TZ),
    )

    assert before_close.expected_latest_date == "2026-01-09"
    assert weekend.expected_latest_date == "2026-01-09"
    assert before_close.status == weekend.status == "ok"
    assert before_close.calendar == "weekday_fallback"
    assert "holidays" in before_close.calendar_notice


def test_missing_and_unexpected_symbols_are_explicit():
    summary = DailyBarDataQualityService(1).audit(
        [bar("601398.SH", "2026-01-07")],
        expected_symbols=("600000.SH",),
        as_of=datetime(2026, 1, 7, 16, tzinfo=SHANGHAI_TZ),
    )

    assert summary.missing_symbols == ("600000.SH",)
    assert summary.unexpected_symbols == ("601398.SH",)
    assert item_for(summary, "600000.SH").status == "missing"
    assert summary.expected_date_symbol_count == 0
    assert summary.latest_common_date is None
    assert summary.status == "degraded"


def test_future_rows_do_not_mask_missing_expected_history():
    summary = DailyBarDataQualityService(2).audit(
        [bar("600000.SH", "2026-01-06"), bar("600000.SH", "2026-01-08")],
        as_of=datetime(2026, 1, 7, 16, tzinfo=SHANGHAI_TZ),
    )

    item = summary.symbols[0]
    assert item.future_dates == ("2026-01-08",)
    assert item.future_date_count == 1
    assert item.latest_date == "2026-01-08"
    assert item.latest_usable_date == "2026-01-06"
    assert item.latest_expected_present is False
    assert item.available_history_bars == 1
    assert item.status == "insufficient"


def test_off_calendar_daily_bar_is_reported_excluded_and_degrades_result():
    summary = DailyBarDataQualityService(2).audit(
        [
            bar("600000.SH", "2026-01-06"),
            bar("600000.SH", "2026-01-07"),
            bar("600000.SH", "2026-01-11"),  # Sunday
        ],
        expected_symbols=("600000.SH",),
        as_of=datetime(2026, 1, 7, 16, tzinfo=SHANGHAI_TZ),
    )

    item = summary.symbols[0]
    assert summary.schema_version == 3
    assert summary.status == "degraded"
    assert item.status == "warning"
    assert item.available_history_bars == 2
    assert item.off_calendar_date_count == 1
    assert item.off_calendar_dates == ("2026-01-11",)
    assert item.future_dates == ("2026-01-11",)
    assert item.to_dict()["off_calendar_dates"] == ["2026-01-11"]


def test_summary_is_immutable_and_json_native():
    summary = DailyBarDataQualityService(1).audit(
        [bar("600000.SH", "2026-01-07")],
        as_of=datetime(2026, 1, 7, 16, tzinfo=SHANGHAI_TZ),
    )

    payload = summary.to_dict()
    assert json.loads(json.dumps(payload, ensure_ascii=False))["symbols"][0]["symbol"] == (
        "600000.SH"
    )
    assert isinstance(payload["symbols"], list)
    assert isinstance(payload["cross_section"]["common_dates"], list)
    assert payload["source_lineage"]["source_consistent"] is True
    with pytest.raises(FrozenInstanceError):
        summary.status = "changed"


def test_rejects_invalid_contract_inputs():
    with pytest.raises(ValueError, match="positive"):
        DailyBarDataQualityService(0)
    with pytest.raises(TypeError, match="integer"):
        DailyBarDataQualityService(True)
    with pytest.raises(TypeError, match="integer"):
        DailyBarDataQualityService(1.5)

    service = DailyBarDataQualityService(1)
    intraday = Bar(
        symbol="600000.SH",
        interval="1m",
        timestamp=datetime(2026, 1, 7, 10, tzinfo=SHANGHAI_TZ),
        open="10",
        high="11",
        low="9",
        close="10",
    )
    with pytest.raises(ValueError, match="interval '1d'"):
        service.audit(
            [intraday],
            as_of=datetime(2026, 1, 7, 16, tzinfo=SHANGHAI_TZ),
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        service.audit([], as_of=datetime(2026, 1, 7, 16))
    with pytest.raises(TypeError, match="normalized Bar"):
        service.audit(
            [object()],
            as_of=datetime(2026, 1, 7, 16, tzinfo=SHANGHAI_TZ),
        )
