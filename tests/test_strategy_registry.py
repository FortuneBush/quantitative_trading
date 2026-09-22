"""Offline contract tests for the configuration-driven strategy catalogue."""

from __future__ import annotations

import pytest

from quant_monitor.domain import Signal
from quant_monitor.strategies import (
    BankDailyMultifactorStrategy,
    MovingAverageCrossStrategy,
    Strategy,
    StrategyContext,
    StrategyRegistry,
    strategy_parameter_schema,
)


class _FixtureStrategy(Strategy):
    def __init__(self, threshold: float = 0.5) -> None:
        self.threshold = float(threshold)

    @property
    def name(self) -> str:
        return "fixture"

    def evaluate(self, context: StrategyContext):
        return ()


def test_registry_normalizes_ids_and_rejects_unknown_constructor_parameters():
    registry = StrategyRegistry()
    registry.register("  FIXTURE ", _FixtureStrategy)

    assert registry.names() == ("fixture",)
    assert isinstance(registry.create("FiXtUrE", threshold=0.8), _FixtureStrategy)
    with pytest.raises(TypeError, match=r"unknown strategy parameter\(s\).*typo"):
        registry.create("fixture", typo=True)
    with pytest.raises(KeyError, match="unknown strategy type: absent"):
        registry.create("absent")


def test_registry_duplicate_and_replace_are_explicit():
    registry = StrategyRegistry()
    registry.register("fixture", _FixtureStrategy)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("FIXTURE", _FixtureStrategy)
    registry.register("FIXTURE", _FixtureStrategy, replace=True)
    assert registry.unregister(" FIXTURE ") is True
    assert registry.unregister("fixture") is False


def test_registry_supports_cohort_strategies_with_explicit_constructor_validation():
    registry = StrategyRegistry()
    registry.register("bank", BankDailyMultifactorStrategy)
    strategy = registry.create("BANK", min_history=121, min_universe=2)
    assert isinstance(strategy, BankDailyMultifactorStrategy)
    with pytest.raises(TypeError, match="unknown strategy parameter"):
        registry.create("bank", unexpected=True)


def test_parameter_schema_exposes_effective_values_without_private_state():
    strategy = MovingAverageCrossStrategy(fast_window=3, slow_window=9)
    schema = {item["name"]: item for item in strategy_parameter_schema(strategy)}
    assert schema["fast_window"]["value"] == 3
    assert schema["slow_window"]["default"] == 20
    assert schema["strategy_name"]["value"] == "moving_average_cross"
    assert all(not item["name"].startswith("_") for item in schema.values())
