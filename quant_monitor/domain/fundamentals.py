"""Point-in-time fundamental observations for research-only calculations.

Providers may publish revised financial values long after a reporting period.
This contract keeps the reporting period separate from the first time a value
was available to a strategy, so an as-of query cannot accidentally see future
information.  It deliberately performs no fetching, persistence, or factor
calculation.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional, Tuple

from .models import ensure_market_time, normalize_symbol


_METRIC_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")


def _text(value: Any, name: str, *, lower: bool = False) -> str:
    if value is None:
        raise TypeError("{} is required".format(name))
    result = str(value).strip()
    if not result or "\x00" in result:
        raise ValueError("{} cannot be empty or contain NUL".format(name))
    return result.lower() if lower else result


def _decimal(value: Any, name: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("{} must be a valid decimal".format(name)) from exc
    if not result.is_finite():
        raise ValueError("{} must be finite".format(name))
    return result


def _hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _freeze(value: Any) -> Any:
    """Recursively freeze provider metadata at the domain boundary.

    ``MappingProxyType(dict(...))`` alone only protects the outer mapping;
    nested lists and dictionaries would otherwise let a caller mutate an
    observation after it had been hashed or selected for a research run.
    """

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
class FundamentalObservation:
    """One immutable, revision-aware fundamental value."""

    observation_id: str
    symbol: str
    metric: str
    value: Optional[Decimal]
    unit: str
    scope: str
    period_start: date
    period_end: date
    available_at: datetime
    received_at: datetime
    source: str
    revision_id: str
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "observation_id", _text(self.observation_id, "observation_id"))
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        metric = _text(self.metric, "metric", lower=True)
        if not _METRIC_RE.fullmatch(metric):
            raise ValueError("metric must use lowercase [a-z0-9_.-] characters")
        object.__setattr__(self, "metric", metric)
        if self.value is not None:
            object.__setattr__(self, "value", _decimal(self.value, "value"))
        object.__setattr__(self, "unit", _text(self.unit, "unit", lower=True))
        scope = _text(self.scope, "scope", lower=True)
        if scope not in {"consolidated", "standalone", "unknown"}:
            raise ValueError("scope must be consolidated, standalone, or unknown")
        object.__setattr__(self, "scope", scope)
        for name in ("period_start", "period_end"):
            value = getattr(self, name)
            if not isinstance(value, date) or isinstance(value, datetime):
                raise TypeError("{} must be a date".format(name))
        if self.period_start > self.period_end:
            raise ValueError("period_start cannot follow period_end")
        available = ensure_market_time(self.available_at)
        received = ensure_market_time(self.received_at)
        if received < available:
            raise ValueError("received_at cannot precede available_at")
        object.__setattr__(self, "available_at", available)
        object.__setattr__(self, "received_at", received)
        object.__setattr__(self, "source", _text(self.source, "source"))
        object.__setattr__(self, "revision_id", _text(self.revision_id, "revision_id"))
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        object.__setattr__(self, "metadata", _freeze(self.metadata))

    @property
    def content_hash(self) -> str:
        """Hash the economic content, excluding receive-time metadata."""

        return _hash(
            {
                "observation_id": self.observation_id,
                "symbol": self.symbol,
                "metric": self.metric,
                "value": None if self.value is None else str(self.value),
                "unit": self.unit,
                "scope": self.scope,
                "period_start": self.period_start.isoformat(),
                "period_end": self.period_end.isoformat(),
                "available_at": self.available_at.isoformat(),
                "source": self.source,
                "revision_id": self.revision_id,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "symbol": self.symbol,
            "metric": self.metric,
            "value": None if self.value is None else str(self.value),
            "unit": self.unit,
            "scope": self.scope,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "available_at": self.available_at.isoformat(),
            "received_at": self.received_at.isoformat(),
            "source": self.source,
            "revision_id": self.revision_id,
            "metadata": _thaw(self.metadata),
            "content_hash": self.content_hash,
        }


def fundamentals_as_of(
    observations: Iterable[FundamentalObservation],
    *,
    decision_at: datetime,
    symbol: Optional[str] = None,
    metric: Optional[str] = None,
    period_start: Optional[date] = None,
    period_end: Optional[date] = None,
    unit: Optional[str] = None,
    scope: Optional[str] = None,
    source: Optional[str] = None,
) -> Tuple[FundamentalObservation, ...]:
    """Return point-in-time observations without collapsing unlike facts.

    Only observations with ``available_at <= decision_at`` participate.  A
    missing metric is omitted rather than represented by zero.  Unless an
    explicit period is requested, the newest *reporting period* is selected
    before revision time.  Consequently a late restatement of an older report
    cannot displace a newer report merely because it arrived later.

    ``unit``, ``scope`` and ``source`` are part of a fact's comparability
    identity.  If eligible records for one symbol/metric contain more than
    one such identity, the query fails closed and asks the caller to provide
    filters; unlike units, scopes or vendors are never silently merged.  At a
    selected period, two different values published at exactly the same
    ``available_at`` are also an ambiguity and raise ``ValueError``.  Equal
    values may use the lexicographically greatest revision ID as a stable
    tie-breaker.
    """

    cutoff = ensure_market_time(decision_at)
    symbol_filter = normalize_symbol(symbol) if symbol is not None else None
    metric_filter = _text(metric, "metric", lower=True) if metric is not None else None
    if metric_filter is not None and not _METRIC_RE.fullmatch(metric_filter):
        raise ValueError("metric must use lowercase [a-z0-9_.-] characters")

    def _date_filter(value: Optional[date], name: str) -> Optional[date]:
        if value is None:
            return None
        if not isinstance(value, date) or isinstance(value, datetime):
            raise TypeError("{} must be a date".format(name))
        return value

    period_start_filter = _date_filter(period_start, "period_start")
    period_end_filter = _date_filter(period_end, "period_end")
    if (
        period_start_filter is not None
        and period_end_filter is not None
        and period_start_filter > period_end_filter
    ):
        raise ValueError("period_start cannot follow period_end")
    unit_filter = _text(unit, "unit", lower=True) if unit is not None else None
    scope_filter = _text(scope, "scope", lower=True) if scope is not None else None
    if scope_filter is not None and scope_filter not in {"consolidated", "standalone", "unknown"}:
        raise ValueError("scope must be consolidated, standalone, or unknown")
    source_filter = _text(source, "source") if source is not None else None

    # An observation ID is the provider's stable identity for one published
    # revision.  Without this guard a malformed feed could reuse an ID for a
    # different metric/period and the as-of filters might silently hide the
    # contradiction (for example when the newer row is outside the selected
    # period).  Compare the economic content hash rather than receive-time
    # metadata so harmless duplicate deliveries remain deterministic.
    seen_ids: dict[str, str] = {}
    candidates: dict[tuple[str, str], list[FundamentalObservation]] = {}
    for observation in observations:
        if not isinstance(observation, FundamentalObservation):
            raise TypeError("observations must contain FundamentalObservation objects")
        prior_hash = seen_ids.get(observation.observation_id)
        if prior_hash is not None and prior_hash != observation.content_hash:
            raise ValueError(
                "conflicting fundamental observation_id {}".format(
                    observation.observation_id
                )
            )
        seen_ids[observation.observation_id] = observation.content_hash
        if symbol_filter is not None and observation.symbol != symbol_filter:
            continue
        if metric_filter is not None and observation.metric != metric_filter:
            continue
        if observation.available_at > cutoff:
            continue
        if period_start_filter is not None and observation.period_start != period_start_filter:
            continue
        if period_end_filter is not None and observation.period_end != period_end_filter:
            continue
        if unit_filter is not None and observation.unit != unit_filter:
            continue
        if scope_filter is not None and observation.scope != scope_filter:
            continue
        if source_filter is not None and observation.source != source_filter:
            continue
        key = (observation.symbol, observation.metric)
        candidates.setdefault(key, []).append(observation)

    selected: dict[tuple[str, str], FundamentalObservation] = {}
    for key, eligible in candidates.items():
        dimensions = {(item.unit, item.scope, item.source) for item in eligible}
        if len(dimensions) > 1:
            raise ValueError(
                "ambiguous fundamental dimensions for {} {}: specify unit, scope, and source".format(
                    key[0], key[1]
                )
            )

        periods = {(item.period_start, item.period_end) for item in eligible}
        # A partially specified period still chooses the newest matching
        # period (e.g. latest year for a given period_start).  Only when both
        # bounds are supplied is the period an exact identity.
        if period_start_filter is None or period_end_filter is None:
            latest_period = max(periods, key=lambda value: (value[1], value[0]))
            eligible = [
                item
                for item in eligible
                if (item.period_start, item.period_end) == latest_period
            ]
        if not eligible:
            continue
        latest_available = max(item.available_at for item in eligible)
        at_latest_time = [item for item in eligible if item.available_at == latest_available]
        values = {item.value for item in at_latest_time}
        if len(values) > 1:
            raise ValueError(
                "conflicting fundamental revisions at the same availability time for {} {}".format(
                    key[0], key[1]
                )
            )
        selected[key] = max(at_latest_time, key=lambda item: (item.revision_id, item.observation_id))
    return tuple(selected[key] for key in sorted(selected))


__all__ = ["FundamentalObservation", "fundamentals_as_of"]
