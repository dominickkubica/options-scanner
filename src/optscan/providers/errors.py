"""Provider failure taxonomy.

The distinction that matters is retryable versus not. Retrying a bad symbol forever
is a bug; not retrying a rate limit is a different bug. Every provider maps its
vendor's failures onto these, so the snapshot job never has to know which vendor
it is talking to.
"""

from __future__ import annotations


class ProviderError(Exception):
    """Base for anything a provider could not deliver."""

    retryable: bool = False


class SymbolNotFound(ProviderError):
    """The vendor has no such symbol. Retrying will not help."""

    retryable = False


class NoDataAvailable(ProviderError):
    """The symbol exists but has no options, no expiries, or an empty chain.

    Not an error in the sense of a failure: some symbols simply have no listed options.
    """

    retryable = False


class MalformedResponse(ProviderError):
    """The vendor returned something we cannot normalize.

    Not retryable by default: a schema change will not fix itself on attempt two,
    and quietly retrying hides it.
    """

    retryable = False


class RateLimited(ProviderError):
    """The vendor is throttling us."""

    retryable = True

    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ProviderUnavailable(ProviderError):
    """Network failure, timeout, or a vendor 5xx."""

    retryable = True


class AuthenticationError(ProviderError):
    """Missing, expired, or rejected credentials. Retrying with the same token is pointless."""

    retryable = False
