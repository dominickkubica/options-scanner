"""The contract every market data vendor is squeezed into.

This interface is the reason Phase 5 can swap yfinance for Schwab without touching
analytics. Four rules for implementers:

1. Return the models in optscan.models, never vendor objects, never DataFrames.
2. Raise from optscan.providers.errors, never a vendor exception.
3. Stamp fetched_at at the moment the data was received, and source with self.name.
4. Never silently substitute. If a chain is partial, say so; if a quote is missing,
   return None for that field rather than a plausible looking guess.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, date, datetime
from typing import ClassVar

from optscan.models import OptionChain, PriceBar, Quote, SymbolEvents


class MarketDataProvider(ABC):
    """Read only access to one vendor's market data."""

    #: Stamped onto every record this provider returns. Must be stable across runs:
    #: it is how a stored snapshot is attributed years later.
    name: ClassVar[str]

    #: Whether this vendor's option quotes are real time. Delayed data is usable for
    #: IV history and unusable for deciding a fill, and the UI must be able to say which.
    realtime: ClassVar[bool] = False

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote:
        """Current quote for the underlying.

        Raises SymbolNotFound if the vendor does not know the symbol.
        """

    @abstractmethod
    def get_expirations(self, symbol: str) -> list[date]:
        """All listed expiration dates, ascending.

        Returns an empty list for a symbol with no listed options. That is a fact,
        not a failure.
        """

    @abstractmethod
    def get_chain(self, symbol: str, expiry: date) -> OptionChain:
        """Every contract at one expiry.

        Raises NoDataAvailable if the expiry is not listed for this symbol.
        """

    @abstractmethod
    def get_history(self, symbol: str, days: int) -> list[PriceBar]:
        """Daily bars for roughly the last `days` calendar days, ascending by date.

        Calendar days, not trading days: the caller asks for a window of time and
        gets whatever sessions fall inside it.
        """

    def get_events(self, symbol: str) -> SymbolEvents:
        """Scheduled earnings and ex dividend dates.

        Not abstract, because not every vendor sells a corporate calendar and a
        provider that cannot answer should say so rather than be forced to invent an
        implementation. Callers must handle the refusal: no event data is a reason to
        widen the screen, not to assume the calendar is clear.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not provide a corporate event calendar"
        )

    def now(self) -> datetime:
        """Fetch timestamp source. Overridable so tests can freeze time."""
        return datetime.now(UTC)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} realtime={self.realtime}>"
