"""Journal reporting over settled candidates.

Read only, and reporting only. Nothing here settles anything: `optscan resolve` writes
outcomes and this reads them back, so a browser refresh cannot re-settle a position or
change what was recorded. That separation is the same one the positions router keeps
for alerts, and for the same reason.

The response is deliberately not called a track record anywhere in it. Every aggregate
carries its cluster count, and `reportable` is false until the sample clears the
minimum in `calibration.py`. See `analytics/journal.py` for why the row count is not
the sample size.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from optscan.analytics.journal import build_report
from optscan.api.deps import SettingsDep
from optscan.api.schemas import JournalOut
from optscan.api.views import journal_view
from optscan.storage import db, validation

router = APIRouter(tags=["journal"])


@router.get("/journal", response_model=JournalOut)
def journal(
    settings: SettingsDep,
    *,
    symbol: Annotated[str | None, Query(description="Restrict to one symbol.")] = None,
    strategy: Annotated[str | None, Query(description="Restrict to one strategy.")] = None,
) -> JournalOut:
    """Settled candidates, aggregated the way a trade journal is read.

    Filtering narrows the sample, which narrows the cluster count with it. That is why
    every group in the response carries its own count rather than inheriting the
    report's: a symbol filter can take a fourteen cluster sample down to two without
    changing anything else on the screen.
    """
    with db.session(settings.sqlite_path) as conn:
        items = validation.resolved(conn, symbol=symbol, strategy=strategy)
    return journal_view(build_report(items))
