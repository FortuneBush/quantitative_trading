"""Offline trade-distribution semantics and immutable artifact audit regressions."""

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import hashlib
import json

import pytest

from quant_monitor.backtesting import (
    BacktestConfig, BacktestEngine, audit_backtest_payload_metrics,
    audit_backtest_result_metrics,
)
from quant_monitor.serialization import backtest_result_dict
from tests.test_backtesting import _bar, BuyThenSellStrategy, BuyFirstSessionStrategy, HoldStrategy


TRADE_FIELDS = (
    "closed_trade_count", "winning_trade_count", "losing_trade_count",
    "break_even_trade_count", "win_rate", "gross_profit", "gross_loss",
    "realized_pnl", "profit_factor", "expectancy_per_closed_trade",
    "trade_statistics_status", "profit_factor_status",
)


def result_for(prices=("12",), strategy=None):
    config = BacktestConfig(
        initial_cash="10000", buy_commission_rate="0", sell_commission_rate="0",
        minimum_commission="0", buy_slippage_rate="0", sell_slippage_rate="0",
        stamp_duty_rate="0",
    )
    symbols = ("600000.SH", "000001.SZ", "600036.SH")
    series = {
        symbol: [_bar(symbol, day, open_price="10" if day < 2 else price) for day in range(4)]
        for symbol, price in zip(symbols, prices)
    }
    return BacktestEngine(strategy or BuyThenSellStrategy(), config).run(series)


@pytest.mark.parametrize("strategy", [HoldStrategy(), BuyFirstSessionStrategy()])
def test_no_closing_sell_preserves_unavailable_statistics(strategy):
    result = result_for(strategy=strategy)
    metrics = result.metrics
    assert metrics.closed_trade_count == 0
    assert metrics.win_rate is None
    assert metrics.profit_factor is None
    assert metrics.expectancy_per_closed_trade is None
    assert metrics.trade_statistics_status == metrics.profit_factor_status == "no_closed_trades"
    assert metrics.realized_pnl == metrics.gross_profit == metrics.gross_loss == Decimal("0")
    report = audit_backtest_result_metrics(result)
    assert report.verified
    assert report.trade_statistics_status == "verified"
    assert audit_backtest_payload_metrics(backtest_result_dict(result)).to_dict() == report.to_dict()


@pytest.mark.parametrize("prices,wins,losses,even,pf,net", [
    (("12",), 1, 0, 0, None, "2000"),
    (("8",), 0, 1, 0, 0.0, "-2000"),
    (("10",), 0, 0, 1, None, "0"),
    (("12", "8"), 1, 1, 0, 1.0, "0"),
])
def test_completed_distribution_empty_denominators_and_signed_money(prices, wins, losses, even, pf, net):
    result = result_for(prices)
    metrics = result.metrics
    closed = wins + losses + even
    assert metrics.closed_trade_count == closed
    assert (metrics.winning_trade_count, metrics.losing_trade_count, metrics.break_even_trade_count) == (wins, losses, even)
    assert metrics.win_rate == wins / closed
    assert metrics.profit_factor == pf
    assert metrics.profit_factor_status == ("available" if losses else "no_losses")
    assert metrics.realized_pnl == Decimal(net)
    assert metrics.expectancy_per_closed_trade == Decimal(net) / closed
    assert isinstance(metrics.expectancy_per_closed_trade, Decimal)
    assert audit_backtest_result_metrics(result).verified
    assert audit_backtest_payload_metrics(backtest_result_dict(result)).verified


def test_realized_distribution_includes_buy_and_sell_fees():
    result = result_for()
    config = replace(result.config, buy_commission_rate="0.001", sell_commission_rate="0.001", stamp_duty_rate="0.0005", minimum_commission="5")
    bars = [_bar("600000.SH", day, open_price="10" if day < 2 else "12") for day in range(4)]
    result = BacktestEngine(BuyThenSellStrategy(), config).run({"600000.SH": bars})
    assert result.metrics.realized_pnl == Decimal("1774.80")
    assert result.metrics.expectancy_per_closed_trade == Decimal("1774.80")
    assert audit_backtest_payload_metrics(backtest_result_dict(result)).verified


def test_legacy_without_distribution_remains_auditable_without_fabricating_trade_evidence():
    payload = backtest_result_dict(result_for())
    for name in TRADE_FIELDS + ("sortino_ratio", "calmar_ratio", "positive_session_ratio", "maximum_drawdown_duration_sessions"):
        payload["metrics"].pop(name, None)
    for trade in payload["trades"]:
        trade.pop("realized_pnl", None)
    before = deepcopy(payload)
    before_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    report = audit_backtest_payload_metrics(payload)
    assert report.verified
    assert report.trade_statistics_status == "unavailable"
    assert "trade_distribution_matches_ledger" in report.unavailable_checks
    assert "legacy_trade_statistics_unavailable" in report.warnings
    assert "sell_realized_pnl_complete" not in report.checks
    assert payload == before
    assert hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest() == before_hash


def test_legacy_missing_new_net_aggregate_audits_existing_fields():
    payload = backtest_result_dict(result_for(("12", "8")))
    for name in ("trade_statistics_status", "profit_factor_status", "realized_pnl"):
        payload["metrics"].pop(name)
    report = audit_backtest_payload_metrics(payload)
    assert report.verified
    assert report.trade_statistics_status == "unavailable"
    assert "realized_pnl_matches_ledger" in report.unavailable_checks
    assert report.checks["gross_profit_matches_ledger"]
    assert report.checks["realized_pnl_matches_acquisition_cost"]


@pytest.mark.parametrize("no_trades", [False, True])
def test_legacy_undefined_zero_ratios_remain_unavailable(no_trades):
    payload = backtest_result_dict(result_for(strategy=HoldStrategy() if no_trades else None))
    for name in ("trade_statistics_status", "profit_factor_status"):
        payload["metrics"].pop(name)
    for name in ("profit_factor", "win_rate", "expectancy_per_closed_trade"):
        if payload["metrics"][name] is None:
            payload["metrics"][name] = 0.0
    report = audit_backtest_payload_metrics(payload)
    assert report.verified
    assert report.trade_statistics_status == "unavailable"
    assert "profit_factor_matches_ledger" in report.unavailable_checks
    assert "legacy_undefined_zero_convention" in report.warnings


@pytest.mark.parametrize("legacy", [False, True])
def test_declared_distribution_requires_realized_pnl_on_every_sell(legacy):
    payload = backtest_result_dict(result_for())
    payload["trades"][1].pop("realized_pnl")
    if legacy:
        payload["metrics"].pop("trade_statistics_status")
        payload["metrics"].pop("profit_factor_status")
    report = audit_backtest_payload_metrics(payload)
    assert not report.verified
    assert report.trade_statistics_status == "failed"
    assert "sell_realized_pnl_complete" in report.mismatches


def test_buy_cannot_smuggle_realized_pnl_even_without_statistics():
    payload = backtest_result_dict(result_for())
    for name in TRADE_FIELDS:
        payload["metrics"].pop(name, None)
    payload["trades"][0]["realized_pnl"] = "100"
    report = audit_backtest_payload_metrics(payload)
    assert not report.verified
    assert "buy_realized_pnl_absent" in report.mismatches


def test_self_consistent_forged_sell_pnl_and_aggregates_fail_cost_basis_audit():
    payload = backtest_result_dict(result_for())
    payload["trades"][1]["realized_pnl"] = "3000"
    for name in ("realized_pnl", "gross_profit", "expectancy_per_closed_trade"):
        payload["metrics"][name] = "3000"
    report = audit_backtest_payload_metrics(payload)
    assert not report.verified
    assert "realized_pnl_matches_acquisition_cost" in report.mismatches
    assert report.checks["realized_pnl_matches_ledger"]


def test_new_statistics_cannot_omit_aggregate_or_set_undefined_ratio_zero():
    payload = backtest_result_dict(result_for())
    payload["metrics"].pop("realized_pnl")
    payload["metrics"]["profit_factor"] = 0
    report = audit_backtest_payload_metrics(payload)
    assert not report.verified
    assert {"realized_pnl_matches_ledger", "profit_factor_matches_ledger"} <= set(report.mismatches)


def test_engine_refuses_to_compute_distribution_with_missing_sell_pnl():
    result = result_for()
    incomplete = (result.trades[0], replace(result.trades[1], realized_pnl=None))
    with pytest.raises(ValueError, match="closing sell is missing"):
        BacktestEngine(HoldStrategy(), result.config)._metrics(result.equity_curve, incomplete)


@pytest.mark.parametrize("field_name", ["commission", "gross_notional", "stamp_duty"])
def test_missing_trade_amount_is_failed_audit_not_an_exception(field_name):
    payload = backtest_result_dict(result_for())
    payload["trades"][1].pop(field_name)
    report = audit_backtest_payload_metrics(payload)
    assert not report.verified
    assert report.trade_statistics_status == "failed"
    assert "trade_ledger_amounts_parseable" in report.mismatches


@pytest.mark.parametrize("remove_all", [False, True])
def test_availability_marker_cannot_bypass_missing_or_forged_statistics(remove_all):
    payload = backtest_result_dict(result_for())
    if remove_all:
        for name in TRADE_FIELDS:
            if name != "trade_statistics_status":
                payload["metrics"].pop(name, None)
    else:
        payload["metrics"]["trade_statistics_status"] = "unavailable"
    report = audit_backtest_payload_metrics(payload)
    assert not report.verified
    assert report.trade_statistics_status == "failed"


@pytest.mark.parametrize("field_name,bad_value", [("action", "hold"), ("quantity", True), ("realized_pnl", "NaN")])
def test_forged_trade_fields_fail_closed(field_name, bad_value):
    payload = backtest_result_dict(result_for())
    payload["trades"][1][field_name] = bad_value
    report = audit_backtest_payload_metrics(payload)
    assert not report.verified
    assert report.trade_statistics_status == "failed"


def test_new_metric_model_rejects_fabricated_availability():
    metrics = result_for().metrics
    with pytest.raises(ValueError, match="profit_factor is undefined"):
        replace(metrics, profit_factor=0.0)
    with pytest.raises(ValueError, match="trade_statistics_status"):
        replace(metrics, trade_statistics_status="no_closed_trades")
    with pytest.raises(ValueError, match="unavailable trade statistics"):
        replace(metrics, trade_statistics_status="unavailable")


def test_integrity_report_rejects_truthy_string_booleans():
    with pytest.raises(TypeError, match="verified must be a boolean"):
        from quant_monitor.backtesting.integrity import MetricIntegrityReport
        MetricIntegrityReport(
            method="backtest_metric_integrity_v1", verified="false",
            tolerance=0.0, observation_count=0, trade_count=0,
            checks={}, mismatches=(),
        )
    with pytest.raises(TypeError, match="checks values must be booleans"):
        from quant_monitor.backtesting.integrity import MetricIntegrityReport
        MetricIntegrityReport(
            method="backtest_metric_integrity_v1", verified=True,
            tolerance=0.0, observation_count=0, trade_count=0,
            checks={"tampered": "false"}, mismatches=(),
        )
