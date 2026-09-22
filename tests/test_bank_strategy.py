"""Offline tests for the bank-share cross-sectional research model."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quant_monitor.domain import Bar, Quote, SignalAction
from quant_monitor.strategies import (
    BankDailyMultifactorStrategy,
    CohortStrategyContext,
    StrategyContext,
)


BASE = datetime.fromisoformat("2026-01-05T15:00:00+08:00")


def _symbol(index: int) -> str:
    return "60{:04d}.SH".format(index)


def _bars(
    index: int,
    *,
    count: int = 130,
    daily_return: float = 0.001,
    end: datetime = BASE,
    zero_volume: bool = False,
    corporate_action: bool = False,
):
    price = 10.0 + index * 0.1
    rows = []
    start = end - timedelta(days=count - 1)
    for offset in range(count):
        close = price * ((1.0 + daily_return) ** offset)
        opened = close * 0.999
        low = close * 0.995
        high = close * 1.005
        if corporate_action and offset == count - 2:
            previous_close = float(rows[-1].close)
            close = previous_close * 0.90
            opened = previous_close * 0.90
            low = previous_close * 0.89
            high = previous_close * 0.91
        rows.append(
            Bar(
                symbol=_symbol(index),
                interval="1d",
                timestamp=start + timedelta(days=offset),
                open=Decimal(str(opened)),
                high=Decimal(str(high)),
                low=Decimal(str(low)),
                close=Decimal(str(close)),
                volume=0 if zero_volume and offset >= count - 20 else 1_000_000 + index,
                turnover=Decimal(str(close * (1_000_000 + index))),
                source="fixture",
            )
        )
    return rows


def _cohort(
    *,
    count: int = 15,
    history: int = 130,
    end: datetime = BASE,
    corporate_action_index=None,
    zero_volume_index=None,
):
    members = {}
    for index in range(count):
        bars = _bars(
            index,
            count=history,
            daily_return=0.0002 + index * 0.00012,
            end=end,
            corporate_action=index == corporate_action_index,
            zero_volume=index == zero_volume_index,
        )
        members[_symbol(index)] = StrategyContext(
            symbol=_symbol(index), timestamp=bars[-1].timestamp, bars=tuple(bars)
        )
    return CohortStrategyContext(timestamp=end, members=members, cohort="a_share_banks")


def test_contract_normalizes_sorts_and_is_immutable_by_copy():
    bars = _bars(0)
    original = {
        _symbol(0).lower(): StrategyContext(_symbol(0), bars[-1].timestamp, bars=tuple(bars))
    }
    context = CohortStrategyContext(timestamp=BASE, members=original, cohort="banks")
    original.clear()
    assert list(context.members) == [_symbol(0)]
    with pytest.raises(TypeError):
        context.members[_symbol(1)] = context.members[_symbol(0)]
    with pytest.raises(TypeError):
        CohortStrategyContext(timestamp=BASE, members={_symbol(0): object()})


@pytest.mark.parametrize("history", [129, 130])
def test_history_boundary_is_explicit(history):
    strategy = BankDailyMultifactorStrategy(min_universe=2)
    signals = strategy.evaluate(_cohort(count=2, history=history))
    if history == 129:
        assert signals == ()
    else:
        assert isinstance(signals, tuple)


def test_requires_minimum_universe_and_one_common_as_of_date():
    strategy = BankDailyMultifactorStrategy()
    assert strategy.evaluate(_cohort(count=14)) == ()

    context = _cohort()
    members = dict(context.members)
    shifted = _bars(14, end=BASE - timedelta(days=1), daily_return=0.002)
    members[_symbol(14)] = StrategyContext(
        _symbol(14), shifted[-1].timestamp, bars=tuple(shifted)
    )
    mixed = CohortStrategyContext(timestamp=BASE, members=members, cohort="banks")
    assert strategy.evaluate(mixed) == ()


def test_expected_session_metadata_rejects_common_but_stale_cross_section():
    strategy = BankDailyMultifactorStrategy()
    original = _cohort()
    context = CohortStrategyContext(
        timestamp=original.timestamp,
        members=original.members,
        cohort="banks",
        metadata={"expected_bar_date": "2026-01-06"},
    )

    analysis = strategy.analyze(context)

    assert analysis["ready"] is False
    assert analysis["reason"] == "latest_expected_session_not_met"
    assert analysis["actual_bar_date"] == "2026-01-05"
    assert analysis["expected_bar_date"] == "2026-01-06"
    assert strategy.evaluate(context) == ()


def test_top_candidate_is_explainable_and_signal_only():
    signals = BankDailyMultifactorStrategy().evaluate(_cohort())
    buys = [item for item in signals if item.action == SignalAction.BUY]
    assert buys
    best = min(buys, key=lambda item: item.metadata["rank"])
    assert best.metadata["rank"] == 1
    assert best.metadata["universe_size"] == 15
    assert best.metadata["price_basis"] == "raw_unadjusted"
    assert best.metadata["model_version"] == "bank_daily_multifactor_v1"
    assert best.metadata["signal_only"] is True

    assert best.metadata["entry_eligible"] is True
    assert best.metadata["gate_failures"] == ()
    assert best.metadata["exit_reasons"] == ()
    assert best.metadata["bar_as_of"] == best.timestamp.isoformat()
    assert best.strength > 0
    assert set(best.metadata["factor_percentiles"]) == {
        "price_to_sma_120",
        "sma_20_to_60",
        "momentum_60_5",
        "momentum_120_20",
        "low_volatility_60",
        "shallow_drawdown_60",
    }
    contributions = best.metadata["factor_contributions"]
    weights = best.metadata["factor_weights"]
    assert set(contributions) == set(weights) == set(best.metadata["factor_percentiles"])
    assert sum(contributions.values()) == pytest.approx(best.metadata["percentile"])
    assert best.metadata["score_thresholds"] == {"entry": 0.80, "exit": 0.50}


def test_non_candidate_explains_score_threshold_separately_from_boolean_gates():
    """A HOLD must say whether score/review timing blocked entry.

    ``gate_failures`` only represents boolean health gates.  The explicit
    entry reasons prevent a mathematically valid, but below-threshold, row
    from looking unexplained in research screens.
    """
    strategy = BankDailyMultifactorStrategy()
    snapshot = strategy.analyze(_cohort())
    non_candidates = [
        item for item in snapshot["items"] if not item["entry_eligible"]
    ]
    assert non_candidates
    item = next(
        item
        for item in non_candidates
        if item["score_thresholds"]["exit"] <= item["score"] < item["score_thresholds"]["entry"]
    )
    assert "score_below_entry_percentile" in item["entry_failure_reasons"]
    assert item["action"] == SignalAction.HOLD.value


def test_data_quality_not_decision_ready_suppresses_cross_sectional_signals():
    context = _cohort()
    blocked = CohortStrategyContext(
        timestamp=context.timestamp,
        members=context.members,
        cohort=context.cohort,
        metadata={"strategy_decision_ready": False},
    )
    strategy = BankDailyMultifactorStrategy()
    analysis = strategy.analyze(blocked)
    assert analysis["ready"] is False
    assert analysis["reason"] == "data_quality_not_decision_ready"
    assert all(
        reason == "data_quality_not_decision_ready"
        for reason in analysis["excluded"].values()
    )
    assert strategy.evaluate(blocked) == ()


def _fundamental_report(context, *, decision_at=BASE, scores=None):
    symbols = tuple(sorted(context.members))
    values = scores or {
        symbol: 0.05 + (index / max(len(symbols) - 1, 1)) * 0.90
        for index, symbol in enumerate(symbols)
    }
    return {
        "selection": "bank_fundamental_factors_as_of_v1",
        "decision_at": decision_at.isoformat(),
        "quality": {"status": "ok", "complete_count": len(symbols)},
        "warnings": [],
        "items": [
            {"symbol": symbol, "score": values[symbol], "score_status": "complete"}
            for symbol in symbols
        ],
    }


def test_complete_point_in_time_fundamentals_are_explicitly_blended():
    context = _cohort(count=15)
    price_only = BankDailyMultifactorStrategy(fundamental_weight=0.0).analyze(context)
    report = _fundamental_report(context)
    enriched = CohortStrategyContext(
        timestamp=context.timestamp,
        members=context.members,
        cohort=context.cohort,
        metadata={"fundamental_factor_report": report},
    )
    strategy = BankDailyMultifactorStrategy(fundamental_weight=0.20)
    snapshot = strategy.analyze(enriched)
    assert snapshot["ready"] is True
    assert snapshot["fundamental_overlay"]["enabled"] is True
    assert snapshot["fundamental_overlay"]["status"] == "applied"
    baseline = {item["symbol"]: item for item in price_only["items"]}
    for item in snapshot["items"]:
        expected = baseline[item["symbol"]]["score"] * 0.80 + report["items"][sorted(context.members).index(item["symbol"])] ["score"] * 0.20
        assert item["price_score"] == pytest.approx(baseline[item["symbol"]]["score"])
        assert item["score"] == pytest.approx(expected)
        assert item["fundamental_overlay"]["enabled"] is True
        assert item["fundamental_overlay"]["contribution"] == pytest.approx(
            item["fundamental_overlay"]["score"] * 0.20
        )
    signals = strategy.evaluate(enriched)
    if signals:
        assert all(signal.metadata["model_variant"] == "price_plus_fundamental_v1" for signal in signals)


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda report: report.update({"warnings": ["fundamental_metric_coverage_incomplete"]}), "fundamental_report_has_warnings"),
        (lambda report: report.update({"decision_at": (BASE + timedelta(days=1)).isoformat()}), "fundamental_report_future_of_decision"),
        (lambda report: report["items"].pop(), "fundamental_report_coverage_mismatch"),
    ],
)
def test_incomplete_or_future_fundamental_facts_fall_back_to_price_only(mutate, reason):
    context = _cohort(count=15)
    report = _fundamental_report(context)
    mutate(report)
    strategy = BankDailyMultifactorStrategy(fundamental_weight=0.20)
    snapshot = strategy.analyze(
        CohortStrategyContext(
            timestamp=context.timestamp,
            members=context.members,
            cohort=context.cohort,
            metadata={"fundamental_factor_report": report},
        )
    )
    assert snapshot["fundamental_overlay"]["enabled"] is False
    assert snapshot["fundamental_overlay"]["reason"] == reason
    assert all(item["price_score"] == pytest.approx(item["score"]) for item in snapshot["items"])


def test_quote_does_not_change_completed_daily_signal():
    context = _cohort()
    strategy = BankDailyMultifactorStrategy()
    variants = []
    for price in (Decimal("1"), Decimal("999")):
        with_quotes = {
            symbol: StrategyContext(
                symbol,
                member.timestamp,
                quote=Quote(
                    symbol=symbol,
                    timestamp=member.timestamp,
                    last=price,
                    source="fixture",
                ),
                bars=member.bars,
                metrics=member.metrics,
            )
            for symbol, member in context.members.items()
        }
        variants.append(
            strategy.evaluate(
                CohortStrategyContext(
                    timestamp=context.timestamp,
                    members=with_quotes,
                    cohort=context.cohort,
                )
            )
        )
    first, second = variants
    assert first == second


def test_entry_and_low_score_exit_only_run_on_weekly_review():
    tuesday = BASE + timedelta(days=1)
    signals = BankDailyMultifactorStrategy().evaluate(_cohort(end=tuesday))
    assert signals == ()


def test_context_timestamp_blocks_future_daily_bar():
    cohort = _cohort(count=2)
    context = CohortStrategyContext(
        timestamp=BASE - timedelta(days=1),
        members=cohort.members,
        cohort=cohort.cohort,
    )
    assert BankDailyMultifactorStrategy(min_universe=2).evaluate(context) == ()


def test_suspected_corporate_action_suspends_affected_symbol():
    signals = BankDailyMultifactorStrategy(min_universe=14).evaluate(
        _cohort(corporate_action_index=14)
    )
    assert _symbol(14) not in {item.symbol for item in signals}


def test_historical_corporate_action_gap_is_not_allowed_to_contaminate_factors():
    """A gap earlier in the lookback is still a reason to exclude the symbol.

    Momentum and drawdown use nearly the entire 130-session input.  This
    regression protects against checking only the latest few bars, which
    would allow an older ex-dividend/split discontinuity to produce a false
    rank and research signal.
    """
    context = _cohort()
    members = dict(context.members)
    bars = list(members[_symbol(14)].bars)
    index = 70
    previous = bars[index - 1]
    close = previous.close * Decimal("0.90")
    bars[index] = Bar(
        symbol=previous.symbol,
        interval=previous.interval,
        timestamp=bars[index].timestamp,
        open=close,
        high=close * Decimal("1.01"),
        low=close * Decimal("0.99"),
        close=close,
        volume=previous.volume,
        turnover=close * Decimal(previous.volume),
        source=previous.source,
    )
    members[_symbol(14)] = StrategyContext(
        _symbol(14), bars[-1].timestamp, bars=tuple(bars)
    )
    analysis = BankDailyMultifactorStrategy(min_universe=14).analyze(
        CohortStrategyContext(
            timestamp=context.timestamp,
            members=members,
            cohort=context.cohort,
        )
    )
    assert analysis["ready"] is True
    assert analysis["excluded"][_symbol(14)] == "suspected_corporate_action"
    assert _symbol(14) not in {item["symbol"] for item in analysis["items"]}


def test_risk_exit_overrides_scheduled_entry():
    context = _cohort(count=2)
    members = dict(context.members)
    bars = list(members[_symbol(1)].bars)
    for offset in range(60):
        index = len(bars) - 60 + offset
        original = bars[index]
        multiplier = Decimal("1.03") if offset % 2 == 0 else Decimal("0.97")
        close = original.close * multiplier
        bars[index] = Bar(
            symbol=original.symbol,
            interval=original.interval,
            timestamp=original.timestamp,
            open=close,
            high=close * Decimal("1.01"),
            low=close * Decimal("0.99"),
            close=close,
            volume=original.volume,
            turnover=close * Decimal(original.volume),
            source=original.source,
        )
    members[_symbol(1)] = StrategyContext(
        _symbol(1), bars[-1].timestamp, bars=tuple(bars)
    )
    signals = BankDailyMultifactorStrategy(
        min_universe=2,
        entry_percentile=0.5,
        exit_percentile=0.1,
        corporate_action_gap=0.50,
    ).evaluate(CohortStrategyContext(timestamp=BASE, members=members, cohort="banks"))
    risky = next(item for item in signals if item.symbol == _symbol(1))
    assert risky.action == SignalAction.SELL
    assert risky.strength < 0
    assert risky.metadata["risk_exit"] is True
    assert "risk_limit" in risky.metadata["exit_reasons"]


def test_invalid_zero_volume_member_is_excluded_and_may_make_universe_insufficient():
    signals = BankDailyMultifactorStrategy().evaluate(_cohort(zero_volume_index=1))
    assert signals == ()


def test_tied_factors_get_equal_percentiles_not_symbol_order_advantage():
    members = {}
    for index in range(3):
        bars = _bars(index, daily_return=0.001)
        # Normalize prices/turnover so every factor ties exactly.
        reference = _bars(0, daily_return=0.001)
        normalized = [
            Bar(
                symbol=_symbol(index),
                interval=item.interval,
                timestamp=item.timestamp,
                open=item.open,
                high=item.high,
                low=item.low,
                close=item.close,
                volume=item.volume,
                turnover=item.turnover,
                source=item.source,
            )
            for item in reference
        ]
        members[_symbol(index)] = StrategyContext(
            _symbol(index), normalized[-1].timestamp, bars=tuple(normalized)
        )
    strategy = BankDailyMultifactorStrategy(
        min_universe=3, entry_percentile=0.49, exit_percentile=0.1
    )
    signals = strategy.evaluate(
        CohortStrategyContext(timestamp=BASE, members=members, cohort="banks")
    )
    assert {round(item.metadata["percentile"], 8) for item in signals} == {0.5}
    # Composite ties must also have the same displayed rank.  Symbol order is
    # only a rendering order and must not become an implicit preference.
    assert {item.metadata["rank"] for item in signals} == {1}


def test_parameter_validation_protects_hysteresis_and_lookbacks():
    with pytest.raises(ValueError, match="at least 121"):
        BankDailyMultifactorStrategy(min_history=120)
    with pytest.raises(ValueError, match="exit < entry"):
        BankDailyMultifactorStrategy(entry_percentile=0.5, exit_percentile=0.5)
