from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from quant_monitor.backtesting import (
    BacktestConfig,
    BacktestDiagnostics,
    BacktestEngine,
    BacktestMetrics,
    BacktestResult,
    BacktestValidationError,
    EquityPoint,
    Position,
    SkippedExecution,
    Trade,
    TradingConstraint,
)
from quant_monitor.domain import (
    Adjustment,
    AdjustmentFactor,
    Bar,
    SHANGHAI_TZ,
    Signal,
    SignalAction,
)
from quant_monitor.serialization import backtest_result_dict
from quant_monitor.strategies import CohortStrategy, Strategy
from quant_monitor.trading_calendar import VersionedSessionCalendar


BASE = datetime(2025, 1, 6, 15, 0, tzinfo=SHANGHAI_TZ)


def _bar(symbol, day, *, open_price="10", close=None, volume=100000):
    timestamp = BASE + timedelta(days=day)
    opening = Decimal(open_price)
    closing = Decimal(close if close is not None else open_price)
    return Bar(
        symbol=symbol,
        interval="1d",
        timestamp=timestamp,
        open=opening,
        high=max(opening, closing),
        low=min(opening, closing),
        close=closing,
        volume=volume,
        turnover=Decimal("1000000"),
        source="fixture",
    )


class BuyThenSellStrategy(Strategy):
    @property
    def name(self):
        return "buy_then_sell"

    def evaluate(self, context):
        action = {1: SignalAction.BUY, 2: SignalAction.SELL}.get(len(context.bars))
        if action is None:
            return ()
        return (
            Signal(
                strategy=self.name,
                symbol=context.symbol,
                timestamp=context.bars[-1].timestamp,
                action=action,
                strength=1.0 if action == SignalAction.BUY else -1.0,
                price=context.bars[-1].close,
                reason="deterministic fixture",
            ),
        )


class BuyFirstSessionStrategy(Strategy):
    @property
    def name(self):
        return "buy_first_session"

    def evaluate(self, context):
        if len(context.bars) != 1:
            return ()
        return (
            Signal(
                strategy=self.name,
                symbol=context.symbol,
                timestamp=context.timestamp,
                action=SignalAction.BUY,
                strength=1.0,
            ),
        )


class HoldStrategy(Strategy):
    @property
    def name(self):
        return "hold"

    def evaluate(self, context):
        return ()


class CaptureFundamentalReportStrategy(CohortStrategy):
    """Fixture proving that replay receives only explicitly bound reports."""

    def __init__(self):
        self.reports = []

    @property
    def name(self):
        return "capture_fundamental_report"

    def evaluate(self, context):
        self.reports.append(context.metadata.get("fundamental_factor_report"))
        return ()


def _fundamental_report(decision_at):
    return {
        "selection": "bank_fundamental_factors_as_of_v1",
        "decision_at": decision_at.isoformat(),
        "items": [],
    }


def test_backtest_binds_explicit_point_in_time_fundamental_reports_per_session():
    strategy = CaptureFundamentalReportStrategy()
    bars = {
        "600036.SH": [_bar("600036.SH", day) for day in range(2)],
    }
    reports = {
        date(2025, 1, 6): _fundamental_report(BASE.replace(hour=14)),
        date(2025, 1, 7): _fundamental_report(
            (BASE + timedelta(days=1)).replace(hour=14)
        ),
    }
    result = BacktestEngine(strategy).run(
        bars,
        fundamental_factor_reports=reports,
    )

    assert [item["decision_at"] for item in strategy.reports] == [
        reports[date(2025, 1, 6)]["decision_at"],
        reports[date(2025, 1, 7)]["decision_at"],
    ]
    binding = result.diagnostics.assumptions["fundamental_factor_binding"]
    assert binding["status"] == "bound_unattested"
    assert binding["complete"] is True
    assert binding["report_count"] == 2
    assert binding["hash_algorithm"] == "sha256-json-v1"
    assert binding["report_hashes"]["2025-01-06"].startswith("sha256:")
    assert len(binding["report_hashes"]["2025-01-06"]) == 71


def test_backtest_rejects_future_or_out_of_range_fundamental_report():
    bars = {"600036.SH": [_bar("600036.SH", day) for day in range(2)]}
    future = {
        date(2025, 1, 6): _fundamental_report(BASE.replace(hour=15, minute=31)),
    }
    with pytest.raises(BacktestValidationError, match="after its backtest decision"):
        BacktestEngine(CaptureFundamentalReportStrategy()).run(
            bars,
            fundamental_factor_reports=future,
        )
    outside = {
        date(2025, 1, 8): _fundamental_report(BASE.replace(hour=14)),
    }
    with pytest.raises(BacktestValidationError, match="outside backtest calendar"):
        BacktestEngine(CaptureFundamentalReportStrategy()).run(
            bars,
            fundamental_factor_reports=outside,
        )


def test_replay_cancellation_stops_after_current_strategy_evaluation():
    evaluated = []

    class CancelAfterFirst(HoldStrategy):
        def evaluate(self, context):
            evaluated.append(context.timestamp)
            return ()

    def checkpoint():
        if evaluated:
            raise InterruptedError("fixture cancelled")

    bars = [_bar("600036.SH", day) for day in range(3)]
    with pytest.raises(InterruptedError, match="fixture cancelled"):
        BacktestEngine(CancelAfterFirst()).run(
            {"600036.SH": bars}, cancellation_check=checkpoint
        )
    assert len(evaluated) == 1


def test_replay_cancellation_hook_does_not_change_completed_result():
    engine = BacktestEngine(HoldStrategy())
    bars = {"600036.SH": [_bar("600036.SH", day) for day in range(3)]}
    assert engine.run(bars) == engine.run(bars, cancellation_check=lambda: None)


@pytest.mark.parametrize("invalid", [False, True, 0, "cancel"])
def test_replay_rejects_non_callable_cancellation_hook(invalid):
    with pytest.raises(TypeError, match="cancellation_check must be callable"):
        BacktestEngine(HoldStrategy()).run({}, cancellation_check=invalid)


class BuyCohortOnFirstSession(CohortStrategy):
    @property
    def name(self):
        return "buy_cohort_first_session"

    def evaluate(self, context):
        assert context.metadata["backtest"] is True
        assert context.metadata["expected_bar_date"] == context.timestamp.date().isoformat()
        signals = []
        for symbol, member in context.members.items():
            if len(member.bars) == 1:
                signals.append(
                    Signal(
                        strategy=self.name,
                        symbol=symbol,
                        timestamp=member.bars[-1].timestamp,
                        action=SignalAction.BUY,
                        strength=1.0,
                    )
                )
        return tuple(signals)


def test_signals_execute_at_next_session_open_with_a_share_costs_and_lots():
    symbol = "600000.SH"
    bars = [
        _bar(symbol, 0, open_price="10"),
        _bar(symbol, 1, open_price="10"),
        _bar(symbol, 2, open_price="12"),
        _bar(symbol, 3, open_price="11"),
    ]
    config = BacktestConfig(
        initial_cash=Decimal("10000"),
        buy_commission_rate=Decimal("0.001"),
        sell_commission_rate=Decimal("0.001"),
        minimum_commission=Decimal("5"),
        buy_slippage_rate=Decimal("0"),
        sell_slippage_rate=Decimal("0"),
        stamp_duty_rate=Decimal("0.0005"),
    )

    result = BacktestEngine(BuyThenSellStrategy(), config).run({symbol: bars})

    assert [trade.action for trade in result.trades] == [
        SignalAction.BUY,
        SignalAction.SELL,
    ]
    buy, sell = result.trades
    assert buy.signal_timestamp == datetime.combine(
        bars[0].timestamp.date(), time(15, 30), tzinfo=SHANGHAI_TZ
    )
    assert buy.execution_timestamp == datetime.combine(
        bars[1].timestamp.date(), time(9, 30), tzinfo=SHANGHAI_TZ
    )
    assert buy.quantity == 900
    assert buy.commission == Decimal("9.00")
    assert buy.cash_after == Decimal("991.00")
    assert sell.signal_timestamp == datetime.combine(
        bars[1].timestamp.date(), time(15, 30), tzinfo=SHANGHAI_TZ
    )
    assert sell.execution_timestamp == datetime.combine(
        bars[2].timestamp.date(), time(9, 30), tzinfo=SHANGHAI_TZ
    )
    assert sell.quantity == 900
    assert sell.commission == Decimal("10.80")
    assert sell.stamp_duty == Decimal("5.40")
    assert sell.cash_after == Decimal("11774.80")
    assert buy.realized_pnl is None
    assert sell.realized_pnl == Decimal("1774.80")
    assert result.metrics.ending_nav == Decimal("11774.80")
    assert result.metrics.total_return == pytest.approx(0.17748)
    assert result.metrics.trade_count == 2
    assert result.metrics.total_commission == Decimal("19.80")
    assert result.metrics.total_stamp_duty == Decimal("5.40")
    assert result.metrics.closed_trade_count == 1
    assert result.metrics.winning_trade_count == 1
    assert result.metrics.losing_trade_count == 0
    assert result.metrics.break_even_trade_count == 0
    assert result.metrics.win_rate == pytest.approx(1.0)
    assert result.metrics.gross_profit == Decimal("1774.80")
    assert result.metrics.gross_loss == Decimal("0")
    assert result.metrics.realized_pnl == Decimal("1774.80")
    assert result.metrics.profit_factor is None
    assert result.metrics.profit_factor_status == "no_losses"
    assert result.metrics.expectancy_per_closed_trade == Decimal("1774.80")
    assert result.positions == {}
    assert all(point.timestamp.time() == time(15, 30) for point in result.equity_curve)
    assert result.diagnostics.calendar == "observed_sessions_fallback"
    assert result.diagnostics.price_basis == "raw_unadjusted"
    assert result.diagnostics.constraint_coverage == 0.0
    assert result.diagnostics.unverified_fill_count == 2
    assert result.diagnostics.decision_grade is False
    assert result.diagnostics.assumptions["missing_execution_bar"].startswith("unknown")


def test_realized_pnl_preserves_losing_sell():
    trade = Trade(
        symbol="600000.SH",
        action=SignalAction.SELL,
        signal_timestamp=datetime(2025, 1, 6, 15, 30, tzinfo=SHANGHAI_TZ),
        execution_timestamp=datetime(2025, 1, 7, 9, 30, tzinfo=SHANGHAI_TZ),
        quantity=100,
        raw_price=Decimal("9.00"),
        execution_price=Decimal("9.00"),
        gross_notional=Decimal("900.00"),
        commission=Decimal("5.00"),
        stamp_duty=Decimal("0.45"),
        slippage_cost=Decimal("0"),
        cash_after=Decimal("9000"),
        tradeability_verified=True,
        realized_pnl=Decimal("-105.45"),
    )
    assert trade.realized_pnl == Decimal("-105.45")


def test_t_plus_one_means_a_sell_signal_after_purchase_executes_next_day():
    symbol = "600000.SH"
    bars = [_bar(symbol, day, open_price="10") for day in range(4)]

    result = BacktestEngine(
        BuyThenSellStrategy(),
        BacktestConfig(
            initial_cash=Decimal("10000"),
            buy_commission_rate=Decimal("0"),
            sell_commission_rate=Decimal("0"),
            minimum_commission=Decimal("0"),
            stamp_duty_rate=Decimal("0"),
            buy_slippage_rate=Decimal("0"),
            sell_slippage_rate=Decimal("0"),
        ),
    ).run({symbol: bars})

    assert result.trades[0].execution_timestamp.date() == bars[1].timestamp.date()
    assert result.trades[1].execution_timestamp.date() == bars[2].timestamp.date()
    assert not [item for item in result.skipped_executions if item.reason == "t_plus_one"]


def test_default_backtest_rejects_weekend_observation_without_calendar():
    symbol = "600000.SH"
    weekend = Bar(
        symbol=symbol, interval="1d",
        timestamp=datetime(2025, 1, 11, 15, 0, tzinfo=SHANGHAI_TZ),
        open=Decimal("10"), high=Decimal("10"), low=Decimal("10"),
        close=Decimal("10"), volume=1000, turnover=Decimal("10000"),
        source="fixture",
    )
    with pytest.raises(BacktestValidationError, match="not a weekday session"):
        BacktestEngine(HoldStrategy(), BacktestConfig()).run({symbol: [weekend]})


@pytest.mark.parametrize(
    "volume,constraint,reason",
    [
        (0, None, "zero_volume_execution_bar"),
        (
            100000,
            TradingConstraint(
                symbol="600000.SH",
                session_date=(BASE + timedelta(days=1)).date(),
                can_buy=False,
                reason="limit_up_locked",
            ),
            "explicit_buy_not_allowed",
        ),
    ],
)
def test_suspension_or_explicit_limit_lock_skips_without_fabricating_a_fill(
    volume, constraint, reason
):
    symbol = "600000.SH"
    bars = [
        _bar(symbol, 0),
        _bar(symbol, 1, volume=volume),
        _bar(symbol, 2),
    ]
    constraints = () if constraint is None else (constraint,)

    result = BacktestEngine(BuyFirstSessionStrategy()).run(
        {symbol: bars}, trading_constraints=constraints
    )

    assert result.trades == ()
    assert len(result.skipped_executions) == 1
    assert result.skipped_executions[0].reason == reason
    assert result.metrics.ending_nav == result.config.initial_cash


def test_price_limit_boundaries_use_effective_slippage_and_tick_price():
    """Limit locks must block the price that would actually be submitted."""

    symbol = "600000.SH"
    bars = [
        _bar(symbol, 0, open_price="10"),
        _bar(symbol, 1, open_price="9.995"),
        _bar(symbol, 2, open_price="9.50"),
    ]
    config = BacktestConfig(
        buy_slippage_rate=Decimal("0.001"),
        buy_commission_rate=Decimal("0"),
        minimum_commission=Decimal("0"),
    )
    result = BacktestEngine(BuyFirstSessionStrategy(), config).run(
        {symbol: bars},
        trading_constraints=(
            TradingConstraint(
                symbol=symbol,
                session_date=bars[1].timestamp.date(),
                limit_up_price=Decimal("10.01"),
                reason="limit_up_locked",
            ),
        ),
    )

    # 9.995 * 1.001 rounds up to 10.01, so the lock is respected even though
    # the raw bar open itself was below the boundary.
    assert result.trades == ()
    assert result.skipped_executions[0].reason == "limit_up_locked"
    assert result.skipped_executions[0].detail == "limit_up_locked"


def test_limit_down_boundary_blocks_sell_without_fabricating_a_fill():
    symbol = "600000.SH"
    bars = [
        _bar(symbol, 0, open_price="10"),
        _bar(symbol, 1, open_price="10"),
        _bar(symbol, 2, open_price="9.995"),
    ]
    config = BacktestConfig(
        buy_commission_rate=Decimal("0"),
        sell_commission_rate=Decimal("0"),
        minimum_commission=Decimal("0"),
        buy_slippage_rate=Decimal("0"),
        sell_slippage_rate=Decimal("0.001"),
        stamp_duty_rate=Decimal("0"),
    )
    result = BacktestEngine(BuyThenSellStrategy(), config).run(
        {symbol: bars},
        trading_constraints=(
            TradingConstraint(
                symbol=symbol,
                session_date=bars[2].timestamp.date(),
                limit_down_price=Decimal("9.99"),
                reason="limit_down_locked",
            ),
        ),
    )

    # 9.995 * (1 - 0.001) rounds down to 9.98, below the lower boundary.
    assert len(result.trades) == 1
    assert result.trades[0].action == SignalAction.BUY
    assert [item.reason for item in result.skipped_executions] == ["limit_down_locked"]


def test_trading_constraint_limit_prices_are_positive_and_ordered():
    with pytest.raises(ValueError, match="limit_up_price must be positive"):
        TradingConstraint(
            symbol="600000.SH",
            session_date=BASE.date(),
            limit_up_price=Decimal("0"),
        )
    with pytest.raises(ValueError, match="cannot be below"):
        TradingConstraint(
            symbol="600000.SH",
            session_date=BASE.date(),
            limit_up_price=Decimal("9"),
            limit_down_price=Decimal("10"),
        )


def test_cohort_strategy_uses_point_in_time_members_and_deterministic_cash_split():
    symbols = ("600000.SH", "601009.SH")
    bars = {
        symbol: [_bar(symbol, 0), _bar(symbol, 1), _bar(symbol, 2)]
        for symbol in reversed(symbols)
    }
    config = BacktestConfig(
        initial_cash=Decimal("20000"),
        buy_commission_rate=Decimal("0"),
        minimum_commission=Decimal("0"),
        buy_slippage_rate=Decimal("0"),
    )

    first = BacktestEngine(BuyCohortOnFirstSession(), config).run(bars)
    second = BacktestEngine(BuyCohortOnFirstSession(), config).run(bars)

    assert first == second
    assert [item.symbol for item in first.trades] == sorted(symbols)
    assert [item.quantity for item in first.trades] == [1000, 1000]
    assert first.metrics.turnover == pytest.approx(1.0)
    assert set(first.positions) == set(symbols)
    assert all(item.quantity % 100 == 0 for item in first.positions.values())


def test_cohort_preserves_stale_member_so_strategy_can_apply_quality_gates():
    symbols = ("600000.SH", "601009.SH")

    class ObserveMembers(CohortStrategy):
        def __init__(self):
            self.snapshots = []

        @property
        def name(self):
            return "observe_members"

        def evaluate(self, context):
            self.snapshots.append(
                {
                    symbol: member.bars[-1].timestamp.date()
                    for symbol, member in context.members.items()
                }
            )
            return ()

    strategy = ObserveMembers()
    BacktestEngine(strategy).run(
        {
            symbols[0]: [_bar(symbols[0], 0), _bar(symbols[0], 1)],
            symbols[1]: [_bar(symbols[1], 0)],
        }
    )

    assert set(strategy.snapshots[-1]) == set(symbols)
    assert strategy.snapshots[-1][symbols[1]] == BASE.date()


def test_signal_on_final_bar_is_recorded_as_no_next_session():
    symbol = "600000.SH"
    result = BacktestEngine(BuyFirstSessionStrategy()).run({symbol: [_bar(symbol, 0)]})

    assert result.trades == ()
    assert result.skipped_executions[0].attempted_at is None
    assert result.skipped_executions[0].reason == "no_next_session"
    assert result.skipped_executions[0].signal.timestamp.time() == time(15, 30)


def test_input_validation_rejects_duplicate_sessions_wrong_interval_and_future_signal():
    symbol = "600000.SH"
    duplicate = [_bar(symbol, 0), _bar(symbol, 0)]
    with pytest.raises(BacktestValidationError, match="multiple daily bars"):
        BacktestEngine(BuyFirstSessionStrategy()).run({symbol: duplicate})

    with pytest.raises(BacktestValidationError, match="strictly increasing"):
        BacktestEngine(BuyFirstSessionStrategy()).run(
            {symbol: [_bar(symbol, 1), _bar(symbol, 0)]}
        )

    minute = Bar(
        symbol=symbol,
        interval="1m",
        timestamp=BASE,
        open=Decimal("10"),
        high=Decimal("10"),
        low=Decimal("10"),
        close=Decimal("10"),
        volume=1,
    )
    with pytest.raises(BacktestValidationError, match="bar interval"):
        BacktestEngine(BuyFirstSessionStrategy()).run({symbol: [minute]})

    class FutureStrategy(BuyFirstSessionStrategy):
        @property
        def name(self):
            return "future"

        def evaluate(self, context):
            return (
                Signal(
                    strategy=self.name,
                    symbol=context.symbol,
                    timestamp=context.timestamp + timedelta(days=1),
                    action=SignalAction.BUY,
                    strength=1.0,
                ),
            )

    with pytest.raises(BacktestValidationError, match="future-dated"):
        BacktestEngine(FutureStrategy()).run({symbol: [_bar(symbol, 0)]})

    after_evaluation = Bar(
        symbol=symbol,
        interval="1d",
        timestamp=datetime(2025, 1, 6, 16, 0, tzinfo=SHANGHAI_TZ),
        open=Decimal("10"),
        high=Decimal("10"),
        low=Decimal("10"),
        close=Decimal("10"),
        volume=1,
    )
    with pytest.raises(BacktestValidationError, match="after daily_evaluation_time"):
        BacktestEngine(BuyFirstSessionStrategy()).run({symbol: [after_evaluation]})


def test_drawdown_volatility_sharpe_and_slippage_are_finite_and_reproducible():
    symbol = "600000.SH"
    bars = [
        _bar(symbol, 0, open_price="10", close="10"),
        _bar(symbol, 1, open_price="10", close="12"),
        _bar(symbol, 2, open_price="12", close="8"),
        _bar(symbol, 3, open_price="8", close="9"),
    ]
    config = BacktestConfig(
        initial_cash=Decimal("10000"),
        buy_commission_rate=Decimal("0"),
        minimum_commission=Decimal("0"),
        buy_slippage_rate=Decimal("0.01"),
    )

    result = BacktestEngine(BuyFirstSessionStrategy(), config).run({symbol: bars})

    assert result.trades[0].execution_price == Decimal("10.10")
    assert result.trades[0].slippage_cost == Decimal("90.00")
    assert result.metrics.maximum_drawdown > 0
    assert math_is_finite(
        result.metrics.annualized_return,
        result.metrics.annualized_volatility,
        result.metrics.sharpe_ratio,
        result.metrics.turnover,
        result.metrics.sortino_ratio,
        result.metrics.calmar_ratio,
        result.metrics.positive_session_ratio,
    )
    assert 0.0 <= result.metrics.positive_session_ratio <= 1.0
    assert result.metrics.maximum_drawdown_duration_sessions >= 1


def test_sortino_uses_arithmetic_mean_excess_return_not_cagr():
    """Sortino must use the sample mean excess return in its numerator.

    CAGR is a geometric growth statistic and is not interchangeable with the
    arithmetic return convention used by downside deviation.  This fixture
    deliberately has volatile returns so the two numerators differ materially.
    """

    symbol = "600000.SH"
    bars = [
        _bar(symbol, 0, open_price="10", close="10"),
        _bar(symbol, 1, open_price="10", close="11"),
        _bar(symbol, 2, open_price="10", close="12"),
        _bar(symbol, 3, open_price="10", close="11"),
    ]
    annualization = 252
    annual_risk_free = Decimal("0.252")  # one basis point per session
    config = BacktestConfig(
        initial_cash=Decimal("10000"),
        buy_commission_rate=Decimal("0"),
        sell_commission_rate=Decimal("0"),
        minimum_commission=Decimal("0"),
        stamp_duty_rate=Decimal("0"),
        buy_slippage_rate=Decimal("0"),
        sell_slippage_rate=Decimal("0"),
        annualization_sessions=annualization,
        annual_risk_free_rate=annual_risk_free,
    )
    result = BacktestEngine(BuyFirstSessionStrategy(), config).run({symbol: bars})

    returns = [point.daily_return for point in result.equity_curve[1:]]
    daily_risk_free = float(annual_risk_free) / annualization
    excess = [item - daily_risk_free for item in returns]
    downside = (
        sum(min(item, 0.0) ** 2 for item in excess) / len(excess)
    ) ** 0.5 * annualization ** 0.5
    expected = (sum(excess) / len(excess) * annualization) / downside
    assert result.metrics.sortino_ratio == pytest.approx(expected)

    # Regression guard: the previous CAGR-based formula produces a different
    # answer for this volatile path.
    assert result.metrics.sortino_ratio != pytest.approx(
        (result.metrics.annualized_return - float(annual_risk_free)) / downside
    )


def test_backtest_result_serialization_is_complete_and_reproducible():
    symbol = "600000.SH"
    bars = [_bar(symbol, 0, open_price="10"), _bar(symbol, 1, open_price="11")]
    first = BacktestEngine(BuyThenSellStrategy()).run({symbol: bars})
    second = BacktestEngine(BuyThenSellStrategy()).run({symbol: bars})

    first_payload = backtest_result_dict(first)
    second_payload = backtest_result_dict(second)
    assert first_payload["result_hash"] == second_payload["result_hash"]
    assert first_payload["metrics"]["sortino_ratio"] == first.metrics.sortino_ratio
    assert first_payload["diagnostics"]["decision_grade"] is False
    assert "result_hash" not in first_payload["metrics"]


def test_explicit_session_calendar_is_validated_and_exposes_missing_execution_bar():
    symbol = "600000.SH"
    bars = [_bar(symbol, 0), _bar(symbol, 2)]
    calendar = [BASE.date(), (BASE + timedelta(days=1)).date(), (BASE + timedelta(days=2)).date()]

    result = BacktestEngine(BuyFirstSessionStrategy()).run(
        {symbol: bars}, session_calendar=calendar
    )

    assert result.diagnostics.calendar == "explicit_session_calendar"
    skipped = result.skipped_executions[0]
    assert skipped.reason == "missing_execution_bar"
    assert "suspension is not inferred" in skipped.detail
    assert skipped.will_retry is False
    assert result.trades == ()

    explicitly_suspended = BacktestEngine(BuyFirstSessionStrategy()).run(
        {symbol: bars},
        session_calendar=calendar,
        trading_constraints=(
            TradingConstraint(
                symbol=symbol,
                session_date=calendar[1],
                suspended=True,
                reason="exchange_notice",
            ),
        ),
    )
    assert explicitly_suspended.skipped_executions[0].reason == "explicit_suspended"
    assert explicitly_suspended.skipped_executions[0].detail == "exchange_notice"

    with pytest.raises(BacktestValidationError, match="strictly increasing"):
        BacktestEngine(BuyFirstSessionStrategy()).run(
            {symbol: bars}, session_calendar=[calendar[1], calendar[0]]
        )
    with pytest.raises(BacktestValidationError, match="timezone-aware"):
        BacktestEngine(BuyFirstSessionStrategy()).run(
            {symbol: bars}, session_calendar=[datetime(2025, 1, 6)]
        )
    with pytest.raises(BacktestValidationError, match="omits observed"):
        BacktestEngine(BuyFirstSessionStrategy()).run(
            {symbol: bars}, session_calendar=[calendar[0], calendar[1]]
        )
    with pytest.raises(BacktestValidationError, match="outside bars_by_symbol"):
        BacktestEngine(BuyFirstSessionStrategy()).run(
            {symbol: bars},
            session_calendar=calendar,
            trading_constraints=(
                TradingConstraint(symbol="601009.SH", session_date=calendar[0]),
            ),
        )


def test_backtest_fails_closed_when_held_position_loses_valuation_bar():
    """A stale previous close must not be carried through a missing session."""

    symbol = "600000.SH"
    bars = [_bar(symbol, 0), _bar(symbol, 1), _bar(symbol, 3)]
    sessions = [
        (BASE + timedelta(days=offset)).date()
        for offset in range(4)
    ]

    with pytest.raises(BacktestValidationError, match="missing valuation bar"):
        BacktestEngine(BuyFirstSessionStrategy()).run(
            {symbol: bars}, session_calendar=sessions
        )


def test_backtest_accepts_version_labeled_calendar_but_keeps_it_unverified():
    symbol = "600000.SH"
    bars = [_bar(symbol, day) for day in range(3)]
    sessions = tuple(bar.timestamp.date() for bar in bars)
    target = VersionedSessionCalendar(
        sessions,
        identifier="backtest_offline_fixture",
        source="unit_test",
        version="v1",
    )

    result = BacktestEngine(BuyFirstSessionStrategy()).run(
        {symbol: bars}, trading_calendar=target
    )

    assert result.diagnostics.calendar == "backtest_offline_fixture"
    assert result.diagnostics.calendar_source == "unit_test"
    assert result.diagnostics.calendar_version == "v1"
    assert result.diagnostics.calendar_verified is False
    assert result.diagnostics.decision_grade is False
    assert len(result.equity_curve) == len(bars)

    with pytest.raises(BacktestValidationError, match="mutually exclusive"):
        BacktestEngine(BuyFirstSessionStrategy()).run(
            {symbol: bars},
            session_calendar=sessions,
            trading_calendar=target,
        )


def test_backtest_rejects_calendar_outside_coverage_and_forged_verified_metadata():
    symbol = "600000.SH"
    bars = [_bar(symbol, day) for day in range(2)]

    outside = VersionedSessionCalendar(
        (bars[1].timestamp.date(),), source="unit_test", version="v1"
    )
    with pytest.raises(BacktestValidationError, match="outside.*coverage"):
        BacktestEngine(BuyFirstSessionStrategy()).run(
            {symbol: bars}, trading_calendar=outside
        )

    class ForgedCalendar:
        metadata = type(
            "Metadata",
            (),
            {
                "identifier": "claimed_official",
                "source": "claimed_exchange",
                "version": "v999",
                "notice": "caller supplied",
                "verified": True,
            },
        )()
        coverage = (bars[0].timestamp.date(), bars[1].timestamp.date())

        def sessions_between(self, start, end):
            return (start, end)

    result = BacktestEngine(BuyFirstSessionStrategy()).run(
        {symbol: bars}, trading_calendar=ForgedCalendar()
    )
    assert result.diagnostics.calendar == "claimed_official"
    assert result.diagnostics.calendar_verified is False
    assert result.diagnostics.assumptions["calendar_lineage"] == "unverified"
    assert result.diagnostics.decision_grade is False


def test_explicit_suspension_is_distinct_and_valid_sessions_enable_bounded_retry():
    symbol = "600000.SH"

    class ValidForThree(BuyFirstSessionStrategy):
        @property
        def name(self):
            return "valid_for_three"

        def evaluate(self, context):
            produced = super().evaluate(context)
            if not produced:
                return ()
            signal = produced[0]
            return (
                Signal(
                    strategy=self.name,
                    symbol=signal.symbol,
                    timestamp=signal.timestamp,
                    action=signal.action,
                    strength=signal.strength,
                    metadata={"valid_sessions": 3},
                ),
            )

    bars = [_bar(symbol, day) for day in range(4)]
    constraints = (
        TradingConstraint(
            symbol=symbol,
            session_date=bars[1].timestamp.date(),
            suspended=True,
            reason="exchange_suspension",
        ),
        TradingConstraint(
            symbol=symbol,
            session_date=bars[2].timestamp.date(),
            can_buy=False,
            reason="limit_up_locked",
        ),
        TradingConstraint(symbol=symbol, session_date=bars[3].timestamp.date()),
    )

    result = BacktestEngine(ValidForThree()).run(
        {symbol: bars}, trading_constraints=constraints
    )

    assert [item.reason for item in result.skipped_executions] == [
        "explicit_suspended",
        "explicit_buy_not_allowed",
    ]
    assert [item.will_retry for item in result.skipped_executions] == [True, True]
    assert len(result.trades) == 1
    assert result.trades[0].execution_timestamp.date() == bars[3].timestamp.date()
    assert result.trades[0].tradeability_verified is True
    assert [item.detail for item in result.skipped_executions] == [
        "exchange_suspension",
        "limit_up_locked",
    ]


def test_adjusted_basis_requires_explicit_factor_lineage():
    symbol = "600000.SH"
    bars = [_bar(symbol, day) for day in range(2)]
    calendar = [bar.timestamp.date() for bar in bars]
    constraints = tuple(
        TradingConstraint(symbol=symbol, session_date=session) for session in calendar
    )
    with pytest.raises(BacktestValidationError, match="requires an adjustment factor"):
        BacktestEngine(
            BuyFirstSessionStrategy(), BacktestConfig(price_basis="qfq_adjusted_v1")
        ).run({symbol: bars}, session_calendar=calendar, trading_constraints=constraints)


def test_adjusted_basis_applies_explicit_factors_and_records_binding():
    symbol = "600000.SH"
    bars = [_bar(symbol, day) for day in range(2)]
    calendar = [bar.timestamp.date() for bar in bars]
    constraints = tuple(
        TradingConstraint(symbol=symbol, session_date=session) for session in calendar
    )
    factors = {
        (symbol, session): AdjustmentFactor(
            factor_id="{}-{}".format(symbol, session.isoformat()),
            symbol=symbol,
            adjustment=Adjustment.QFQ,
            session_date=session,
            benchmark_date=calendar[-1],
            factor="1.1",
            factor_version="qfq-fixture-v1",
            source="fixture-factor",
            revision_id="fixture-revision-1",
            input_hash="a" * 64,
            computed_at=datetime(2025, 1, 7, 16, 0, tzinfo=SHANGHAI_TZ),
        )
        for session in calendar
    }
    result = BacktestEngine(
        BuyFirstSessionStrategy(), BacktestConfig(price_basis="qfq_adjusted_v1")
    ).run(
        {symbol: bars},
        session_calendar=calendar,
        trading_constraints=constraints,
        adjustment_factors=factors,
    )

    assert result.diagnostics.constraint_coverage == 1.0
    assert result.diagnostics.unverified_fill_count == 0
    assert result.diagnostics.decision_grade is False
    assert "calendar_lineage_unverified" in result.diagnostics.warnings
    assert "corporate_action_factor_lineage_unverified" in result.diagnostics.warnings
    assert result.diagnostics.assumptions["corporate_action_lineage"] == "unverified"
    binding = result.diagnostics.assumptions["adjustment_factor_binding"]
    assert binding["status"] == "bound_unattested"
    assert binding["factor_count"] == 2
    assert binding["factor_versions"] == ("qfq-fixture-v1",)
    with pytest.raises(TypeError):
        result.diagnostics.assumptions["new"] = "value"


def test_adjusted_prices_are_signal_only_raw_prices_drive_execution_and_nav():
    symbol = "600000.SH"
    bars = [
        _bar(symbol, 0, open_price="10", close="10"),
        _bar(symbol, 1, open_price="11", close="11"),
        _bar(symbol, 2, open_price="12", close="12"),
    ]
    sessions = tuple(item.timestamp.date() for item in bars)

    class ObserveStrategy(Strategy):
        @property
        def name(self):
            return "observe_adjusted_only"

        def __init__(self):
            self.seen_closes = []

        def evaluate(self, context):
            self.seen_closes.append(context.bars[-1].close)
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

    def factors_for(basis):
        return {
            (symbol, session): AdjustmentFactor(
                factor_id="{}-{}".format(symbol, session.isoformat()),
                symbol=symbol,
                adjustment=basis,
                session_date=session,
                benchmark_date=sessions[-1],
                factor="2",
                factor_version="fixture-v2",
                source="fixture-factor",
                revision_id="fixture-revision-2",
                input_hash="b" * 64,
                computed_at=datetime(2025, 1, 7, 16, 0, tzinfo=SHANGHAI_TZ),
            )
            for session in sessions
        }

    raw_strategy = ObserveStrategy()
    raw_result = BacktestEngine(raw_strategy).run({symbol: bars})
    adjusted_strategy = ObserveStrategy()
    adjusted_result = BacktestEngine(
        adjusted_strategy, BacktestConfig(price_basis="qfq_adjusted_v2")
    ).run({symbol: bars}, adjustment_factors=factors_for(Adjustment.QFQ))

    assert raw_strategy.seen_closes == [Decimal("10"), Decimal("11"), Decimal("12")]
    assert adjusted_strategy.seen_closes == [Decimal("20"), Decimal("22"), Decimal("24")]
    assert adjusted_result.trades[0].execution_price == raw_result.trades[0].execution_price
    assert adjusted_result.trades[0].quantity == raw_result.trades[0].quantity
    assert adjusted_result.metrics.ending_nav == raw_result.metrics.ending_nav
    assert adjusted_result.equity_curve == raw_result.equity_curve
    assumptions = adjusted_result.diagnostics.assumptions
    assert assumptions["strategy_price_basis"] == "qfq_adjusted_v2"
    assert assumptions["execution_price_basis"] == "raw_unadjusted"
    assert assumptions["valuation_price_basis"] == "raw_unadjusted"
    assert assumptions["bar_sources"] == ("fixture",)


def test_raw_basis_rejects_materialized_adjusted_input():
    symbol = "600000.SH"
    source_marked = _bar(symbol, 0)
    source_marked = Bar(
        symbol=source_marked.symbol,
        interval=source_marked.interval,
        timestamp=source_marked.timestamp,
        open=source_marked.open,
        high=source_marked.high,
        low=source_marked.low,
        close=source_marked.close,
        volume=source_marked.volume,
        turnover=source_marked.turnover,
        source="fixture-raw|qfq|fixture-factor-v1",
    )
    with pytest.raises(BacktestValidationError, match="materialized adjusted"):
        BacktestEngine(HoldStrategy()).run({symbol: (source_marked,)})


def test_constraint_coverage_denominator_ignores_sessions_without_an_execution_bar():
    """Sparse symbols must not be penalised for non-existent execution bars."""

    first = _bar("600000.SH", 0)
    second = _bar("600000.SH", 1)
    sparse = _bar("601398.SH", 1)
    bars = {"600000.SH": (first, second), "601398.SH": (sparse,)}
    constraints = tuple(
        TradingConstraint(symbol=bar.symbol, session_date=bar.timestamp.date())
        for values in bars.values()
        for bar in values
    )

    result = BacktestEngine(HoldStrategy()).run(
        bars, trading_constraints=constraints
    )

    assert result.diagnostics.constrained_session_count == 3
    assert result.diagnostics.constraint_coverage == 1.0
    assert "tradeability_constraints_incomplete" not in result.diagnostics.warnings


def test_constraint_coverage_ignores_snapshot_rows_without_a_matching_bar():
    """Calendar-wide constraint snapshots cannot inflate executable coverage."""

    symbol = "600000.SH"
    observed = _bar(symbol, 0)
    missing_date = (BASE + timedelta(days=1)).date()
    constraints = (
        TradingConstraint(symbol=symbol, session_date=observed.timestamp.date()),
        # The calendar includes this date, but no execution bar exists for it.
        TradingConstraint(symbol=symbol, session_date=missing_date),
    )

    result = BacktestEngine(HoldStrategy()).run(
        {symbol: (observed,)},
        session_calendar=(observed.timestamp.date(), missing_date),
        trading_constraints=constraints,
    )

    assert result.diagnostics.constrained_session_count == 1
    assert result.diagnostics.constraint_coverage == 1.0


def test_result_invariants_reject_inconsistent_metrics_and_empty_curve():
    metrics = BacktestMetrics(
        starting_nav=Decimal("100"),
        ending_nav=Decimal("100"),
        total_return=0,
        annualized_return=0,
        annualized_volatility=0,
        sharpe_ratio=0,
        maximum_drawdown=0,
        turnover=0,
        total_commission=Decimal("0"),
        total_stamp_duty=Decimal("0"),
        total_slippage_cost=Decimal("0"),
        session_count=0,
        trade_count=0,
    )
    diagnostics = BacktestDiagnostics(
        calendar="observed_sessions_fallback",
        calendar_source="built_in_weekday_rule",
        calendar_version="1",
        calendar_verified=False,
        price_basis="raw_unadjusted",
        bar_availability="assumed",
        execution_bar_availability="assumed",
        constraint_coverage=0,
        constrained_session_count=0,
        execution_attempt_count=0,
        unverified_fill_count=0,
        assumptions={"mode": "test"},
        warnings=(),
        decision_grade=False,
    )
    with pytest.raises(ValueError, match="equity_curve cannot be empty"):
        BacktestResult(
            strategy_name="invalid",
            config=BacktestConfig(),
            started_at=BASE,
            ended_at=BASE,
            equity_curve=(),
            trades=(),
            skipped_executions=(),
            metrics=metrics,
            diagnostics=diagnostics,
        )

    with pytest.raises(ValueError, match="at or after 15:30"):
        BacktestConfig(daily_evaluation_time=time(15, 0))


@pytest.mark.parametrize(
    "field,value",
    [
        ("lot_size", True),
        ("lot_size", 100.0),
        ("lot_size", "100"),
        ("annualization_sessions", 252.0),
        ("default_signal_valid_sessions", 1.5),
        ("maximum_signal_valid_sessions", "20"),
    ],
)
def test_backtest_config_integer_fields_reject_coercion(field, value):
    with pytest.raises(TypeError, match="integer"):
        BacktestConfig(**{field: value})


@pytest.mark.parametrize("field,value", [("suspended", 0), ("can_buy", 1), ("can_sell", "true")])
def test_trading_constraint_boolean_fields_reject_truthy_substitutes(field, value):
    with pytest.raises(TypeError, match="boolean"):
        TradingConstraint(
            symbol="600000.SH",
            session_date=BASE.date(),
            **{field: value},
        )


def test_backtest_diagnostics_fail_closed_and_reject_coerced_counts_and_flags():
    arguments = dict(
        calendar="claimed_official",
        calendar_source="caller",
        calendar_version="v1",
        calendar_verified=True,
        price_basis="raw_unadjusted",
        bar_availability="assumed",
        execution_bar_availability="assumed",
        constraint_coverage=0,
        constrained_session_count=0,
        execution_attempt_count=0,
        unverified_fill_count=0,
        assumptions={"mode": "test"},
        warnings=(),
        decision_grade=False,
    )
    diagnostics = BacktestDiagnostics(**arguments)
    assert diagnostics.calendar_verified is False
    assert diagnostics.decision_grade_checks["calendar_lineage_attested"] is False
    assert diagnostics.decision_grade_checks["constraints_complete"] is False
    assert diagnostics.decision_grade_checks["fills_tradeability_verified"] is True
    assert "calendar_lineage_attested" in diagnostics.decision_grade_failures
    assert "bar_availability_attested" in diagnostics.decision_grade_failures
    assert "calendar_version" not in diagnostics.decision_grade_failures
    claimed_grade = dict(arguments)
    claimed_grade["decision_grade"] = True
    assert BacktestDiagnostics(**claimed_grade).decision_grade is False

    for field, value in (
        ("constrained_session_count", True),
        ("execution_attempt_count", 1.5),
        ("unverified_fill_count", "0"),
        ("calendar_verified", 1),
        ("decision_grade", 0),
    ):
        invalid = dict(arguments)
        invalid[field] = value
        with pytest.raises(TypeError):
            BacktestDiagnostics(**invalid)


def test_trade_position_metrics_and_skipped_execution_reject_coerced_contract_fields():
    signal = Signal(
        strategy="fixture",
        symbol="600000.SH",
        timestamp=BASE,
        action=SignalAction.BUY,
        strength=1.0,
    )
    with pytest.raises(TypeError, match="boolean"):
        SkippedExecution(signal, None, "fixture", will_retry=1)
    with pytest.raises(TypeError, match="integer"):
        Position("600000.SH", 100.0, 100, "10", "10", "1000", "0")
    with pytest.raises(TypeError, match="integer"):
        BacktestMetrics(
            starting_nav="100",
            ending_nav="100",
            total_return=0,
            annualized_return=0,
            annualized_volatility=0,
            sharpe_ratio=0,
            maximum_drawdown=0,
            turnover=0,
            total_commission="0",
            total_stamp_duty="0",
            total_slippage_cost="0",
            session_count=1.0,
            trade_count=0,
        )
    with pytest.raises(TypeError, match="integer"):
        Trade(
            symbol="600000.SH",
            action=SignalAction.BUY,
            signal_timestamp=BASE,
            execution_timestamp=BASE + timedelta(days=1),
            quantity="100",
            raw_price="10",
            execution_price="10",
            gross_notional="1000",
            commission="0",
            stamp_duty="0",
            slippage_cost="0",
            cash_after="0",
            tradeability_verified=False,
        )


def math_is_finite(*values):
    return all(value == value and value not in (float("inf"), float("-inf")) for value in values)
