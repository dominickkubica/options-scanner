"""A vendor's daily history for one symbol: prices, and the volatility they quoted.

This is the shape of a Market Chameleon daily export, and it is deliberately not the
shape of `PriceBar`. A `PriceBar` is OHLCV fetched live from a provider through the
`MarketDataProvider` interface. A row here is a line of a file a human downloaded, and
it carries something no provider on that interface has ever been able to give us: the
vendor's own 30 day constant maturity implied volatility, for every session going back
years.

## Why this arrives as a file instead of through a provider

Because there is no endpoint for it. Every vendor looked at so far publishes current
implied volatility and nothing historical: Tradier's chain is today's chain, yfinance
has no vol at all, and the two paid backfills researched in Phase 8 both wanted money
for raw chains that would then have to be re-solved. IV rank needs a year of daily
observations and the only way this project could ever get one was to wait a year.

A downloaded file skips the wait. That is its entire value and it is worth the
awkwardness of an import step.

## Vol is stored as a decimal

The file quotes IV30 as `17.19`, meaning 17.19 vol points. Everything inside this
project stores volatility as a decimal, `0.1719`, and only `format.js` multiplies by
100 for display. The conversion happens once, in the parser, and there is a test
pinning it. A hundredfold error in a volatility is not subtle in a payoff diagram but
it is completely invisible in a rank, which is where this number is actually going.

## One row per session per vendor per symbol

A day is a day, so identity is `(source, symbol, session_date)` and needs no digest.
That is the one way this differs from the broker ledger, where two identical fills on
one day are two real trades and a content hash cannot separate them.
"""

from __future__ import annotations

from datetime import date
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class VendorDailyBar(BaseModel):
    """One session of one symbol, as one vendor published it.

    Every field past the close is optional because the coverage genuinely varies
    within a single file: the QQQ export carries option open interest only from
    2018-01-30 onward and has three isolated days with no IV30 at all. None means the
    vendor published nothing, never that it published zero.
    """

    model_config = ConfigDict(frozen=True)

    source: str = Field(description="Which vendor published this. Never pool two.")
    symbol: str
    session_date: date

    open: float
    high: float
    low: float
    close: float
    #: Back adjusted for dividends. Returns are computed from this, not from `close`,
    #: because a dividend is not volatility. Measured on 12.7 years of QQQ the choice
    #: moves HV30 by 0.002 vol points on average, so it is nearly free rather than
    #: load bearing, and it is documented here so nobody re-derives that.
    adj_close: float | None = None
    volume: int | None = None
    vwap: float | None = None
    #: Number of trades in the session, where the vendor publishes one. Volume and
    #: trade count answer different questions: 30 million shares in 500,000 prints
    #: is an ordinary day, the same volume in 5,000 prints is a few blocks.
    trade_count: int | None = None

    #: 30 day constant maturity implied volatility, as a decimal. The reason this
    #: model exists.
    iv30: float | None = None

    call_volume: int | None = None
    put_volume: int | None = None
    call_open_interest: int | None = None
    put_open_interest: int | None = None

    @model_validator(mode="after")
    def _check_bar_is_coherent(self) -> Self:
        """The same contract PriceBar enforces, and for the same reason.

        This model was written for a hand downloaded file, where the arithmetic is the
        vendor's and reliable. It is now also filled from an API in bulk, where a
        malformed row is one of thousands nobody reads, and a close outside its own
        high and low would sit in the history forever producing a realized volatility
        that is wrong by an amount nothing can recover.

        Refusing rather than clamping: a repaired bar is a number with no provenance,
        and the caller drops it and says how many it dropped.
        """
        if self.high < self.low:
            raise ValueError(f"high {self.high} is below low {self.low}")
        if not (self.low <= self.open <= self.high):
            raise ValueError(f"open {self.open} outside the low/high range")
        if not (self.low <= self.close <= self.high):
            raise ValueError(f"close {self.close} outside the low/high range")
        return self

    @property
    def average_trade_size(self) -> float | None:
        """Shares per print, or None when either side is missing.

        The cheapest read on who was trading. A day whose volume arrived in far
        fewer, far larger prints than usual is an institutional day, and it looks
        identical to any other day if only volume is stored.
        """
        if not self.trade_count or self.volume is None:
            return None
        return self.volume / self.trade_count
