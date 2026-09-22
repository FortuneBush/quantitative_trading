"""Strategy extension API."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
import inspect
from types import MappingProxyType
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple, Type, Union

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
    """Allow-listed registry for both per-symbol and cohort strategies.

    The registry deliberately accepts only the two pure strategy contracts;
    it never imports a configured Python path or evaluates configuration.
    """

    def __init__(self) -> None:
        self._types: Dict[str, Type[Union[Strategy, CohortStrategy]]] = {}

    def register(
        self,
        name: str,
        strategy_type: Type[Union[Strategy, CohortStrategy]],
        replace: bool = False,
    ) -> None:
        normalized = str(name).strip().lower()
        if not normalized:
            raise ValueError("strategy registry name cannot be empty")
        if not isinstance(strategy_type, type) or not issubclass(
            strategy_type, (Strategy, CohortStrategy)
        ):
            raise TypeError("strategy_type must inherit Strategy or CohortStrategy")
        if normalized in self._types and not replace:
            raise ValueError("strategy type already registered: {}".format(normalized))
        self._types[normalized] = strategy_type

    def unregister(self, name: str) -> bool:
        return self._types.pop(str(name).strip().lower(), None) is not None

    def create(self, name: str, **configuration: object) -> Union[Strategy, CohortStrategy]:
        normalized = str(name).strip().lower()
        try:
            strategy_type = self._types[normalized]
        except KeyError as exc:
            raise KeyError("unknown strategy type: {}".format(normalized)) from exc
        # Fail with a stable, actionable error before invoking a constructor.
        # This matters for configuration-driven callers: a typo should never
        # silently fall back to a constructor default or leak a Python import
        # path/traceback through an API response.
        signature = inspect.signature(strategy_type.__init__)
        accepted = {
            parameter.name
            for parameter in signature.parameters.values()
            if parameter.name != "self"
            and parameter.kind
            not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        }
        if not any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        ):
            unknown = sorted(set(configuration).difference(accepted))
            if unknown:
                raise TypeError(
                    "unknown strategy parameter(s) for {}: {}".format(
                        normalized, ", ".join(unknown)
                    )
                )
        return strategy_type(**configuration)

    def names(self) -> Tuple[str, ...]:
        return tuple(sorted(self._types))

    def get(self, name: str) -> Optional[Type[Union[Strategy, CohortStrategy]]]:
        return self._types.get(str(name).strip().lower())


def strategy_parameter_schema(strategy: object) -> Tuple[Mapping[str, object], ...]:
    """Return a deterministic constructor schema for a strategy instance.

    The schema is intentionally descriptive only.  It does not import or
    execute arbitrary code and is therefore safe to expose from the strategy
    catalogue API.  Runtime values are included alongside defaults so a
    research client can reproduce the exact configured instance.
    """

    constructor = getattr(type(strategy), "__init__", None)
    if constructor is None:
        return ()
    entries = []
    for parameter in inspect.signature(constructor).parameters.values():
        if parameter.name == "self" or parameter.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        default = parameter.default
        required = default is inspect.Parameter.empty
        annotation = parameter.annotation
        if annotation is inspect.Parameter.empty:
            type_name = "unknown"
        elif isinstance(annotation, type):
            type_name = annotation.__name__
        else:
            type_name = str(annotation)
        value = getattr(strategy, parameter.name, None)
        # ``strategy_name`` is stored privately by built-ins; expose the
        # stable public property instead of an implementation detail.
        if parameter.name == "strategy_name" and hasattr(strategy, "name"):
            value = getattr(strategy, "name")
        item: Dict[str, object] = {
            "name": parameter.name,
            "type": type_name,
            "required": required,
            "value": value,
        }
        if not required:
            # Constructor defaults should be JSON-safe.  Omit exotic defaults
            # rather than serialising reprs that could be mistaken for data.
            if default is None or isinstance(default, (bool, int, float, str)):
                item["default"] = default
        entries.append(item)
    return tuple(entries)
