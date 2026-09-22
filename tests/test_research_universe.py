"""Offline point-in-time universe and survivorship-bias contract tests."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from quant_monitor.domain import Exchange, SecurityType
from quant_monitor.research import (
    PointInTimeUniverse,
    UniverseMembership,
    UniverseSnapshot,
    UniverseSnapshotError,
    hash_canonical,
)


KNOWN = datetime(2020, 1, 2, 8, 0, tzinfo=timezone.utc)


def _member(symbol, *, start=date(2019, 1, 1), end=None, known=KNOWN, active=True, industry="银行", revision="r1", metadata=None):
    return UniverseMembership(
        universe="banks",
        symbol=symbol,
        effective_from=start,
        effective_to=end,
        known_at=known,
        active=active,
        exchange=symbol.rsplit(".", 1)[1],
        security_type="stock",
        industry=industry,
        source="fixture",
        version="catalogue-v1",
        revision_id=revision,
        metadata={} if metadata is None else metadata,
    )


def test_historical_lookup_excludes_future_listing_and_keeps_later_delisted_member():
    universe = PointInTimeUniverse(
        [
            _member("600000.SH", end=date(2022, 12, 31)),
            _member("600001.SH", start=date(2023, 1, 1)),
        ],
        version="banks-v1",
    )

    old = universe.members_at(date(2020, 6, 1), KNOWN)
    assert [item.symbol for item in old] == ["600000.SH"]
    current = universe.members_at(date(2023, 6, 1), KNOWN)
    assert [item.symbol for item in current] == ["600001.SH"]


def test_classification_revision_only_applies_after_knowledge_cutoff():
    revised_at = datetime(2021, 6, 1, 8, 0, tzinfo=timezone.utc)
    universe = PointInTimeUniverse(
        [
            _member("600000.SH", known=KNOWN, industry="银行", revision="r1"),
            _member("600000.SH", known=revised_at, industry="综合金融", revision="r2"),
        ],
        version="banks-v2",
    )
    before = universe.members_at(date(2021, 1, 1), KNOWN)
    after = universe.members_at(date(2021, 1, 1), revised_at)
    assert before[0].industry == "银行"
    assert after[0].industry == "综合金融"


def test_latest_inactive_revision_removes_member_without_deleting_history():
    delisted_known = datetime(2022, 1, 1, 8, 0, tzinfo=timezone.utc)
    universe = PointInTimeUniverse(
        [
            _member("600000.SH", end=None, known=KNOWN, active=True, revision="r1"),
            _member("600000.SH", end=None, known=delisted_known, active=False, revision="r2"),
        ],
        version="banks-v2",
    )
    assert [item.symbol for item in universe.members_at(date(2021, 1, 1), KNOWN)] == ["600000.SH"]
    assert universe.members_at(date(2021, 1, 1), delisted_known) == ()
    assert universe.all_members_at(date(2021, 1, 1), delisted_known)[0].active is False


def test_snapshot_is_order_independent_and_hash_changes_with_cutoff_or_member():
    rows = [_member("600001.SH"), _member("600000.SH")]
    first = PointInTimeUniverse(rows, version="banks-v1").snapshot_at(date(2020, 1, 3), KNOWN)
    second = PointInTimeUniverse(reversed(rows), version="banks-v1").snapshot_at(date(2020, 1, 3), KNOWN)
    later = PointInTimeUniverse(rows, version="banks-v1").snapshot_at(
        date(2020, 1, 3), datetime(2020, 1, 3, 8, 0, tzinfo=timezone.utc)
    )
    assert first == second
    assert first.content_hash == second.content_hash
    assert first.content_hash != later.content_hash
    assert first.to_dict()["content_hash"] == first.content_hash
    assert UniverseSnapshot.from_dict(first.to_dict()) == first
    forged = first.to_dict()
    forged["members"][0]["industry"] = "非银行"
    with pytest.raises(UniverseSnapshotError, match="content_hash"):
        UniverseSnapshot.from_dict(forged)


def test_snapshot_rejects_unsorted_or_duplicate_members():
    row_a, row_b = _member("600000.SH"), _member("600001.SH")
    with pytest.raises(UniverseSnapshotError, match="sorted"):
        UniverseSnapshot(
            universe="banks",
            session_date=date(2020, 1, 1),
            knowledge_cutoff=KNOWN,
            version="banks-v1",
            members=(row_b, row_a),
        )
    with pytest.raises(UniverseSnapshotError, match="unique"):
        UniverseSnapshot(
            universe="banks",
            session_date=date(2020, 1, 1),
            knowledge_cutoff=KNOWN,
            version="banks-v1",
            members=(row_a, row_a),
        )


def test_explicit_cutoff_is_required():
    universe = PointInTimeUniverse([_member("600000.SH")], version="banks-v1")
    with pytest.raises(TypeError):
        universe.members_at(date(2020, 1, 1))  # type: ignore[call-arg]
    assert universe.universe_at(date(2020, 1, 1), KNOWN) == universe.members_at(
        date(2020, 1, 1), KNOWN
    )


def test_conflicting_same_revision_is_rejected():
    first = _member("600000.SH", revision="same")
    second = _member("600000.SH", revision="same", industry="非银行")
    with pytest.raises(UniverseSnapshotError, match="conflicting"):
        PointInTimeUniverse([first, second], version="banks-v1")


def test_membership_metadata_is_deeply_immutable_and_serialization_detached():
    source = {"nested": {"tags": ["bank"], "weights": {"value": 1}}}
    member = _member("600000.SH", metadata=source)

    # Mutating the caller-owned payload must not mutate a hashed membership.
    source["nested"]["tags"].append("mutated")
    source["nested"]["weights"]["value"] = 99
    assert member.metadata["nested"]["tags"] == ("bank",)
    assert member.metadata["nested"]["weights"]["value"] == 1
    with pytest.raises(TypeError):
        member.metadata["nested"]["weights"]["value"] = 2

    # Public serialization is detached and can be edited without changing the
    # immutable membership used by the point-in-time snapshot.
    payload = member.to_dict()
    payload["metadata"]["nested"]["tags"].append("detached")
    assert member.metadata["nested"]["tags"] == ("bank",)


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"active": False}, "active"),
        ({"known_at": KNOWN + timedelta(seconds=1)}, "knowledge_cutoff"),
        ({"effective_from": date(2020, 1, 4)}, "effective"),
        ({"effective_to": date(2020, 1, 2)}, "effective"),
    ],
)
def test_direct_snapshot_rejects_temporally_ineligible_members(changes, message):
    member = replace(_member("600000.SH"), **changes)
    with pytest.raises(UniverseSnapshotError, match=message):
        UniverseSnapshot(
            universe="banks", session_date=date(2020, 1, 3),
            knowledge_cutoff=KNOWN, version="banks-v1", members=(member,),
        )


def test_rehashed_payload_cannot_turn_a_future_member_into_pit_evidence():
    snapshot = PointInTimeUniverse([_member("600000.SH")], version="banks-v1").snapshot_at(
        date(2020, 1, 3), KNOWN
    )
    payload = snapshot.to_dict()
    payload["members"][0]["known_at"] = (KNOWN + timedelta(days=1)).isoformat()
    # This is a internally hash-consistent payload, not just a corrupted hash.
    # Deserialization must also enforce the eligibility time semantics.
    payload["content_hash"] = hash_canonical(
        {key: value for key, value in payload.items() if key != "content_hash"},
        namespace="point-in-time-universe-snapshot-v1",
    )
    with pytest.raises(UniverseSnapshotError, match="invalid snapshot"):
        UniverseSnapshot.from_dict(payload)


@pytest.mark.parametrize("field", ["active", "industry", "source", "effective_to"])
def test_same_time_conflicting_assertions_do_not_use_revision_label_precedence(field):
    original = _member("600000.SH", revision="r1")
    changes = {
        "active": False, "industry": "综合金融", "source": "another-source",
        "effective_to": date(2020, 12, 31),
    }
    revised = replace(original, revision_id="r9", **{field: changes[field]})
    for rows in ((original, revised), (revised, original)):
        universe = PointInTimeUniverse(rows, version="banks-v1")
        with pytest.raises(UniverseSnapshotError, match="same known_at"):
            universe.members_at(date(2020, 1, 3), KNOWN)


def test_same_time_equal_assertions_allow_deterministic_revision_tie_breaker():
    original = _member("600000.SH", revision="r1")
    revised = replace(original, revision_id="r9", version="catalogue-v2")
    first = PointInTimeUniverse([original, revised], version="banks-v2")
    second = PointInTimeUniverse([revised, original], version="banks-v2")
    assert first.members_at(date(2020, 1, 3), KNOWN) == (revised,)
    assert first.snapshot_at(date(2020, 1, 3), KNOWN) == second.snapshot_at(date(2020, 1, 3), KNOWN)


def test_snapshot_from_members_rejects_catalogue_rows_not_selected_at_cutoff():
    later = KNOWN + timedelta(days=1)
    old = _member("600000.SH", known=KNOWN, revision="r1")
    revised = replace(old, known_at=later, revision_id="r2", industry="综合金融")
    universe = PointInTimeUniverse([old, revised], version="banks-v2")
    with pytest.raises(UniverseSnapshotError, match="point-in-time revision"):
        universe.snapshot_from_members(date(2020, 1, 3), KNOWN, (revised,))
    with pytest.raises(UniverseSnapshotError, match="point-in-time revision"):
        universe.snapshot_from_members(date(2020, 1, 3), KNOWN, (replace(old, revision_id="forged"),))


def test_future_conflicts_do_not_poison_past_and_later_revision_can_resolve_conflict():
    original = _member("600000.SH", revision="r1")
    later = KNOWN + timedelta(days=1)
    conflict_a = replace(original, known_at=later, revision_id="r2")
    conflict_b = replace(conflict_a, revision_id="r3", active=False)
    resolution = replace(original, known_at=later + timedelta(days=1), revision_id="r4")
    universe = PointInTimeUniverse(
        [original, conflict_a, conflict_b, resolution], version="banks-v4"
    )
    assert universe.members_at(date(2020, 1, 3), KNOWN) == (original,)
    with pytest.raises(UniverseSnapshotError, match="same known_at"):
        universe.members_at(date(2020, 1, 3), later)
    assert universe.members_at(date(2020, 1, 3), resolution.known_at) == (resolution,)


def test_disjoint_intervals_are_not_conflicting_and_effective_end_is_inclusive():
    first = _member("600000.SH", end=date(2020, 1, 3), revision="r1")
    second = _member("600000.SH", start=date(2020, 1, 4), active=False, revision="r2")
    universe = PointInTimeUniverse([first, second], version="banks-v1")
    assert universe.members_at(date(2020, 1, 3), KNOWN) == (first,)
    assert universe.members_at(date(2020, 1, 4), KNOWN) == ()


@pytest.mark.parametrize("field", ["universe", "source", "version", "revision_id", "industry"])
@pytest.mark.parametrize("value", [None, False, 123])
def test_membership_cannot_stringify_missing_provenance_or_classification(field, value):
    with pytest.raises(UniverseSnapshotError, match="string"):
        replace(_member("600000.SH"), **{field: value})


def test_membership_exchange_matches_symbol_and_accepts_domain_enums():
    member = replace(_member("600000.SH"), exchange=Exchange.SH, security_type=SecurityType.STOCK)
    assert member.exchange == "SH"
    assert member.security_type == "stock"
    with pytest.raises(UniverseSnapshotError, match="suffix"):
        replace(member, exchange="SZ")
    with pytest.raises(UniverseSnapshotError, match="security_type"):
        replace(member, security_type="bogus")
