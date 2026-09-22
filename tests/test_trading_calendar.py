from dataclasses import FrozenInstanceError
from datetime import date, datetime, time, timezone

import pytest

from quant_monitor.domain import SHANGHAI_TZ
from quant_monitor.market import expected_latest_daily_date, market_status
from quant_monitor.services import DailyBarDataQualityService
from quant_monitor.trading_calendar import (
    CalendarMetadata,
    CalendarRangeError,
    CalendarSnapshotError,
    CalendarSnapshotStore,
    TradingCalendar,
    VersionedSessionCalendar,
    WeekdayFallbackCalendar,
    load_calendar_snapshot,
    session_offset,
    write_calendar_snapshot,
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


def test_versioned_calendar_snapshot_round_trip_is_deterministic_and_integrity_checked():
    target = calendar()
    snapshot = target.to_snapshot()

    assert snapshot["schema_version"] == 1
    assert snapshot["sessions"] == [item.isoformat() for item in SESSIONS]
    assert snapshot["fingerprint"] == target.fingerprint
    restored = VersionedSessionCalendar.from_snapshot(snapshot)
    assert restored.session_dates == target.session_dates
    assert restored.metadata.to_dict() == target.metadata.to_dict()
    assert restored.fingerprint == target.fingerprint
    assert restored.to_snapshot() == snapshot
    assert target.snapshot() == snapshot


def test_snapshot_file_helpers_and_append_only_store_preserve_revisions(tmp_path):
    first = calendar()
    direct_path = tmp_path / "first.json"
    assert write_calendar_snapshot(first, direct_path) == direct_path
    assert load_calendar_snapshot(direct_path).fingerprint == first.fingerprint
    # Idempotent publication of the same immutable revision is safe.
    assert write_calendar_snapshot(first, direct_path) == direct_path

    revised = VersionedSessionCalendar(
        SESSIONS + (date(2026, 10, 13),),
        source="offline_test_fixture",
        version="2026.fixture.2",
        identifier="cn_a_offline_fixture",
    )
    with pytest.raises(CalendarSnapshotError, match="refusing to replace"):
        write_calendar_snapshot(revised, direct_path)

    store = CalendarSnapshotStore(tmp_path / "calendar-store")
    first_path = store.save(first)
    second_path = store.save(revised)
    assert first_path.name == first.fingerprint + ".json"
    assert second_path.name == revised.fingerprint + ".json"
    assert first_path != second_path
    assert store.load(first.fingerprint).metadata.version == "2026.fixture.1"
    assert store.load(revised.fingerprint).metadata.version == "2026.fixture.2"
    assert {item.fingerprint for item in store.list()} == {
        first.fingerprint,
        revised.fingerprint,
    }


def test_weekday_rule_builder_handles_holidays_and_weekend_makeup_sessions():
    target = VersionedSessionCalendar.from_weekday_rules(
        date(2026, 10, 1),
        date(2026, 10, 11),
        holidays=(date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5)),
        makeup_sessions=(date(2026, 10, 10),),
        source="offline_exchange_fixture",
        version="2026-holiday-v1",
    )
    assert target.session_dates == (
        date(2026, 10, 6),
        date(2026, 10, 7),
        date(2026, 10, 8),
        date(2026, 10, 9),
        date(2026, 10, 10),
    )
    assert target.is_session(date(2026, 10, 10)) is True
    assert target.is_session(date(2026, 10, 11)) is False
    with pytest.raises(ValueError, match="overlap"):
        VersionedSessionCalendar.from_weekday_rules(
            date(2026, 10, 1), date(2026, 10, 2),
            holidays=(date(2026, 10, 1),),
            makeup_sessions=(date(2026, 10, 1),),
            source="fixture", version="v1",
        )
    with pytest.raises(ValueError, match="outside requested range"):
        VersionedSessionCalendar.from_weekday_rules(
            date(2026, 10, 1), date(2026, 10, 2),
            holidays=(date(2026, 10, 3),), source="fixture", version="v1",
        )
    with pytest.raises(ValueError, match="holidays must be weekdays"):
        VersionedSessionCalendar.from_weekday_rules(
            date(2026, 10, 1), date(2026, 10, 4),
            holidays=(date(2026, 10, 3),), source="fixture", version="v1",
        )
    with pytest.raises(ValueError, match="makeup_sessions must be weekend"):
        VersionedSessionCalendar.from_weekday_rules(
            date(2026, 10, 1), date(2026, 10, 4),
            makeup_sessions=(date(2026, 10, 2),), source="fixture", version="v1",
        )


def test_snapshot_file_rejects_duplicate_json_keys_and_tampered_store_names(tmp_path):
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"schema_version":1,"schema_version":1}', encoding="utf-8"
    )
    with pytest.raises(CalendarSnapshotError, match="duplicate field"):
        load_calendar_snapshot(duplicate)

    store = CalendarSnapshotStore(tmp_path / "store")
    store.root.mkdir()
    (store.root / "not-a-fingerprint.json").write_text("{}", encoding="utf-8")
    with pytest.raises(CalendarSnapshotError, match="unexpected calendar snapshot filename"):
        store.list()
    with pytest.raises(ValueError, match="path cannot be empty"):
        load_calendar_snapshot("")
    with pytest.raises(ValueError, match="path cannot be empty"):
        CalendarSnapshotStore("")


def test_snapshot_import_contract_can_pin_fingerprint_and_declared_identity(tmp_path):
    target = calendar()
    path = tmp_path / "calendar.json"
    write_calendar_snapshot(target, path)

    assert load_calendar_snapshot(
        path,
        expected_fingerprint=target.fingerprint,
        expected_identifier=target.metadata.identifier,
        expected_source=target.metadata.source,
        expected_version=target.metadata.version,
    ).fingerprint == target.fingerprint

    with pytest.raises(CalendarSnapshotError, match="fingerprint mismatch"):
        load_calendar_snapshot(path, expected_fingerprint="0" * 64)
    with pytest.raises(CalendarSnapshotError, match="identifier mismatch"):
        load_calendar_snapshot(path, expected_identifier="another-calendar")
    with pytest.raises(CalendarSnapshotError, match="source mismatch"):
        load_calendar_snapshot(path, expected_source="untrusted-import")
    with pytest.raises(CalendarSnapshotError, match="version mismatch"):
        load_calendar_snapshot(path, expected_version="2026.fixture.0")
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        load_calendar_snapshot(path, expected_fingerprint=target.fingerprint.upper())
    with pytest.raises(ValueError, match="expected_source"):
        load_calendar_snapshot(path, expected_source=" ")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: dict(value, fingerprint="0" * 64),
        lambda value: dict(value, sessions=list(value["sessions"][:-1])),
        lambda value: dict(value, unexpected="field"),
        lambda value: dict(value, schema_version=2),
        lambda value: dict(value, sessions=["2026-09-25", "2026-09-25"]),
        lambda value: dict(value, sessions=["2026/09/25"]),
        lambda value: dict(value, source=123),
        lambda value: {**value, 1: "invalid-field-name"},
    ],
)
def test_versioned_calendar_snapshot_rejects_tampering_and_schema_drift(mutate):
    with pytest.raises(CalendarSnapshotError):
        VersionedSessionCalendar.from_snapshot(mutate(calendar().to_snapshot()))


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
    assert result["calendar_fingerprint"] == target.fingerprint
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
    assert summary.calendar_fingerprint == target.fingerprint
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


def test_session_offset_uses_previous_session_anchor_and_preserves_finite_bounds():
    target = calendar()

    # A holiday/weekend is anchored to the most recent known session.  This
    # prevents callers from accidentally treating a natural-day increment as
    # the next tradable session.
    assert session_offset(target, date(2026, 10, 8), 0) == date(2026, 9, 30)
    assert session_offset(target, date(2026, 10, 8), 1) == date(2026, 10, 9)
    assert session_offset(target, date(2026, 10, 8), -1) == date(2026, 9, 29)
    assert session_offset(target, date(2026, 9, 29), 0) == date(2026, 9, 29)

    with pytest.raises(CalendarRangeError, match="no next session"):
        session_offset(target, SESSIONS[-1], 1)
    with pytest.raises(CalendarRangeError, match="no previous session"):
        session_offset(target, SESSIONS[0], -1)
    for invalid in (True, 1.0, "1"):
        with pytest.raises(TypeError, match="offset must be an integer"):
            session_offset(target, SESSIONS[1], invalid)
    with pytest.raises(CalendarRangeError, match="outside calendar coverage"):
        session_offset(target, date(2026, 10, 13), 0)


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
