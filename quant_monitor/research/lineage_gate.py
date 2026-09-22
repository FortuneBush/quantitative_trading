"""Pure, fail-closed checks for research input lineage.

The backtest engine intentionally accepts the legacy :class:`Bar` projection.
This module gives callers a small, side-effect-free way to audit the stronger
``BarObservation`` evidence before treating a result as decision-ready.  It
never promotes ``BacktestDiagnostics.decision_grade``; the caller must still
bind the returned evidence to its own immutable research run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

from quant_monitor.domain import (
    BarObservation,
    CollectionBatch,
    CollectionBatchStatus,
    ObservationQualityStatus,
)
from quant_monitor.trading_calendar import (
    CalendarRangeError,
    DEFAULT_TRADING_CALENDAR,
    TradingCalendar,
)

from .manifest import hash_input_records


def _observation_record(item: BarObservation) -> Mapping[str, Any]:
    """Return the exact legacy-bar JSON shape used by ``bar_dict`` hashes."""

    return {
        "symbol": item.symbol,
        "interval": item.interval,
        "timestamp": item.period_end.isoformat(),
        "open": str(item.open),
        "high": str(item.high),
        "low": str(item.low),
        "close": str(item.close),
        "volume": item.volume,
        "turnover": str(item.turnover),
        "source": item.source,
        "amount": str(item.turnover),
        "time": item.period_end.isoformat(),
    }


@dataclass(frozen=True)
class LineageGateReport:
    """Explainable evidence result; ``verified`` is always derived."""

    checks: Mapping[str, bool]
    failures: Tuple[str, ...] = field(init=False)
    verified: bool = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.checks, Mapping):
            raise TypeError("checks must be a mapping")
        checks = {}
        for key, value in dict(self.checks).items():
            if not isinstance(key, str):
                raise TypeError("check names must be strings")
            if type(value) is not bool:
                raise TypeError("lineage checks must be booleans")
            checks[key] = value
        if not checks:
            raise ValueError("checks cannot be empty")
        if any(not key.strip() for key in checks):
            raise ValueError("check names cannot be empty")
        checks = dict(sorted(checks.items()))
        object.__setattr__(self, "checks", MappingProxyType(checks))
        failures = tuple(key for key, passed in checks.items() if not passed)
        object.__setattr__(self, "failures", failures)
        object.__setattr__(self, "verified", not failures)


def audit_observation_lineage(
    observations: Sequence[BarObservation],
    *,
    expected_input_hash: Optional[str] = None,
    expected_record_count: Optional[int] = None,
    expected_source: Optional[str] = None,
    decision_at: Optional[datetime] = None,
    batches: Optional[Iterable[CollectionBatch]] = None,
    require_accepted_quality: bool = True,
    calendar: Optional[TradingCalendar] = None,
) -> LineageGateReport:
    """Audit observations without network, database, or clock access.

    ``decision_at`` is required to pass availability attestation: absent
    decision time is intentionally a failed check rather than an assumption.
    Batch evidence is likewise required when ``batches`` is supplied; callers
    that omit it receive a conservative failure. Inputs are never coerced.

    ``calendar`` may be supplied to validate session dates against a finite,
    versioned calendar. When omitted, the explicit weekday fallback is used
    only to reject weekends; it cannot identify exchange holidays. This keeps
    the gate backwards-compatible while preventing an obviously invalid
    weekend observation from becoming decision evidence.
    """

    if not isinstance(observations, Sequence) or isinstance(observations, (str, bytes)):
        raise TypeError("observations must be a sequence")
    if any(not isinstance(item, BarObservation) for item in observations):
        raise TypeError("observations must contain BarObservation values")
    selected_calendar = calendar or DEFAULT_TRADING_CALENDAR
    if not isinstance(selected_calendar, TradingCalendar):
        raise TypeError("calendar must implement TradingCalendar")
    session_dates_ok = False
    if observations:
        try:
            session_dates_ok = all(
                bool(selected_calendar.is_session(item.period_end.date()))
                for item in observations
            )
        except (CalendarRangeError, TypeError, ValueError):
            # A finite calendar must not silently classify an unknown date as
            # a holiday. Unknown coverage therefore fails the evidence gate.
            session_dates_ok = False
    expected_count_ok = (
        type(expected_record_count) is int
        and expected_record_count >= 0
        and len(observations) == expected_record_count
        if expected_record_count is not None
        else False
    )
    source = str(expected_source).strip().casefold() if expected_source is not None else ""
    source_ok = bool(source) and bool(observations) and all(
        item.source.strip().casefold() == source
        or item.source.strip().casefold().startswith(source + ":")
        for item in observations
    )
    quality_ok = bool(observations) and all(
        item.quality_status == ObservationQualityStatus.ACCEPTED
        for item in observations
    )
    if not require_accepted_quality:
        quality_ok = bool(observations)
    availability_ok = False
    if bool(observations) and decision_at is not None:
        try:
            availability_ok = all(
                item.available_at is not None
                and item.available_at <= decision_at
                and item.received_at <= decision_at
                for item in observations
            )
        except TypeError:
            # A naive or otherwise incomparable decision timestamp cannot
            # prove point-in-time availability; retain fail-closed semantics.
            availability_ok = False
    unique_keys = [
        (item.symbol, item.interval, item.period_end, item.revision_id)
        for item in observations
    ]
    unique_ok = len(unique_keys) == len(set(unique_keys))
    hash_ok = False
    if expected_input_hash is not None:
        try:
            hash_ok = hash_input_records(_observation_record(item) for item in observations) == expected_input_hash
        except (TypeError, ValueError):
            hash_ok = False
    batch_ok = False
    batch_item_count_ok = False
    if batches is not None:
        batch_values = tuple(batches)
        # A succeeded batch is the immutable unit that produced the rows.  A
        # positive item_count alone is insufficient: a rebound/tampered batch
        # could claim success while its persisted count disagrees with the
        # observations actually used by the run.  Require one-to-one coverage
        # for every supplied batch and an exact count for each batch.
        batch_by_id = {batch.batch_id: batch for batch in batch_values}
        batch_ok = bool(observations) and all(
            batch_by_id.get(item.batch_id) is not None
            and batch_by_id[item.batch_id].status == CollectionBatchStatus.SUCCEEDED
            and batch_by_id[item.batch_id].item_count > 0
            for item in observations
        )
        observed_counts = {}
        for item in observations:
            observed_counts[item.batch_id] = observed_counts.get(item.batch_id, 0) + 1
        batch_item_count_ok = bool(observed_counts) and (
            len(batch_values) == len(observed_counts)
            and len(batch_by_id) == len(batch_values)
            and all(
                batch_by_id.get(batch_id) is not None
                and batch_by_id[batch_id].item_count == count
                for batch_id, count in observed_counts.items()
            )
        )
    checks = {
        "observations_present": bool(observations),
        "record_count_matches": expected_count_ok,
        "input_hash_matches": hash_ok,
        "source_matches": source_ok,
        "accepted_quality": quality_ok,
        "session_dates_valid": session_dates_ok,
        "availability_attested": availability_ok,
        "unique_revision_keys": unique_ok,
        "successful_collection_batches": batch_ok,
        "batch_item_count_matches": batch_item_count_ok,
    }
    return LineageGateReport(checks)


__all__ = ["LineageGateReport", "audit_observation_lineage"]
