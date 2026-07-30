"""Market data adapters. The only place a vendor SDK may be imported."""

from __future__ import annotations

from typing import TYPE_CHECKING

from optscan.providers.base import MarketDataProvider
from optscan.providers.errors import (
    AuthenticationError,
    MalformedResponse,
    NoDataAvailable,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
    SymbolNotFound,
)

if TYPE_CHECKING:
    from optscan.config import Settings

__all__ = [
    "AuthenticationError",
    "MalformedResponse",
    "MarketDataProvider",
    "NoDataAvailable",
    "ProviderError",
    "ProviderUnavailable",
    "RateLimited",
    "SymbolNotFound",
    "get_provider",
]


def get_provider(settings: Settings) -> MarketDataProvider:
    """Build the provider named in config.

    Imports are deferred so that installing a broker SDK is only required by the
    people who actually configured that broker.
    """
    if settings.provider == "yfinance":
        from optscan.providers.yfinance_provider import YFinanceProvider

        return YFinanceProvider()

    raise NotImplementedError(
        f"provider {settings.provider!r} is not implemented yet. "
        "Schwab and Tradier arrive in Phase 5."
    )
