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

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from optscan.analytics.levels import build_levels
from optscan.api.deps import (
    ProviderFactoryDep,
    ScreenConfigDep,
    SettingsDep,
    price_history,
    solved_symbol,
)
from optscan.api.schemas import LevelsOut
from optscan.api.views import levels_view
from optscan.logging import get_logger

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
