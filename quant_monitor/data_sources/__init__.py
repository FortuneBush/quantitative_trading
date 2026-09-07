"""Market-data provider interfaces and built-in providers."""

from .base import (
    BarFetchResult,
    BarFetchTransport,
    MarketDataProvider,
    MarketDataProviderError,
)
from .asharehub import (
    AShareHubAuthenticationError,
    AShareHubBudgetError,
    AShareHubConfigurationError,
    AShareHubDataError,
    AShareHubError,
    AShareHubMarketDataProvider,
    AShareHubPermissionError,
    AShareHubRateLimitError,
    AShareHubRequestError,
    AShareHubTransportError,
)
from .alltick import (
    AllTickAuthenticationError,
    AllTickConfigurationError,
    AllTickDataError,
    AllTickDependencyError,
    AllTickError,
    AllTickMarketDataProvider,
    AllTickPermissionError,
    AllTickRateLimitError,
    AllTickTransportError,
)
from .akshare import (
    AkShareDataError,
    AkShareDependencyError,
    AkShareError,
    AKShareMarketDataProvider,
    AkShareMarketDataProvider,
)
from .mock import MockMarketDataProvider

__all__ = [
    "BarFetchResult",
    "BarFetchTransport",
    "MarketDataProvider",
    "MarketDataProviderError",
    "MockMarketDataProvider",
    "AShareHubError",
    "AShareHubConfigurationError",
    "AShareHubAuthenticationError",
    "AShareHubPermissionError",
    "AShareHubRateLimitError",
    "AShareHubBudgetError",
    "AShareHubRequestError",
    "AShareHubTransportError",
    "AShareHubDataError",
    "AShareHubMarketDataProvider",
    "AllTickError",
    "AllTickConfigurationError",
    "AllTickDependencyError",
    "AllTickAuthenticationError",
    "AllTickPermissionError",
    "AllTickRateLimitError",
    "AllTickTransportError",
    "AllTickDataError",
    "AllTickMarketDataProvider",
    "AkShareError",
    "AkShareDependencyError",
    "AkShareDataError",
    "AkShareMarketDataProvider",
    "AKShareMarketDataProvider",
]
