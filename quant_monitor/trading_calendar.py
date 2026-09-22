"""Injectable, offline trading-session calendar contracts.

No calendar in this module is an official exchange calendar.  The weekday
fallback is deliberately approximate.  ``VersionedSessionCalendar`` preserves
caller-supplied offline sessions together with source/version labels, but those
labels are not proof of provenance and ``verified`` is therefore fixed false.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Protocol, Tuple, runtime_checkable

from quant_monitor.domain import ensure_market_time


class CalendarRangeError(ValueError):
    """Raised when a finite offline calendar cannot answer a range query."""


class CalendarSnapshotError(ValueError):
    """Raised when a serialized calendar snapshot is malformed or tampered."""


@dataclass(frozen=True)
class CalendarMetadata:
    """Immutable calendar identity and provenance disclosure.

    P0 intentionally has no public path for setting ``verified=True``.  A
    future official importer must authenticate and version its input rather
    than trusting caller-provided strings.
    """

    identifier: str
    source: str
    version: str
    notice: str

    def __post_init__(self) -> None:
        for name in ("identifier", "source", "version", "notice"):
            value = str(getattr(self, name)).strip()
            if not value:
                raise ValueError("calendar metadata {} cannot be empty".format(name))
            object.__setattr__(self, name, value)

    @property
    def verified(self) -> bool:
        """Remain false until a separate authenticated attestation exists."""

        return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "identifier": self.identifier,
            "source": self.source,
            "version": self.version,
            "verified": False,
            "notice": self.notice,
        }


@runtime_checkable
class TradingCalendar(Protocol):
    """Pure session calendar used by quality checks and deterministic replay."""

    @property
    def metadata(self) -> CalendarMetadata:
        ...

    @property
    def coverage(self) -> Optional[Tuple[date, date]]:
        ...

    def is_session(self, value: date) -> bool:
        ...

    def session_on_or_before(self, value: date) -> date:
        ...

    def previous_session(self, value: date) -> date:
        ...

    def next_session(self, value: date) -> date:
        ...

    def sessions_between(self, start: date, end: date) -> Tuple[date, ...]:
        ...

    def sessions_ending_on(self, end: date, count: int) -> Tuple[date, ...]:
        ...

    def latest_published_session(
        self, at: datetime, published_after: time = time(15, 30)
    ) -> date:
        ...


def session_offset(calendar: TradingCalendar, value: date, offset: int) -> date:
    """Return a session at a deterministic offset from ``value``.

    ``value`` is first anchored to the session on or before that date, so a
    weekend/holiday has an unambiguous anchor.  ``offset=0`` therefore means
    the latest session not after ``value``; positive values walk forward and
    negative values walk backward by sessions.  The operation deliberately
    delegates range checks to the supplied calendar, so a finite snapshot
    raises :class:`CalendarRangeError` rather than silently clipping to its
    first/last date.  This helper is kept outside the Protocol to preserve
    compatibility with existing calendar implementations while providing one
    shared, auditable convention for T+1 and signal-expiry calculations.
    """

    if isinstance(offset, bool) or not isinstance(offset, int):
        raise TypeError("offset must be an integer")
    if not isinstance(calendar, TradingCalendar):
        raise TypeError("calendar must implement TradingCalendar")
    target = _date(value, "value")

    current = calendar.session_on_or_before(target)
    if offset > 0:
        for _ in range(offset):
            current = calendar.next_session(current)
    elif offset < 0:
        for _ in range(-offset):
            current = calendar.previous_session(current)
    return current


def _date(value: date, name: str) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError("{} must be a date".format(name))
    return value


def _positive_count(value: int) -> int:
    if isinstance(value, bool):
        raise TypeError("count must be an integer")
    if not isinstance(value, int):
        raise TypeError("count must be an integer")
    count = value
    if count <= 0:
        raise ValueError("count must be positive")
    return count


def _publication_time(value: time) -> time:
    if not isinstance(value, time):
        raise TypeError("published_after must be a datetime.time")
    if value.tzinfo is not None:
        raise ValueError("published_after must be timezone-naive Shanghai wall time")
    return value.replace(tzinfo=None)


class WeekdayFallbackCalendar:
    """Unbounded Monday-Friday approximation; holidays are not represented."""

    _METADATA = CalendarMetadata(
        identifier="weekday_fallback",
        source="built_in_weekday_rule",
        version="1",
        notice=(
            "Weekdays only; exchange holidays, temporary closures and symbol-level "
            "suspensions are not represented. This is not an official calendar."
        ),
    )

    @property
    def metadata(self) -> CalendarMetadata:
        return self._METADATA

    @property
    def coverage(self) -> Optional[Tuple[date, date]]:
        return None

    def is_session(self, value: date) -> bool:
        return _date(value, "value").weekday() < 5

    def session_on_or_before(self, value: date) -> date:
        candidate = _date(value, "value")
        while not self.is_session(candidate):
            candidate -= timedelta(days=1)
        return candidate

    def previous_session(self, value: date) -> date:
        return self.session_on_or_before(_date(value, "value") - timedelta(days=1))

    def next_session(self, value: date) -> date:
        candidate = _date(value, "value") + timedelta(days=1)
        while not self.is_session(candidate):
            candidate += timedelta(days=1)
        return candidate

    def sessions_between(self, start: date, end: date) -> Tuple[date, ...]:
        first = _date(start, "start")
        last = _date(end, "end")
        if first > last:
            raise ValueError("start cannot be after end")
        result = []
        candidate = first
        while candidate <= last:
            if self.is_session(candidate):
                result.append(candidate)
            candidate += timedelta(days=1)
        return tuple(result)

    def sessions_ending_on(self, end: date, count: int) -> Tuple[date, ...]:
        remaining = _positive_count(count)
        current = self.session_on_or_before(_date(end, "end"))
        result = [current]
        while len(result) < remaining:
            current = self.previous_session(current)
            result.append(current)
        result.reverse()
        return tuple(result)

    def latest_published_session(
        self, at: datetime, published_after: time = time(15, 30)
    ) -> date:
        current = ensure_market_time(at)
        boundary = _publication_time(published_after)
        today = current.date()
        if self.is_session(today) and current.time().replace(tzinfo=None) >= boundary:
            return today
        if self.is_session(today):
            return self.previous_session(today)
        return self.session_on_or_before(today)


class VersionedSessionCalendar:
    """Finite, version-labeled offline session set with unverified provenance.

    The calendar can be exported as a deterministic, integrity-protected
    snapshot.  The fingerprint is useful for research lineage and cache keys;
    it is *not* an exchange signature and never promotes ``verified`` to true.
    """

    SNAPSHOT_SCHEMA_VERSION = 1
    _FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")

    def __init__(
        self,
        sessions: Tuple[date, ...],
        *,
        source: str,
        version: str,
        identifier: str = "offline_versioned",
        notice: str = (
            "Caller-supplied offline sessions; provenance has not been verified "
            "against an official exchange publication."
        ),
    ) -> None:
        values = tuple(sessions)
        if not values:
            raise ValueError("sessions cannot be empty")
        normalized = tuple(_date(item, "session") for item in values)
        if any(left >= right for left, right in zip(normalized, normalized[1:])):
            raise ValueError("sessions must be strictly increasing and unique")
        self._sessions = normalized
        self._session_set = frozenset(normalized)
        self._metadata = CalendarMetadata(
            identifier=identifier,
            source=source,
            version=version,
            notice=notice,
        )
        self._fingerprint = self._compute_fingerprint()

    @classmethod
    def from_weekday_rules(
        cls,
        start: date,
        end: date,
        *,
        holidays: Iterable[date] = (),
        makeup_sessions: Iterable[date] = (),
        source: str,
        version: str,
        identifier: str = "offline_weekday_rules",
        notice: str = (
            "Caller-supplied weekday/holiday rules; provenance has not been "
            "verified against an official exchange publication."
        ),
    ) -> "VersionedSessionCalendar":
        """Build a finite calendar from explicit closures and weekend makeups.

        ``start``/``end`` are inclusive.  Weekdays are sessions unless listed
        in ``holidays``; dates listed in ``makeup_sessions`` are sessions even
        when they fall on Saturday or Sunday.  Every exception must lie inside
        the requested range and duplicates/conflicting rules are rejected so a
        malformed holiday import cannot silently change the covered window.
        """

        first = _date(start, "start")
        last = _date(end, "end")
        if first > last:
            raise ValueError("start cannot be after end")

        def normalize_exceptions(values: Iterable[date], name: str):
            if values is None:
                raise TypeError("{} must be an iterable of dates".format(name))
            try:
                raw = tuple(values)
            except TypeError:
                raise TypeError("{} must be an iterable of dates".format(name)) from None
            result = tuple(_date(item, name) for item in raw)
            if len(result) != len(set(result)):
                raise ValueError("{} cannot contain duplicate dates".format(name))
            outside = [item for item in result if item < first or item > last]
            if outside:
                raise ValueError(
                    "{} date {} is outside requested range".format(name, outside[0].isoformat())
                )
            return frozenset(result)

        holiday_set = normalize_exceptions(holidays, "holidays")
        makeup_set = normalize_exceptions(makeup_sessions, "makeup_sessions")
        overlap = holiday_set.intersection(makeup_set)
        if overlap:
            raise ValueError(
                "holidays and makeup_sessions overlap at {}".format(
                    min(overlap).isoformat()
                )
            )
        # Reject no-op exceptions instead of silently accepting a malformed
        # holiday import.  A holiday on Saturday/Sunday does not remove a
        # session, while a makeup date on a weekday is already a session;
        # accepting either typo would make the caller believe its source
        # rules were applied when the resulting calendar is unchanged.
        weekend_holidays = sorted(
            item for item in holiday_set if item.weekday() >= 5
        )
        if weekend_holidays:
            raise ValueError(
                "holidays must be weekdays; {} is a weekend date".format(
                    weekend_holidays[0].isoformat()
                )
            )
        weekday_makeups = sorted(
            item for item in makeup_set if item.weekday() < 5
        )
        if weekday_makeups:
            raise ValueError(
                "makeup_sessions must be weekend dates; {} is a weekday date".format(
                    weekday_makeups[0].isoformat()
                )
            )
        sessions = []
        current = first
        while current <= last:
            if current in makeup_set or (current.weekday() < 5 and current not in holiday_set):
                sessions.append(current)
            current += timedelta(days=1)
        if not sessions:
            raise ValueError("weekday rules produce no sessions")
        return cls(
            tuple(sessions),
            source=source,
            version=version,
            identifier=identifier,
            notice=notice,
        )

    def _compute_fingerprint(self) -> str:
        """Return a stable SHA-256 digest of metadata and ordered sessions."""

        payload = {
            "identifier": self._metadata.identifier,
            "source": self._metadata.source,
            "version": self._metadata.version,
            "notice": self._metadata.notice,
            "sessions": [item.isoformat() for item in self._sessions],
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def metadata(self) -> CalendarMetadata:
        return self._metadata

    @property
    def coverage(self) -> Optional[Tuple[date, date]]:
        return self._sessions[0], self._sessions[-1]

    @property
    def session_dates(self) -> Tuple[date, ...]:
        return self._sessions

    @property
    def fingerprint(self) -> str:
        """Deterministic content/provenance-label fingerprint (SHA-256 hex)."""

        return self._fingerprint

    def to_snapshot(self) -> Dict[str, Any]:
        """Serialize this finite calendar into a JSON-compatible snapshot.

        The returned mapping contains only primitive values and can safely be
        persisted by callers.  ``fingerprint`` covers every field except the
        snapshot envelope itself.
        """

        return {
            "schema_version": self.SNAPSHOT_SCHEMA_VERSION,
            "identifier": self._metadata.identifier,
            "source": self._metadata.source,
            "version": self._metadata.version,
            "notice": self._metadata.notice,
            "sessions": [item.isoformat() for item in self._sessions],
            "fingerprint": self._fingerprint,
        }

    # ``snapshot`` is a concise alias for integrations that use noun-style
    # serializers; retain ``to_snapshot`` as the explicit API.
    snapshot = to_snapshot

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any]) -> "VersionedSessionCalendar":
        """Create a calendar from a strict, integrity-checked snapshot.

        Unknown or missing fields are rejected so a producer cannot silently
        lose lineage information.  The supplied fingerprint must match the
        normalized metadata and ordered session list exactly.
        """

        if not isinstance(snapshot, Mapping):
            raise CalendarSnapshotError("snapshot must be a mapping")
        if any(not isinstance(key, str) for key in snapshot.keys()):
            raise CalendarSnapshotError("snapshot field names must be strings")
        expected_keys = {
            "schema_version",
            "identifier",
            "source",
            "version",
            "notice",
            "sessions",
            "fingerprint",
        }
        keys = set(snapshot.keys())
        if keys != expected_keys:
            missing = sorted(expected_keys - keys)
            extra = sorted(keys - expected_keys)
            details = []
            if missing:
                details.append("missing " + ", ".join(missing))
            if extra:
                details.append("unknown " + ", ".join(extra))
            raise CalendarSnapshotError("invalid snapshot fields: " + "; ".join(details))
        schema_version = snapshot["schema_version"]
        if isinstance(schema_version, bool) or schema_version != cls.SNAPSHOT_SCHEMA_VERSION:
            raise CalendarSnapshotError(
                "unsupported snapshot schema_version {}".format(schema_version)
            )
        fingerprint = snapshot["fingerprint"]
        if not isinstance(fingerprint, str) or not cls._FINGERPRINT_RE.fullmatch(fingerprint):
            raise CalendarSnapshotError("fingerprint must be a lowercase SHA-256 hex string")
        raw_sessions = snapshot["sessions"]
        if not isinstance(raw_sessions, list):
            raise CalendarSnapshotError("sessions must be a list of ISO dates")
        if not raw_sessions:
            raise CalendarSnapshotError("sessions cannot be empty")
        parsed = []
        for index, raw in enumerate(raw_sessions):
            if not isinstance(raw, str):
                raise CalendarSnapshotError("sessions[{}] must be an ISO date".format(index))
            try:
                value = date.fromisoformat(raw)
            except (TypeError, ValueError):
                raise CalendarSnapshotError(
                    "sessions[{}] must be an ISO date".format(index)
                ) from None
            # fromisoformat accepts only date text here; reject alternate
            # spellings so the snapshot remains canonical.
            if value.isoformat() != raw:
                raise CalendarSnapshotError(
                    "sessions[{}] must use canonical YYYY-MM-DD".format(index)
                )
            parsed.append(value)
        for field in ("identifier", "source", "version", "notice"):
            if not isinstance(snapshot[field], str) or not snapshot[field].strip():
                raise CalendarSnapshotError("{} must be a non-empty string".format(field))
        try:
            target = cls(
                tuple(parsed),
                source=snapshot["source"],
                version=snapshot["version"],
                identifier=snapshot["identifier"],
                notice=snapshot["notice"],
            )
        except (TypeError, ValueError) as exc:
            raise CalendarSnapshotError("invalid calendar metadata or sessions: {}".format(exc)) from None
        if target.fingerprint != fingerprint:
            raise CalendarSnapshotError("snapshot fingerprint does not match content")
        return target

    def _require_covered(self, value: date, name: str) -> date:
        target = _date(value, name)
        first, last = self._sessions[0], self._sessions[-1]
        if target < first or target > last:
            raise CalendarRangeError(
                "{} {} is outside calendar coverage {} through {}".format(
                    name, target.isoformat(), first.isoformat(), last.isoformat()
                )
            )
        return target

    def is_session(self, value: date) -> bool:
        target = _date(value, "value")
        first, last = self._sessions[0], self._sessions[-1]
        if target < first or target > last:
            # A finite calendar cannot safely classify an out-of-range
            # weekday (a future holiday or makeup session may be unknown),
            # but weekends are unambiguously non-sessions unless explicitly
            # present in the covered snapshot.
            if target.weekday() >= 5:
                return False
            raise CalendarRangeError(
                "value {} is outside calendar coverage {} through {}".format(
                    target.isoformat(), first.isoformat(), last.isoformat()
                )
            )
        return target in self._session_set

    def session_on_or_before(self, value: date) -> date:
        target = self._require_covered(value, "value")
        index = bisect_right(self._sessions, target) - 1
        if index < 0:
            raise CalendarRangeError("no session exists on or before {}".format(target))
        return self._sessions[index]

    def previous_session(self, value: date) -> date:
        target = self._require_covered(value, "value")
        index = bisect_left(self._sessions, target) - 1
        if index < 0:
            raise CalendarRangeError("no previous session exists before {}".format(target))
        return self._sessions[index]

    def next_session(self, value: date) -> date:
        target = self._require_covered(value, "value")
        index = bisect_right(self._sessions, target)
        if index >= len(self._sessions):
            raise CalendarRangeError("no next session exists after {}".format(target))
        return self._sessions[index]

    def sessions_between(self, start: date, end: date) -> Tuple[date, ...]:
        first = _date(start, "start")
        last = _date(end, "end")
        if first > last:
            raise ValueError("start cannot be after end")
        self._require_covered(first, "start")
        self._require_covered(last, "end")
        left = bisect_left(self._sessions, first)
        right = bisect_right(self._sessions, last)
        return self._sessions[left:right]

    def sessions_ending_on(self, end: date, count: int) -> Tuple[date, ...]:
        size = _positive_count(count)
        target = self.session_on_or_before(self._require_covered(end, "end"))
        right = bisect_right(self._sessions, target)
        left = right - size
        if left < 0:
            raise CalendarRangeError(
                "calendar does not contain {} sessions ending on {}".format(size, end)
            )
        return self._sessions[left:right]

    def latest_published_session(
        self, at: datetime, published_after: time = time(15, 30)
    ) -> date:
        current = ensure_market_time(at)
        boundary = _publication_time(published_after)
        today = current.date()
        self._require_covered(today, "publication date")
        if self.is_session(today) and current.time().replace(tzinfo=None) >= boundary:
            return today
        if self.is_session(today):
            return self.previous_session(today)
        return self.session_on_or_before(today)


def _reject_duplicate_json_keys(pairs):
    """Reject duplicate object keys instead of silently accepting last-wins JSON."""

    result = {}
    for key, value in pairs:
        if key in result:
            raise CalendarSnapshotError("snapshot contains duplicate field {}".format(key))
        result[key] = value
    return result


def _validate_snapshot_expectation(
    calendar: VersionedSessionCalendar,
    *,
    expected_fingerprint: Optional[str] = None,
    expected_identifier: Optional[str] = None,
    expected_source: Optional[str] = None,
    expected_version: Optional[str] = None,
) -> VersionedSessionCalendar:
    """Apply an explicit import allow-list to a decoded calendar snapshot.

    A valid fingerprint proves that the file was not modified after it was
    published; it does not prove that the caller selected the intended
    revision.  Research jobs and deployment manifests can therefore pass the
    identity they expect and fail closed when a valid, but different, local
    snapshot is supplied.  Metadata comparisons use the normalized values
    held by :class:`CalendarMetadata`.
    """

    expectations = {
        "fingerprint": expected_fingerprint,
        "identifier": expected_identifier,
        "source": expected_source,
        "version": expected_version,
    }
    for name, value in expectations.items():
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise ValueError("expected_{} must be a non-empty string".format(name))
        value = value.strip()
        if name == "fingerprint" and not VersionedSessionCalendar._FINGERPRINT_RE.fullmatch(value):
            raise ValueError(
                "expected_fingerprint must be a lowercase SHA-256 hex string"
            )
        actual = (
            calendar.fingerprint
            if name == "fingerprint"
            else getattr(calendar.metadata, name)
        )
        if actual != value:
            raise CalendarSnapshotError(
                "snapshot {} mismatch: expected {!r}, got {!r}".format(
                    name, value, actual
                )
            )
    return calendar


def load_calendar_snapshot(
    path: os.PathLike,
    *,
    expected_fingerprint: Optional[str] = None,
    expected_identifier: Optional[str] = None,
    expected_source: Optional[str] = None,
    expected_version: Optional[str] = None,
) -> VersionedSessionCalendar:
    """Load and integrity-check one UTF-8 calendar snapshot from disk.

    The file is parsed with duplicate-key detection and then passed through
    ``VersionedSessionCalendar.from_snapshot``.  This helper deliberately
    performs no network access and does not infer an official source from the
    metadata labels.  Optional ``expected_*`` arguments pin the import to an
    identity declared by a research manifest or deployment configuration;
    mismatches fail closed.  The caller must retain the returned calendar's
    ``fingerprint`` in research lineage.
    """

    if path is None or not str(path).strip():
        raise ValueError("calendar snapshot path cannot be empty")
    target = Path(path).expanduser()
    try:
        raw = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CalendarSnapshotError("unable to read calendar snapshot: {}".format(exc)) from None
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
    except CalendarSnapshotError:
        raise
    except (TypeError, ValueError) as exc:
        raise CalendarSnapshotError("calendar snapshot is not valid JSON: {}".format(exc)) from None
    calendar = VersionedSessionCalendar.from_snapshot(value)
    return _validate_snapshot_expectation(
        calendar,
        expected_fingerprint=expected_fingerprint,
        expected_identifier=expected_identifier,
        expected_source=expected_source,
        expected_version=expected_version,
    )


def write_calendar_snapshot(
    calendar: VersionedSessionCalendar,
    path: os.PathLike,
) -> Path:
    """Write a deterministic snapshot without silently replacing a revision.

    An existing path is accepted only when it contains the exact same
    fingerprint.  A different snapshot raises ``CalendarSnapshotError``;
    callers should publish revisions under new paths rather than overwrite an
    old calendar used by a research run.  Publication uses a same-directory
    temporary file followed by an atomic, non-overwriting hard-link publish.
    """

    if not isinstance(calendar, VersionedSessionCalendar):
        raise TypeError("calendar must be a VersionedSessionCalendar")
    if path is None or not str(path).strip():
        raise ValueError("calendar snapshot path cannot be empty")
    target = Path(path).expanduser()
    payload = json.dumps(
        calendar.to_snapshot(), ensure_ascii=False, sort_keys=True, indent=2
    ) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        existing = load_calendar_snapshot(target)
        if existing.fingerprint != calendar.fingerprint or existing.to_snapshot() != calendar.to_snapshot():
            raise CalendarSnapshotError(
                "refusing to replace an existing calendar snapshot with a different revision"
            )
        return target
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=str(target.parent),
            prefix=target.name + ".", suffix=".tmp", delete=False
        ) as handle:
            temporary_name = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # Hard-link creation is atomic and fails when another process has
        # already published the path; unlike os.replace it cannot overwrite a
        # concurrent revision.  Both files live in the same directory.
        try:
            os.link(temporary_name, target)
        except FileExistsError:
            existing = load_calendar_snapshot(target)
            if existing.fingerprint != calendar.fingerprint:
                raise CalendarSnapshotError(
                    "refusing to replace a concurrently-created calendar revision"
                )
            return target
        except OSError as exc:
            raise CalendarSnapshotError(
                "unable to publish immutable calendar snapshot: {}".format(exc)
            ) from None
        else:
            os.unlink(temporary_name)
            temporary_name = None
            return target
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


class CalendarSnapshotStore:
    """Append-only local store keyed by calendar fingerprint.

    Files are named ``<sha256 fingerprint>.json``.  Therefore publishing a
    revised holiday schedule creates a new immutable artifact and cannot
    mutate a previous research input.  This is a local provenance mechanism,
    not an exchange signature or an official-data verifier.
    """

    _FILENAME_RE = re.compile(r"^[0-9a-f]{64}\.json$")

    def __init__(self, root: os.PathLike):
        if root is None or not str(root).strip():
            raise ValueError("calendar snapshot store path cannot be empty")
        self.root = Path(root).expanduser()

    def save(self, calendar: VersionedSessionCalendar) -> Path:
        path = self.root / (calendar.fingerprint + ".json")
        return write_calendar_snapshot(calendar, path)

    def load(self, fingerprint: str) -> VersionedSessionCalendar:
        if not isinstance(fingerprint, str) or not VersionedSessionCalendar._FINGERPRINT_RE.fullmatch(fingerprint):
            raise CalendarSnapshotError("fingerprint must be a lowercase SHA-256 hex string")
        path = self.root / (fingerprint + ".json")
        return load_calendar_snapshot(path, expected_fingerprint=fingerprint)

    def list(self) -> Tuple[VersionedSessionCalendar, ...]:
        if not self.root.exists():
            return tuple()
        calendars = []
        for path in sorted(self.root.glob("*.json")):
            if not self._FILENAME_RE.fullmatch(path.name):
                raise CalendarSnapshotError(
                    "unexpected calendar snapshot filename {}".format(path.name)
                )
            calendars.append(self.load(path.stem))
        return tuple(calendars)


DEFAULT_TRADING_CALENDAR: TradingCalendar = WeekdayFallbackCalendar()


__all__ = [
    "CalendarMetadata",
    "CalendarRangeError",
    "CalendarSnapshotError",
    "CalendarSnapshotStore",
    "DEFAULT_TRADING_CALENDAR",
    "load_calendar_snapshot",
    "TradingCalendar",
    "VersionedSessionCalendar",
    "WeekdayFallbackCalendar",
    "session_offset",
    "write_calendar_snapshot",
]
