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
    "provider_is_realtime",
]


def get_provider(settings: Settings) -> MarketDataProvider:
    """Build the provider named in config.

    Imports are deferred so that installing a broker SDK is only required by the
    people who actually configured that broker.
    """
    if settings.provider == "yfinance":
        from optscan.providers.yfinance_provider import YFinanceProvider

        return YFinanceProvider()

    if settings.provider == "tradier":
        from optscan.providers.tradier import TradierProvider

        if settings.tradier_token is None:
            raise AuthenticationError(
                "OPTSCAN_PROVIDER is tradier but OPTSCAN_TRADIER_TOKEN is not set. "
                "Create a free sandbox token at developer.tradier.com and put it in .env."
            )
        return TradierProvider(
            settings.tradier_token.get_secret_value(),
            base_url=settings.tradier_base_url,
            realtime=settings.tradier_is_realtime,
            timeout=settings.request_timeout_seconds,
            requests_per_minute=settings.tradier_requests_per_minute,
        )

    raise NotImplementedError(
        f"provider {settings.provider!r} is not implemented yet. Schwab arrives later: "
        "its three legged OAuth needs a browser redirect and cannot be set up unattended."
    )


def provider_is_realtime(settings: Settings) -> bool:
    """Whether the configured provider quotes in real time, without building one.

    A function rather than a set of provider names, because for Tradier the answer
    depends on the environment and the account's entitlement rather than on the vendor.
    Health checks need this answer on every request and must not open a connection to
    get it.
    """
    if settings.provider == "tradier":
        return settings.tradier_is_realtime
    return False
