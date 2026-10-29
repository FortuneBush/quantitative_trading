"""Deterministic, cache-only decision dashboard aggregation.

The reference project puts a daily decision dashboard at the centre of its
user experience.  This module provides the same useful shape for the local
monitor without adding an LLM, a news fetch, a notification side effect, or an
order path.  It only combines already-normalized domain objects and explicit
runtime readiness metadata.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from quant_monitor.domain import Alert, Quote, Signal, ensure_market_time, now_market_time


_ACTIONS = ("buy", "hold", "sell")
_FRESHNESS_STATES = ("fresh", "stale", "future_timestamp", "unknown", "untracked")


def _timestamp(value: Any) -> Optional[datetime]:
    candidate = getattr(value, "timestamp", None)
    if not isinstance(candidate, datetime):
        return None
    try:
        return ensure_market_time(candidate)
    except (TypeError, ValueError):
        return None


def _action(value: Any) -> str:
    raw = getattr(value, "action", value)
    return str(getattr(raw, "value", raw or "unknown")).strip().lower() or "unknown"


def _finite_float(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _quote_change_pct(quote: Any) -> Optional[float]:
    last = getattr(quote, "last", None)
    previous = getattr(quote, "previous_close", None)
    if last is None or previous is None:
        return None
    try:
        previous_decimal = previous if isinstance(previous, Decimal) else Decimal(str(previous))
        if previous_decimal == 0:
            return None
        return _finite_float(
            (last if isinstance(last, Decimal) else Decimal(str(last)))
            / previous_decimal
            * Decimal("100")
            - Decimal("100")
        )
    except (ArithmeticError, TypeError, ValueError):
        return None


def _freshness(
    timestamp: Optional[datetime],
    *,
    reference_time: datetime,
    stale_after_seconds: Optional[float],
) -> Tuple[str, Optional[float]]:
    if timestamp is None:
        return "unknown", None
    age = (reference_time - timestamp).total_seconds()
    if age < -5.0:
        return "future_timestamp", round(age, 3)
    if stale_after_seconds is None:
        return "untracked", round(max(0.0, age), 3)
    try:
        threshold = float(stale_after_seconds)
    except (TypeError, ValueError):
        threshold = None
    if threshold is None or threshold < 0:
        return "untracked", round(max(0.0, age), 3)
    return ("stale" if age > threshold else "fresh"), round(max(0.0, age), 3)


def _signal_sort_key(signal: Any) -> Tuple[Any, ...]:
    timestamp = _timestamp(signal)
    return (
        timestamp or datetime.min.replace(tzinfo=now_market_time().tzinfo),
        int(getattr(signal, "id", 0) or 0),
        str(getattr(signal, "strategy", "")),
        _action(signal),
        str(getattr(signal, "symbol", "")),
    )


def _quote_sort_key(quote: Any) -> Tuple[Any, ...]:
    """Choose a deterministic winner when a provider replays equal timestamps."""

    timestamp = _timestamp(quote)
    numeric_fields = (
        "last",
        "previous_close",
        "open",
        "high",
        "low",
        "volume",
        "turnover",
    )

    def numeric(value: Any) -> Decimal:
        try:
            return Decimal(str(value))
        except (ArithmeticError, TypeError, ValueError):
            return Decimal("-Infinity")

    return (
        timestamp or datetime.min.replace(tzinfo=now_market_time().tzinfo),
        tuple(numeric(getattr(quote, name, None)) for name in numeric_fields),
        str(getattr(quote, "source", "")),
    )


def _alert_sort_key(alert: Any) -> Tuple[Any, ...]:
    timestamp = _timestamp(alert)
    return (
        timestamp or datetime.min.replace(tzinfo=now_market_time().tzinfo),
        int(getattr(alert, "id", 0) or 0),
        str(getattr(alert, "symbol", "") or ""),
        str(getattr(getattr(alert, "level", None), "value", getattr(alert, "level", ""))),
    )


def _top_quote_rows(rows: Sequence[Mapping[str, Any]], *, reverse: bool) -> List[Dict[str, Any]]:
    candidates = [row for row in rows if row.get("pct_change") is not None]
    candidates.sort(
        key=lambda row: (float(row["pct_change"]), str(row.get("symbol", ""))),
        reverse=reverse,
    )
    return [dict(row) for row in candidates[:3]]


def build_decision_digest(
    quotes: Sequence[Quote],
    signals: Sequence[Signal] = (),
    alerts: Sequence[Alert] = (),
    *,
    configured_symbols: Sequence[str] = (),
    instrument_names: Optional[Mapping[str, str]] = None,
    provider: str = "unknown",
    market_status: Optional[Mapping[str, Any]] = None,
    data_readiness: Optional[Mapping[str, Any]] = None,
    research_readiness: Optional[Mapping[str, Any]] = None,
    generated_at: Optional[datetime] = None,
    as_of: Optional[datetime] = None,
    stale_after_seconds: Optional[float] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """Build a stable daily dashboard from persisted/cache objects.

    The function is intentionally side-effect free.  Missing prices, prior
    closes and quality evidence remain ``None`` or explicit states; no value is
    filled with zero.  The output is suitable for JSON, UI rendering and an
    offline Markdown export.
    """

    try:
        item_limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        item_limit = 50
    generated = ensure_market_time(generated_at) if generated_at is not None else now_market_time()
    reference = ensure_market_time(as_of) if as_of is not None else generated
    names = {str(key).upper(): str(value) for key, value in (instrument_names or {}).items()}

    # Keep the latest quote per symbol and make ties deterministic.  Providers
    # may replay a timestamp, so list order must never decide the dashboard.
    latest_quotes: Dict[str, Any] = {}
    for quote in quotes or ():
        symbol = str(getattr(quote, "symbol", "")).upper()
        if not symbol:
            continue
        previous = latest_quotes.get(symbol)
        if previous is None or _quote_sort_key(quote) >= _quote_sort_key(previous):
            latest_quotes[symbol] = quote

    ordered_signals = sorted(tuple(signals or ()), key=_signal_sort_key, reverse=True)
    latest_signals: Dict[str, Any] = {}
    for signal in ordered_signals:
        symbol = str(getattr(signal, "symbol", "")).upper()
        if symbol and symbol not in latest_signals:
            latest_signals[symbol] = signal

    configured = {str(symbol).upper() for symbol in configured_symbols if str(symbol).strip()}
    symbols = sorted(configured | set(latest_quotes) | set(latest_signals))
    quote_rows: List[Dict[str, Any]] = []
    freshness_counts = {state: 0 for state in _FRESHNESS_STATES}
    advancers = decliners = unchanged = unknown_change = 0
    for symbol in symbols:
        quote = latest_quotes.get(symbol)
        timestamp = _timestamp(quote) if quote is not None else None
        freshness, age = _freshness(
            timestamp,
            reference_time=reference,
            stale_after_seconds=stale_after_seconds,
        )
        freshness_counts[freshness] = freshness_counts.get(freshness, 0) + 1
        change_pct = _quote_change_pct(quote) if quote is not None else None
        if change_pct is None:
            unknown_change += 1
        elif change_pct > 0:
            advancers += 1
        elif change_pct < 0:
            decliners += 1
        else:
            unchanged += 1
        signal = latest_signals.get(symbol)
        signal_payload = None
        if signal is not None:
            signal_timestamp = _timestamp(signal)
            signal_payload = {
                "strategy": str(getattr(signal, "strategy", "")),
                "action": _action(signal),
                "strength": _finite_float(getattr(signal, "strength", None)),
                "timestamp": signal_timestamp.isoformat() if signal_timestamp else None,
                "reason": str(getattr(signal, "reason", "")),
            }
        quote_rows.append(
            {
                "symbol": symbol,
                "name": names.get(symbol, symbol),
                "price": str(getattr(quote, "last")) if quote is not None and getattr(quote, "last", None) is not None else None,
                "pct_change": change_pct,
                "timestamp": timestamp.isoformat() if timestamp else None,
                "age_seconds": age,
                "freshness": freshness,
                "signal": signal_payload,
            }
        )

    signal_counts = {action: 0 for action in _ACTIONS}
    signal_counts["unknown"] = 0
    for signal in latest_signals.values():
        action = _action(signal)
        signal_counts[action if action in _ACTIONS else "unknown"] += 1

    ordered_alerts = sorted(tuple(alerts or ()), key=_alert_sort_key, reverse=True)
    alert_counts = {"info": 0, "warning": 0, "critical": 0, "unknown": 0}
    for alert in ordered_alerts:
        level = str(
            getattr(
                getattr(alert, "level", None),
                "value",
                getattr(alert, "level", "unknown"),
            )
        ).lower()
        alert_counts[level if level in alert_counts else "unknown"] += 1
    alert_rows: List[Dict[str, Any]] = []
    for alert in ordered_alerts[:item_limit]:
        level = str(getattr(getattr(alert, "level", None), "value", getattr(alert, "level", "unknown"))).lower()
        timestamp = _timestamp(alert)
        alert_rows.append(
            {
                "id": getattr(alert, "id", None),
                "symbol": getattr(alert, "symbol", None),
                "level": level,
                "title": str(getattr(alert, "title", "")),
                "message": str(getattr(alert, "message", "")),
                "category": str(getattr(alert, "category", "monitor")),
                "acknowledged": bool(getattr(alert, "acknowledged", False)),
                "timestamp": timestamp.isoformat() if timestamp else None,
            }
        )

    warnings: List[str] = []
    if freshness_counts.get("stale", 0):
        warnings.append("stale_quotes_present")
    if freshness_counts.get("future_timestamp", 0):
        warnings.append("future_quote_timestamp_present")
    missing_count = len(configured - set(latest_quotes))
    if missing_count:
        warnings.append("configured_symbols_missing_quotes")
    if any(
        str(
            getattr(
                getattr(alert, "level", None),
                "value",
                getattr(alert, "level", "unknown"),
            )
        ).lower()
        in {"warning", "critical"}
        and not bool(getattr(alert, "acknowledged", False))
        for alert in ordered_alerts
    ):
        warnings.append("unacknowledged_alerts_present")
    if not latest_signals:
        warnings.append("no_persisted_signals")
    if not symbols:
        warnings.append("empty_dashboard_cache")
    if data_readiness and data_readiness.get("ready") is False:
        warnings.append("market_data_not_ready")
    if research_readiness and research_readiness.get("ready") is False:
        warnings.append("research_history_not_ready")

    observed_timestamps = [timestamp for timestamp in (_timestamp(item) for item in latest_quotes.values()) if timestamp]
    observed_as_of = max(observed_timestamps, default=reference)
    research = dict(research_readiness or {})
    research.setdefault("ready", False)
    research.setdefault("reason", "research_evidence_is_not_certified")
    research.update(
        {
            "research_only": True,
            "orders_supported": False,
            # The dashboard aggregates cache evidence but does not attest a
            # decision-grade run.  Keep that stronger gate explicit even when
            # the minimum history bootstrap is complete.
            "decision_ready": False,
            "decision_ready_reason": "dashboard_aggregation_not_certified",
        }
    )
    readiness = dict(data_readiness or {})
    readiness.setdefault("ready", bool(symbols) and missing_count == 0)
    readiness["freshness_counts"] = dict(freshness_counts)
    readiness["configured_symbols"] = len(configured)
    readiness["quoted_symbols"] = len(latest_quotes)
    readiness["missing_symbols"] = sorted(configured - set(latest_quotes))

    payload = {
        "schema_version": "decision_digest_v1",
        "generated_at": generated.isoformat(),
        "as_of": observed_as_of.isoformat() if observed_timestamps else None,
        "provider": str(provider),
        "market_status": dict(market_status or {}),
        "summary": {
            "configured_symbols": len(configured),
            "quoted_symbols": len(latest_quotes),
            "missing_quotes": missing_count,
            "advancers": advancers,
            "decliners": decliners,
            "unchanged": unchanged,
            "unknown_change": unknown_change,
            "signal_counts": signal_counts,
            "alert_counts": alert_counts,
            "warning_count": len(warnings),
        },
        "data_quality": readiness,
        "research_assurance": research,
        "rankings": {
            "top_gainers": _top_quote_rows(quote_rows, reverse=True),
            "top_decliners": _top_quote_rows(quote_rows, reverse=False),
        },
        "items": quote_rows[:item_limit],
        "signals": [
            {
                "id": getattr(signal, "id", None),
                "symbol": getattr(signal, "symbol", None),
                "strategy": getattr(signal, "strategy", None),
                "action": _action(signal),
                "strength": _finite_float(getattr(signal, "strength", None)),
                "price": str(getattr(signal, "price")) if getattr(signal, "price", None) is not None else None,
                "reason": str(getattr(signal, "reason", "")),
                "timestamp": _timestamp(signal).isoformat() if _timestamp(signal) else None,
            }
            for signal in ordered_signals[:item_limit]
        ],
        "alerts": alert_rows,
        "warnings": warnings,
        "research_only": True,
        "orders_supported": False,
        "notice": "本日报仅聚合本地缓存与研究信号，不构成投资建议或交易指令。",
    }
    return payload


def render_decision_digest_markdown(payload: Mapping[str, Any]) -> str:
    """Render a bounded, human-readable Markdown export from a digest."""

    summary = payload.get("summary") or {}
    lines = [
        "# 量化研究决策日报",
        "",
        "- 生成时间：{}".format(payload.get("generated_at") or "未知"),
        "- 行情截至：{}".format(payload.get("as_of") or "未知"),
        "- 数据源：{}".format(payload.get("provider") or "未知"),
        "- 研究边界：仅研究信号，不支持下单",
        "",
        "## 市场摘要",
        "",
        "- 覆盖：{}/{}，缺失 {}".format(summary.get("quoted_symbols", 0), summary.get("configured_symbols", 0), summary.get("missing_quotes", 0)),
        "- 上涨 / 下跌 / 平盘 / 未知：{} / {} / {} / {}".format(summary.get("advancers", 0), summary.get("decliners", 0), summary.get("unchanged", 0), summary.get("unknown_change", 0)),
        "- 信号：买入 {}，持有 {}，卖出 {}".format(
            *(summary.get("signal_counts", {}).get(key, 0) for key in ("buy", "hold", "sell"))
        ),
        "",
        "## 重点标的",
        "",
    ]
    items = payload.get("items") or []
    if items:
        lines.append("| 证券 | 价格 | 涨跌幅 | 新鲜度 | 最新信号 |")
        lines.append("|---|---:|---:|---|---|")
        for item in items:
            signal = item.get("signal") or {}
            lines.append("| {symbol} {name} | {price} | {change} | {freshness} | {action} |".format(
                symbol=item.get("symbol", ""),
                name=item.get("name", ""),
                price=item.get("price") if item.get("price") is not None else "—",
                change=("{:.2f}%".format(item["pct_change"]) if item.get("pct_change") is not None else "—"),
                freshness=item.get("freshness", "unknown"),
                action=signal.get("action", "—"),
            ))
    else:
        lines.append("暂无本地缓存行情。")
    warnings = payload.get("warnings") or []
    lines.extend(["", "## 数据质量与限制", ""])
    lines.append("- 研究就绪：{}".format("是" if (payload.get("research_assurance") or {}).get("ready") else "否"))
    lines.append("- 数据就绪：{}".format("是" if (payload.get("data_quality") or {}).get("ready") else "否"))
    lines.append("- 警告：{}".format("、".join(str(item) for item in warnings) if warnings else "无"))
    lines.extend(["", str(payload.get("notice") or "")])
    return "\n".join(lines).strip() + "\n"


__all__ = ["build_decision_digest", "render_decision_digest_markdown"]
