"""Levels and projections: the context layer for choosing a strike.

One endpoint, because the phase's exit criterion is one chart. Splitting price history,
levels, the cone and the candidate strikes across four requests would let the browser
assemble a picture from four moments in time, and this is precisely the panel where
that matters: the whole point of it is to judge a strike against a level, and a strike
priced from one capture drawn over levels built from another is a comparison of two
different afternoons.

The payload carries two provenances rather than one, for the same reason. The bars are
fetched from the vendor at request time; the implied volatilities behind the cone and
the candidate strikes come from the last stored capture. Those are genuinely different
ages and the header says both.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from optscan.analytics.gex import OiRow, build_profile
from optscan.analytics.greeks import time_to_expiry
from optscan.analytics.levels import build_levels
from optscan.api.deps import (
    MAX_OI_EXPIRIES,
    ProviderFactoryDep,
    ScreenConfigDep,
    SettingsDep,
    live_quotes,
    make_provenance,
    oi_chain,
    oi_expiries,
    price_history,
    solved_symbol,
    vendor_symbol,
)
from optscan.api.schemas import GexOut, LevelsOut
from optscan.api.views import gex_view, levels_view
from optscan.config import Settings
from optscan.logging import get_logger
from optscan.market_calendar import market_local_date, previous_trading_day, session_state
from optscan.models import OptionChain
from optscan.tickers import normalize_ticker

router = APIRouter(tags=["levels"])
log = get_logger("optscan.api.levels")

#: Sessions of history behind the levels. A year is long enough for a 200 day moving
#: average to exist and short enough that the volume profile is not describing a
#: regime that ended eighteen months ago.
DEFAULT_DAYS = 365

MIN_DAYS = 60
MAX_DAYS = 1825

DaysQuery = Annotated[int, Query(ge=MIN_DAYS, le=MAX_DAYS)]
ExpiryQuery = Annotated[date | None, Query()]


@router.get("/symbols/{symbol}/levels", response_model=LevelsOut)
def symbol_levels(  # noqa: PLR0917
    symbol: str,
    settings: SettingsDep,
    config: ScreenConfigDep,
    provider_factory: ProviderFactoryDep,
    days: DaysQuery = DEFAULT_DAYS,
    expiry: ExpiryQuery = None,
) -> LevelsOut:
    """Price levels, the expected move cone, and the screener's strikes for one symbol.

    `expiry` selects which expiry the cone highlights and which one the terminal
    distribution is drawn for. Left unset the server picks the first expiry at or
    beyond the screen's own minimum DTE, the same rule the chain endpoint uses, so the
    browser never has to know a screen threshold.
    """
    solved = solved_symbol(symbol, settings, config, provider_factory=provider_factory)
    if solved is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No stored snapshot for {symbol.strip().upper()}. "
                "Run `optscan snapshot` to capture one."
            ),
        )

    bars, note = price_history(solved.symbol, days, provider_factory=provider_factory)

    # Levels are built from the bars alone, so a failed candle fetch costs the levels
    # and nothing else. The cone and the strikes still render off the stored capture,
    # which is the honest partial rather than an error page.
    levels = build_levels(bars, spot=solved.analysis.spot) if bars else None

    return levels_view(
        solved=solved,
        bars=bars,
        bars_note=note,
        levels=levels,
        expiry=_choose_expiry(solved, config, expiry),
        rate=settings.risk_free_rate,
        config=config,
    )


def _choose_expiry(solved, config, requested: date | None) -> date | None:
    """The expiry to project, resolved the same way the chain endpoint resolves it.

    A requested expiry that was never captured is refused rather than quietly replaced
    with a neighbour, because a cone labelled with one date and drawn from another
    expiry's volatility is wrong in a way nobody would catch by looking.
    """
    listed = [item.expiry for item in solved.analysis.expiries]
    if not listed:
        return None
    if requested is not None:
        if requested not in listed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"{solved.symbol} has no captured chain for {requested}. "
                    f"Captured expiries: {', '.join(item.isoformat() for item in listed)}."
                ),
            )
        return requested

    minimum = config.filters.dte.min_dte
    eligible = [item for item in solved.analysis.expiries if item.dte >= minimum]
    return (eligible[0] if eligible else solved.analysis.expiries[0]).expiry


# --------------------------------------------------------------------------------
# Open interest and gamma
# --------------------------------------------------------------------------------

#: Said on every response rather than only when something looks wrong, because the
#: staleness here is structural and permanent: OCC publishes open interest overnight,
#: so these levels always describe the prior close however fresh the fetch was.
OPEN_INTEREST_NOTE = (
    "Open interest is published overnight, so these levels describe where contracts "
    "stood at the prior close. Today's trading is not in them."
)

#: Bounds on the "everything inside N days" toggle. The count of expiries that fall
#: inside the window is capped separately, by MAX_OI_EXPIRIES, because a weekly name
#: lists far more inside 30 days than anyone wants to pay vendor round trips for.
MIN_WITHIN_DAYS = 1
MAX_WITHIN_DAYS = 90

WithinDaysQuery = Annotated[int | None, Query(ge=MIN_WITHIN_DAYS, le=MAX_WITHIN_DAYS)]


@router.get("/symbols/{symbol}/gex", response_model=GexOut)
def symbol_gex(
    symbol: str,
    settings: SettingsDep,
    provider_factory: ProviderFactoryDep,
    expiry: ExpiryQuery = None,
    within_days: WithinDaysQuery = None,
) -> GexOut:
    """Open interest walls, max pain, and dealer gamma for one expiry.

    ## Why this is not part of the levels payload

    The levels endpoint is drawn from the stored capture and costs no vendor request.
    This one fetches a chain per expiry, and folding the two together would make every
    levels request pay for open interest that most of them never show. They are also
    genuinely different ages: the levels above are price history, and open interest is
    a settlement figure from last night.

    ## Why it does not go through the solved snapshot

    Because `^SPX` has never been captured and never will be by the nightly job, which
    walks the watchlist. The index chains are half the reason the panel exists, so the
    chain is fetched by symbol rather than read from a capture. `expiry` unset means
    the nearest listed expiry, which is a 0DTE on a day one is listed.
    """
    try:
        normalized = normalize_ticker(symbol)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error

    listed, expiry_note = oi_expiries(normalized, provider_factory=provider_factory)
    if not listed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=expiry_note or f"No options are listed for {normalized}.",
        )

    now = datetime.now(UTC)
    chosen, expired_note = _choose_gex_expiry(normalized, listed, expiry, now)
    today = date.today()
    wanted, window_note = _expiry_window(chosen, listed, within_days, today)

    notes: list[str] = []
    chains: list[OptionChain] = []
    for item in wanted:
        chain, note = oi_chain(normalized, item, provider_factory=provider_factory)
        if chain is not None:
            chains.append(chain)
        elif note:
            notes.append(note)

    if not chains:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=notes[0] if notes else f"No chain could be fetched for {normalized}.",
        )

    spot, spot_source, quote_as_of, spot_note = _gex_spot(normalized, chains, settings)
    if spot is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"No price could be found for {normalized}, so no level can be measured.",
        )
    if spot_note:
        notes.append(spot_note)

    rows = [row for chain in chains for row in _oi_rows(chain, now)]
    profile = build_profile(rows, spot, settings.risk_free_rate)

    newest = max(chain.fetched_at for chain in chains)
    state = session_state(now, settings.market_calendar, settings.market_timezone)
    if expired_note:
        notes.append(expired_note)
    if expiry_note:
        notes.append(expiry_note)
    if window_note:
        notes.append(window_note)
    notes.append(OPEN_INTEREST_NOTE)

    return gex_view(
        symbol=normalized,
        vendor=vendor_symbol(normalized),
        profile=profile,
        expiry=chosen,
        dte=(chosen - today).days,
        expiries=[(item, (item - today).days) for item in listed],
        within_days=within_days,
        expiries_used=[chain.expiry for chain in chains],
        spot_source=spot_source,
        chain_provenance=make_provenance(chains[0].source, newest, now),
        open_interest_as_of=_open_interest_session(newest, settings),
        quote_as_of=quote_as_of,
        market_state=state.value,
        notes=notes,
    )


def _choose_gex_expiry(
    symbol: str,
    listed: list[date],
    requested: date | None,
    now: datetime,
) -> tuple[date, str | None]:
    """The requested expiry, or the nearest one that is still trading.

    A requested expiry that is not listed is refused rather than replaced with a
    neighbour, for the same reason the cone refuses one: a wall labelled Friday and
    computed from next Friday's open interest is wrong in a way nobody would catch.

    The default skips an expiry whose 16:00 has passed. Vendors keep today's expiry in
    the list all evening, so "the nearest listed expiry" means a dead one from the close
    until midnight: every contract has zero time, so gamma cannot be computed for any of
    them and the panel reads as a chain with no volatilities. The walls on an expired
    chain are also the walls on contracts that have already settled. A reader who wants
    it can still select it, and then the notes say what it is.
    """
    if requested is None:
        live = [item for item in listed if time_to_expiry(item, now) > 0.0]
        if not live:
            return listed[0], (
                "Every listed expiry has already stopped trading, so this is the most "
                "recent one. Its contracts have settled."
            )
        if live[0] != listed[0]:
            return live[0], (
                f"{listed[0].isoformat()} stopped trading at 16:00, so the nearest "
                "expiry still open was used instead."
            )
        return live[0], None
    if requested not in listed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"{symbol} has no listed chain for {requested}. "
                f"Listed: {', '.join(item.isoformat() for item in listed[:12])}."
            ),
        )
    return requested, None


def _expiry_window(
    chosen: date,
    listed: list[date],
    within_days: int | None,
    today: date,
) -> tuple[list[date], str | None]:
    """Which expiries this request folds in, and a note when the cap bit.

    Aggregating is opt in. Summing several expiries into one gamma total is a different
    statement from a single expiry's -- a 30 day window is dominated by the monthly and
    says nothing about today's pin -- so the default is the one expiry the selector
    names.
    """
    if within_days is None:
        return [chosen], None

    inside = [item for item in listed if 0 <= (item - today).days <= within_days]
    if chosen not in inside:
        inside = sorted({chosen, *inside})
    if len(inside) <= MAX_OI_EXPIRIES:
        return inside, None

    kept = inside[:MAX_OI_EXPIRIES]
    return kept, (
        f"{len(inside)} expiries fall inside {within_days} days and the nearest "
        f"{MAX_OI_EXPIRIES} were used, through {kept[-1].isoformat()}. Each one is a "
        "vendor request, and the ones dropped are the furthest out."
    )


def _oi_rows(chain: OptionChain, now: datetime) -> list[OiRow]:
    """One chain's contracts as gamma inputs, on that expiry's own clock.

    `time_to_expiry` measures to 16:00 New York on the expiry date, so a 0DTE priced at
    10:00 gets the fraction of the day it actually has rather than a whole day or none.
    That distinction is the entire point of a 0DTE gamma panel.

    The vendor's own implied vol is used rather than the solver's. Not laziness: the
    solver prices from the mid of a two sided market, and the deep wings that carry a
    wall are often one sided after the close, which would drop exactly the strikes the
    panel exists to show. Gamma is also far less sensitive to the vol used than a price
    is.
    """
    years = time_to_expiry(chain.expiry, now)
    return [
        OiRow(
            strike=contract.strike,
            right=contract.right,
            open_interest=contract.open_interest or 0,
            iv=contract.vendor_iv,
            years=years,
        )
        for contract in chain.contracts
    ]


def _gex_spot(
    symbol: str,
    chains: list[OptionChain],
    settings: Settings,
) -> tuple[float | None, str, datetime | None, str | None]:
    """Spot from the delayed quote feed, falling back to the chain's own underlying.

    The quote feed is preferred because it is the price every other panel shows, and a
    gamma chart whose spot line sat a dollar off the header price would be read as a bug
    in the levels rather than in the spot.

    The fallback is what makes indices work at all: the quote feed carries no ^SPX, so
    without it the panel would fail on precisely the symbols it was asked for. Which one
    was used is reported rather than hidden, because they are different prices.
    """
    quotes, _ = live_quotes(settings, [symbol])
    quote = quotes.get(symbol)
    if quote is not None and quote.last:
        return quote.last, "delayed quote feed", quote.as_of, None

    underlying = next((chain.underlying_price for chain in chains if chain.underlying_price), None)
    if underlying:
        return (
            underlying,
            "chain underlying",
            None,
            f"The quote feed carries no price for {symbol}, so spot is the chain's own "
            "underlying price. That is at least the same vendor as the open interest, "
            "but it is not the delayed feed the rest of the app shows.",
        )
    return None, "none", None, None


def _open_interest_session(fetched_at: datetime, settings: Settings) -> date:
    """The session the open interest counts belong to: the last completed one.

    Not the fetch date. A chain pulled at 10:00 on Tuesday carries Monday's settled open
    interest, and labelling it Tuesday would claim the morning's trading is in it.
    """
    local = market_local_date(fetched_at, settings.market_timezone)
    return previous_trading_day(local, settings.market_calendar)
