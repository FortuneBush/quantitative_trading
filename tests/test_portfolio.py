from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant_monitor.backtesting import (
    BacktestConfig,
    BacktestDiagnostics,
    BacktestMetrics,
    BacktestResult,
    EquityPoint,
    PortfolioHolding,
    PortfolioSnapshot,
    build_portfolio_snapshot,
    portfolio_snapshot_from_payload,
    portfolio_snapshot_from_result,
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


def test_snapshot_enforces_accounting_identity_and_calculates_exposure():
    snapshot = build_portfolio_snapshot(
        as_of=AS_OF,
        cash=Decimal("40"),
        positions={
            "600519.SH": _position("600519.SH", "60"),
        },
        run_id="run-1",
        benchmark_name="CSI_BANK",
    )

    assert isinstance(snapshot, PortfolioSnapshot)
    assert snapshot.nav == Decimal("100")
    assert snapshot.cash_weight == pytest.approx(0.4)
    assert snapshot.max_holding_weight == pytest.approx(0.6)
    assert snapshot.concentration_hhi == pytest.approx(0.36)
    assert snapshot.to_dict()["holdings"][0]["symbol"] == "600519.SH"


def test_snapshot_carries_price_basis_and_detaches_nested_adjustment_binding():
    binding = {"status": "bound_unattested", "factor_versions": ["v1"], "meta": {"ids": ["f1"]}}
    snapshot = build_portfolio_snapshot(
        as_of=AS_OF,
        cash=Decimal("40"),
        positions={"600519.SH": _position("600519.SH", "60")},
        strategy_price_basis="qfq_v1",
        execution_price_basis="raw_unadjusted",
        valuation_price_basis="raw_unadjusted",
        adjustment_binding=binding,
    )
    binding["meta"]["ids"].append("f2")
    wire = snapshot.to_dict()
    wire["adjustment_binding"]["meta"]["ids"].append("f3")
    assert snapshot.strategy_price_basis == "qfq_v1"
    assert snapshot.execution_price_basis == "raw_unadjusted"
    assert snapshot.valuation_price_basis == "raw_unadjusted"
    assert snapshot.adjustment_binding["meta"]["ids"] == ("f1",)
    assert snapshot.to_dict()["adjustment_binding"]["meta"]["ids"] == ["f1"]


def test_unknown_or_missing_price_basis_is_unavailable_not_raw():
    snapshot = build_portfolio_snapshot(
        as_of=AS_OF,
        cash=Decimal("40"),
        positions={"600519.SH": _position("600519.SH", "60")},
        strategy_price_basis="vendor_magic_basis",
    )
    assert snapshot.strategy_price_basis == "unavailable"
    assert snapshot.execution_price_basis == "unavailable"
    assert snapshot.valuation_price_basis == "unavailable"


def test_snapshot_rejects_mismatched_keys_and_duplicate_symbols():
    with pytest.raises(ValueError, match="position key"):
        build_portfolio_snapshot(
            as_of=AS_OF,
            cash=Decimal("40"),
            positions={"600519.sz": _position("600519.SH", "60")},
        )

    with pytest.raises(ValueError, match="duplicate normalized"):
        build_portfolio_snapshot(
            as_of=AS_OF,
            cash=Decimal("40"),
            positions={
                "600519.SH": _position("600519.SH", "30"),
                "600519.sh": _position("600519.SH", "30"),
            },
        )


def test_snapshot_rejects_non_positive_nav_and_invalid_quality():
    with pytest.raises(ValueError, match="NAV"):
        build_portfolio_snapshot(as_of=AS_OF, cash=Decimal("0"), positions={})

    with pytest.raises(ValueError, match="valuation_quality"):
        build_portfolio_snapshot(
            as_of=AS_OF,
            cash=Decimal("100"),
            positions={},
            valuation_quality="guessed",
        )


def test_snapshot_payload_rejects_tampered_nav():
    payload = {
        "equity_curve": [
            {
                "timestamp": "2026-01-05T15:00:00+08:00",
                "cash": "40",
                "market_value": "60",
                "nav": "101",
            }
        ],
        "positions": {
            "600519.SH": {
                "symbol": "600519.SH",
                "quantity": 100,
                "settled_quantity": 100,
                "average_cost": "0.60",
                "last_price": "0.60",
                "market_value": "60",
                "unrealized_pnl": "0",
            }
        },
        "diagnostics": {"decision_grade": False, "warnings": []},
    }
    with pytest.raises(ValueError, match="NAV"):
        portfolio_snapshot_from_payload(payload)


def test_holding_and_position_market_values_cannot_be_fabricated():
    with pytest.raises(ValueError, match=r"quantity \* last_price"):
        PortfolioHolding(
            symbol="600519.SH",
            quantity=100,
            settled_quantity=100,
            last_price=Decimal("0.60"),
            market_value=Decimal("59"),
            weight=0.59,
        )

    inconsistent = Position(
        symbol="600519.SH",
        quantity=100,
        settled_quantity=100,
        average_cost=Decimal("0.60"),
        last_price=Decimal("0.60"),
        market_value=Decimal("59"),
        unrealized_pnl=Decimal("0"),
    )
    with pytest.raises(ValueError, match=r"quantity \* last_price"):
        build_portfolio_snapshot(as_of=AS_OF, cash=Decimal("40"), positions={"600519.SH": inconsistent})


def test_snapshot_rejects_tampered_derived_exposures():
    holding = PortfolioHolding(
        symbol="600519.SH",
        quantity=100,
        settled_quantity=100,
        last_price=Decimal("0.60"),
        market_value=Decimal("60"),
        weight=0.60,
    )
    common = dict(
        run_id="run-1",
        as_of=AS_OF,
        nav=Decimal("100"),
        cash=Decimal("40"),
        invested_market_value=Decimal("60"),
        cash_weight=0.40,
        holdings=(holding,),
        max_holding_weight=0.60,
        concentration_hhi=0.36,
    )
    with pytest.raises(ValueError, match="cash_weight"):
        PortfolioSnapshot(**{**common, "cash_weight": 0.50})
    with pytest.raises(ValueError, match="max_holding_weight"):
        PortfolioSnapshot(**{**common, "max_holding_weight": 0.50})
    with pytest.raises(ValueError, match="concentration_hhi"):
        PortfolioSnapshot(**{**common, "concentration_hhi": 0.25})

    with pytest.raises(ValueError, match="holdings market value"):
        PortfolioSnapshot(
            **{
                **common,
                "cash": Decimal("50"),
                "invested_market_value": Decimal("50"),
                "cash_weight": 0.50,
            }
        )


def test_wrong_holding_weights_cannot_hide_behind_a_correct_weight_sum():
    def holding(symbol, price, weight):
        return PortfolioHolding(
            symbol=symbol,
            quantity=100,
            settled_quantity=100,
            last_price=Decimal(price),
            market_value=Decimal(price) * 100,
            weight=weight,
        )

    with pytest.raises(ValueError, match="holding weight"):
        PortfolioSnapshot(
            run_id=None,
            as_of=AS_OF,
            nav=Decimal("100"),
            cash=Decimal("40"),
            invested_market_value=Decimal("60"),
            cash_weight=0.40,
            holdings=(holding("600000.SH", "0.30", 0.20), holding("600036.SH", "0.30", 0.40)),
            max_holding_weight=0.40,
            concentration_hhi=0.20,
        )


def test_payload_requires_timezone_and_reconciles_market_value():
    payload = {
        "equity_curve": [{"timestamp": "2026-01-05T15:00:00", "cash": "40", "market_value": "60", "nav": "100"}],
        "positions": {"600519.SH": {"symbol": "600519.SH", "quantity": 100, "settled_quantity": 100, "average_cost": "0.60", "last_price": "0.60", "market_value": "60", "unrealized_pnl": "0"}},
        "diagnostics": {"decision_grade": True, "warnings": []},
    }
    with pytest.raises(ValueError, match="final equity point"):
        portfolio_snapshot_from_payload(payload)
    payload["equity_curve"][0]["timestamp"] = "2026-01-05T15:00:00+08:00"
    payload["equity_curve"][0]["market_value"] = "61"
    with pytest.raises(ValueError, match="market_value"):
        portfolio_snapshot_from_payload(payload)


def test_payload_decision_grade_true_never_promotes_valuation_quality():
    payload = {
        "equity_curve": [{"timestamp": "2026-01-05T15:00:00+08:00", "cash": "40", "market_value": "60", "nav": "100"}],
        "positions": {"600519.SH": {"symbol": "600519.SH", "quantity": 100, "settled_quantity": 100, "average_cost": "0.60", "last_price": "0.60", "market_value": "60", "unrealized_pnl": "0"}},
        "diagnostics": {"decision_grade": True, "warnings": []},
    }
    snapshot = portfolio_snapshot_from_payload(payload)
    assert snapshot.valuation_quality == "partial"
    assert "valuation_quality_unattested" in snapshot.warnings


@pytest.mark.parametrize(
    "market_value,nav,error",
    [("61", "101", "market_value"), ("60", "101", "nav")],
)
def test_result_wrapper_rejects_final_equity_inconsistency(market_value, nav, error):
    # The result model permits descriptive final positions, so the snapshot
    # boundary must reconcile them instead of silently deriving a different NAV.
    point = EquityPoint(
        timestamp=AS_OF,
        cash=Decimal("40"),
        market_value=Decimal(market_value),
        nav=Decimal(nav),
        daily_return=0.0,
        drawdown=0.0,
    )
    result = BacktestResult(
        strategy_name="fixture",
        config=BacktestConfig(initial_cash=Decimal("100")),
        started_at=AS_OF,
        ended_at=AS_OF,
        equity_curve=(point,),
        trades=(),
        skipped_executions=(),
        metrics=BacktestMetrics(
            starting_nav=Decimal("100"),
            ending_nav=Decimal(nav),
            total_return=0.0,
            annualized_return=0.0,
            annualized_volatility=0.0,
            sharpe_ratio=0.0,
            maximum_drawdown=0.0,
            turnover=0.0,
            total_commission=Decimal("0"),
            total_stamp_duty=Decimal("0"),
            total_slippage_cost=Decimal("0"),
            session_count=1,
            trade_count=0,
        ),
        diagnostics=BacktestDiagnostics(
            calendar="fixture",
            calendar_source="fixture",
            calendar_version="fixture",
            calendar_verified=False,
            price_basis="raw_unadjusted",
            bar_availability="fixture",
            execution_bar_availability="fixture",
            constraint_coverage=0.0,
            constrained_session_count=0,
            execution_attempt_count=0,
            unverified_fill_count=0,
            assumptions={},
            warnings=(),
            decision_grade=False,
        ),
        positions={"600519.SH": _position("600519.SH", "60")},
    )
    with pytest.raises(ValueError, match=error):
        portfolio_snapshot_from_result(result)
