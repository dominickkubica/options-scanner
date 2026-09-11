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
    "get_intraday_providers",
    "get_news_provider",
    "get_provider",
    "get_quote_provider",
    "get_quote_providers",
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


def get_intraday_providers(settings: Settings) -> list[MarketDataProvider]:
    """Every vendor that can serve intraday candles, best first.

    Alpaca first: its bars are the consolidated tape, fifteen minutes behind on the free
    plan but complete. Yahoo second, and not merely as a spare -- on 2026-09-11 Alpaca's
    data host returned 504 for hours and the intraday chart was simply blank while the
    market traded, which is a worse failure than being fifteen minutes late.
    """
    providers: list[MarketDataProvider] = []
    if settings.alpaca_credentials_set:
        from optscan.providers.alpaca import AlpacaProvider

        providers.append(AlpacaProvider(settings))

    from optscan.providers.yfinance_provider import YFinanceProvider

    providers.append(YFinanceProvider())
    return providers


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


def get_quote_providers(settings: Settings) -> list[MarketDataProvider]:
    """Every vendor that can serve a live price, best first.

    A list rather than one vendor, because on 2026-09-11 Alpaca's data host returned 504
    on every endpoint for hours while the market traded, and the whole application
    quietly showed the previous session's closes: the pills, the chart's last candle and
    the header price all fall back together, because they all read the same quote. The
    fallback was honest -- everything said "stored" -- and honestly blind.

    Alpaca stays first: it publishes a rate limit, batches the whole watchlist into one
    request, and races a real time feed against the delayed tape. Yahoo is second and is
    only reached when the first fails, which keeps its unpublished, silently throttled
    budget for the 15:45 capture that cannot be backfilled.
    """
    providers: list[MarketDataProvider] = []
    if settings.alpaca_credentials_set:
        from optscan.providers.alpaca import AlpacaProvider

        providers.append(AlpacaProvider(settings))

    from optscan.providers.yfinance_provider import YFinanceProvider

    providers.append(YFinanceProvider())
    return providers


def get_quote_provider(settings: Settings) -> MarketDataProvider | None:
    """A provider that can serve live headline prices, or None.

    Third instance of the same narrow exception as `get_intraday_provider` and
    `get_news_provider`, and for the same reason: `OPTSCAN_PROVIDER` decides whose
    implied volatility becomes the stored IV history, and switching it restarts every
    rank from zero. A last trade price carries none of that history.

    That exception is what makes this feature affordable at all. The configured
    provider here is yfinance, which publishes no rate limit and throttles silently, so
    polling it every few seconds would risk the 15:45 capture -- the one job whose data
    cannot be backfilled. Alpaca publishes 200 requests a minute and the whole
    watchlist is one request, so the same poll costs a measured 3% of the budget.

    Prices only, never volatility, and nothing returned here is ever stored.
    """
    if settings.alpaca_credentials_set:
        from optscan.providers.alpaca import AlpacaProvider

        return AlpacaProvider(settings)
    return None


def get_news_provider(settings: Settings) -> MarketDataProvider | None:
    """A provider that can serve news, or None if none is configured.

    Same reasoning as `get_intraday_provider`, and the same narrowness. `OPTSCAN_PROVIDER`
    decides which vendor's implied volatility becomes the stored history, which is a
    choice with consequences that outlive the session. A headline carries none of that
    and nothing here is stored, so the panel should not go blank because the capture
    provider happens to be the one without a news endpoint.
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
