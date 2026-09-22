"""Point-in-time security-universe contracts for reproducible research.

The live provider catalogue describes what is known *now*.  It must not be
used as a historical backtest universe because later listings, delistings and
classification revisions introduce survivorship and look-ahead bias.  This
module keeps the small, provider-neutral contract needed to answer
``universe_at(session, knowledge_cutoff)`` without doing I/O.

Providers may build :class:`UniverseMembership` rows from their versioned
security-master snapshots.  A :class:`PointInTimeUniverse` then resolves the
latest row known at the requested cutoff and returns a content-addressed
:class:`UniverseSnapshot`.  The snapshot hash can be placed directly in a
research manifest's ``universe_version`` field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from quant_monitor.domain import SecurityType, ensure_market_time, normalize_symbol

from .manifest import hash_canonical


class UniverseSnapshotError(ValueError):
    """Raised when a point-in-time universe or snapshot is malformed."""


def _text(value: Any, name: str) -> str:
    if isinstance(value, Enum):
        value = value.value
    if not isinstance(value, str):
        raise UniverseSnapshotError("{} must be a string".format(name))
    text = value.strip()
    if not text:
        raise UniverseSnapshotError("{} cannot be empty".format(name))
    if "\x00" in text:
        raise UniverseSnapshotError("{} cannot contain NUL".format(name))
    return text


def _day(value: Any, name: str) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise UniverseSnapshotError("{} must be a date".format(name))
    return value


def _metadata(value: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise TypeError("metadata must be a mapping")
    # MappingProxyType only protects the outer dictionary.  Nested provider
    # payloads (lists/dicts) are otherwise still mutable after the universe
    # snapshot hash has been computed, which would make the advertised
    # content-addressed lineage drift silently.  Freeze recursively at the
    # domain boundary, matching the immutable fundamental-observation
    # contract.
    return _freeze(value)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    if isinstance(value, frozenset):
        return frozenset(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    """Return a detached JSON-friendly copy for public serialization."""

    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_thaw(item) for item in sorted(value, key=repr)]
    return value


@dataclass(frozen=True)
class UniverseMembership:
    """One versioned security-master assertion.

    ``effective_to`` is inclusive.  ``known_at`` is the first wall-clock
    instant at which the assertion may be used by a research run.  Multiple
    revisions may cover one effective date; point-in-time resolution chooses
    the latest revision whose ``known_at`` is no later than the cutoff.
    ``active=False`` is useful for a known delisting/suspension assertion and
    removes the symbol from the eligible result without deleting history.
    """

    universe: str
    symbol: str
    effective_from: date
    effective_to: Optional[date]
    known_at: datetime
    active: bool = True
    exchange: str = ""
    security_type: str = "stock"
    industry: str = ""
    source: str = "unknown"
    version: str = "unknown"
    revision_id: str = "initial"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "universe", _text(self.universe, "universe"))
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        start = _day(self.effective_from, "effective_from")
        end = self.effective_to
        if end is not None:
            end = _day(end, "effective_to")
            if end < start:
                raise UniverseSnapshotError("effective_to cannot precede effective_from")
        object.__setattr__(self, "effective_from", start)
        object.__setattr__(self, "effective_to", end)
        object.__setattr__(self, "known_at", ensure_market_time(self.known_at))
        if not isinstance(self.active, bool):
            raise TypeError("active must be a boolean")
        exchange = _text(self.exchange, "exchange").upper()
        if exchange != self.symbol.rsplit(".", 1)[1]:
            raise UniverseSnapshotError("exchange does not match symbol suffix")
        object.__setattr__(self, "exchange", exchange)
        security_type = _text(self.security_type, "security_type").lower()
        if security_type not in {item.value for item in SecurityType}:
            raise UniverseSnapshotError("security_type is not supported")
        object.__setattr__(self, "security_type", security_type)
        if not isinstance(self.industry, str):
            raise UniverseSnapshotError("industry must be a string")
        object.__setattr__(self, "industry", self.industry.strip())
        object.__setattr__(self, "source", _text(self.source, "source"))
        object.__setattr__(self, "version", _text(self.version, "version"))
        object.__setattr__(self, "revision_id", _text(self.revision_id, "revision_id"))
        object.__setattr__(self, "metadata", _metadata(self.metadata))

    def to_dict(self) -> Dict[str, Any]:
        """Return detached JSON-native data suitable for hashing/auditing."""

        return {
            "universe": self.universe,
            "symbol": self.symbol,
            "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "known_at": self.known_at.isoformat(),
            "active": self.active,
            "exchange": self.exchange,
            "security_type": self.security_type,
            "industry": self.industry,
            "source": self.source,
            "version": self.version,
            "revision_id": self.revision_id,
            "metadata": _thaw(self.metadata),
        }


@dataclass(frozen=True)
class UniverseSnapshot:
    """Resolved members for one session and information cutoff."""

    universe: str
    session_date: date
    knowledge_cutoff: datetime
    version: str
    members: Tuple[UniverseMembership, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "universe", _text(self.universe, "universe"))
        object.__setattr__(self, "session_date", _day(self.session_date, "session_date"))
        object.__setattr__(self, "knowledge_cutoff", ensure_market_time(self.knowledge_cutoff))
        object.__setattr__(self, "version", _text(self.version, "version"))
        members = tuple(self.members)
        if any(not isinstance(item, UniverseMembership) for item in members):
            raise TypeError("members must contain UniverseMembership instances")
        if any(item.universe != self.universe for item in members):
            raise UniverseSnapshotError("snapshot member universe does not match snapshot")
        # Validate at the snapshot boundary as well as in the resolver.  A
        # content hash proves only byte consistency; manually constructing or
        # rehashing a payload must not make a future/inactive member eligible.
        if any(not item.active for item in members):
            raise UniverseSnapshotError("snapshot members must be active")
        if any(item.known_at > self.knowledge_cutoff for item in members):
            raise UniverseSnapshotError("snapshot member known_at exceeds knowledge_cutoff")
        if any(
            item.effective_from > self.session_date
            or (item.effective_to is not None and item.effective_to < self.session_date)
            for item in members
        ):
            raise UniverseSnapshotError("snapshot member is not effective on session_date")
        symbols = tuple(item.symbol for item in members)
        if symbols != tuple(sorted(symbols)) or len(set(symbols)) != len(symbols):
            raise UniverseSnapshotError("snapshot members must be unique and sorted")
        object.__setattr__(self, "members", members)
        payload = {
            "universe": self.universe,
            "session_date": self.session_date.isoformat(),
            "knowledge_cutoff": self.knowledge_cutoff.isoformat(),
            "version": self.version,
            "members": [item.to_dict() for item in members],
        }
        object.__setattr__(
            self,
            "content_hash",
            hash_canonical(payload, namespace="point-in-time-universe-snapshot-v1"),
        )

    @property
    def symbols(self) -> Tuple[str, ...]:
        return tuple(item.symbol for item in self.members)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "universe": self.universe,
            "session_date": self.session_date.isoformat(),
            "knowledge_cutoff": self.knowledge_cutoff.isoformat(),
            "version": self.version,
            "members": [item.to_dict() for item in self.members],
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "UniverseSnapshot":
        """Restore and verify a detached snapshot payload.

        This is intentionally strict: a caller may not silently add fields or
        alter a member while retaining the previously published hash.
        """

        if not isinstance(payload, Mapping):
            raise UniverseSnapshotError("snapshot payload must be a mapping")
        expected = {"universe", "session_date", "knowledge_cutoff", "version", "members", "content_hash"}
        if set(payload) != expected:
            raise UniverseSnapshotError("snapshot fields do not match the schema")
        try:
            session = date.fromisoformat(str(payload["session_date"]))
            if session.isoformat() != payload["session_date"]:
                raise ValueError
            cutoff_text = str(payload["knowledge_cutoff"])
            cutoff = datetime.fromisoformat(cutoff_text)
            if cutoff.isoformat() != cutoff_text:
                raise ValueError
            raw_members = payload["members"]
            if not isinstance(raw_members, (list, tuple)):
                raise ValueError
            members = []
            for raw in raw_members:
                if not isinstance(raw, Mapping):
                    raise ValueError
                member_fields = {
                    "universe", "symbol", "effective_from", "effective_to", "known_at",
                    "active", "exchange", "security_type", "industry", "source",
                    "version", "revision_id", "metadata",
                }
                if set(raw) != member_fields:
                    raise ValueError
                effective_from_text = str(raw["effective_from"])
                effective_from = date.fromisoformat(effective_from_text)
                if effective_from.isoformat() != effective_from_text:
                    raise ValueError
                effective_to = None
                if raw["effective_to"] is not None:
                    effective_to_text = str(raw["effective_to"])
                    effective_to = date.fromisoformat(effective_to_text)
                    if effective_to.isoformat() != effective_to_text:
                        raise ValueError
                known_at_text = str(raw["known_at"])
                known_at = datetime.fromisoformat(known_at_text)
                if known_at.isoformat() != known_at_text:
                    raise ValueError
                members.append(
                    UniverseMembership(
                        universe=raw["universe"],
                        symbol=raw["symbol"],
                        effective_from=effective_from,
                        effective_to=effective_to,
                        known_at=known_at,
                        active=raw["active"],
                        exchange=raw["exchange"],
                        security_type=raw["security_type"],
                        industry=raw["industry"],
                        source=raw["source"],
                        version=raw["version"],
                        revision_id=raw["revision_id"],
                        metadata=raw["metadata"],
                    )
                )
            result = cls(
                universe=payload["universe"],
                session_date=session,
                knowledge_cutoff=cutoff,
                version=payload["version"],
                members=tuple(members),
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise UniverseSnapshotError("invalid snapshot payload") from exc
        if payload["content_hash"] != result.content_hash:
            raise UniverseSnapshotError("snapshot content_hash does not match payload")
        return result


class PointInTimeUniverse:
    """Resolve versioned memberships without provider or database access."""

    def __init__(self, memberships: Iterable[UniverseMembership], *, version: str) -> None:
        self.version = _text(version, "version")
        rows = tuple(memberships)
        if any(not isinstance(item, UniverseMembership) for item in rows):
            raise TypeError("memberships must contain UniverseMembership instances")
        universes = {item.universe for item in rows}
        if len(universes) > 1:
            raise UniverseSnapshotError("memberships must belong to one universe")
        self.universe = next(iter(universes), "universe")
        identity: Dict[Tuple[str, date, Optional[date], datetime, str], Dict[str, Any]] = {}
        for row in rows:
            key = (row.symbol, row.effective_from, row.effective_to, row.known_at, row.revision_id)
            prior = identity.get(key)
            if prior is not None and prior != row.to_dict():
                raise UniverseSnapshotError(
                    "conflicting membership revisions for {}".format(row.symbol)
                )
            identity[key] = row.to_dict()
        self._memberships = tuple(
            sorted(rows, key=lambda item: (item.symbol, item.effective_from, item.known_at, item.revision_id))
        )

    @property
    def memberships(self) -> Tuple[UniverseMembership, ...]:
        return self._memberships

    def snapshot_from_members(
        self,
        session_date: date,
        knowledge_cutoff: datetime,
        members: Iterable[UniverseMembership],
    ) -> UniverseSnapshot:
        """Build a snapshot only from this resolver's PIT result.

        This convenience guard is useful at the persistence boundary: callers
        cannot accidentally pass today's catalogue (or a row from another
        universe/version) while still labelling it as a point-in-time snapshot.
        The supplied members must be exactly the active result of
        :meth:`members_at`; ordering is normalized by symbol for deterministic
        serialization.
        """

        expected = self.members_at(session_date, knowledge_cutoff)
        supplied = tuple(members)
        if any(not isinstance(item, UniverseMembership) for item in supplied):
            raise TypeError("members must contain UniverseMembership instances")
        expected_by_symbol = {item.symbol: item for item in expected}
        supplied_by_symbol = {item.symbol: item for item in supplied}
        if len(supplied_by_symbol) != len(supplied):
            raise UniverseSnapshotError("snapshot members must be unique")
        if set(supplied_by_symbol) != set(expected_by_symbol):
            raise UniverseSnapshotError("snapshot members do not match point-in-time result")
        for symbol, item in supplied_by_symbol.items():
            if item != expected_by_symbol[symbol]:
                raise UniverseSnapshotError(
                    "snapshot member does not match point-in-time revision for {}".format(symbol)
                )
        return UniverseSnapshot(
            universe=self.universe,
            session_date=_day(session_date, "session_date"),
            knowledge_cutoff=ensure_market_time(knowledge_cutoff),
            version=self.version,
            members=tuple(supplied_by_symbol[symbol] for symbol in sorted(supplied_by_symbol)),
        )

    def all_members_at(self, session_date: date, knowledge_cutoff: datetime) -> Tuple[UniverseMembership, ...]:
        """Return one latest-known assertion per symbol, including inactive rows.

        A revision ID is an identifier, not an ordering or precedence rule.
        Conflicting assertions at the same latest ``known_at`` therefore fail
        closed.  Only semantically equal assertions may use revision/version
        labels as a deterministic tie-breaker.  Conflict checks happen after
        both temporal filters, so future revisions cannot affect an earlier
        query merely by being present in the input history.
        """

        session = _day(session_date, "session_date")
        cutoff = ensure_market_time(knowledge_cutoff)
        candidates: Dict[str, list[UniverseMembership]] = {}
        for row in self._memberships:
            if row.known_at > cutoff:
                continue
            if row.effective_from > session or (row.effective_to is not None and row.effective_to < session):
                continue
            candidates.setdefault(row.symbol, []).append(row)
        resolved = []
        for symbol in sorted(candidates):
            latest_known = max(item.known_at for item in candidates[symbol])
            latest = [item for item in candidates[symbol] if item.known_at == latest_known]
            assertions = []
            for item in latest:
                assertion = item.to_dict()
                assertion.pop("revision_id")
                assertion.pop("version")
                assertions.append(assertion)
            if any(assertion != assertions[0] for assertion in assertions[1:]):
                raise UniverseSnapshotError(
                    "conflicting membership assertions at the same known_at for {}".format(symbol)
                )
            resolved.append(max(latest, key=lambda item: (item.revision_id, item.version)))
        return tuple(resolved)

    def members_at(self, session_date: date, knowledge_cutoff: datetime) -> Tuple[UniverseMembership, ...]:
        """Return eligible (active) members as of both dates.

        Supplying an explicit ``knowledge_cutoff`` is mandatory by design: a
        caller cannot accidentally use today's catalogue when replaying the
        past.
        """

        return tuple(
            row for row in self.all_members_at(session_date, knowledge_cutoff) if row.active
        )

    def universe_at(
        self, session_date: date, knowledge_cutoff: datetime
    ) -> Tuple[UniverseMembership, ...]:
        """Public point-in-time query alias used by research callers.

        The explicit name makes it harder to accidentally substitute a live
        catalogue in a backtest.  It intentionally returns immutable
        membership rows; call :meth:`snapshot_at` when a content-addressed
        artifact is required.
        """

        return self.members_at(session_date, knowledge_cutoff)

    def snapshot_at(self, session_date: date, knowledge_cutoff: datetime) -> UniverseSnapshot:
        return self.snapshot_from_members(
            session_date,
            knowledge_cutoff,
            self.members_at(session_date, knowledge_cutoff),
        )


__all__ = [
    "PointInTimeUniverse",
    "UniverseMembership",
    "UniverseSnapshot",
    "UniverseSnapshotError",
]
