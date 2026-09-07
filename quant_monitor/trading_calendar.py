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
from typing import Any, Dict, Optional, Protocol, Tuple, runtime_checkable

from quant_monitor.domain import ensure_market_time


class CalendarRangeError(ValueError):
    """Raised when a finite offline calendar cannot answer a range query."""


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
    """Finite, version-labeled offline session set with unverified provenance."""

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

    @property
    def metadata(self) -> CalendarMetadata:
        return self._metadata

    @property
    def coverage(self) -> Optional[Tuple[date, date]]:
        return self._sessions[0], self._sessions[-1]

    @property
    def session_dates(self) -> Tuple[date, ...]:
        return self._sessions

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
        return self._require_covered(value, "value") in self._session_set

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


DEFAULT_TRADING_CALENDAR: TradingCalendar = WeekdayFallbackCalendar()


__all__ = [
    "CalendarMetadata",
    "CalendarRangeError",
    "DEFAULT_TRADING_CALENDAR",
    "TradingCalendar",
    "VersionedSessionCalendar",
    "WeekdayFallbackCalendar",
]
