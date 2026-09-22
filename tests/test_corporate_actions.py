"""Offline tests for explicit corporate-action and factor contracts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal

import pytest

from quant_monitor.domain import (
    Adjustment,
    AdjustmentFactor,
    AdjustedBar,
    Bar,
    CorporateAction,
    CorporateActionType,
    apply_corporate_action,
    parse_market_time,
)


INPUT_HASH = "a" * 64
RECEIVED = parse_market_time("2026-09-04T16:00:00+08:00")


def _action(**overrides):
    payload = {
        "action_id": "600000.SH-2026-09-04-div-1",
        "symbol": "600000.SH",
        "action_type": CorporateActionType.CASH_DIVIDEND,
        "ex_date": date(2026, 9, 4),
        "announcement_date": date(2026, 8, 20),
        "cash_amount": "0.35",
        "source": "fixture-vendor",
        "revision_id": "vendor-revision-1",
        "received_at": RECEIVED,
    }
    payload.update(overrides)
    return CorporateAction(**payload)


def _factor(**overrides):
    payload = {
        "factor_id": "600000.SH-2026-09-04-qfq-1",
        "symbol": "600000.SH",
        "adjustment": Adjustment.QFQ,
        "session_date": date(2026, 9, 4),
        "benchmark_date": date(2026, 9, 4),
        "factor": "1.125",
        "factor_version": "fixture-factor-v1",
        "source": "fixture-vendor",
        "revision_id": "vendor-revision-1",
        "input_hash": INPUT_HASH,
        "computed_at": RECEIVED,
    }
    payload.update(overrides)
    return AdjustmentFactor(**payload)


def _bar():
    return Bar(
        symbol="600000.SH",
        interval="1d",
        timestamp=parse_market_time("2026-09-04T15:00:00+08:00"),
        open="10",
        high="11",
        low="9",
        close="10.5",
        volume=100,
        turnover="1050",
        source="fixture-raw",
    )


def test_cash_dividend_contract_is_typed_and_hash_is_deterministic():
    action = _action()
    assert action.action_type is CorporateActionType.CASH_DIVIDEND
    assert action.cash_amount == Decimal("0.35")
    assert len(action.content_hash) == 64
    assert action.content_hash == _action().content_hash
    with pytest.raises(TypeError):
        action.metadata["foo"] = "bar"


def test_action_terms_are_rejected_when_incompatible_with_kind():
    with pytest.raises(ValueError, match="cash_dividend requires cash_amount"):
        _action(cash_amount=None)
    with pytest.raises(ValueError, match="cash_dividend cannot include share_ratio"):
        _action(share_ratio="0.1")
    with pytest.raises(ValueError, match="rights_issue requires"):
        _action(action_type=CorporateActionType.RIGHTS_ISSUE, cash_amount=None)
    with pytest.raises(ValueError, match="announcement_date cannot follow"):
        _action(announcement_date=date(2026, 9, 5))
    with pytest.raises(TypeError, match="ex_date must be a date"):
        _action(ex_date="2026-09-04")
    with pytest.raises(ValueError, match="stock_split share_ratio"):
        _action(action_type=CorporateActionType.STOCK_SPLIT, cash_amount=None, share_ratio="1")
    with pytest.raises(ValueError, match="reverse_split share_ratio"):
        _action(action_type=CorporateActionType.REVERSE_SPLIT, cash_amount=None, share_ratio="2")


def test_factor_requires_auditable_identity_and_raw_is_identity_factor():
    with pytest.raises(ValueError, match="raw adjustment factor must equal 1"):
        _factor(adjustment=Adjustment.RAW, factor="1.01")
    with pytest.raises(ValueError, match="input_hash must be"):
        _factor(input_hash="not-a-hash")


def test_factor_returns_explicit_adjusted_view_without_mutating_raw_bar():
    factor = _factor()
    raw = _bar()
    adjusted = factor.apply_bar(raw)
    assert adjusted.raw_bar is raw
    assert adjusted.adjustment is Adjustment.QFQ
    assert adjusted.open == Decimal("11.250")
    assert adjusted.close == Decimal("11.8125")
    assert adjusted.to_bar().source == "fixture-raw|qfq|fixture-factor-v1"
    assert adjusted.to_bar().turnover == Decimal("1050")
    assert raw.close == Decimal("10.5")
    with pytest.raises(FrozenInstanceError):
        adjusted.factor_record = factor


def test_factor_rejects_symbol_or_session_mismatch():
    with pytest.raises(ValueError, match="bar symbol"):
        _factor(symbol="600001.SH").apply_bar(_bar())
    with pytest.raises(ValueError, match="bar date"):
        _factor(session_date=date(2026, 9, 5)).apply_bar(_bar())


def test_adjusted_view_constructor_enforces_identity_and_rejects_double_adjustment():
    factor = _factor()
    raw = _bar()
    # The dataclass is public, so validation must not be limited to apply_bar.
    with pytest.raises(ValueError, match="bar symbol"):
        AdjustedBar(
            raw_bar=Bar(
                symbol="600001.SH", interval="1d", timestamp=raw.timestamp,
                open="10", high="11", low="9", close="10.5",
                volume=100, turnover="1050", source="fixture-raw",
            ),
            factor_record=factor,
        )
    adjusted = factor.apply_bar(raw)
    with pytest.raises(ValueError, match="already a materialized adjustment"):
        factor.apply_bar(adjusted.to_bar())


def test_corporate_action_ledger_preserves_cash_and_applies_explicit_terms():
    effect = apply_corporate_action(
        _action(), quantity=Decimal("100"), cash=Decimal("1000")
    )
    assert effect.new_quantity == Decimal("100")
    assert effect.cash_delta == Decimal("35.00")
    assert effect.new_cash == Decimal("1035.00")
    assert effect.to_dict()["symbol"] == "600000.SH"

    split = _action(
        action_type=CorporateActionType.STOCK_SPLIT,
        cash_amount=None,
        share_ratio="2",
    )
    split_effect = apply_corporate_action(split, quantity=Decimal("100"), cash=Decimal("0"))
    assert split_effect.new_quantity == Decimal("200")
    assert split_effect.cash_delta == Decimal("0")


def test_rights_issue_is_opt_in_and_rejects_insufficient_cash():
    rights = _action(
        action_type=CorporateActionType.RIGHTS_ISSUE,
        cash_amount="0",
        share_ratio="0.2",
        subscription_price="5",
    )
    ignored = apply_corporate_action(
        rights, quantity=Decimal("100"), cash=Decimal("50"), subscribe_rights=False
    )
    assert ignored.new_quantity == Decimal("100")
    assert ignored.rights_entitlement == Decimal("20.0")
    with pytest.raises(ValueError, match="insufficient cash"):
        apply_corporate_action(
            rights, quantity=Decimal("100"), cash=Decimal("50"), subscribe_rights=True
        )
    subscribed = apply_corporate_action(
        rights, quantity=Decimal("100"), cash=Decimal("100"), subscribe_rights=True
    )
    assert subscribed.new_quantity == Decimal("120.0")
    assert subscribed.new_cash == Decimal("0.0")
