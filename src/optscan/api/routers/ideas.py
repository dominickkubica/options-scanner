"""Trade ideas over HTTP: what is triggering, and the evidence for why it is listed.

One route rather than two. The evidence could be a separate endpoint the UI fetches when
somebody asks, and that is exactly the design that produces a screen of tickers with no
numbers on it whenever the second request is slow, fails, or is never wired up. The
strategies and their records come back in the same payload as the symbols.

Retired strategies are included. A rule that failed its holdout is the most instructive
thing in the registry, and hiding it would leave the panel looking like a list of things
that work.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from optscan.api.deps import ProviderFactoryDep, SettingsDep
from optscan.api.schemas import IdeasOut
from optscan.jobs.ideas import DEFAULT_FRESHNESS_DAYS, as_report, find_ideas, spread_plans

router = APIRouter(tags=["ideas"])

#: Sessions a trigger may be old and still be listed. Bounded because a page that let a
#: caller ask for a year of triggers would return a backlog of trades already gone.
MAX_FRESHNESS_DAYS = 30


@router.get("/ideas", response_model=IdeasOut)
def trade_ideas(
    settings: SettingsDep,
    provider_factory: ProviderFactoryDep,
    freshness: int = Query(DEFAULT_FRESHNESS_DAYS, ge=0, le=MAX_FRESHNESS_DAYS),
    provisional: bool = Query(False),
    plans: bool = Query(True),
) -> IdeasOut:
    """Symbols triggering a validated strategy, with that strategy's record attached.

    Oversold bounce triggers also carry a call debit spread plan, from chains fetched
    through the cached loaders. `plans=false` skips the fetch for a caller that only
    wants the list.
    """
    ideas, notes = find_ideas(settings, freshness=freshness, include_provisional=provisional)
    attached = spread_plans(settings, ideas, provider_factory=provider_factory) if plans else None
    return IdeasOut(**as_report(ideas, notes, plans=attached))
