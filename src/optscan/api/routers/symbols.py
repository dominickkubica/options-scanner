"""One underlying: summary, chain grid, and candles.

All three read the same cached solved snapshot, which is the reason the cache exists.
Opening the underlying detail view fires the summary, the chain, and the history at
once, and without the cache that would solve the same five thousand contracts twice.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from optscan.api.deps import (
    DAILY_INTERVAL,
    DEFAULT_HISTORY_DAYS,
    ProviderFactory,
    ProviderFactoryDep,
    ScreenConfigDep,
    SettingsDep,
    SolvedSymbol,
    price_history,
    solved_symbol,
)
from optscan.api.schemas import ChainOut, HistoryOut, SymbolSummaryOut
from optscan.api.views import chain_view, history_view, symbol_summary_view
from optscan.config import Settings
from optscan.screener.config import ScreenConfig
from optscan.screener.context import ExpiryAnalysis

router = APIRouter(prefix="/symbols", tags=["symbols"])

#: Bounds on the candle window. The lower bound is a chart, the upper is what a free
#: vendor will actually serve without complaint.
MIN_HISTORY_DAYS = 5
MAX_HISTORY_DAYS = 1825


def require_symbol(
    symbol: str,
    settings: Settings,
    config: ScreenConfig,
    provider_factory: ProviderFactory,
) -> SolvedSymbol:
    """Resolve a symbol to its solved snapshot, or 404 with something actionable.

    A symbol with no capture is not an error in the server, it is a fact about the
    data directory, and the message says what to do about it.
    """
    solved = solved_symbol(symbol, settings, config, provider_factory=provider_factory)
    if solved is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No stored snapshot for {symbol.strip().upper()}. "
                "Run `optscan snapshot` to capture one."
            ),
        )
    return solved


def _default_expiry(solved: SolvedSymbol, config: ScreenConfig) -> ExpiryAnalysis | None:
    """Which expiry to show when the caller did not name one.

    The nearest expiry at or beyond the screen's own minimum DTE, not the front one.
    The front of a liquid chain is usually a zero or one day expiry, which is the
    least useful grid to open on and the one whose vol is least representative.
    """
    if not solved.analysis.expiries:
        return None
    minimum = config.filters.dte.min_dte
    eligible = [item for item in solved.analysis.expiries if item.dte >= minimum]
    return eligible[0] if eligible else solved.analysis.expiries[0]


@router.get("/{symbol}", response_model=SymbolSummaryOut)
def symbol_summary(
    symbol: str,
    settings: SettingsDep,
    config: ScreenConfigDep,
    provider_factory: ProviderFactoryDep,
) -> SymbolSummaryOut:
    solved = require_symbol(symbol, settings, config, provider_factory)
    return symbol_summary_view(solved, solved.provenance())


@router.get("/{symbol}/chain", response_model=ChainOut)
def symbol_chain(
    symbol: str,
    settings: SettingsDep,
    config: ScreenConfigDep,
    provider_factory: ProviderFactoryDep,
    expiry: Annotated[
        date | None, Query(description="Omit for the first screenable expiry.")
    ] = None,
) -> ChainOut:
    solved = require_symbol(symbol, settings, config, provider_factory)

    analysis = solved.analysis.expiry(expiry) if expiry else _default_expiry(solved, config)
    if analysis is None:
        listed = ", ".join(item.expiry.isoformat() for item in solved.analysis.expiries)
        raise HTTPException(
            status_code=404,
            detail=(
                f"{solved.symbol} has no captured chain for {expiry}. "
                f"Captured expiries: {listed or 'none'}."
            ),
        )
    return chain_view(solved, analysis, solved.provenance())


@router.get("/{symbol}/history", response_model=HistoryOut)
def symbol_history(
    symbol: str,
    settings: SettingsDep,
    provider_factory: ProviderFactoryDep,
    *,
    days: Annotated[int, Query(ge=MIN_HISTORY_DAYS, le=MAX_HISTORY_DAYS)] = DEFAULT_HISTORY_DAYS,
    interval: Annotated[
        str,
        Query(
            description=(
                "1Day, or an intraday interval. Daily bars are read from storage and "
                "work for any synced symbol; intraday is fetched on demand and needs a "
                "vendor that serves it."
            )
        ),
    ] = DAILY_INTERVAL,
    session: Annotated[
        date | None,
        Query(
            description=(
                "One calendar day, for drilling into a single candle. Bounded to the "
                "date rather than to market hours, so pre and post market bars are "
                "included: on a quiet name those are often why somebody opened the day."
            )
        ),
    ] = None,
) -> HistoryOut:
    """Candles for the underlying.

    Degrades to an empty series with a stated reason rather than a 500. A price chart
    is the least load bearing panel on the page: losing it should not take the chain
    and the candidates down with it.
    """
    normalized = symbol.strip().upper()
    bars, note = price_history(
        normalized,
        days,
        interval=interval,
        session=session,
        settings=settings,
        provider_factory=provider_factory,
    )
    return history_view(normalized, bars, note, intraday=interval != DAILY_INTERVAL)
