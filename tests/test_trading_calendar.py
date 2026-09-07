from dataclasses import FrozenInstanceError
from datetime import date, datetime, time, timezone

import pytest

from quant_monitor.domain import SHANGHAI_TZ
from quant_monitor.market import expected_latest_daily_date, market_status
from quant_monitor.services import DailyBarDataQualityService
from quant_monitor.trading_calendar import (
    CalendarMetadata,
    CalendarRangeError,
    TradingCalendar,
    VersionedSessionCalendar,
    WeekdayFallbackCalendar,
)


SESSIONS = (
    date(2026, 9, 25),
    date(2026, 9, 28),
    date(2026, 9, 29),
    date(2026, 9, 30),
    # 1-8 October are deliberately absent as an offline holiday fixture.
    date(2026, 10, 9),
    date(2026, 10, 12),
)


def calendar():
    return VersionedSessionCalendar(
        SESSIONS,
        source="offline_test_fixture",
        version="2026.fixture.1",
        identifier="cn_a_offline_fixture",
    )


def test_calendar_protocol_and_metadata_are_explicitly_unverified_and_immutable():
    target = calendar()

    assert isinstance(target, TradingCalendar)
    assert target.coverage == (SESSIONS[0], SESSIONS[-1])
    assert target.metadata.identifier == "cn_a_offline_fixture"
    assert target.metadata.source == "offline_test_fixture"
    assert target.metadata.version == "2026.fixture.1"
    assert target.metadata.verified is False
    assert target.metadata.to_dict()["verified"] is False
    with pytest.raises(TypeError):
        CalendarMetadata(
            identifier="fake", source="fake", version="1", notice="fake", verified=True
        )
    with pytest.raises(FrozenInstanceError):
        target.metadata.version = "changed"
    with pytest.raises(AttributeError):
        target.metadata.verified = True


def test_versioned_calendar_handles_weekends_holidays_and_adjacent_sessions():
    target = calendar()

    assert target.is_session(date(2026, 9, 28)) is True
    assert target.is_session(date(2026, 9, 27)) is False
    assert target.is_session(date(2026, 10, 1)) is False
    assert target.session_on_or_before(date(2026, 10, 8)) == date(2026, 9, 30)
    assert target.previous_session(date(2026, 10, 9)) == date(2026, 9, 30)
    assert target.next_session(date(2026, 9, 30)) == date(2026, 10, 9)
    assert target.sessions_between(date(2026, 9, 29), date(2026, 10, 10)) == (
        date(2026, 9, 29),
        date(2026, 9, 30),
        date(2026, 10, 9),
    )
    assert target.sessions_ending_on(date(2026, 10, 9), 3) == (
        date(2026, 9, 29),
        date(2026, 9, 30),
        date(2026, 10, 9),
    )


def test_publication_boundary_is_inclusive_and_timezone_normalized():
    target = calendar()
    before = datetime(2026, 10, 9, 15, 29, 59, tzinfo=SHANGHAI_TZ)
    at_boundary = datetime(2026, 10, 9, 15, 30, tzinfo=SHANGHAI_TZ)
    utc_boundary = datetime(2026, 10, 9, 7, 30, tzinfo=timezone.utc)

    assert target.latest_published_session(before) == date(2026, 9, 30)
    assert target.latest_published_session(at_boundary) == date(2026, 10, 9)
    assert target.latest_published_session(utc_boundary) == date(2026, 10, 9)
    assert expected_latest_daily_date(before, calendar=target) == date(2026, 9, 30)
    assert expected_latest_daily_date(at_boundary, calendar=target) == date(2026, 10, 9)


def test_market_status_preserves_shape_and_exposes_calendar_lineage():
    target = calendar()
    holiday = datetime(2026, 10, 1, 10, tzinfo=SHANGHAI_TZ)
    result = market_status(holiday, calendar=target)

    assert result["phase"] == "closed"
    assert result["is_trading"] is False
    assert result["is_trading_day"] is False
    assert result["calendar"] == "cn_a_offline_fixture"
    assert result["calendar_source"] == "offline_test_fixture"
    assert result["calendar_version"] == "2026.fixture.1"
    assert result["calendar_verified"] is False


def test_data_quality_reuses_versioned_sessions_not_weekday_guesses():
    target = calendar()
    service = DailyBarDataQualityService(3, calendar=target)
    summary = service.audit(
        [],
        expected_symbols=("600000.SH",),
        as_of=datetime(2026, 10, 9, 15, 30, tzinfo=SHANGHAI_TZ),
    )

    assert summary.calendar == "cn_a_offline_fixture"
    assert summary.calendar_source == "offline_test_fixture"
    assert summary.calendar_version == "2026.fixture.1"
    assert summary.calendar_verified is False
    assert summary.expected_latest_date == "2026-10-09"
    assert summary.symbols[0].missing_dates == (
        "2026-09-29",
        "2026-09-30",
        "2026-10-09",
    )
    assert summary.to_dict()["calendar_verified"] is False


def test_finite_calendar_rejects_invalid_input_and_out_of_range_navigation():
    with pytest.raises(ValueError, match="strictly increasing"):
        VersionedSessionCalendar(
            (date(2026, 1, 2), date(2026, 1, 2)), source="test", version="1"
        )
    target = calendar()
    with pytest.raises(CalendarRangeError, match="previous"):
        target.previous_session(SESSIONS[0])
    with pytest.raises(CalendarRangeError, match="next"):
        target.next_session(SESSIONS[-1])
    with pytest.raises(CalendarRangeError, match="does not contain"):
        target.sessions_ending_on(SESSIONS[1], 3)
    with pytest.raises(CalendarRangeError, match="outside calendar coverage"):
        target.latest_published_session(
            datetime(2026, 10, 13, 16, tzinfo=SHANGHAI_TZ)
        )
    for value in (date(2026, 9, 24), date(2026, 10, 13)):
        with pytest.raises(CalendarRangeError, match="outside calendar coverage"):
            target.is_session(value)
        with pytest.raises(CalendarRangeError, match="outside calendar coverage"):
            target.session_on_or_before(value)
        with pytest.raises(CalendarRangeError, match="outside calendar coverage"):
            market_status(datetime.combine(value, time(10), tzinfo=SHANGHAI_TZ), calendar=target)
    with pytest.raises(CalendarRangeError, match="start.*outside calendar coverage"):
        target.sessions_between(date(2026, 9, 24), SESSIONS[1])
    with pytest.raises(CalendarRangeError, match="end.*outside calendar coverage"):
        target.sessions_between(SESSIONS[1], date(2026, 10, 13))
    with pytest.raises(CalendarRangeError, match="end.*outside calendar coverage"):
        target.sessions_ending_on(date(2026, 10, 13), 2)
    for invalid_count in (True, 1.5, "2"):
        with pytest.raises(TypeError, match="integer"):
            target.sessions_ending_on(SESSIONS[-1], invalid_count)


def test_weekday_fallback_remains_explicit_and_weekend_aware():
    target = WeekdayFallbackCalendar()
    assert target.metadata.identifier == "weekday_fallback"
    assert target.metadata.verified is False
    assert "not an official" in target.metadata.notice
    assert target.next_session(date(2026, 9, 25)) == date(2026, 9, 28)
    assert target.previous_session(date(2026, 9, 28)) == date(2026, 9, 25)
    assert target.latest_published_session(
        datetime(2026, 9, 28, 15, 29, tzinfo=SHANGHAI_TZ)
    ) == date(2026, 9, 25)


def test_public_results_do_not_trust_structural_protocol_verified_metadata():
    class MaliciousDuckCalendar:
        metadata = type(
            "Metadata",
            (),
            {
                "identifier": "claimed_official",
                "source": "claimed_exchange",
                "version": "claimed-v1",
                "notice": "caller supplied",
                "verified": True,
            },
        )()
        coverage = None

        def is_session(self, value):
            return True

        def session_on_or_before(self, value):
            return value

        def previous_session(self, value):
            return value

        def next_session(self, value):
            return value

        def sessions_between(self, start, end):
            return (start,) if start == end else (start, end)

        def sessions_ending_on(self, end, count):
            return (end,)

        def latest_published_session(self, at, published_after=time(15, 30)):
            return at.astimezone(SHANGHAI_TZ).date()

    target = MaliciousDuckCalendar()
    assert isinstance(target, TradingCalendar)

    status = market_status(
        datetime(2026, 9, 28, 10, tzinfo=SHANGHAI_TZ), calendar=target
    )
    summary = DailyBarDataQualityService(1, calendar=target).audit(
        [],
        expected_symbols=("600000.SH",),
        as_of=datetime(2026, 9, 28, 16, tzinfo=SHANGHAI_TZ),
    )

    assert status["calendar_verified"] is False
    assert summary.calendar_verified is False
