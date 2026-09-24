from datetime import date, datetime
from decimal import Decimal

import pytest

from quant_monitor.backtesting import BacktestConfig, BacktestEngine, TradingConstraint
from quant_monitor.domain import Bar, SHANGHAI_TZ, Signal, SignalAction
from quant_monitor.strategies import Strategy


SYMBOL = "600036.SH"


def _bar(session: date, *, volume: int = 10000) -> Bar:
    timestamp = datetime.combine(session, datetime.min.time()).replace(
        hour=15, tzinfo=SHANGHAI_TZ
    )
    return Bar(
        symbol=SYMBOL,
        interval="1d",
        timestamp=timestamp,
        open=Decimal("10"),
        high=Decimal("10"),
        low=Decimal("10"),
        close=Decimal("10"),
        volume=volume,
        turnover=Decimal("1000000"),
        source="fixture",
    )


class BuyThenSell(Strategy):
    @property
    def name(self):
        return "buy_then_sell_execution_policy"

    def evaluate(self, context):
        action = {1: SignalAction.BUY, 2: SignalAction.SELL}.get(len(context.bars))
        if action is None:
            return ()
        return (
            Signal(
                strategy=self.name,
                symbol=context.symbol,
                timestamp=context.timestamp,
                action=action,
                strength=1.0,
            ),
        )


def _bars():
    return {
        SYMBOL: [
            _bar(date(2025, 1, 6), volume=10000),
            _bar(date(2025, 1, 7), volume=10000),
            _bar(date(2025, 1, 8), volume=1000),
            _bar(date(2025, 1, 9), volume=10000),
        ]
    }


def _constraints():
    return tuple(
        TradingConstraint(symbol=SYMBOL, session_date=session)
        for session in (date(2025, 1, 7), date(2025, 1, 8))
    )


def test_volume_participation_cap_bounds_buy_and_sell_quantities():
    result = BacktestEngine(
        BuyThenSell(),
        BacktestConfig(
            initial_cash=Decimal("100000"),
            max_volume_participation_rate=Decimal("0.10"),
        ),
    ).run(_bars(), trading_constraints=_constraints())

    # The buy is capped at 10% of 10,000 shares and the next-day sell is
    # capped at 10% of 1,000 shares, leaving the remainder explicitly open.
    assert [trade.quantity for trade in result.trades] == [1000, 100]
    assert [trade.action for trade in result.trades] == [SignalAction.BUY, SignalAction.SELL]
    assert [item.reason for item in result.skipped_executions] == [
        "volume_participation_capped",
        "volume_participation_capped",
    ]
    assumptions = result.diagnostics.assumptions
    assert assumptions["volume_execution_model"] == "bar_volume_participation_cap"
    assert assumptions["max_volume_participation_rate"] == "0.10"


def test_strict_tradeability_policy_skips_unconstrained_fills():
    result = BacktestEngine(
        BuyThenSell(),
        BacktestConfig(require_explicit_tradeability=True),
    ).run(_bars())

    assert result.trades == ()
    assert result.skipped_executions[0].reason == "missing_tradeability_constraint"
    assert result.diagnostics.assumptions["require_explicit_tradeability"] is True


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"require_explicit_tradeability": 1}, "boolean"),
        ({"max_volume_participation_rate": Decimal("0")}, "positive"),
        ({"max_volume_participation_rate": Decimal("1.01")}, "must not exceed 1"),
    ],
)
def test_execution_policy_configuration_is_validated(kwargs, expected):
    with pytest.raises((TypeError, ValueError), match=expected):
        BacktestConfig(**kwargs)
