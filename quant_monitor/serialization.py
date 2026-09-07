"""Stable JSON adapters for domain objects."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Mapping, Sequence

from quant_monitor.domain import Alert, Bar, Metric, Quote, Signal, now_market_time


def jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    if isinstance(value, BaseException):
        return {"error_type": type(value).__name__, "message": str(value)}
    return value


def quote_dict(
    quote: Quote,
    name: str = None,
    stale_after_seconds: float = None,
) -> Dict[str, Any]:
    previous = quote.previous_close
    change = quote.last - previous if previous is not None else None
    percentage = change / previous * Decimal("100") if previous else None
    result = jsonable(quote)
    age_seconds = max(0.0, (now_market_time() - quote.timestamp).total_seconds())
    stale = (
        bool(stale_after_seconds is not None and age_seconds > float(stale_after_seconds))
    )
    result.update(
        {
            "name": name or quote.symbol,
            "price": str(quote.last),
            "prev_close": str(previous) if previous is not None else None,
            "change": str(change) if change is not None else None,
            "pct_change": float(percentage) if percentage is not None else None,
            "amount": str(quote.turnover),
            "updated_at": quote.timestamp.isoformat(),
            "data_age_seconds": round(age_seconds, 3),
            "stale": stale,
        }
    )
    return result


def bar_dict(bar: Bar) -> Dict[str, Any]:
    result = jsonable(bar)
    result["amount"] = str(bar.turnover)
    result["time"] = bar.timestamp.isoformat()
    return result


def alert_dict(alert: Alert) -> Dict[str, Any]:
    result = jsonable(alert)
    result["severity"] = alert.level.value
    result["created_at"] = alert.timestamp.isoformat()
    return result


def signal_dict(signal: Signal) -> Dict[str, Any]:
    result = jsonable(signal)
    result["strategy_name"] = signal.strategy
    result["created_at"] = signal.timestamp.isoformat()
    return result
