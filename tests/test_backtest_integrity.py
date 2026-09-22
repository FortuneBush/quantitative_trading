import pytest

from quant_monitor.backtesting import (
    BacktestConfig,
    BacktestEngine,
    audit_backtest_payload_metrics,
    audit_backtest_result_metrics,
)
from quant_monitor.serialization import backtest_result_dict
from quant_monitor.strategies import Strategy
from tests.test_backtesting import _bar


class _NoopStrategy(Strategy):
    @property
    def name(self):
        return "integrity_noop"

    def evaluate(self, context):
        return ()


def _result():
    symbol = "600519.SH"
    bars = [_bar(symbol, index, open_price="10", close="10") for index in range(4)]
    return BacktestEngine(_NoopStrategy(), BacktestConfig()).run({symbol: bars})


def test_metric_integrity_is_deterministic_for_engine_result_and_payload():
    result = _result()
    first = audit_backtest_result_metrics(result)
    second = audit_backtest_payload_metrics(backtest_result_dict(result))
    assert first.verified is True
    assert second.verified is True
    assert first.to_dict() == second.to_dict()
    assert first.mismatches == ()


def test_metric_integrity_detects_tampered_aggregate_and_curve_fields():
    result = _result()
    payload = backtest_result_dict(result)
    payload["metrics"]["total_return"] = 0.25
    payload["equity_curve"][2]["daily_return"] = 0.1
    report = audit_backtest_payload_metrics(payload)
    assert report.verified is False
    assert "total_return_matches_nav_curve" in report.mismatches
    assert "daily_returns_match_nav_curve" in report.mismatches


def test_metric_integrity_rejects_negative_tolerance():
    with pytest.raises(ValueError, match="tolerance cannot be negative"):
        audit_backtest_result_metrics(_result(), tolerance=-1)


@pytest.mark.parametrize(
    "mutate,failed_check",
    [
        (lambda payload: payload["config"].update(annualization_sessions=0), "valid_annualization_sessions"),
        (lambda payload: payload["config"].update(annualization_sessions="252"), "valid_annualization_sessions"),
        (lambda payload: payload["config"].update(annual_risk_free_rate="not-a-rate"), "valid_annual_risk_free_rate"),
        (lambda payload: payload["equity_curve"].__setitem__(1, {**payload["equity_curve"][1], "nav": "0"}), "positive_nav_curve"),
    ],
)
def test_metric_integrity_malformed_numeric_contract_fails_closed(mutate, failed_check):
    payload = backtest_result_dict(_result())
    mutate(payload)
    report = audit_backtest_payload_metrics(payload)
    assert report.verified is False
    assert failed_check in report.mismatches


def test_metric_integrity_report_failed_trade_audit_cannot_be_verified():
    from quant_monitor.backtesting.integrity import MetricIntegrityReport

    report = MetricIntegrityReport(
        method="backtest_metric_integrity_v1",
        verified=True,
        tolerance=0.0,
        observation_count=1,
        trade_count=0,
        checks={"curve": True},
        mismatches=(),
        trade_statistics_status="failed",
    )
    assert report.verified is False
