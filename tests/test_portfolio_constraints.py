from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant_monitor.backtesting import (
    PortfolioConstraintPolicy,
    build_portfolio_snapshot,
    evaluate_portfolio_constraints,
)
from quant_monitor.backtesting.models import Position


AS_OF = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)


def _position(symbol: str, value: str, quantity: int = 100) -> Position:
    price = Decimal(value) / quantity
    return Position(
        symbol=symbol,
        quantity=quantity,
        settled_quantity=quantity,
        average_cost=price,
        last_price=price,
        market_value=Decimal(value),
        unrealized_pnl=Decimal("0"),
    )


def test_constraint_report_exposes_policy_and_passes_complete_snapshot():
    snapshot = build_portfolio_snapshot(
        as_of=AS_OF,
        cash=Decimal("50"),
        positions={
            "600519.SH": _position("600519.SH", "25"),
            "600036.SH": _position("600036.SH", "25"),
        },
        valuation_quality="complete",
    )
    report = evaluate_portfolio_constraints(snapshot)
    assert report.status == "pass"
    assert report.decision_ready is True
    assert report.policy.name == "long_only_conservative_v1"
    assert {item.name for item in report.checks} == {
        "max_holding_weight",
        "max_concentration_hhi",
        "min_cash_weight",
        "max_gross_exposure",
        "max_positions",
    }
    assert all(item.passed for item in report.checks)


def test_constraint_violation_is_fail_and_partial_mark_is_unavailable():
    snapshot = build_portfolio_snapshot(
        as_of=AS_OF,
        cash=Decimal("10"),
        positions={"600519.SH": _position("600519.SH", "90")},
        valuation_quality="partial",
    )
    report = evaluate_portfolio_constraints(
        snapshot,
        policy=PortfolioConstraintPolicy(
            max_holding_weight=0.5,
            max_concentration_hhi=None,
            min_cash_weight=0.2,
            max_gross_exposure=1.0,
            max_positions=None,
        ),
    )
    assert report.status == "fail"
    assert report.decision_ready is False
    assert "valuation_quality_not_complete" in report.warnings
    assert any(item.name == "max_holding_weight" and not item.passed for item in report.checks)
    assert any(item.name == "min_cash_weight" and not item.passed for item in report.checks)


def test_constraint_policy_validation_and_disabled_limits():
    policy = PortfolioConstraintPolicy(
        max_holding_weight=None,
        max_concentration_hhi=None,
        min_cash_weight=None,
        max_gross_exposure=None,
        max_positions=None,
    )
    snapshot = build_portfolio_snapshot(as_of=AS_OF, cash=Decimal("100"), positions={})
    report = evaluate_portfolio_constraints(snapshot, policy=policy)
    assert report.status == "unavailable"
    assert report.checks == ()
    assert "no_portfolio_constraints_configured" in report.warnings

    with pytest.raises(ValueError, match="between zero and one"):
        PortfolioConstraintPolicy(max_holding_weight=1.1)
    with pytest.raises(ValueError, match="max_positions must be positive"):
        PortfolioConstraintPolicy(max_positions=0)
