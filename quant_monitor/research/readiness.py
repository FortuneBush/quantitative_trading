"""Fail-closed readiness gates for decision-grade research.

The dashboard may continue to show exploratory scores when a provider only
has a weekday fallback calendar or a current security catalogue.  A
``decision_ready`` value, however, must carry stronger evidence: an
authenticated/attested trading calendar and a point-in-time universe snapshot
known no later than the decision timestamp.  This module is deliberately
pure.  It reads neither the provider nor SQLite, so callers cannot accidentally
spend an upstream request while checking readiness.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

from quant_monitor.domain import ensure_market_time, normalize_symbol
from quant_monitor.trading_calendar import TradingCalendar

from .universe import UniverseSnapshot


def _text(value: Any) -> str:
    return str(value or "").strip()


@dataclass(frozen=True)
class DecisionReadiness:
    """Explainable fail-closed gate for a research decision.

    ``calendar_ready`` and ``universe_ready`` intentionally remain separate
    from the aggregate ``decision_ready`` flag.  API consumers can therefore
    distinguish an unavailable exchange calendar from a missing point-in-time
    universe instead of treating every failure as generic missing data.
    """

    calendar_ready: bool
    calendar_reason: str
    calendar_identity: Dict[str, Any] = field(default_factory=dict)
    universe_ready: bool = False
    universe_reason: str = "universe_unavailable"
    universe_identity: Dict[str, Any] = field(default_factory=dict)
    point_in_time_ready: bool = False
    point_in_time_reason: str = "point_in_time_snapshot_missing"
    failures: Tuple[str, ...] = field(default_factory=tuple)
    decision_ready: bool = field(init=False)

    def __post_init__(self) -> None:
        failures = tuple(dict.fromkeys(str(item).strip() for item in self.failures if str(item).strip()))
        ready = bool(self.calendar_ready and self.universe_ready and self.point_in_time_ready and not failures)
        object.__setattr__(self, "calendar_identity", dict(self.calendar_identity or {}))
        object.__setattr__(self, "universe_identity", dict(self.universe_identity or {}))
        object.__setattr__(self, "failures", failures)
        object.__setattr__(self, "decision_ready", ready)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision_ready": self.decision_ready,
            "calendar_ready": bool(self.calendar_ready),
            "calendar_reason": self.calendar_reason,
            "calendar": dict(self.calendar_identity),
            "universe_ready": bool(self.universe_ready),
            "universe_reason": self.universe_reason,
            "universe": dict(self.universe_identity),
            "point_in_time_ready": bool(self.point_in_time_ready),
            "point_in_time_reason": self.point_in_time_reason,
            "failures": list(self.failures),
        }


def _calendar_gate(calendar: Optional[TradingCalendar]) -> Tuple[bool, str, Dict[str, Any]]:
    """Return a strict calendar gate without trusting descriptive labels."""

    if calendar is None:
        return False, "calendar_missing", {"verified": False}
    metadata = getattr(calendar, "metadata", None)
    identity = {
        "identifier": _text(getattr(metadata, "identifier", None)) or None,
        "source": _text(getattr(metadata, "source", None)) or None,
        "version": _text(getattr(metadata, "version", None)) or None,
        "fingerprint": _text(getattr(calendar, "fingerprint", None)) or None,
        "verified": bool(getattr(metadata, "verified", False)),
        "coverage": (
            [value.isoformat() for value in coverage]
            if (coverage := getattr(calendar, "coverage", None)) is not None
            else None
        ),
    }
    if identity["identifier"] == "weekday_fallback":
        return False, "calendar_fallback_unverified", identity
    if not identity["verified"]:
        return False, "calendar_unverified", identity
    # A verified identity without a content fingerprint cannot be bound to a
    # research manifest or replay.  Keep this check independent of the source
    # string, which is descriptive metadata rather than evidence.
    if not identity["fingerprint"]:
        return False, "calendar_fingerprint_missing", identity
    return True, "calendar_verified", identity


def _universe_gate(
    *,
    universe_source: Optional[str],
    universe_size: int,
    minimum_universe: int,
    snapshot: Optional[UniverseSnapshot],
    as_of: Optional[datetime],
    expected_symbols: Sequence[str],
    session_date: Optional[date],
    allow_offline_fixture: bool,
) -> Tuple[bool, str, Dict[str, Any], bool, str]:
    """Validate current catalogue identity plus immutable PIT evidence."""

    source = _text(universe_source) or "unknown"
    size = max(0, int(universe_size))
    minimum = max(1, int(minimum_universe))
    identity: Dict[str, Any] = {
        "source": source,
        "size": size,
        "minimum_size": minimum,
        "point_in_time": snapshot is not None,
    }
    failures: list[str] = []
    if size < minimum:
        failures.append("universe_below_minimum")
    # A static fallback or configured watchlist is useful for the UI but is
    # not evidence that the historical eligible set was known at the time.
    if source in {"unknown", "static_bank_fallback", "configured_watchlist", "watchlist_fallback"}:
        failures.append("universe_source_unverified")
    elif not allow_offline_fixture and source.startswith("mock"):
        failures.append("universe_source_unverified")

    pit_ready = snapshot is not None
    pit_reason = "point_in_time_snapshot_verified" if pit_ready else "point_in_time_snapshot_missing"
    if snapshot is not None:
        identity.update(
            {
                "snapshot_hash": snapshot.content_hash,
                "snapshot_version": snapshot.version,
                "snapshot_universe": snapshot.universe,
                "snapshot_session_date": snapshot.session_date.isoformat(),
                "snapshot_knowledge_cutoff": snapshot.knowledge_cutoff.isoformat(),
                "snapshot_size": len(snapshot.members),
            }
        )
        if snapshot.universe != source and source not in {"asharehub_industry", "provider_catalogue"}:
            failures.append("universe_snapshot_identity_mismatch")
        if as_of is not None:
            cutoff = ensure_market_time(as_of)
            if snapshot.knowledge_cutoff > cutoff:
                pit_ready = False
                pit_reason = "point_in_time_snapshot_after_decision"
                failures.append("point_in_time_snapshot_after_decision")
        if session_date is not None and snapshot.session_date != session_date:
            pit_ready = False
            pit_reason = "point_in_time_snapshot_session_mismatch"
            failures.append("point_in_time_snapshot_session_mismatch")
        normalized_expected = tuple(sorted({normalize_symbol(item) for item in expected_symbols}))
        if normalized_expected and tuple(snapshot.symbols) != normalized_expected:
            pit_ready = False
            pit_reason = "point_in_time_snapshot_members_mismatch"
            failures.append("point_in_time_snapshot_members_mismatch")
    elif not allow_offline_fixture:
        failures.append("point_in_time_snapshot_missing")
    else:
        # Offline fixtures remain useful in tests and local UI development, but
        # the response keeps the distinction visible instead of calling them
        # decision-grade evidence.
        pit_ready = False
        pit_reason = "offline_fixture_not_decision_grade"

    # A PIT snapshot must not be accepted when the current source is below the
    # minimum either; the two checks intentionally remain independent.
    ready = not failures and pit_ready
    reason = "universe_verified" if ready else (failures[0] if failures else pit_reason)
    return ready, reason, identity, pit_ready, pit_reason


def build_decision_readiness(
    *,
    calendar: Optional[TradingCalendar],
    universe_source: Optional[str],
    universe_size: int,
    minimum_universe: int = 1,
    point_in_time_snapshot: Optional[UniverseSnapshot] = None,
    as_of: Optional[datetime] = None,
    expected_symbols: Iterable[str] = (),
    session_date: Optional[date] = None,
    allow_offline_fixture: bool = False,
) -> DecisionReadiness:
    """Build a strict, side-effect-free decision gate.

    ``calendar`` and ``point_in_time_snapshot`` are deliberately explicit
    inputs.  A current ``Instrument`` catalogue or a source/version label is
    not promoted to historical evidence implicitly.  Callers that want
    exploratory mock output may pass ``allow_offline_fixture=True``; the gate
    remains false and the reason is preserved in the returned payload.
    """

    if as_of is not None:
        as_of = ensure_market_time(as_of)
    calendar_ready, calendar_reason, calendar_identity = _calendar_gate(calendar)
    universe_ready, universe_reason, universe_identity, pit_ready, pit_reason = _universe_gate(
        universe_source=universe_source,
        universe_size=universe_size,
        minimum_universe=minimum_universe,
        snapshot=point_in_time_snapshot,
        as_of=as_of,
        expected_symbols=tuple(expected_symbols),
        session_date=session_date,
        allow_offline_fixture=allow_offline_fixture,
    )
    failures = []
    if not calendar_ready:
        failures.append(calendar_reason)
    if not universe_ready:
        failures.append(universe_reason)
    if not pit_ready:
        failures.append(pit_reason)
    return DecisionReadiness(
        calendar_ready=calendar_ready,
        calendar_reason=calendar_reason,
        calendar_identity=calendar_identity,
        universe_ready=universe_ready,
        universe_reason=universe_reason,
        universe_identity=universe_identity,
        point_in_time_ready=pit_ready,
        point_in_time_reason=pit_reason,
        failures=tuple(failures),
    )


__all__ = ["DecisionReadiness", "build_decision_readiness"]
