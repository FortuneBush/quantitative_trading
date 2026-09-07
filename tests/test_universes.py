"""Offline tests for deterministic research-universe selection."""

from quant_monitor.domain import Exchange, Instrument, SecurityType
from quant_monitor.universes import (
    BANK_STOCK_SYMBOLS,
    fallback_bank_instruments,
    is_active_bank_stock,
    select_bank_instruments,
)


def instrument(
    symbol="600036.SH",
    name="招商银行",
    *,
    metadata=None,
    active=True,
    security_type=SecurityType.STOCK,
):
    return Instrument(
        symbol=symbol,
        name=name,
        exchange=Exchange(symbol.rsplit(".", 1)[1]),
        security_type=security_type,
        active=active,
        metadata=metadata or {},
    )


def test_static_bank_fallback_has_42_unique_shanghai_and_shenzhen_stocks():
    rows = fallback_bank_instruments()

    assert len(rows) == 42
    assert len(BANK_STOCK_SYMBOLS) == len(set(BANK_STOCK_SYMBOLS)) == 42
    assert {item.exchange for item in rows} == {Exchange.SH, Exchange.SZ}
    assert all(item.active and item.security_type == SecurityType.STOCK for item in rows)
    assert all(item.metadata["source"] == "static_bank_fallback" for item in rows)
    assert "000001.SZ" in BANK_STOCK_SYMBOLS
    assert "603323.SH" in BANK_STOCK_SYMBOLS


def test_non_empty_dynamic_stock_industry_result_takes_precedence():
    bank = instrument(metadata={"source": "asharehub", "industry": "银行"})
    liquor = instrument(
        "600519.SH",
        "贵州茅台",
        metadata={"source": "asharehub", "industry": "白酒"},
    )

    selected = select_bank_instruments([liquor, bank])

    assert selected == (bank,)
    assert selected[0].metadata["source"] == "asharehub"


def test_sw2021_level_one_industry_is_recognized_and_result_is_deterministic():
    shanghai = instrument(
        metadata={"source": "asharehub", "l1_name": "银行"}
    )
    shenzhen = instrument(
        "000001.SZ",
        "平安银行",
        metadata={"source": "asharehub", "sw_l1_name": "银行"},
    )

    selected = select_bank_instruments([shanghai, shenzhen, shanghai])

    assert [item.symbol for item in selected] == ["000001.SZ", "600036.SH"]
    assert all(is_active_bank_stock(item) for item in selected)


def test_inactive_non_stock_and_name_only_rows_do_not_count_as_dynamic_banks():
    inactive = instrument(metadata={"industry": "银行"}, active=False)
    index = instrument(metadata={"industry": "银行"}, security_type=SecurityType.INDEX)
    name_only = instrument(metadata={})

    selected = select_bank_instruments([inactive, index, name_only])

    assert len(selected) == 42
    assert all(item.metadata["source"] == "static_bank_fallback" for item in selected)
