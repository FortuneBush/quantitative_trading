"""China A-share market-session helpers used by the local dashboard.

The first release intentionally models regular weekdays only.  A real data
adapter should supply the official exchange calendar before these values are
used for trading decisions.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Dict, Optional

from quant_monitor.domain import ensure_market_time, now_market_time
from quant_monitor.trading_calendar import DEFAULT_TRADING_CALENDAR, TradingCalendar


def expected_latest_daily_date(
    at: datetime = None,
    *,
    calendar: Optional[TradingCalendar] = None,
) -> date:
    """Latest daily bar expected under the documented weekday fallback.

    A same-day bar is considered available only from 15:30, allowing time for
    the upstream end-of-day dataset to settle.  This is not an exchange holiday
    calendar and callers must expose that limitation to users.
    """

    current = ensure_market_time(at) if at is not None else now_market_time()
    selected = calendar or DEFAULT_TRADING_CALENDAR
    return selected.latest_published_session(current, time(15, 30))


def market_status(
    at: datetime = None,
    *,
    calendar: Optional[TradingCalendar] = None,
) -> Dict[str, Any]:
    """Describe the regular A-share session at a market-local timestamp."""
    current = ensure_market_time(at) if at is not None else now_market_time()
    selected = calendar or DEFAULT_TRADING_CALENDAR
    is_session = selected.is_session(current.date())
    value = current.time().replace(tzinfo=None)

    phase = "closed"
    label = "休市"
    is_trading = False
    if is_session:
        if time(9, 15) <= value < time(9, 25):
            phase, label = "opening_auction", "开盘集合竞价"
        elif time(9, 25) <= value < time(9, 30):
            phase, label = "pre_open", "开盘等待"
        elif time(9, 30) <= value < time(11, 30):
            phase, label, is_trading = "morning", "上午连续竞价", True
        elif time(11, 30) <= value < time(13, 0):
            phase, label = "lunch_break", "午间休市"
        elif time(13, 0) <= value < time(14, 57):
            phase, label, is_trading = "afternoon", "下午连续竞价", True
        elif time(14, 57) <= value < time(15, 0):
            phase, label, is_trading = "closing_auction", "收盘集合竞价", True
        elif time(15, 0) <= value < time(15, 5):
            phase, label = "closed", "已收盘"

    return {
        "phase": phase,
        "label": label,
        "is_trading": is_trading,
        "is_trading_day": is_session,
        "trading_date": current.date().isoformat(),
        "timestamp": current.isoformat(),
        "calendar": selected.metadata.identifier,
        "calendar_notice": selected.metadata.notice,
        "calendar_source": selected.metadata.source,
        "calendar_version": selected.metadata.version,
        # Finite versioned calendars expose a deterministic content digest;
        # fallback/third-party calendars may not, so absence is explicit.
        "calendar_fingerprint": getattr(selected, "fingerprint", None),
        # No current calendar implementation has an authenticated official
        # attestation.  Do not propagate a truthy flag from a structural
        # Protocol implementation supplied by arbitrary application code.
        "calendar_verified": False,
    }
