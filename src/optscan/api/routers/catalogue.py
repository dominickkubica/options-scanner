"""Symbol search, group browsing, and watchlist editing.

## Why the watchlist is editable over HTTP when nothing else is

Every other route in this API reads. This one writes, and it is the one exception
worth making: the watchlist decides what the daily capture job fetches, and requiring a
terminal to add a ticker means the dashboard can show a symbol, say it has ten years of
prices and no chains, and offer no way to fix that.

The writes are small, reversible and idempotent. Adding a symbol already on the list
reports `changed: false` rather than failing, because a double click is not an error.

## The thing every response here has to keep saying

**Adding a symbol does not capture a chain.** It makes the next snapshot run fetch one.
Until that run there is nothing to screen, and an empty scan result would look like a
broken feature rather than a wait. Every add returns a note saying so.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from optscan.api.deps import SettingsDep
from optscan.api.schemas import (
    CatalogueEntryOut,
    CatalogueOut,
    HomeOut,
    WatchlistChangeOut,
)
from optscan.catalogue import SymbolEntry, build_catalogue, search
from optscan.config import Settings
from optscan.logging import get_logger
from optscan.storage import db

router = APIRouter(tags=["catalogue"])

log = get_logger("optscan.api.catalogue")

#: How many symbols one search may return. The browser renders a list, not a table of
#: three hundred rows, and an unbounded limit would make the group tabs a wall.
MAX_RESULTS = 200


def _last_moves(settings: Settings, symbols: list[str]) -> dict[str, tuple[float, float | None]]:
    """Last stored close and the percent move into it, per symbol.

    From `vendor_daily` rather than a quote, for three reasons: it costs one query for
    the whole watchlist instead of a request per symbol, it works when the market is
    shut and when no provider is configured, and it cannot be mistaken for live because
    the row it came from is dated and the UI shows that date.
    """
    if not symbols:
        return {}
    placeholders = ",".join("?" * len(symbols))
    rows = db_moves(settings, placeholders, symbols)
    out: dict[str, tuple[float, float | None]] = {}
    for symbol, close, previous in rows:
        if close is None:
            continue
        change = None
        if previous:
            change = (close - previous) / previous
        out[symbol] = (close, change)
    return out


def db_moves(settings: Settings, placeholders: str, symbols: list[str]):
    """The two most recent closes per symbol, **from a single vendor**.

    ## The bug this shape exists to prevent

    The obvious query ranks every row for a symbol by `session_date DESC` and takes the
    top two. That is wrong the moment a symbol has more than one source: the two most
    recent rows are then the *same session* from two different vendors, and the
    "daily change" computed from them is the disagreement between the vendors rather
    than a market move.

    It was measured doing exactly that. AAPL and QQQ are the only symbols here holding
    both an Alpaca series and a Market Chameleon one, and they were the only two
    reporting +0.00% while every single-sourced symbol reported a real move. The two
    richest histories in the database were the two that showed nothing.

    So a source is chosen per symbol first, the one with the latest session, and both
    closes come from it. Ties break on the source name so the answer is stable between
    calls rather than depending on scan order.
    """
    sql = f"""
        WITH latest_source AS (
            SELECT symbol, source,
                   ROW_NUMBER() OVER (
                       PARTITION BY symbol
                       ORDER BY MAX(session_date) DESC, source
                   ) AS pick
            FROM vendor_daily
            WHERE symbol IN ({placeholders})
            GROUP BY symbol, source
        ),
        ranked AS (
            SELECT v.symbol, v.close,
                   ROW_NUMBER() OVER (
                       PARTITION BY v.symbol ORDER BY v.session_date DESC
                   ) AS rn
            FROM vendor_daily v
            JOIN latest_source s
              ON s.symbol = v.symbol AND s.source = v.source AND s.pick = 1
        )
        SELECT a.symbol, a.close, b.close
        FROM ranked a
        LEFT JOIN ranked b ON b.symbol = a.symbol AND b.rn = 2
        WHERE a.rn = 1
    """
    with db.session(settings.sqlite_path) as conn:
        return conn.execute(sql, symbols).fetchall()


def _entry_view(
    entry: SymbolEntry, moves: dict[str, tuple[float, float | None]]
) -> CatalogueEntryOut:
    close, change = moves.get(entry.symbol, (None, None))
    return CatalogueEntryOut(
        symbol=entry.symbol,
        groups=list(entry.groups),
        on_watchlist=entry.on_watchlist,
        has_prices=entry.has_prices,
        price_sessions=entry.price_sessions,
        price_first=entry.price_first,
        price_last=entry.price_last,
        price_sources=list(entry.price_sources),
        has_iv_history=entry.has_iv_history,
        last_capture=entry.last_capture,
        screenable=entry.screenable,
        status=entry.status(),
        last_close=close,
        change_pct=change,
    )


def _views(entries: list[SymbolEntry], settings: Settings) -> list[CatalogueEntryOut]:
    moves = _last_moves(settings, [e.symbol for e in entries])
    return [_entry_view(entry, moves) for entry in entries]


@router.get("/catalogue", response_model=CatalogueOut)
def catalogue(
    settings: SettingsDep,
    *,
    q: str = Query("", description="Ticker substring. Exact matches rank first."),
    group: str | None = Query(None, description="Restrict to one universe group."),
    only_watchlist: bool = Query(False),
    only_screenable: bool = Query(
        False,
        description=(
            "Only symbols with a captured option chain. Price history alone is not "
            "enough to screen a symbol and this is the filter that says so."
        ),
    ),
    limit: int = Query(50, ge=1, le=MAX_RESULTS),
) -> CatalogueOut:
    """Search every symbol this installation knows anything about."""
    built = build_catalogue(settings)
    if group and group not in built.groups:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No group named {group!r}. Groups come from universe.yaml and are "
                f"currently: {', '.join(sorted(built.groups))}."
            ),
        )

    matches = search(
        built,
        q,
        group=group,
        only_watchlist=only_watchlist,
        only_screenable=only_screenable,
        limit=limit,
    )
    return CatalogueOut(
        entries=_views(matches, settings),
        groups=built.groups,
        total_symbols=len(built.entries),
        matched=len(matches),
        universe_checked=built.universe_checked,
        universe_note=built.universe_note,
    )


@router.get("/home", response_model=HomeOut)
def home(settings: SettingsDep) -> HomeOut:
    """The landing page: what is pinned, what is held, and what needs attention."""
    built = build_catalogue(settings)
    pinned = list(built.watchlist)

    notes: list[str] = []
    waiting = [e.symbol for e in pinned if not e.screenable]
    if waiting:
        notes.append(
            f"{', '.join(waiting)} {'is' if len(waiting) == 1 else 'are'} on the "
            "watchlist with no captured chain yet. The next `optscan snapshot` run "
            "fetches one; until then there is nothing to screen."
        )

    without_prices = [e.symbol for e in pinned if not e.has_prices]
    if without_prices:
        notes.append(
            f"No stored daily bars for {', '.join(without_prices)}. "
            "Run `optscan prices sync --symbol <TICKER>` to chart or rank them."
        )

    return HomeOut(
        watchlist=_views(pinned, settings),
        groups=built.groups,
        total_symbols=len(built.entries),
        with_prices=sum(1 for e in built.entries if e.has_prices),
        screenable=len(built.screenable),
        universe_note=built.universe_note,
        notes=notes,
    )


@router.post("/watchlist/{symbol}", response_model=WatchlistChangeOut)
def add_to_watchlist(symbol: str, settings: SettingsDep) -> WatchlistChangeOut:
    """Pin a symbol so the capture job starts fetching its chains.

    Idempotent: adding one already there reports `changed: false`.
    """
    ticker = symbol.strip().upper()
    if not ticker or not ticker.replace(".", "").replace("-", "").isalnum():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{symbol!r} is not a ticker.",
        )

    with db.session(settings.sqlite_path) as conn:
        added = db.add_symbol(conn, ticker, note="added from the dashboard")

    entry = build_catalogue(settings).by_symbol(ticker)
    log.info("watchlist add", symbol=ticker, changed=bool(added))

    if not added:
        note = f"{ticker} was already on the watchlist."
    elif entry and entry.screenable:
        note = f"{ticker} already has captured chains and can be screened now."
    else:
        note = (
            f"{ticker} will be captured by the next `optscan snapshot` run. It cannot "
            "be screened until then, because no chain has ever been stored for it."
        )
        if entry and not entry.has_prices:
            note += " It also has no stored daily bars; `optscan prices sync` adds those."

    return WatchlistChangeOut(symbol=ticker, on_watchlist=True, changed=bool(added), note=note)


@router.delete("/watchlist/{symbol}", response_model=WatchlistChangeOut)
def remove_from_watchlist(symbol: str, settings: SettingsDep) -> WatchlistChangeOut:
    """Unpin a symbol. Captured chains and price history are left alone.

    Removing stops future captures and does not delete anything already stored. That
    is deliberate: a snapshot cannot be recreated for a day that has passed, so a
    removal that deleted history would be irreversible in a way nothing warns about.
    """
    ticker = symbol.strip().upper()
    with db.session(settings.sqlite_path) as conn:
        removed = db.remove_symbol(conn, ticker)

    log.info("watchlist remove", symbol=ticker, changed=bool(removed))
    note = (
        f"{ticker} will no longer be captured. Everything already stored for it is "
        "kept: a chain from a day that has passed cannot be captured again."
        if removed
        else f"{ticker} was not on the watchlist."
    )
    return WatchlistChangeOut(symbol=ticker, on_watchlist=False, changed=bool(removed), note=note)
