"""Normalized market data records.

Vendor quirks are absorbed here so nothing downstream has to know which provider a
number came from. Two conventions that matter:

- None means unknown. 0.0 means the vendor said zero, which for a bid means no bid.
  Collapsing the two would quietly turn a missing quote into a worthless contract.
- Prices are floats, not Decimal. Options analytics is floating point end to end
  (vol solving, greeks, monte carlo), so an exact decimal price buys nothing and
  costs a conversion at every boundary. Rounding for display is the UI's job.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Self

from pydantic import BeforeValidator, Field, field_validator, model_validator

from optscan.models.base import Record, UtcDatetime
from optscan.models.enums import Right

# Above this an implied vol is almost certainly a vendor artifact from a stale or
# one-sided quote rather than a real market. 1000 percent annualized.
MAX_PLAUSIBLE_IV = 10.0

# Vendors use a tiny positive number as a null for implied vol. Anything at or below
# this is a placeholder, not a market.
MIN_PLAUSIBLE_IV = 1e-4


def _normalize_symbol(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"symbol must be a string, got {type(value).__name__}")
    symbol = value.strip().upper()
    if not symbol:
        raise ValueError("symbol must not be blank")
    return symbol


Symbol = Annotated[str, BeforeValidator(_normalize_symbol), Field(min_length=1, max_length=32)]
Price = Annotated[float, Field(ge=0.0)]
Size = Annotated[int, Field(ge=0)]


class Quote(Record):
    """A snapshot of the underlying."""

    symbol: Symbol
    last: Price | None = None
    bid: Price | None = None
    ask: Price | None = None
    bid_size: Size | None = None
    ask_size: Size | None = None
    previous_close: Price | None = None
    volume: Size | None = None
    currency: str = "USD"
    market_state: str | None = None

    @property
    def mid(self) -> float | None:
        """Midpoint, only when both sides are present and positive."""
        if self.bid is None or self.ask is None or self.bid <= 0 or self.ask <= 0:
            return None
        return (self.bid + self.ask) / 2

    @property
    def price(self) -> float | None:
        """Best available spot: mid when there is a two sided market, else last trade.

        Mid is preferred because last can be minutes or hours old on a thin name,
        and every downstream greek is a function of this number.
        """
        return self.mid if self.mid is not None else self.last


class PriceBar(Record):
    """One OHLCV bar. Used for realized vol, levels, and charting."""

    symbol: Symbol
    ts: UtcDatetime
    open: Price
    high: Price
    low: Price
    close: Price
    volume: Size | None = None

    @model_validator(mode="after")
    def _check_bar_is_coherent(self) -> Self:
        if self.high < self.low:
            raise ValueError(f"high {self.high} is below low {self.low}")
        if not (self.low <= self.open <= self.high):
            raise ValueError(f"open {self.open} outside the low/high range")
        if not (self.low <= self.close <= self.high):
            raise ValueError(f"close {self.close} outside the low/high range")
        return self


class OptionContract(Record):
    """A single listed option contract as the vendor reported it, normalized.

    vendor_iv is kept for comparison only. Phase 2 solves our own implied vol from
    the mid price with our own rate assumption, and that is the number analytics use.
    """

    symbol: Symbol
    contract_symbol: str | None = None
    expiry: date
    strike: float = Field(gt=0.0)
    right: Right

    bid: Price | None = None
    ask: Price | None = None
    last: Price | None = None
    last_trade_at: UtcDatetime | None = None
    volume: Size | None = None
    open_interest: Size | None = None
    vendor_iv: float | None = Field(default=None, gt=0.0, le=MAX_PLAUSIBLE_IV)
    in_the_money: bool | None = None
    contract_size: int = Field(default=100, gt=0)
    currency: str = "USD"

    @field_validator("right", mode="before")
    @classmethod
    def _parse_right(cls, value: str | Right) -> Right:
        return Right.parse(value)

    @field_validator("vendor_iv", mode="before")
    @classmethod
    def _drop_junk_iv(cls, value: float | None) -> float | None:
        """Vendors report 0.0 or a placeholder like 1e-05 when they have nothing."""
        if value is None:
            return None
        numeric = float(value)
        if numeric <= MIN_PLAUSIBLE_IV or numeric > MAX_PLAUSIBLE_IV:
            return None
        return numeric

    @property
    def has_two_sided_market(self) -> bool:
        """Both sides quoted and positive. Anything else cannot be priced honestly."""
        return self.bid is not None and self.ask is not None and self.bid > 0 and self.ask > 0

    @property
    def is_crossed(self) -> bool:
        """Bid above ask. Always bad data, never an opportunity."""
        return self.bid is not None and self.ask is not None and self.bid > self.ask

    @property
    def mid(self) -> float | None:
        if not self.has_two_sided_market or self.is_crossed:
            return None
        return (self.bid + self.ask) / 2  # type: ignore[operator]

    @property
    def spread(self) -> float | None:
        if not self.has_two_sided_market:
            return None
        return self.ask - self.bid  # type: ignore[operator]

    @property
    def spread_pct_of_mid(self) -> float | None:
        """Spread as a fraction of mid. The single best cheap liquidity filter."""
        mid = self.mid
        spread = self.spread
        if mid is None or spread is None or mid <= 0:
            return None
        return spread / mid

    def dte(self, asof: date) -> int:
        """Calendar days to expiry. Negative once expired, and callers must handle that."""
        return (self.expiry - asof).days

    def quote_age_seconds(self, now: datetime | None = None) -> float | None:
        """Seconds since the last trade print. None when the vendor gave no trade time.

        This is trade staleness, not quote staleness. A contract can have a live
        two sided quote and a last trade from three days ago.
        """
        if self.last_trade_at is None:
            return None
        reference = now or datetime.now(UTC)
        return (reference - self.last_trade_at).total_seconds()


class OptionChain(Record):
    """Every contract for one underlying at one expiry, as of one fetch."""

    symbol: Symbol
    expiry: date
    underlying_price: float | None = Field(default=None, gt=0.0)
    contracts: tuple[OptionContract, ...] = ()

    @model_validator(mode="after")
    def _contracts_belong_to_this_chain(self) -> Self:
        for contract in self.contracts:
            if contract.symbol != self.symbol:
                raise ValueError(
                    f"contract {contract.contract_symbol or contract.strike} has symbol "
                    f"{contract.symbol}, chain is {self.symbol}"
                )
            if contract.expiry != self.expiry:
                raise ValueError(
                    f"contract {contract.contract_symbol or contract.strike} expires "
                    f"{contract.expiry}, chain is {self.expiry}"
                )
        return self

    @property
    def calls(self) -> tuple[OptionContract, ...]:
        return tuple(c for c in self.contracts if c.right is Right.CALL)

    @property
    def puts(self) -> tuple[OptionContract, ...]:
        return tuple(c for c in self.contracts if c.right is Right.PUT)

    @property
    def strikes(self) -> tuple[float, ...]:
        return tuple(sorted({c.strike for c in self.contracts}))

    def get(self, strike: float, right: Right | str) -> OptionContract | None:
        """Exact strike lookup. Returns None rather than raising, since a missing
        strike is a normal fact about a chain, not an error."""
        wanted = Right.parse(right)
        for contract in self.contracts:
            if contract.right is wanted and contract.strike == strike:
                return contract
        return None


class NewsItem(Record):
    """One published story about a symbol.

    Vendor text, carried through rather than interpreted. A `Record` like everything
    else that came from outside the process, so it says which provider wrote it and
    when it was fetched: a headline with no attribution is the kind of thing that ends
    up quoted back as fact.

    The tagged symbols are whatever the wire attached, which on a market wrap can be
    thirty tickers. `primary_for` exists so a caller can tell "this story is about
    AAPL" from "AAPL appeared in a list of thirty".
    """

    id: int
    headline: str
    published_at: UtcDatetime
    wire: str = ""
    author: str = ""
    summary: str = ""
    url: str | None = None
    image: str | None = None
    symbols: tuple[str, ...] = ()

    @property
    def breadth(self) -> int:
        """How many symbols the story was tagged with."""
        return len(self.symbols)

    def primary_for(self, symbol: str, limit: int = 6) -> bool:
        """Is this plausibly *about* the symbol, rather than a list it appears in?

        A market wrap tagged with thirty tickers is not news about any one of them. It
        is still shown, dimmed, because occasionally the wrap is the thing that moved
        the price.
        """
        return symbol.upper() in self.symbols and self.breadth <= limit
