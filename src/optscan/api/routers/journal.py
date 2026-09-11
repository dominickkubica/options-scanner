"""Journal reporting over the imported broker ledger.

Reading is read only: no GET here writes a transaction, so a browser refresh cannot
alter what was recorded. There is one write, `POST /journal/import`, and it is safe for
the same reason the CLI import is -- the ledger inserts on a content digest, so the same
statement uploaded twice adds nothing the second time and says so. Overlapping exports
are the normal case, since brokers hand out date ranges rather than deltas.

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

import csv
import io
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Query

from optscan.analytics.journal import build_report
from optscan.analytics.ledger import build_trades, journal_entries
from optscan.api.deps import SettingsDep
from optscan.api.schemas import ImportResultOut, JournalOut
from optscan.api.views import journal_view
from optscan.imports import RobinhoodParseError
from optscan.imports.robinhood import parse_rows
from optscan.storage import db, ledger

router = APIRouter(tags=["journal"])

#: Largest statement accepted, in bytes. A year of active option trading is a few
#: hundred kilobytes; this is generous and still small enough that a mistaken upload
#: cannot occupy the process.
MAX_STATEMENT_BYTES = 5 * 1024 * 1024


@router.post("/journal/import", response_model=ImportResultOut)
def import_statement(
    settings: SettingsDep,
    body: Annotated[str, Body(media_type="text/csv")],
) -> ImportResultOut:
    """Append a Robinhood statement to the ledger.

    Takes the CSV as a plain text body rather than a multipart upload, which keeps
    `python-multipart` out of the dependency list for what is, after all, a text file.
    The browser reads the file and posts its contents.

    Re-uploading is the expected case, not an edge case: the broker exports date ranges,
    so every download after the first overlaps the last. Rows are keyed by a digest of
    their own contents, so duplicates are counted and skipped rather than doubling a
    position -- and the count is returned, because "457 rows, 25 new" is the only way to
    tell a working import from one that silently did nothing.
    """
    if len(body.encode("utf-8")) > MAX_STATEMENT_BYTES:
        raise HTTPException(status_code=413, detail="That file is larger than 5 MB.")

    try:
        rows = list(csv.DictReader(io.StringIO(body)))
        txns = parse_rows(rows)
    except RobinhoodParseError as error:
        # The parser's own sentence, which names the line and the column. Replacing it
        # with "invalid file" would throw away the only thing that helps.
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (csv.Error, ValueError) as error:
        raise HTTPException(
            status_code=422,
            detail=f"That does not read as a Robinhood CSV export: {error}",
        ) from error

    if not txns:
        raise HTTPException(
            status_code=422,
            detail="No transaction rows found. Is this the account activity export?",
        )

    with db.session(settings.sqlite_path) as conn:
        report = ledger.import_transactions(conn, txns)
        conn.commit()

    return ImportResultOut(
        parsed=report.rows_parsed,
        inserted=report.rows_inserted,
        duplicate=report.rows_duplicate,
        first_date=report.first_activity,
        last_date=report.last_activity,
        detail=(
            f"{report.rows_parsed} rows read, {report.rows_inserted} new, "
            f"{report.rows_duplicate} already held."
        ),
    )


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
