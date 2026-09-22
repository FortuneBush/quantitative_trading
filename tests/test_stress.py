from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quant_monitor.backtesting import (
    CapacityAssumptions,
    BacktestConfig,
    BacktestEngine,
    StressScenario,
    audit_capacity,
    run_stress_test,
    run_stress_test_from_payload,
)
from quant_monitor.domain import Bar, SHANGHAI_TZ, Signal, SignalAction
from quant_monitor.serialization import backtest_result_dict
from quant_monitor.strategies import Strategy


class BuyOnFirstSession(Strategy):
    @property
    def name(self):
        return "stress_fixture"

    def evaluate(self, context):
        if len(context.bars) == 1:
            return (
                Signal(
                    strategy=self.name,
                    symbol=context.symbol,
                    timestamp=context.timestamp,
                    action=SignalAction.BUY,
                    strength=1.0,
                ),
            )
        return ()


def _bars(symbol="600000.SH"):
    start = datetime(2025, 1, 6, 15, tzinfo=SHANGHAI_TZ)
    return [
        Bar(
            symbol=symbol,
            interval="1d",
            timestamp=start + timedelta(days=offset),
            open=Decimal("10"),
            high=Decimal("10"),
            low=Decimal("10"),
            close=Decimal("10"),
            volume=100000,
            turnover=Decimal("1000000"),
            source="fixture",
        )
        for offset in range(2)
    ]


def _result():
    return BacktestEngine(
        BuyOnFirstSession(),
        BacktestConfig(initial_cash=Decimal("10000")),
    ).run({"600000.SH": _bars()})


def test_price_shock_marks_open_position_and_identifies_worst_case():
    result = _result()
    report = run_stress_test(
        result,
        [StressScenario("baseline"), StressScenario("crash", price_shock="-0.10")],
    )
    baseline, crash = report.scenarios
    assert baseline.stressed_ending_nav == result.metrics.ending_nav
    assert crash.stressed_ending_nav < baseline.stressed_ending_nav
    assert crash.ending_nav_delta < 0
    assert report.worst_scenario == "crash"
    assert crash.stressed_maximum_drawdown >= 0


def test_cost_multiplier_is_charged_only_after_execution():
    result = _result()
    report = run_stress_test(result, [StressScenario("friction", cost_multiplier="2")])
    outcome = report.scenarios[0]
    total_cost = sum(
        (trade.commission + trade.stamp_duty + trade.slippage_cost for trade in result.trades),
        Decimal("0"),
    )
    assert outcome.stressed_ending_nav == result.metrics.ending_nav - total_cost


def test_serialized_payload_path_is_provider_independent_and_equivalent():
    result = _result()
    scenario = StressScenario("shock", price_shock="-0.05", cost_multiplier="1.5")
    direct = run_stress_test(result, [scenario]).to_dict()
    payload = run_stress_test_from_payload(backtest_result_dict(result), [scenario]).to_dict()
    assert payload == direct


def test_scenarios_validate_and_empty_collection_is_safe():
    with pytest.raises(ValueError, match="below -1"):
        StressScenario("invalid", price_shock="-1.01")
    with pytest.raises(ValueError, match="negative"):
        StressScenario("invalid", cost_multiplier="-0.1")
    assert run_stress_test(_result(), []).scenarios == ()


def test_stress_timestamps_require_timezone_and_normalize_to_market_time():
    result = _result()
    payload = backtest_result_dict(result)
    payload["equity_curve"][0]["timestamp"] = "2025-01-06T07:00:00+00:00"
    payload["equity_curve"][1]["timestamp"] = "2025-01-07T07:00:00+00:00"
    # UTC timestamps represent the same Shanghai sessions as the source.
    report = run_stress_test_from_payload(payload, [StressScenario("baseline")])
    assert report.scenarios[0].observations == len(payload["equity_curve"])
    payload["equity_curve"][0]["timestamp"] = "2025-01-06T15:00:00"
    with pytest.raises(ValueError, match="timezone-aware"):
        run_stress_test_from_payload(payload, [StressScenario("baseline")])


def test_capacity_audit_marks_participation_breach_without_mutating_result():
    result = _result()
    # The execution occurs on the second day.  A 10% participation cap on
    # 1,000 shares allows only 100 shares, while the fixture buys 900.
    report = audit_capacity(
        result,
        {"600000.SH": [replace(_bars()[1], volume=1000)]},
        CapacityAssumptions(max_participation_rate="0.10"),
    )
    assert report.trade_count == 1
    assert report.assessed_trade_count == 1
    assert report.capacity_limited_trade_count == 1
    assert report.observations[0].participation_rate == pytest.approx(0.9)
    assert report.executable_notional == Decimal("1001.00")
    assert "some_trades_exceed_max_participation_rate" in report.warnings
    assert result.trades[0].quantity == 900


def test_capacity_audit_fails_closed_when_volume_is_unavailable():
    result = _result()
    report = audit_capacity(result, {})
    assert report.unassessed_trade_count == 1
    assert report.capacity_limited_trade_count == 0
    assert report.executable_notional == Decimal("0")
    assert report.observations[0].reason == "missing_execution_volume"


def test_capacity_payload_rejects_noncanonical_trade_fields():
    payload = backtest_result_dict(_result())
    payload["trades"][0]["quantity"] = True
    with pytest.raises(ValueError, match="invalid trade"):
        from quant_monitor.backtesting import audit_capacity_from_payload

        audit_capacity_from_payload(payload, {})
