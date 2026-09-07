"""Events exchanged inside the monitoring process."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from .models import ensure_market_time, now_market_time


class EventType(str, Enum):
    QUOTE = "quote"
    BAR = "bar"
    METRIC = "metric"
    ALERT = "alert"
    SIGNAL = "signal"
    ERROR = "error"
    HEARTBEAT = "heartbeat"


@dataclass(frozen=True)
class MarketEvent:
    type: EventType
    payload: Any
    timestamp: datetime = field(default_factory=now_market_time)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        event_type = self.type if isinstance(self.type, EventType) else EventType(self.type)
        object.__setattr__(self, "type", event_type)
        object.__setattr__(self, "timestamp", ensure_market_time(self.timestamp))
        object.__setattr__(self, "metadata", dict(self.metadata or {}))
