from datetime import datetime

import pytest

from quant_monitor.backtesting import (
    build_attribution_report,
    build_attribution_report_from_payload,
)
from quant_monitor.serialization import backtest_result_dict
from tests.test_risk import _result


def test_attribution_keeps_cash_and_active_components_explicit():
    result = _result(4)
    portfolio = [point.daily_return for point in result.equity_curve[1:]]
    benchmark = [value / 2.0 for value in portfolio]
    report = build_attribution_report(result, benchmark, cash_returns=[0.0] * 4)

    assert report.status == "complete"
    assert report.complete_observations == 4
    assert report.benchmark_contribution is not None
    assert report.cash_contribution == 0
    assert report.active_contribution is not None
    assert report.unallocated_contribution == 0
    assert all(row.active_contribution is not None for row in report.rows)
    payload = report.to_dict()
    assert payload["transaction_cost_contribution"] == "0"
    assert len(payload["rows"]) == 4


def test_attribution_does_not_treat_missing_cash_as_zero():
    result = _result(3)
    benchmark = [point.daily_return for point in result.equity_curve[1:]]
    report = build_attribution_report(result, benchmark)

    assert report.status == "benchmark_only"
    assert report.cash_contribution is None
    assert report.active_contribution is None
    assert "cash_returns_not_supplied" in report.warnings
    assert all(row.cash_contribution is None for row in report.rows)


def test_attribution_mapping_preserves_missing_session_and_payload_round_trip():
    result = _result(3)
    points = result.equity_curve[1:]
    benchmark = {points[0].timestamp.date(): 0.01}
    cash = {points[0].timestamp.date(): 0.0}
    report = build_attribution_report(result, benchmark, cash_returns=cash)
    assert report.status == "unavailable"
    assert report.complete_observations == 1
    assert "benchmark_sessions_missing_for_portfolio_dates" in report.warnings
    assert "cash_sessions_missing_for_portfolio_dates" in report.warnings
    payload = backtest_result_dict(result)
    replay = build_attribution_report_from_payload(
        payload, benchmark, cash_returns=cash
    )
    assert replay.to_dict() == report.to_dict()


def test_attribution_rejects_invalid_inputs_without_network_or_filling_values():
    result = _result(2)
    with pytest.raises(ValueError, match="cannot be below -1"):
        build_attribution_report(result, [-1.1, 0.0])
    with pytest.raises(ValueError, match="sequence length"):
        build_attribution_report(result, [0.01])
    with pytest.raises(TypeError, match="timezone-aware"):
        build_attribution_report(result, {datetime(2025, 1, 3): 0.01, datetime(2025, 1, 4): 0.01})
