"""Live headline prices for a set of symbols.

## Why this is its own endpoint and not a field on /watchlist

Because it is the only thing on the page that is polled. `/watchlist` seeds the
watchlist table, lists it, and stats a snapshot directory per symbol to find the last
capture; none of that changes between two ticks of a price, and doing it every ten
seconds would be a database write and seven directory walks to learn that QQQ moved a
cent. This endpoint touches the database only when the vendor fails.

`/watchlist` still carries quotes, so the first paint has prices without waiting for a
second round trip. The poll then targets this.

## What "as fresh as possible" actually means here

Measured against the live API on 2026-09-08, this account cannot buy a real time
consolidated price at any polling rate:

    feed=sip          403, "subscription does not permit querying recent SIP data"
    feed=iex          200 and genuinely real time, but IEX only -- 634k of QQQ's
                      28.7m shares, and TJX quoted 122.13/136.73 after hours against
                      a consolidated 129.00/129.80
    feed=delayed_sip  200, the whole tape, fifteen minutes behind

So the ceiling is fifteen minutes, and the delay is *published* rather than absorbed:
`delay_minutes` rides on every response and the UI prints the age. The alternative on
offer -- IEX, real time, two percent of the volume -- is the worse trade for this tool,
because a price that is wrong by an unknown amount looks exactly like one that is
right, whereas a price that is late says so.

The delay is a subscription, not a design. Nothing here assumes it: when the entitlement
changes, `STOCK_SNAPSHOT_FEED` becomes `sip` and this same poll is second by second.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query

from optscan.api.deps import SettingsDep, live_quotes
from optscan.api.schemas import QuoteOut, QuotesOut
from optscan.market_calendar import SessionState, session_state
from optscan.models import LiveQuote

router = APIRouter(tags=["quotes"])

#: Seconds between polls, by session. Open is the only state where a price moves, and
#: even there this is a delayed feed, so a tighter loop buys nothing but requests.
#: Closed is not "never": somebody leaves the tab open overnight and the pills should
#: come alive at the bell without a reload.
POLL_SECONDS = {
    SessionState.OPEN: 10.0,
    SessionState.PRE: 30.0,
    SessionState.POST: 30.0,
    SessionState.CLOSED: 300.0,
}


def quote_view(quote: LiveQuote) -> QuoteOut:
    """Shape one quote for the wire. The derived moves are computed once, here."""
    return QuoteOut(
        last=quote.last if quote.last is not None else 0.0,
        change=quote.change,
        change_pct=quote.change_pct,
        extended=quote.extended,
        extended_change=quote.extended_change,
        volume=quote.volume,
        as_of=quote.as_of,
        session=quote.session,
        realtime=quote.realtime,
        feed=quote.feed,
        delay_minutes=quote.delay_minutes,
    )


def quote_views(quotes: dict[str, LiveQuote]) -> dict[str, QuoteOut]:
    # A quote with no price at all is dropped rather than sent as zero. Downstream this
    # renders as a pill, and a pill reading 0.00 is a claim about the market.
    return {symbol: quote_view(quote) for symbol, quote in quotes.items() if quote.last is not None}


@router.get("/quotes", response_model=QuotesOut)
def quotes(
    settings: SettingsDep,
    symbols: Annotated[
        str, Query(description="Comma separated tickers. Unknown ones are omitted, not an error.")
    ],
) -> QuotesOut:
    wanted = [part.strip().upper() for part in symbols.split(",") if part.strip()]
    resolved, note = live_quotes(settings, wanted)

    state = session_state(datetime.now(UTC))

    # The delay belongs to the feed that answered, and a mixed batch has no single one:
    # a symbol that fell back to a stored close is not "fifteen minutes late", it is a
    # day late, and `as_of` on that quote is what says so. The worst case is reported,
    # so the headline caveat can never understate how old the oldest pill is.
    delays = [quote.delay_minutes for quote in resolved.values() if quote.delay_minutes is not None]

    return QuotesOut(
        quotes=quote_views(resolved),
        note=note,
        delay_minutes=max(delays) if delays else None,
        poll_seconds=POLL_SECONDS.get(state, 60.0),
    )
