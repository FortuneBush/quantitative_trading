"""Application services composing providers, storage, metrics and strategies."""

from .collection import (
    BarCollectionResult,
    CollectionResult,
    MarketDataCollectionService,
    MarketDataCollector,
    QuoteCollectionResult,
    QuoteSnapshotError,
)
from .event_bus import EventBus, EventHandler, Subscription
from .analysis import AnalysisSnapshot, QuantitativeAnalysisService
from .data_quality import (
    DailyBarDataQualityService,
    DailyDataQualitySummary,
    SymbolDailyQuality,
)
from .indicators import IndicatorCalculator
from .monitoring import CollectionJobSnapshot, MonitoringCycleResult, MonitoringService
from .decision_digest import build_decision_digest, render_decision_digest_markdown

__all__ = [
    "CollectionResult",
    "BarCollectionResult",
    "CollectionJobSnapshot",
    "AnalysisSnapshot",
    "DailyBarDataQualityService",
    "DailyDataQualitySummary",
    "EventBus",
    "EventHandler",
    "IndicatorCalculator",
    "MarketDataCollectionService",
    "MarketDataCollector",
    "QuoteCollectionResult",
    "QuoteSnapshotError",
    "MonitoringCycleResult",
    "MonitoringService",
    "QuantitativeAnalysisService",
    "Subscription",
    "SymbolDailyQuality",
    "build_decision_digest",
    "render_decision_digest_markdown",
]
