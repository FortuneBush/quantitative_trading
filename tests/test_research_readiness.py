"""Strict P0 calendar/security-master decision gates."""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

from quant_monitor.domain import SHANGHAI_TZ
from quant_monitor.research import (
    UniverseMembership,
    UniverseSnapshot,
    build_decision_readiness,
)
from quant_monitor.trading_calendar import WeekdayFallbackCalendar


class VerifiedCalendar:
    """Tiny offline protocol fixture with an explicit attestation flag."""

    metadata = SimpleNamespace(
        identifier="official_sse_szse",
        source="exchange_attestation",
        version="2026-v1",
        verified=True,
    )
    coverage = (date(2026, 1, 1), date(2026, 12, 31))
    fingerprint = "a" * 64


def _snapshot():
    cutoff = datetime(2026, 9, 24, 15, 30, tzinfo=SHANGHAI_TZ)
    members = tuple(
        UniverseMembership(
            universe="a_share_banks",
            symbol=symbol,
            effective_from=date(2026, 1, 1),
            effective_to=None,
            known_at=cutoff,
            exchange=symbol.rsplit(".", 1)[1],
            security_type="stock",
            industry="银行",
            source="exchange_attestation",
            version="2026-v1",
            revision_id="r1",
        )
        for symbol in ("000001.SZ", "600036.SH")
    )
    return UniverseSnapshot(
        universe="a_share_banks",
        session_date=date(2026, 9, 24),
        knowledge_cutoff=cutoff,
        version="2026-v1",
        members=members,
    )


def test_missing_or_unverified_calendar_fails_closed_even_with_catalogue_and_pit_snapshot():
    report = build_decision_readiness(
        calendar=WeekdayFallbackCalendar(),
        universe_source="provider_catalogue",
        universe_size=2,
        minimum_universe=2,
        point_in_time_snapshot=_snapshot(),
        as_of=datetime(2026, 9, 24, 16, 0, tzinfo=SHANGHAI_TZ),
        expected_symbols=("000001.SZ", "600036.SH"),
        session_date=date(2026, 9, 24),
    )
    assert report.decision_ready is False
    assert report.calendar_ready is False
    assert report.point_in_time_ready is True
    assert "calendar_fallback_unverified" in report.failures


def test_current_catalogue_without_point_in_time_snapshot_is_not_decision_ready():
    report = build_decision_readiness(
        calendar=VerifiedCalendar(),
        universe_source="asharehub_industry",
        universe_size=42,
        minimum_universe=40,
        as_of=datetime(2026, 9, 24, 16, 0, tzinfo=SHANGHAI_TZ),
    )
    assert report.calendar_ready is True
    assert report.universe_ready is False
    assert report.point_in_time_ready is False
    assert report.decision_ready is False
    assert "point_in_time_snapshot_missing" in report.failures


def test_verified_calendar_and_pit_snapshot_pass_when_identity_matches():
    snapshot = _snapshot()
    report = build_decision_readiness(
        calendar=VerifiedCalendar(),
        # Provider source labels are descriptive; the PIT snapshot identity is
        # checked against its own universe name below.
        universe_source="provider_catalogue",
        universe_size=2,
        minimum_universe=2,
        point_in_time_snapshot=snapshot,
        as_of=datetime(2026, 9, 24, 16, 0, tzinfo=SHANGHAI_TZ),
        expected_symbols=snapshot.symbols,
        session_date=snapshot.session_date,
    )
    assert report.decision_ready is True
    assert report.failures == ()
    payload = report.to_dict()
    assert payload["calendar"]["verified"] is True
    assert payload["universe"]["snapshot_hash"] == snapshot.content_hash


def test_snapshot_after_decision_is_rejected():
    snapshot = _snapshot()
    report = build_decision_readiness(
        calendar=VerifiedCalendar(),
        universe_source="provider_catalogue",
        universe_size=2,
        minimum_universe=1,
        point_in_time_snapshot=snapshot,
        as_of=datetime(2026, 9, 24, 15, 0, tzinfo=SHANGHAI_TZ),
        expected_symbols=snapshot.symbols,
        session_date=snapshot.session_date,
    )
    assert report.decision_ready is False
    assert report.point_in_time_reason == "point_in_time_snapshot_after_decision"

