"""Journal reporting over the imported broker ledger, and what the trader adds to it.

Every GET here is read only, so a browser refresh cannot alter anything. The writes are
all explicit and all small:

  - `POST /journal/import` appends a statement. Safe to repeat: the ledger inserts on a
    content digest, so an overlapping export adds only what is new and says so.
  - `PUT /journal/annotation` saves a position's tags, notes, planned exit and typed
    times. These never touch the ledger; see migration 11.
  - `POST /journal/screenshot` stores an image beside a position.
  - `PUT /journal/balance` sets the account balance before the first statement.
  - `POST /journal/regime` fetches VIX history for the regime breakdown.

## Two grains, on purpose

The headline tiles, the curve and the calendar keep the `(symbol, closing day)` grain of
`analytics/ledger.py`, which is audited and is also the unit of independence. The book
(`analytics/positions.py`) is one row per position as it was placed, for the trade
table, the strategy tags and everything reported per trade. Filters apply to positions
first and the headline is rebuilt from the chosen positions' fills, so both agree.

## Why this is not the validation study

It used to report `opportunity_outcome`: candidates the screen surfaced and `optscan
resolve` settled at expiry, with no fill, no slippage and no early management. That is
a measurement of the screen, not an account, and summed onto one curve it read as a
+$426,644 track record that was never traded. `optscan validate` is its report.
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response

from optscan.analytics.journal import build_report
from optscan.analytics.ledger import build_trades, journal_entries
from optscan.analytics.positions import (
    DTE_CLASSES,
    MISTAKE_TAGS,
    STOP_MULTIPLE,
    Annotation,
    Position,
    attach,
    build_book,
    build_positions,
    cap_to_median_risk,
    cash_flows,
    entries_from_positions,
    filter_positions,
    to_trader,
    trend_labels,
)
from optscan.api.deps import SettingsDep
from optscan.api.schemas import (
    AnnotationIn,
    BalanceIn,
    ImportResultOut,
    JournalOut,
    RegimeOut,
    ScreenshotOut,
    ViewOut,
)
from optscan.api.views import book_view, journal_view
from optscan.config import Settings
from optscan.imports import RobinhoodParseError
from optscan.imports.robinhood import parse_rows
from optscan.models.broker import BrokerTxn
from optscan.storage import db, ledger, vol_index
from optscan.storage import journal_book as book_store
from optscan.storage.vendor import daily_bars, preferred_source

router = APIRouter(tags=["journal"])

#: Largest statement accepted, in bytes. A year of active option trading is a few
#: hundred kilobytes; this is generous and still small enough that a mistaken upload
#: cannot occupy the process.
MAX_STATEMENT_BYTES = 5 * 1024 * 1024

#: Largest screenshot accepted. A full-resolution phone screenshot is 2 to 4 MB.
MAX_SCREENSHOT_BYTES = 8 * 1024 * 1024

IMAGE_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

#: "HH:MM", 24 hour, on the trader's clock (Pacific). See positions.TRADER_TZ.
CLOCK = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")

MAX_NOTE_CHARS = 10_000
MAX_TAGS = 20
MAX_LABEL_CHARS = 60

#: Extra calendar days of VIX fetched before the first trade, so the first day has one.
REGIME_MARGIN_DAYS = 45

#: The index the trend label is read from. Stored locally, so it needs no fetch.
TREND_SYMBOL = "SPY"


# --------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------


def _closes(conn, symbol: str) -> list[tuple[date, float]]:
    source = preferred_source(conn, symbol)
    if source is None:
        return []
    return [(row.session_date, row.close) for row in daily_bars(conn, symbol, source=source)]


def _load(settings: Settings) -> tuple[list[BrokerTxn], list[Position], float | None]:
    """Every position in the account, with everything the trader added hung on it."""
    with db.session(settings.sqlite_path) as conn:
        txns = ledger.all_transactions(conn)
        notes = book_store.annotations(conn)
        shots = book_store.screenshots(conn)
        times = book_store.fill_times(conn)
        # VIX is already stored daily in vol_index for SPY's IV rank, so the journal
        # reads it from there and the regime panel works without a separate fetch. The
        # journal's own fetched table, the same Yahoo closes, only fills any gap.
        vix = {**dict(vol_index.series(conn, "^VIX")), **book_store.vix(conn)}
        balance = book_store.starting_balance(conn)
        spy = _closes(conn, TREND_SYMBOL)

    everything = build_positions(build_trades(txns))
    attach(
        everything,
        annotations=notes,
        screenshots=shots,
        fill_times=times,
        vix=vix,
        trend=trend_labels(spy),
    )
    return txns, everything, balance


def _filters(everything: list[Position]) -> dict[str, list[str]]:
    classes = [label for label, _low, _high in DTE_CLASSES] + ["stock"]
    return {
        "symbols": sorted({p.symbol for p in everything}),
        "strategies": sorted({p.strategy for p in everything}),
        "tags": sorted({tag for p in everything for tag in p.annotation.tags}),
        "dte": [label for label in classes if any(p.dte_class == label for p in everything)],
    }


def _require_position(settings: Settings, key: str) -> Position:
    _, everything, _ = _load(settings)
    for position in everything:
        if position.key == key:
            return position
    raise HTTPException(status_code=404, detail=f"No position {key!r} in the ledger.")


# --------------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------------


@router.get("/journal", response_model=JournalOut)
def journal(
    settings: SettingsDep,
    *,
    symbol: Annotated[str | None, Query(description="Restrict to one underlying.")] = None,
    strategy: Annotated[str | None, Query(description="Restrict to one strategy tag.")] = None,
    tag: Annotated[str | None, Query(description="Restrict to one mistake tag.")] = None,
    dte: Annotated[str | None, Query(description="0DTE, 1-7 DTE, 8+ DTE or stock.")] = None,
    cap: Annotated[
        bool, Query(description="Scale trades above the account's median risk down to it.")
    ] = False,
) -> JournalOut:
    """Closed trades from imported statements: the headline, and one row per position.

    Filtering narrows the sample, which narrows the cluster count with it. That is why
    every group in the response carries its own count rather than inheriting the
    report's: a symbol filter can take a thirty nine day sample down to one.
    """
    txns, everything, balance = _load(settings)
    chosen = filter_positions(everything, symbol=symbol, strategy=strategy, tag=tag, dte_class=dte)
    actual_total = round(sum(p.realized or 0.0 for p in chosen if not p.is_open), 2)

    # The capped view scales every trade that risked more than the account's median 1R
    # down to it and leaves the rest alone. It only changes what this response counts:
    # nothing is written, and the trade table still shows real results.
    ceiling, capped = (None, 0)
    if cap:
        ceiling, capped = cap_to_median_risk(chosen, everything)
        report = build_report(entries_from_positions(chosen))
    else:
        # Real dollars keep the audited path, built from the fills themselves.
        report = build_report(journal_entries([t for p in chosen for t in p.trades]))
    view = ViewOut(
        capped=cap,
        cap_risk=ceiling,
        capped_trades=capped,
        actual_total=actual_total,
    )

    book = build_book(
        chosen,
        everything,
        cash_flows=cash_flows(txns),
        starting_balance=balance,
        day_profits=[point.profit for point in report.days],
    )
    return journal_view(
        report,
        book_view(
            book, filters=_filters(everything), stop_multiple=STOP_MULTIPLE, tags=MISTAKE_TAGS
        ),
        view,
    )


@router.get("/journal/export.csv")
def export_csv(
    settings: SettingsDep,
    kind: Annotated[str, Query(pattern="^(positions|fills)$")] = "positions",
) -> Response:
    """One row per position, or every fill as the broker wrote it, for taxes or a sheet."""
    _, everything, _ = _load(settings)
    buffer = io.StringIO()
    writer = csv.writer(buffer)

    if kind == "positions":
        writer.writerow(
            [
                "key", "symbol", "strategy", "strategy_guess", "opened", "closed", "expiry",
                "dte_at_entry", "units", "entry_price", "exit_price", "opening_cash",
                "closing_cash", "fees", "realized", "max_loss", "r_multiple", "entry_time",
                "exit_time", "tags", "flags", "notes", "legs",
            ]
        )  # fmt: skip
        for p in everything:
            legs = "; ".join(
                f"{leg.side} {leg.right or 'shares'} {leg.strike or ''} x{leg.size:g}"
                f" @{leg.open_price if leg.open_price is not None else ''}"
                f"->{leg.close_price if leg.close_price is not None else ''}"
                for leg in p.legs
            )
            writer.writerow(
                [
                    p.key, p.symbol, p.strategy, p.guess, p.opened_at, p.closed_at or "",
                    p.contract_expiry or "", p.dte_at_entry if p.dte_at_entry is not None else "",
                    p.units, _num(p.entry_price), _num(p.exit_price), p.opening_cash,
                    p.closing_cash, p.fees, _num(p.realized), _num(p.max_loss),
                    _num(p.r_multiple), _clock(p.entry_at), _clock(p.exit_at),
                    "|".join(p.annotation.tags), "|".join(p.flags), p.annotation.notes or "", legs,
                ]
            )  # fmt: skip
    else:
        with db.session(settings.sqlite_path) as conn:
            times = book_store.fill_times(conn)
        owner = {
            (t.digest, t.dup_index): p.key
            for p in everything
            for leg in p.legs
            for t in (*leg.opening, *leg.closing)
        }
        writer.writerow(
            [
                "activity_date", "process_date", "settle_date", "symbol", "description",
                "trans_code", "quantity", "price", "amount", "fee", "expiry", "right",
                "strike", "position", "executed_at",
            ]
        )  # fmt: skip
        with db.session(settings.sqlite_path) as conn:
            txns = ledger.all_transactions(conn)
        for t in txns:
            executed = times.get((t.digest, t.dup_index))
            writer.writerow(
                [
                    t.activity_date, t.process_date or "", t.settle_date or "", t.symbol or "",
                    t.description, t.trans_code, _num(t.quantity), _num(t.price), _num(t.amount),
                    _num(t.fee), t.expiry or "", t.right.value if t.right else "",
                    _num(t.strike), owner.get((t.digest, t.dup_index), ""),
                    executed.isoformat() if executed else "",
                ]
            )  # fmt: skip

    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="epicfin-journal-{kind}.csv"'},
    )


def _num(value: float | None) -> str | float:
    return "" if value is None else round(value, 4)


def _clock(moment: datetime | None) -> str:
    return "" if moment is None else f"{to_trader(moment):%H:%M}"


# --------------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------------


@router.post("/journal/import", response_model=ImportResultOut)
def import_statement(
    settings: SettingsDep,
    body: Annotated[str, Body(media_type="text/csv")],
) -> ImportResultOut:
    """Append a Robinhood statement to the ledger.

    Takes the CSV as a plain text body rather than a multipart upload, which keeps
    `python-multipart` out of the dependency list for what is, after all, a text file.

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


def _clean_clock(value: str | None, field: str) -> str | None:
    if value is None or not value.strip():
        return None
    text = value.strip()
    if not CLOCK.match(text):
        raise HTTPException(status_code=422, detail=f"{field} must be HH:MM, 24 hour, Pacific.")
    hours, minutes = text.split(":")
    return f"{int(hours):02d}:{minutes}"


@router.put("/journal/annotation", response_model=AnnotationIn)
def save_annotation(settings: SettingsDep, payload: AnnotationIn) -> AnnotationIn:
    """Replace one position's tags, notes, planned exit and typed times."""
    _require_position(settings, payload.key)

    notes = (payload.notes or "").strip() or None
    if notes and len(notes) > MAX_NOTE_CHARS:
        raise HTTPException(
            status_code=422, detail=f"Notes are limited to {MAX_NOTE_CHARS} characters."
        )
    tags = []
    for raw in payload.tags:
        tag = raw.strip().lower()[:MAX_LABEL_CHARS]
        if tag and tag not in tags:
            tags.append(tag)
    if len(tags) > MAX_TAGS:
        raise HTTPException(status_code=422, detail=f"At most {MAX_TAGS} tags.")
    strategy = (payload.strategy or "").strip()[:MAX_LABEL_CHARS] or None
    if payload.planned_exit is not None and payload.planned_exit < 0:
        raise HTTPException(status_code=422, detail="A planned exit is a price, so not negative.")

    cleaned = AnnotationIn(
        key=payload.key,
        strategy=strategy,
        notes=notes,
        tags=tags,
        planned_exit=payload.planned_exit,
        entry_time=_clean_clock(payload.entry_time, "Entry time"),
        exit_time=_clean_clock(payload.exit_time, "Exit time"),
    )
    with db.session(settings.sqlite_path) as conn:
        book_store.save_annotation(
            conn,
            cleaned.key,
            Annotation(
                strategy=cleaned.strategy,
                notes=cleaned.notes,
                tags=tuple(cleaned.tags),
                planned_exit=cleaned.planned_exit,
                entry_time=cleaned.entry_time,
                exit_time=cleaned.exit_time,
            ),
        )
    return cleaned


def _screenshot_dir(settings: Settings) -> Path:
    folder = settings.data_path / "journal" / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@router.post("/journal/screenshot", response_model=ScreenshotOut)
async def upload_screenshot(
    settings: SettingsDep,
    request: Request,
    key: Annotated[str, Query()],
    name: Annotated[str | None, Query(max_length=200)] = None,
) -> ScreenshotOut:
    """Store an image beside a position. The body is the raw image, typed by its header.

    Kept on this machine under the data directory, never sent anywhere.
    """
    kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if kind not in IMAGE_TYPES:
        raise HTTPException(status_code=415, detail="Screenshots must be PNG, JPEG, WebP or GIF.")
    body = await request.body()
    if not body:
        raise HTTPException(status_code=422, detail="The image was empty.")
    if len(body) > MAX_SCREENSHOT_BYTES:
        raise HTTPException(status_code=413, detail="Screenshots are limited to 8 MB.")
    _require_position(settings, key)

    path = _screenshot_dir(settings) / f"{uuid.uuid4().hex}{IMAGE_TYPES[kind]}"
    path.write_bytes(body)
    with db.session(settings.sqlite_path) as conn:
        shot_id = book_store.add_screenshot(
            conn, key=key, file_name=name, content_type=kind, path=str(path)
        )
    return ScreenshotOut(id=shot_id, key=key)


def _stored(settings: Settings, shot_id: int) -> tuple[Path, str]:
    with db.session(settings.sqlite_path) as conn:
        row = book_store.screenshot(conn, shot_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such screenshot.")
    path = Path(row["path"])
    folder = _screenshot_dir(settings).resolve()
    # Only ever serve from the screenshot folder, whatever the row says.
    if folder not in path.resolve().parents:
        raise HTTPException(status_code=404, detail="No such screenshot.")
    return path, row["content_type"]


@router.get("/journal/screenshot/{shot_id}")
def get_screenshot(settings: SettingsDep, shot_id: int) -> FileResponse:
    path, kind = _stored(settings, shot_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="The screenshot file is missing.")
    return FileResponse(path, media_type=kind)


@router.delete("/journal/screenshot/{shot_id}", response_model=ScreenshotOut)
def delete_screenshot(settings: SettingsDep, shot_id: int) -> ScreenshotOut:
    path, _ = _stored(settings, shot_id)
    with db.session(settings.sqlite_path) as conn:
        row = book_store.screenshot(conn, shot_id)
        book_store.delete_screenshot(conn, shot_id)
    path.unlink(missing_ok=True)
    return ScreenshotOut(id=shot_id, key=row["position_key"] if row else "")


@router.put("/journal/balance", response_model=BalanceIn)
def set_balance(settings: SettingsDep, payload: BalanceIn) -> BalanceIn:
    """The account balance before the first imported statement, for sizing."""
    if payload.starting_balance is not None and payload.starting_balance < 0:
        raise HTTPException(status_code=422, detail="A balance cannot be negative.")
    with db.session(settings.sqlite_path) as conn:
        book_store.set_starting_balance(conn, payload.starting_balance)
    return payload


def _vix_history(days: int) -> list[tuple[date, float]]:
    """Daily VIX closes. A seam: tests replace it, because it reaches Yahoo."""
    from optscan.providers.yfinance_provider import YFinanceProvider  # noqa: PLC0415

    return [(bar.ts.date(), bar.close) for bar in YFinanceProvider().get_history("^VIX", days)]


@router.post("/journal/regime", response_model=RegimeOut)
def fetch_regime(settings: SettingsDep) -> RegimeOut:
    """Fetch VIX closes covering every trade in the ledger."""
    with db.session(settings.sqlite_path) as conn:
        first = conn.execute("SELECT MIN(activity_date) FROM broker_txn").fetchone()[0]
    if first is None:
        raise HTTPException(status_code=422, detail="Import a statement first.")
    span = (datetime.now(UTC).date() - date.fromisoformat(first)).days + REGIME_MARGIN_DAYS

    try:
        rows = _vix_history(span)
    except Exception as error:  # any vendor failure is reported to the page, not raised
        raise HTTPException(status_code=502, detail=f"Could not fetch VIX: {error}") from error

    with db.session(settings.sqlite_path) as conn:
        stored = book_store.save_vix(conn, rows)
    days = sorted(day for day, _ in rows)
    return RegimeOut(
        stored=stored,
        first=days[0] if days else None,
        last=days[-1] if days else None,
        detail=f"{stored} VIX closes stored.",
    )
