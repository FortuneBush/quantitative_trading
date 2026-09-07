"""Immutable collection-lineage models for auditable market observations.

These models deliberately separate a provider response from the legacy
``Bar`` projection.  A bar observation records both market-period semantics
and the wall-clock times at which this process received and persisted it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional

from .models import Bar, SHANGHAI_TZ, ensure_market_time, normalize_symbol


class CollectionBatchStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


class Adjustment(str, Enum):
    RAW = "raw"
    QFQ = "qfq"
    HFQ = "hfq"


class ObservationQualityStatus(str, Enum):
    ACCEPTED = "accepted"
    DEGRADED = "degraded"
    QUARANTINED = "quarantined"
    UNTRUSTED_LEGACY = "untrusted_legacy"


def _required_text(value: Any, name: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError("{} cannot be empty".format(name))
    if "\x00" in text:
        raise ValueError("{} cannot contain NUL".format(name))
    return text


def _metadata(value: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise TypeError("metadata must be a mapping")
    return MappingProxyType(dict(value.items()))


def normalized_request_hash(value: Any) -> str:
    """Hash non-secret normalized request coordinates.

    The caller is responsible for supplying only normalized identifiers and
    public request parameters; credentials and raw request URLs never belong
    in this value or in collection metadata.
    """

    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CollectionBatch:
    batch_id: str
    provider: str
    request_kind: str
    input_hash: str
    status: CollectionBatchStatus
    started_at: datetime
    completed_at: Optional[datetime] = None
    item_count: int = 0
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "batch_id", _required_text(self.batch_id, "batch_id"))
        object.__setattr__(self, "provider", _required_text(self.provider, "provider"))
        object.__setattr__(
            self, "request_kind", _required_text(self.request_kind, "request_kind")
        )
        input_hash = _required_text(self.input_hash, "input_hash").lower()
        if len(input_hash) != 64 or any(char not in "0123456789abcdef" for char in input_hash):
            raise ValueError("input_hash must be a lowercase SHA-256 hex digest")
        object.__setattr__(self, "input_hash", input_hash)
        status = (
            self.status
            if isinstance(self.status, CollectionBatchStatus)
            else CollectionBatchStatus(self.status)
        )
        object.__setattr__(self, "status", status)
        started_at = ensure_market_time(self.started_at)
        completed_at = (
            ensure_market_time(self.completed_at)
            if self.completed_at is not None
            else None
        )
        if status == CollectionBatchStatus.RUNNING and completed_at is not None:
            raise ValueError("running batch cannot have completed_at")
        if status != CollectionBatchStatus.RUNNING and completed_at is None:
            raise ValueError("terminal batch must have completed_at")
        if completed_at is not None and completed_at < started_at:
            raise ValueError("completed_at cannot precede started_at")
        if isinstance(self.item_count, bool) or int(self.item_count) < 0:
            raise ValueError("item_count must be a non-negative integer")
        if status in (CollectionBatchStatus.FAILED, CollectionBatchStatus.ABANDONED) and not self.error_type:
            raise ValueError("failed or abandoned batch must have error_type")
        if status not in (CollectionBatchStatus.FAILED, CollectionBatchStatus.ABANDONED) and (
            self.error_type is not None or self.error_message is not None
        ):
            raise ValueError("only failed or abandoned batches may contain error details")
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "completed_at", completed_at)
        object.__setattr__(self, "item_count", int(self.item_count))
        object.__setattr__(
            self,
            "error_type",
            _required_text(self.error_type, "error_type")
            if self.error_type is not None
            else None,
        )
        object.__setattr__(
            self,
            "error_message",
            str(self.error_message) if self.error_message is not None else None,
        )
        object.__setattr__(self, "metadata", _metadata(self.metadata))


@dataclass(frozen=True)
class BarObservation:
    batch_id: str
    symbol: str
    interval: str
    session_date: date
    period_start: datetime
    period_end: datetime
    available_at: Optional[datetime]
    received_at: datetime
    persisted_at: datetime
    source: str
    adjustment: Adjustment
    revision_id: str
    quality_status: ObservationQualityStatus
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int = 0
    turnover: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        object.__setattr__(self, "batch_id", _required_text(self.batch_id, "batch_id"))
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        interval = _required_text(self.interval, "interval").lower()
        object.__setattr__(self, "interval", interval)
        if not isinstance(self.session_date, date) or isinstance(self.session_date, datetime):
            raise TypeError("session_date must be a date")
        period_start = ensure_market_time(self.period_start)
        period_end = ensure_market_time(self.period_end)
        available_at = (
            ensure_market_time(self.available_at)
            if self.available_at is not None
            else None
        )
        received_at = ensure_market_time(self.received_at)
        persisted_at = ensure_market_time(self.persisted_at)
        quality = (
            self.quality_status
            if isinstance(self.quality_status, ObservationQualityStatus)
            else ObservationQualityStatus(self.quality_status)
        )
        if period_end <= period_start:
            raise ValueError("period_end must be after period_start")
        if available_at is not None and available_at < period_end:
            raise ValueError("available_at cannot precede period_end")
        if available_at is not None and available_at > received_at:
            raise ValueError("available_at cannot follow received_at")
        if quality != ObservationQualityStatus.UNTRUSTED_LEGACY and available_at is None:
            raise ValueError("non-legacy observation requires available_at")
        if persisted_at < received_at:
            raise ValueError("persisted_at cannot precede received_at")
        if period_end.date() != self.session_date:
            raise ValueError("session_date must match period_end market date")
        adjustment = (
            self.adjustment
            if isinstance(self.adjustment, Adjustment)
            else Adjustment(self.adjustment)
        )
        source = _required_text(self.source, "source")
        revision_id = _required_text(self.revision_id, "revision_id")
        bar = Bar(
            symbol=self.symbol,
            interval=interval,
            timestamp=period_end,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            turnover=self.turnover,
            source=source,
        )
        object.__setattr__(self, "period_start", period_start)
        object.__setattr__(self, "period_end", period_end)
        object.__setattr__(self, "available_at", available_at)
        object.__setattr__(self, "received_at", received_at)
        object.__setattr__(self, "persisted_at", persisted_at)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "adjustment", adjustment)
        object.__setattr__(self, "revision_id", revision_id)
        object.__setattr__(self, "quality_status", quality)
        for name in ("open", "high", "low", "close", "volume", "turnover"):
            object.__setattr__(self, name, getattr(bar, name))

    def to_legacy_bar(self) -> Bar:
        return Bar(
            symbol=self.symbol,
            interval=self.interval,
            timestamp=self.period_end,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            turnover=self.turnover,
            source=self.source,
        )


def observation_from_bar(
    bar: Bar,
    *,
    batch_id: str,
    received_at: datetime,
    persisted_at: datetime,
    adjustment: Adjustment = Adjustment.RAW,
    quality_status: ObservationQualityStatus = ObservationQualityStatus.DEGRADED,
    revision_id: Optional[str] = None,
) -> BarObservation:
    """Create the conservative v3 projection of an already-normalized ``Bar``."""

    received = ensure_market_time(received_at)
    persisted = ensure_market_time(persisted_at)
    if bar.interval == "1d":
        period_start = datetime.combine(bar.timestamp.date(), time(9, 30), SHANGHAI_TZ)
        period_end = datetime.combine(bar.timestamp.date(), time(15, 0), SHANGHAI_TZ)
        # Existing providers do not expose a signed/historical publication
        # timestamp.  The earliest defensible knowledge time is therefore when
        # this process received the response, never the old bar timestamp.
        available_at: Optional[datetime] = received
    else:
        units = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}
        minutes = units.get(bar.interval)
        if minutes is None:
            raise ValueError(
                "cannot infer v3 period semantics for interval {}; persist an explicit "
                "BarObservation instead".format(bar.interval)
            )
        period_end = bar.timestamp
        period_start = period_end - timedelta(minutes=minutes)
        available_at = received
    adjustment_value = (
        adjustment if isinstance(adjustment, Adjustment) else Adjustment(adjustment)
    )
    if revision_id is None:
        revision_id = normalized_request_hash(
            {
                "adjustment": adjustment_value.value,
                "close": str(bar.close),
                "high": str(bar.high),
                "interval": bar.interval,
                "low": str(bar.low),
                "open": str(bar.open),
                "period_end": period_end.isoformat(timespec="microseconds"),
                "source": bar.source,
                "symbol": bar.symbol,
                "turnover": str(bar.turnover),
                "volume": bar.volume,
            }
        )
    return BarObservation(
        batch_id=batch_id,
        symbol=bar.symbol,
        interval=bar.interval,
        session_date=period_end.date(),
        period_start=period_start,
        period_end=period_end,
        available_at=available_at,
        received_at=received,
        persisted_at=persisted,
        source=bar.source,
        adjustment=adjustment_value,
        revision_id=revision_id,
        quality_status=quality_status,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        turnover=bar.turnover,
    )


__all__ = [
    "Adjustment",
    "BarObservation",
    "CollectionBatch",
    "CollectionBatchStatus",
    "ObservationQualityStatus",
    "normalized_request_hash",
    "observation_from_bar",
]
