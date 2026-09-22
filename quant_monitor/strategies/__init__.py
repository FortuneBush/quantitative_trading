"""Pluggable strategy contracts and built-in examples."""

from .bank_multifactor import BankDailyMultifactorStrategy
from .base import (
    BaseStrategy,
    CohortStrategy,
    CohortStrategyContext,
    Strategy,
    StrategyContext,
    StrategyRegistry,
    strategy_parameter_schema,
)
from .moving_average import MovingAverageCrossStrategy

__all__ = [
    "BaseStrategy",
    "BankDailyMultifactorStrategy",
    "CohortStrategy",
    "CohortStrategyContext",
    "MovingAverageCrossStrategy",
    "Strategy",
    "StrategyContext",
    "StrategyRegistry",
    "strategy_parameter_schema",
]
