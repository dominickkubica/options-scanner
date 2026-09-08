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
    "get_intraday_provider",
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

    if settings.provider == "alpaca":
        from optscan.providers.alpaca import AlpacaProvider

        if not settings.alpaca_credentials_set:
            raise AuthenticationError(
                "OPTSCAN_PROVIDER is alpaca but the key pair is incomplete. Both "
                "OPTSCAN_ALPACA_KEY_ID and OPTSCAN_ALPACA_SECRET_KEY must be set; one "
                "without the other cannot authenticate. Generate them at "
                "app.alpaca.markets and put them in .env."
            )
        return AlpacaProvider(settings)

    raise NotImplementedError(
        f"provider {settings.provider!r} is not implemented. Configured providers are "
        "yfinance and alpaca."
    )


def get_intraday_provider(settings: Settings) -> MarketDataProvider | None:
    """A provider that can serve intraday candles, or None if none is configured.

    Deliberately not `get_provider`. `OPTSCAN_PROVIDER` chooses what captures option
    chains, and that choice is load bearing for reasons unrelated to charting: an IV
    history belongs to one vendor, so switching it restarts every rank from zero. A
    minute candle carries none of that, so a chart should not be held hostage to it.

    Narrow on purpose. Prices only, never volatility, and nothing it returns is stored.
    """
    if settings.alpaca_credentials_set:
        from optscan.providers.alpaca import AlpacaProvider

        return AlpacaProvider(settings)
    return None


def provider_is_realtime(settings: Settings) -> bool:
    """Whether the configured provider quotes in real time, without building one.

    A function rather than a set of provider names, because the answer can depend on
    an account's entitlement rather than on the vendor: Alpaca is delayed on the free
    feed and real time with a signed OPRA agreement, and nothing in a response says
    which. Health checks need this on every request and must not open a connection.
    """
    if settings.provider == "alpaca":
        return settings.alpaca_is_realtime
    return False
