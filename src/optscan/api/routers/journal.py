"""Journal reporting over the imported broker ledger.

Read only. Nothing here writes a transaction: `optscan import` appends the ledger and
this reads it back, so a browser refresh cannot alter what was recorded.

## Why this is not the validation study

It used to report `opportunity_outcome`, which is a very different thing: candidates the
screen surfaced and `optscan resolve` settled by holding them to expiry, with no fill,
no slippage and no early management. That is a measurement of the screen, not a record
of an account, and it produced a total of +$426,644 across 2,060 candidates that were
never held at the same time and never traded at all. Summed onto one equity curve it
read as a track record, which is the most misleading thing this app could draw.

Those rows still exist and still matter. They are the calibration sample for the score
and `optscan validate` is their report. They are simply not a journal, so they are not
here any more.

The grain is `(symbol, closing day)`, set in `analytics/ledger.py`. That is both how a
trader reads a day and the unit of independence, so trade count and cluster count are
equal here rather than needing every interval widened from one to the other.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from optscan.analytics.journal import build_report
from optscan.analytics.ledger import build_trades, journal_entries
from optscan.api.deps import SettingsDep
from optscan.api.schemas import JournalOut
from optscan.api.views import journal_view
from optscan.storage import db, ledger

router = APIRouter(tags=["journal"])


@router.get("/journal", response_model=JournalOut)
def journal(
    settings: SettingsDep,
    *,
    symbol: Annotated[str | None, Query(description="Restrict to one underlying.")] = None,
    strategy: Annotated[
        str | None, Query(description="Restrict to options, equities or mixed.")
    ] = None,
) -> JournalOut:
    """Closed round trips from imported broker statements, aggregated as a journal.

    Filtering narrows the sample, which narrows the cluster count with it. That is why
    every group in the response carries its own count rather than inheriting the
    report's: a symbol filter can take a thirty nine day sample down to one.
    """
    with db.session(settings.sqlite_path) as conn:
        txns = ledger.all_transactions(conn)

    entries = journal_entries(build_trades(txns))
    if symbol:
        wanted = symbol.strip().upper()
        entries = [entry for entry in entries if entry.symbol == wanted]
    if strategy:
        entries = [entry for entry in entries if entry.strategy == strategy.strip().lower()]

    return journal_view(build_report(entries))
