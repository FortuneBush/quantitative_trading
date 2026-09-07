"""Strategy extension API."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple, Type

from quant_monitor.domain import Bar, Metric, Quote, Signal, ensure_market_time, normalize_symbol


@dataclass(frozen=True)
class StrategyContext:
    """Read-only data passed to one strategy evaluation."""

    symbol: str
    timestamp: datetime
    quote: Optional[Quote] = None
    bars: Tuple[Bar, ...] = field(default_factory=tuple)
    metrics: Tuple[Metric, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        symbol = normalize_symbol(self.symbol)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "timestamp", ensure_market_time(self.timestamp))
        object.__setattr__(self, "bars", tuple(sorted(self.bars, key=lambda item: item.timestamp)))
        object.__setattr__(self, "metrics", tuple(self.metrics))
        if self.quote is not None and self.quote.symbol != symbol:
            raise ValueError("strategy quote symbol does not match context")
        if any(item.symbol != symbol for item in self.bars):
            raise ValueError("strategy bar symbol does not match context")
        if any(item.symbol != symbol for item in self.metrics):
            raise ValueError("strategy metric symbol does not match context")


class Strategy(ABC):
    """Stable interface for monitoring signals and future backtests."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique, stable strategy identifier."""

    @abstractmethod
    def evaluate(self, context: StrategyContext) -> Sequence[Signal]:
        """Evaluate the latest context and return zero or more signals."""


BaseStrategy = Strategy


@dataclass(frozen=True)
class CohortStrategyContext:
    """Read-only, point-in-time input for a cross-sectional strategy.

    A cohort strategy receives all members together so it can rank comparable
    observations without reaching into a database or provider.  Individual
    strategies remain unchanged; orchestration code must opt into this
    contract explicitly.
    """

    timestamp: datetime
    members: Mapping[str, StrategyContext]
    cohort: str = "cohort"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        timestamp = ensure_market_time(self.timestamp)
        normalized: Dict[str, StrategyContext] = {}
        for key, context in dict(self.members or {}).items():
            symbol = normalize_symbol(key)
            if not isinstance(context, StrategyContext):
                raise TypeError("cohort members must be StrategyContext instances")
            if context.symbol != symbol:
                raise ValueError("cohort member key does not match context symbol")
            if symbol in normalized:
                raise ValueError("cohort member symbols must be unique")
            normalized[symbol] = context
        cohort = str(self.cohort).strip()
        if not cohort:
            raise ValueError("cohort name cannot be empty")
        object.__setattr__(self, "timestamp", timestamp)
        object.__setattr__(
            self, "members", MappingProxyType(dict(sorted(normalized.items())))
        )
        object.__setattr__(self, "cohort", cohort)
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata or {}))
        )


class CohortStrategy(ABC):
    """Pure cross-sectional strategy contract returning research signals."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique, stable strategy identifier."""

    @abstractmethod
    def evaluate(self, context: CohortStrategyContext) -> Sequence[Signal]:
        """Evaluate one point-in-time cohort without I/O or side effects."""


class StrategyRegistry:
    """Registry supporting configuration-driven strategy construction."""

    def __init__(self) -> None:
        self._types: Dict[str, Type[Strategy]] = {}

    def register(self, name: str, strategy_type: Type[Strategy], replace: bool = False) -> None:
        normalized = str(name).strip().lower()
        if not normalized:
            raise ValueError("strategy registry name cannot be empty")
        if not isinstance(strategy_type, type) or not issubclass(strategy_type, Strategy):
            raise TypeError("strategy_type must inherit Strategy")
        if normalized in self._types and not replace:
            raise ValueError("strategy type already registered: {}".format(normalized))
        self._types[normalized] = strategy_type

    def unregister(self, name: str) -> bool:
        return self._types.pop(str(name).strip().lower(), None) is not None

    def create(self, name: str, **configuration: object) -> Strategy:
        normalized = str(name).strip().lower()
        try:
            strategy_type = self._types[normalized]
        except KeyError as exc:
            raise KeyError("unknown strategy type: {}".format(normalized)) from exc
        return strategy_type(**configuration)

    def names(self) -> Tuple[str, ...]:
        return tuple(sorted(self._types))

    def get(self, name: str) -> Optional[Type[Strategy]]:
        return self._types.get(str(name).strip().lower())
