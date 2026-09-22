from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quant_monitor.backtesting import (
    BacktestConfig,
    BacktestDiagnostics,
    BacktestMetrics,
    BacktestResult,
    EquityPoint,
    Position,
    build_risk_report,
    build_risk_report_from_payload,
)
from quant_monitor.domain import SHANGHAI_TZ
from quant_monitor.serialization import backtest_result_dict


def _result(observations=25):
    start = datetime(2025, 1, 2, 15, 30, tzinfo=SHANGHAI_TZ)
    nav = Decimal("100000")
    curve = []
    returns = []
    for index in range(observations + 1):
        daily_return = 0.01 if index % 5 else -0.04
        if index == 0:
            daily_return = 0.0
        if index:
            returns.append(daily_return)
            nav = (nav * (Decimal("1") + Decimal(str(daily_return)))).quantize(Decimal("0.01"))
        curve.append(
            EquityPoint(
                timestamp=start + timedelta(days=index),
                cash=nav * Decimal("0.4"),
                market_value=nav * Decimal("0.6"),
                nav=nav,
                daily_return=daily_return,
                drawdown=min(0.0, -0.04 if index % 5 == 0 and index else 0.0),
            )
        )
    metrics = BacktestMetrics(
        starting_nav=Decimal("100000"),
        ending_nav=curve[-1].nav,
        total_return=float(curve[-1].nav / Decimal("100000") - 1),
        annualized_return=0.1,
        annualized_volatility=0.2,
        sharpe_ratio=0.4,
        maximum_drawdown=0.04,
        turnover=0.5,
        total_commission=Decimal("10"),
        total_stamp_duty=Decimal("2"),
        total_slippage_cost=Decimal("1"),
        session_count=len(curve),
        trade_count=0,
        maximum_drawdown_duration_sessions=2,
    )
    diagnostics = BacktestDiagnostics(
        calendar="weekday_fallback",
        calendar_source="fixture",
        calendar_version="v1",
        calendar_verified=False,
        price_basis="raw_unadjusted",
        bar_availability="fixture",
        execution_bar_availability="fixture",
        constraint_coverage=0.0,
        constrained_session_count=0,
        execution_attempt_count=0,
        unverified_fill_count=0,
        assumptions={"fixture": "true"},
        warnings=("fixture_warning",),
        decision_grade=False,
    )
    position = Position(
        symbol="600000.SH",
        quantity=100,
        settled_quantity=100,
        average_cost=Decimal("500"),
        last_price=Decimal("600"),
        market_value=curve[-1].nav * Decimal("0.6"),
        unrealized_pnl=Decimal("10000"),
    )
    return BacktestResult(
        strategy_name="fixture",
        config=BacktestConfig(initial_cash=Decimal("100000")),
        started_at=curve[0].timestamp,
        ended_at=curve[-1].timestamp,
        equity_curve=tuple(curve),
        trades=(),
        skipped_executions=(),
        metrics=metrics,
        diagnostics=diagnostics,
        positions={position.symbol: position},
    )


def test_risk_report_calculates_tail_and_exposure_without_provider_calls():
    report = build_risk_report(_result())
    payload = report.to_dict()
    assert report.sample_sufficient is True
    assert payload["historical_var"]["0.95"] == pytest.approx(0.04)
    assert payload["historical_cvar"]["0.95"] >= payload["historical_var"]["0.95"]
    assert payload["worst_session_return"] == pytest.approx(-0.04)
    assert payload["downside_session_ratio"] == pytest.approx(0.2)
    assert payload["cash_weight"] == pytest.approx(0.4)
    assert payload["gross_exposure"] == pytest.approx(0.6)
    assert payload["top_position_weight"] == pytest.approx(0.6)
    assert payload["concentration_warning"] is True
    assert "fixture_warning" in payload["warnings"]


def test_risk_report_fails_closed_for_short_history_and_invalid_confidence():
    report = build_risk_report(_result(3), minimum_observations=20)
    assert report.sample_sufficient is False
    assert report.historical_var["0.95"] is None
    assert report.historical_cvar["0.99"] is None
    assert "insufficient_return_observations_for_historical_tail_metrics" in report.warnings
    with pytest.raises(ValueError, match="between zero and one"):
        build_risk_report(_result(), confidence_levels=(1.0,))
    with pytest.raises(ValueError, match="unique"):
        build_risk_report(_result(), confidence_levels=(0.95, 0.95))


def test_historical_tail_uses_nearest_rank_and_keeps_single_worst_loss():
    """A 95% tail over 20 observations must include the worst observation.

    The previous floor-index implementation selected the second observation
    (a positive return in this fixture), producing a zero VaR despite a 10%
    loss.  Nearest-rank (ceil) is conservative and deterministic for small
    samples, which is important for the short research windows users often
    inspect interactively.
    """

    result = _result(20)
    returns = (-0.10,) + (0.01,) * 19
    curve = tuple(
        replace(point, daily_return=value)
        for point, value in zip(result.equity_curve, (0.0,) + returns)
    )
    result = replace(result, equity_curve=curve)
    report = build_risk_report(result, minimum_observations=20)
    assert report.historical_var["0.95"] == pytest.approx(0.10)
    assert report.historical_cvar["0.95"] == pytest.approx(0.10)


def test_risk_payload_exposes_missing_returns_without_treating_them_as_zero():
    """Malformed persisted returns must reduce the sample, not hide risk."""

    payload = {
        "equity_curve": [
            {"timestamp": "2026-01-02T15:30:00+08:00", "cash": "100", "nav": "100"},
            {"timestamp": "2026-01-03T15:30:00+08:00", "cash": "100", "nav": "100", "daily_return": None},
            {"timestamp": "2026-01-04T15:30:00+08:00", "cash": "100", "nav": "100", "daily_return": "nan"},
        ],
        "metrics": {"maximum_drawdown": 0.0},
        "diagnostics": {},
        "positions": {},
    }

    report = build_risk_report_from_payload(payload, minimum_observations=1)

    assert report.return_observations == 0
    assert report.missing_return_observations == 2
    assert report.sample_sufficient is False
    assert report.historical_var["0.95"] is None
    assert "missing_or_invalid_return_observations" in report.warnings


def test_risk_payload_invalid_ending_exposure_fails_closed_with_warning():
    payload = {
        "equity_curve": [
            {
                "timestamp": "2026-01-02T15:30:00+08:00",
                "cash": None,
                "nav": "not-a-number",
                "daily_return": 0.0,
            }
        ],
        "metrics": {"maximum_drawdown": "not-a-number"},
        "diagnostics": {},
        "positions": {},
    }

    report = build_risk_report_from_payload(payload, minimum_observations=1)

    assert report.ending_nav is None
    assert report.cash_weight is None
    assert report.maximum_drawdown is None
    assert "missing_or_invalid_ending_nav" in report.warnings


@pytest.mark.parametrize("cash", ["-1", "-1e-9999", "NaN", "Infinity", True])
def test_risk_payload_impossible_ending_cash_never_becomes_negative_exposure(cash):
    """Untrusted persisted cash must not produce a negative/invalid weight."""

    payload = {
        "equity_curve": [
            {
                "timestamp": "2026-01-02T15:30:00+08:00",
                "cash": cash,
                "nav": "100",
                "daily_return": 0.0,
            }
        ],
        "metrics": {"maximum_drawdown": 0.0},
        "diagnostics": {},
        "positions": {},
    }

    report = build_risk_report_from_payload(payload, minimum_observations=1)

    assert report.cash_weight is None
    assert "missing_or_invalid_ending_cash" in report.warnings


def test_risk_payload_cash_above_nav_is_explicitly_unavailable():
    payload = {
        "equity_curve": [
            {
                "timestamp": "2026-01-02T15:30:00+08:00",
                "cash": "101",
                "nav": "100",
                "daily_return": 0.0,
            }
        ],
        "metrics": {"maximum_drawdown": 0.0},
        "diagnostics": {},
        "positions": {},
    }

    report = build_risk_report_from_payload(payload, minimum_observations=1)

    assert report.cash_weight is None
    assert "ending_cash_exceeds_ending_nav" in report.warnings


@pytest.mark.parametrize("invalid_return", [None, "bad", "NaN", "Infinity", True])
def test_invalid_return_blocks_tail_metrics_even_with_enough_valid_observations(invalid_return):
    payload = backtest_result_dict(_result(22))
    payload["equity_curve"][1]["daily_return"] = invalid_return

    report = build_risk_report_from_payload(payload, minimum_observations=20)

    assert report.return_observations == 21
    assert report.missing_return_observations == 1
    assert report.sample_sufficient is False
    assert all(value is None for value in report.historical_var.values())
    assert all(value is None for value in report.historical_cvar.values())
    assert report.worst_session_return == pytest.approx(-0.04)
    assert report.best_session_return == pytest.approx(0.01)
    assert report.downside_session_ratio == pytest.approx(4 / 21)
    assert "missing_or_invalid_return_observations" in report.warnings
    assert "insufficient_return_observations_for_historical_tail_metrics" not in report.warnings


def test_absent_return_blocks_tail_metrics_but_initial_return_is_not_required():
    payload = backtest_result_dict(_result(21))
    payload["equity_curve"][0].pop("daily_return")
    assert build_risk_report_from_payload(payload).sample_sufficient is True

    payload["equity_curve"][1].pop("daily_return")
    report = build_risk_report_from_payload(payload)
    assert report.return_observations == 20
    assert report.missing_return_observations == 1
    assert report.sample_sufficient is False
    assert all(value is None for value in report.historical_var.values())


@pytest.mark.parametrize(
    "invalid_position",
    [
        None,
        {},
        {"market_value": None},
        {"market_value": "bad"},
        {"market_value": "NaN"},
        {"market_value": "sNaN"},
        {"market_value": "Infinity"},
        {"market_value": "-Infinity"},
        {"market_value": "-1"},
        {"market_value": "-1e-9999"},
        {"market_value": "1e9999"},
        {"market_value": "1e-9999"},
        {"market_value": True},
    ],
)
def test_partial_invalid_holdings_never_report_underestimated_exposure(invalid_position):
    payload = backtest_result_dict(_result())
    payload["positions"]["600036.SH"] = invalid_position

    report = build_risk_report_from_payload(payload)

    assert report.sample_sufficient is True
    assert report.cash_weight == pytest.approx(0.4)
    assert report.gross_exposure is None
    assert report.top_position_weight is None
    assert report.concentration_warning is None
    assert report.position_count == 2
    assert report.missing_position_observations == 1
    assert report.position_data_complete is False
    assert "missing_or_invalid_position_market_values" in report.warnings
    assert report.to_dict()["missing_position_observations"] == 1
    assert report.to_dict()["position_data_complete"] is False


@pytest.mark.parametrize("absent_positions", [True, False])
def test_missing_positions_are_distinct_from_explicit_empty_holdings(absent_positions):
    payload = backtest_result_dict(_result())
    if absent_positions:
        payload.pop("positions")
    else:
        payload["positions"] = None

    report = build_risk_report_from_payload(payload)

    assert report.gross_exposure is None
    assert report.top_position_weight is None
    assert report.concentration_warning is None
    assert report.position_count is None
    assert report.position_data_complete is False
    assert "missing_position_data" in report.warnings

    payload["positions"] = {}
    report = build_risk_report_from_payload(payload)
    assert report.gross_exposure == 0.0
    assert report.top_position_weight is None
    assert report.concentration_warning is False
    assert report.position_count == 0
    assert report.missing_position_observations == 0
    assert report.position_data_complete is True
    assert "missing_position_data" not in report.warnings


def test_finite_holdings_with_overflowing_aggregate_are_explicitly_unknown():
    payload = backtest_result_dict(_result())
    payload["positions"] = {
        "600000.SH": {"market_value": "1e308"},
        "600036.SH": {"market_value": "1e308"},
    }
    report = build_risk_report_from_payload(payload)
    assert report.gross_exposure is None
    assert report.top_position_weight is None
    assert report.concentration_warning is None
    assert report.position_data_complete is False
    assert "invalid_total_position_market_value" in report.warnings


def test_complete_payload_retains_domain_result_tail_and_exposure_values():
    result = _result()
    report = build_risk_report_from_payload(backtest_result_dict(result))
    assert report.to_dict() == build_risk_report(result).to_dict()
    assert report.missing_return_observations == 0
    assert report.missing_position_observations == 0
    assert report.position_data_complete is True
