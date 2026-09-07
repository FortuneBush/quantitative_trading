"""Deterministic research universes built from provider security masters.

Provider catalogues remain authoritative whenever they contain a usable
classification.  The static bank list exists only so the local dashboard can
still expose a useful, clearly labelled universe before a catalogue has been
downloaded or while the upstream reference endpoint is unavailable.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Tuple

from quant_monitor.domain import Exchange, Instrument, SecurityType


# All 42 mainland-listed banks in the local fallback snapshot.  Keep this
# mapping deliberately small and auditable; dynamic AShareHub industry data is
# preferred as soon as it yields at least one active bank stock.
BANK_STOCK_FALLBACK: Tuple[Tuple[str, str], ...] = (
    ("000001.SZ", "平安银行"),
    ("001227.SZ", "兰州银行"),
    ("002142.SZ", "宁波银行"),
    ("002807.SZ", "江阴银行"),
    ("002839.SZ", "张家港行"),
    ("002936.SZ", "郑州银行"),
    ("002948.SZ", "青岛银行"),
    ("002958.SZ", "青农商行"),
    ("002966.SZ", "苏州银行"),
    ("600000.SH", "浦发银行"),
    ("600015.SH", "华夏银行"),
    ("600016.SH", "民生银行"),
    ("600036.SH", "招商银行"),
    ("600908.SH", "无锡银行"),
    ("600919.SH", "江苏银行"),
    ("600926.SH", "杭州银行"),
    ("600928.SH", "西安银行"),
    ("601009.SH", "南京银行"),
    ("601077.SH", "渝农商行"),
    ("601128.SH", "常熟银行"),
    ("601166.SH", "兴业银行"),
    ("601169.SH", "北京银行"),
    ("601187.SH", "厦门银行"),
    ("601229.SH", "上海银行"),
    ("601288.SH", "农业银行"),
    ("601328.SH", "交通银行"),
    ("601398.SH", "工商银行"),
    ("601528.SH", "瑞丰银行"),
    ("601577.SH", "长沙银行"),
    ("601658.SH", "邮储银行"),
    ("601665.SH", "齐鲁银行"),
    ("601818.SH", "光大银行"),
    ("601825.SH", "沪农商行"),
    ("601838.SH", "成都银行"),
    ("601860.SH", "紫金银行"),
    ("601916.SH", "浙商银行"),
    ("601939.SH", "建设银行"),
    ("601963.SH", "重庆银行"),
    ("601988.SH", "中国银行"),
    ("601997.SH", "贵阳银行"),
    ("601998.SH", "中信银行"),
    ("603323.SH", "苏农银行"),
)

BANK_STOCK_SYMBOLS: Tuple[str, ...] = tuple(
    symbol for symbol, _ in BANK_STOCK_FALLBACK
)

_BANK_INDUSTRY_FIELDS = (
    "industry",
    "l1_name",
    "industry_name",
    "sw_l1_name",
)


def _is_bank_metadata(metadata: Mapping[str, object]) -> bool:
    """Recognize the provider stock-list and SW2021 level-one field names."""

    return any(
        str(metadata.get(field) or "").strip() == "银行"
        for field in _BANK_INDUSTRY_FIELDS
    )


def is_active_bank_stock(instrument: Instrument) -> bool:
    """Return whether a normalized instrument is an active Shanghai/Shenzhen bank."""

    return (
        isinstance(instrument, Instrument)
        and instrument.active
        and instrument.security_type == SecurityType.STOCK
        and instrument.exchange in {Exchange.SH, Exchange.SZ}
        and _is_bank_metadata(instrument.metadata)
    )


def fallback_bank_instruments() -> Tuple[Instrument, ...]:
    """Build the labelled static fallback without network or mutable state."""

    return tuple(
        Instrument(
            symbol=symbol,
            name=name,
            exchange=Exchange(symbol.rsplit(".", 1)[1]),
            security_type=SecurityType.STOCK,
            active=True,
            metadata={
                "source": "static_bank_fallback",
                "industry": "银行",
                "industry_standard": "static_fallback",
                "universe": "bank",
            },
        )
        for symbol, name in BANK_STOCK_FALLBACK
    )


def select_bank_instruments(
    instruments: Iterable[Instrument],
) -> Tuple[Instrument, ...]:
    """Prefer a non-empty dynamic industry result, otherwise use the fallback.

    Dynamic results are de-duplicated and sorted so downstream quote baskets,
    cache keys and tests remain deterministic.  A partially populated dynamic
    catalogue is not silently mixed with the fallback because that would hide
    upstream classification quality issues and create ambiguous provenance.
    """

    by_symbol = {
        item.symbol: item
        for item in instruments
        if is_active_bank_stock(item)
    }
    if by_symbol:
        return tuple(by_symbol[symbol] for symbol in sorted(by_symbol))
    return fallback_bank_instruments()


__all__ = [
    "BANK_STOCK_FALLBACK",
    "BANK_STOCK_SYMBOLS",
    "fallback_bank_instruments",
    "is_active_bank_stock",
    "select_bank_instruments",
]
