"""Refresh now.

Every panel here refreshes on its own clock -- quotes every ten seconds, charts every
fifteen, the live chain every twenty. Those intervals are chosen to be kind to a shared
request budget, and they are exactly wrong in the one moment somebody wants an answer
about *now*: a price moves, and the screen takes a cycle to agree.

So this drops the short-lived caches and lets the next request go to the vendor. It
fetches nothing itself, which keeps it cheap and keeps the cost where it belongs -- on
the panels the browser actually has open, rather than refetching the whole watchlist for
a page showing one symbol.
"""

from __future__ import annotations

from fastapi import APIRouter

from optscan.api.deps import clear_live_caches
from optscan.api.schemas import ApiModel

router = APIRouter(tags=["refresh"])


class RefreshOut(ApiModel):
    cleared: bool = True
    detail: str


@router.post("/refresh", response_model=RefreshOut)
def refresh() -> RefreshOut:
    clear_live_caches()
    return RefreshOut(
        detail=(
            "Quote, chain and candle caches dropped. The next request for each panel "
            "goes to the vendor."
        )
    )
