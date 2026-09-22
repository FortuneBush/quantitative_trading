from datetime import date, datetime, timedelta, timezone

import pytest

from quant_monitor.backtesting import build_benchmark_report
from tests.test_risk import _result


def _portfolio_returns(result):
    return [point.daily_return for point in result.equity_curve[1:]]


def test_benchmark_report_calculates_regression_and_capture_without_provider_calls():
    result = _result(25)
    portfolio = _portfolio_returns(result)
    benchmark = [value / 2.0 for value in portfolio]

    report = build_benchmark_report(
        result,
        benchmark,
        benchmark_name="fixture-index",
        minimum_observations=20,
    )
    payload = report.to_dict()
    assert payload["benchmark_name"] == "fixture-index"
    assert payload["aligned_observations"] == 25
    assert payload["sample_sufficient"] is True
    assert payload["beta"] == pytest.approx(2.0)
    assert payload["correlation"] == pytest.approx(1.0)
    assert payload["upside_capture"] == pytest.approx(2.0)
    assert payload["downside_capture"] == pytest.approx(2.0)
    assert payload["tracking_error_annualized"] is not None
    assert payload["information_ratio"] is not None


def test_benchmark_alpha_uses_arithmetic_regression_intercept_annualization():
    result = _result(25)
    portfolio = _portfolio_returns(result)
    # Construct p = 2*b + 0.02.  With a zero risk-free rate the daily Jensen
    # alpha is therefore 0.02 and annualized alpha is 0.02 * 252, not a
    # compounded one-day return raised to the 252nd power.
    benchmark = [value / 2.0 - 0.01 for value in portfolio]

    report = build_benchmark_report(
        result,
        benchmark,
        minimum_observations=20,
        annualization_sessions=252,
    )

    assert report.alpha_annualized == pytest.approx(0.02 * 252, rel=1e-9)


def test_benchmark_mapping_aligns_by_session_and_surfaces_missing_dates():
    result = _result(5)
    points = result.equity_curve[1:]
    benchmark = {
        points[0].timestamp.date(): 0.01,
        points[2].timestamp.date(): -0.01,
        points[4].timestamp.date(): 0.02,
        date(2099, 1, 1): 0.03,
    }
    report = build_benchmark_report(result, benchmark, minimum_observations=3)
    assert report.benchmark_observations == 4
    assert report.aligned_observations == 3
    assert report.sample_sufficient is True
    assert "benchmark_sessions_missing_for_portfolio_dates" in report.warnings

    with pytest.raises(ValueError, match="sequence length"):
        build_benchmark_report(result, [0.01])


def test_benchmark_report_fails_closed_for_short_or_constant_benchmark_samples():
    result = _result(3)
    report = build_benchmark_report(result, [0.0] * 3, minimum_observations=20)
    assert report.sample_sufficient is False
    assert report.beta is None
    assert report.correlation is None
    assert report.tracking_error_annualized is None
    assert "insufficient_aligned_observations_for_benchmark_statistics" in report.warnings

    result = _result(25)
    report = build_benchmark_report(result, [0.0] * 25, minimum_observations=20)
    assert report.sample_sufficient is True
    assert report.beta is None
    assert "benchmark_return_variance_is_zero" in report.warnings
    assert "no_positive_benchmark_sessions_for_upside_capture" in report.warnings
    assert "no_negative_benchmark_sessions_for_downside_capture" in report.warnings


def test_benchmark_input_validation_rejects_invalid_returns_and_options():
    result = _result(3)
    with pytest.raises(ValueError, match="cannot be below -1"):
        build_benchmark_report(result, [-1.1] * 3)
    with pytest.raises(TypeError, match="finite number"):
        build_benchmark_report(result, [True] * 3)
    with pytest.raises(ValueError, match="cannot be empty"):
        build_benchmark_report(result, [0.0] * 3, benchmark_name=" ")
    with pytest.raises(ValueError, match="positive"):
        build_benchmark_report(result, [0.0] * 3, minimum_observations=0)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        build_benchmark_report(result, {"2026-01-01T00:00:00": 0.01})


def test_benchmark_datetime_keys_require_timezone_and_use_market_date():
    result = _result(1)
    session = result.equity_curve[1].timestamp
    # A UTC evening timestamp belongs to the following Shanghai session date.
    utc_key = datetime.combine(
        session.date() - timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
    ).replace(hour=16)
    report = build_benchmark_report(
        result,
        {utc_key: 0.01},
        minimum_observations=1,
    )
    assert report.aligned_observations == 1
    with pytest.raises(TypeError, match="timezone-aware"):
        build_benchmark_report(
            result,
            {datetime.combine(session.date(), datetime.min.time()): 0.01},
            minimum_observations=1,
        )
