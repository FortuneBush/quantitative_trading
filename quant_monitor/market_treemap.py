"""Cache-only industry/stock hierarchy for the market treemap dashboard.

The provider-neutral security master carries industry labels in ``Instrument``
metadata while quotes carry the latest move and turnover.  This module keeps
the aggregation pure and deliberately does not perform provider or database
I/O.  It is therefore safe to use from HTTP handlers, tests, and offline
replays.  The size of a tile is labelled as turnover (not market
capitalisation): the current free data contracts do not guarantee market-cap
fields, and inventing one would make the visualization look more precise than
the source data allows.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from quant_monitor.domain import Instrument, Quote


_INDUSTRY_KEYS: Tuple[str, ...] = (
    "industry",
    "sw_l1_name",
    "l1_name",
    "sector",
    "category",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def instrument_industry(instrument: Optional[Instrument]) -> str:
    """Resolve an instrument's provider classification without guessing.

    AShareHub has used ``industry`` historically, while imported security
    masters may use level-one aliases.  Keep the aliases in this adapter and
    expose an explicit ``未分类`` bucket when no source classification exists.
    """

    metadata = getattr(instrument, "metadata", {}) or {}
    for key in _INDUSTRY_KEYS:
        value = _text(metadata.get(key)) if isinstance(metadata, Mapping) else ""
        if value:
            return value[:80]
    return "未分类"


def _decimal(value: Any) -> Optional[Decimal]:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
        if result.is_finite():
            return result
    except (InvalidOperation, TypeError, ValueError):
        pass
    return None


def _pct_change(quote: Quote) -> Optional[float]:
    previous = quote.previous_close
    if previous in (None, Decimal("0")):
        return None
    return float((quote.last - previous) / previous * Decimal("100"))


def _tile_size(quote: Quote) -> Tuple[Decimal, str]:
    """Return a positive visual size and the evidence used for it.

    Turnover is the only provider-neutral liquidity/scale field available to
    both AShareHub and mock data.  For a zero-turnover quote use traded volume
    times last price, then a deterministic one-unit placeholder.  Missing
    values are not converted to a fake price or percentage.
    """

    turnover = _decimal(getattr(quote, "turnover", None))
    if turnover is not None and turnover > 0:
        return turnover, "turnover"
    try:
        volume = int(getattr(quote, "volume", 0) or 0)
    except (TypeError, ValueError):
        volume = 0
    last = _decimal(getattr(quote, "last", None))
    if volume > 0 and last is not None and last > 0:
        return last * volume, "volume_times_price"
    return Decimal("1"), "placeholder"


def _timestamp(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else None


def _median(values: Sequence[float]) -> Optional[float]:
    """Return a deterministic median for a non-empty numeric sequence."""

    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 6)
    return round((ordered[middle - 1] + ordered[middle]) / 2.0, 6)


def _breadth(items: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Summarize priced tiles without turning missing changes into zeroes."""

    known = [
        float(item["pct_change"])
        for item in items
        if item.get("pct_change") is not None
    ]
    advancers = sum(1 for value in known if value > 0)
    decliners = sum(1 for value in known if value < 0)
    unchanged = sum(1 for value in known if value == 0)
    return {
        "count": len(items),
        "known_change_count": len(known),
        "missing_change_count": max(0, len(items) - len(known)),
        "advancers": advancers,
        "decliners": decliners,
        "unchanged": unchanged,
        "average_pct_change": round(sum(known) / len(known), 6) if known else None,
        "median_pct_change": _median(known),
        "change_coverage": round(len(known) / len(items), 6) if items else 0.0,
        "total_volume": sum(int(item.get("volume") or 0) for item in items),
        "total_turnover": str(
            sum((_decimal(item.get("turnover")) or Decimal("0") for item in items), Decimal("0"))
        ),
    }


def _stock_tile(
    quote: Quote,
    instrument: Optional[Instrument],
    *,
    size: Decimal,
    size_basis: str,
) -> Dict[str, Any]:
    name = _text(getattr(instrument, "name", "")) or quote.symbol
    industry = instrument_industry(instrument)
    pct = _pct_change(quote)
    return {
        "id": quote.symbol,
        "symbol": quote.symbol,
        "name": name,
        "industry": industry,
        "exchange": getattr(getattr(instrument, "exchange", None), "value", None),
        "value": float(size),
        "size": float(size),
        "size_basis": size_basis,
        "price": str(quote.last),
        "last": str(quote.last),
        "previous_close": str(quote.previous_close) if quote.previous_close is not None else None,
        "pct_change": pct,
        "change_pct": pct,
        "volume": int(quote.volume),
        "turnover": str(quote.turnover),
        # Keep the common quote naming available to generic dashboard
        # components while retaining the explicit turnover semantics above.
        "amount": str(quote.turnover),
        "change": pct,
        "timestamp": _timestamp(quote.timestamp),
        "quote_timestamp": _timestamp(quote.timestamp),
        "source": quote.source,
        "quote_status": "available",
    }


def build_market_treemap(
    instruments: Sequence[Instrument],
    quotes: Sequence[Quote],
    *,
    max_industries: int = 200,
    max_stocks_per_industry: int = 500,
    source: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a deterministic industry → stock treemap payload.

    ``instruments`` may contain the complete cached catalogue while ``quotes``
    is a partial snapshot.  Only priced stocks become tiles; the response
    reports unpriced catalogue rows separately so an empty cache cannot be
    mistaken for an empty market.  Duplicate quotes are resolved by newest
    timestamp (then input order), and inactive/non-stock instruments are
    excluded from the visible tree.
    """

    max_industries = max(1, min(int(max_industries), 2000))
    max_stocks_per_industry = max(1, min(int(max_stocks_per_industry), 5000))
    instrument_by_symbol: Dict[str, Instrument] = {}
    for instrument in instruments or ():
        symbol = _text(getattr(instrument, "symbol", ""))
        if not symbol or getattr(instrument, "active", True) is False:
            continue
        security_type = getattr(getattr(instrument, "security_type", None), "value", getattr(instrument, "security_type", "stock"))
        if str(security_type).lower() != "stock":
            continue
        instrument_by_symbol[symbol] = instrument

    latest: Dict[str, Quote] = {}
    for quote in quotes or ():
        symbol = _text(getattr(quote, "symbol", ""))
        if not symbol or symbol not in instrument_by_symbol:
            continue
        current = latest.get(symbol)
        if current is None or quote.timestamp >= current.timestamp:
            latest[symbol] = quote

    groups: Dict[str, list] = defaultdict(list)
    for symbol, quote in latest.items():
        instrument = instrument_by_symbol[symbol]
        size, basis = _tile_size(quote)
        groups[instrument_industry(instrument)].append(
            _stock_tile(quote, instrument, size=size, size_basis=basis)
        )

    industries = []
    for industry, stocks in groups.items():
        stocks.sort(key=lambda item: (-float(item["value"]), item["symbol"]))
        visible = stocks[:max_stocks_per_industry]
        omitted = stocks[max_stocks_per_industry:]
        visible_value = sum((Decimal(str(item["value"])) for item in visible), Decimal("0"))
        percentages = [item["pct_change"] for item in visible if item["pct_change"] is not None]
        # Turnover-weighted move; if every size is a placeholder, a plain mean
        # is still more honest than treating missing values as zero.
        denominator = sum((Decimal(str(item["value"])) for item in visible if item["pct_change"] is not None), Decimal("0"))
        if percentages and denominator > 0:
            weighted = sum(
                Decimal(str(item["pct_change"])) * Decimal(str(item["value"]))
                for item in visible
                if item["pct_change"] is not None
            ) / denominator
            change = round(float(weighted), 6)
        else:
            change = None
        breadth = _breadth(visible)
        industries.append(
            {
                "id": "industry:" + industry,
                "name": industry,
                "industry": industry,
                "value": float(visible_value),
                "size": float(visible_value),
                "size_basis": "sum_stock_sizes",
                "pct_change": change,
                "change_pct": change,
                "advancers": sum(1 for item in visible if (item["pct_change"] or 0) > 0),
                "decliners": sum(1 for item in visible if (item["pct_change"] or 0) < 0),
                "unchanged": sum(1 for item in visible if item["pct_change"] == 0),
                "amount": str(
                    sum(
                        (Decimal(str(item.get("turnover") or "0")) for item in visible),
                        Decimal("0"),
                    )
                ),
                "priced_count": len(visible),
                "stock_count": len(visible),
                "omitted_count": len(omitted),
                "breadth": breadth,
                "average_pct_change": breadth["average_pct_change"],
                "median_pct_change": breadth["median_pct_change"],
                "total_volume": breadth["total_volume"],
                "total_turnover": breadth["total_turnover"],
                "top_gainers": sorted(
                    (
                        item
                        for item in visible
                        if item.get("pct_change") is not None and item["pct_change"] > 0
                    ),
                    key=lambda item: (-float(item["pct_change"]), item["symbol"]),
                )[:3],
                "top_decliners": sorted(
                    (
                        item
                        for item in visible
                        if item.get("pct_change") is not None and item["pct_change"] < 0
                    ),
                    key=lambda item: (float(item["pct_change"]), item["symbol"]),
                )[:3],
                "children": visible,
                # ``items`` is a compact alias used by lightweight treemap
                # clients that do not implement a generic hierarchy schema.
                "items": visible,
            }
        )
    industries.sort(key=lambda item: (-float(item["value"]), item["name"]))
    omitted_industries = industries[max_industries:]
    industries = industries[:max_industries]
    visible_symbols = sum(len(item["children"]) for item in industries)
    all_symbols = len(instrument_by_symbol)
    priced_symbols = len(latest)
    timestamps = [quote.timestamp for quote in latest.values()]
    as_of = max(timestamps).isoformat() if timestamps else None
    visible_items = [item for group in industries for item in group["children"]]
    breadth = _breadth(visible_items)
    return {
        "items": industries,
        "sectors": industries,
        "count": len(industries),
        "universe_count": all_symbols,
        "priced_count": priced_symbols,
        "visible_stock_count": visible_symbols,
        "unpriced_count": max(0, all_symbols - priced_symbols),
        "coverage": {
            "catalogue_count": all_symbols,
            "priced_count": priced_symbols,
            "unpriced_count": max(0, all_symbols - priced_symbols),
            "quote_coverage": round(priced_symbols / all_symbols, 6) if all_symbols else 0.0,
            "visible_stock_count": visible_symbols,
        },
        "omitted_industry_count": len(omitted_industries),
        "as_of": as_of,
        "summary": {
            "count": len(visible_items),
            **breadth,
        },
        "breadth": breadth,
        "visualization": "electronic_cloud",
        "data_version": "market-treemap-v2",
        "update_semantics": "latest_persisted_quote_by_symbol",
        "source": source,
        "size_semantics": "turnover_preferred; volume_times_price_fallback; placeholder_when_missing",
        "change_semantics": "turnover_weighted_pct_change",
        "served_from": "sqlite_cache",
        "upstream_requests": 0,
        "notice": "行业分类和行情均来自本地缓存；图块大小优先使用成交额，不等同于市值。仅供研究展示。",
    }


__all__ = ["build_market_treemap", "instrument_industry"]
