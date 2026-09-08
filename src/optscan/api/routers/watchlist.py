"""The watchlist, and when each symbol was last captured.

The capture date is on this payload rather than fetched per symbol because it is what
the sidebar needs to grey out a symbol that has no data yet. A fresh install has a
seeded watchlist and no snapshots, and a list of six symbols that all 404 when clicked
is a worse first run than a list that says so up front.
"""

from __future__ import annotations

from fastapi import APIRouter

from optscan.api.deps import SettingsDep
from optscan.api.schemas import WatchlistOut
from optscan.catalogue import last_capture
from optscan.storage import db

router = APIRouter(tags=["watchlist"])


@router.get("/watchlist", response_model=WatchlistOut)
def watchlist(settings: SettingsDep) -> WatchlistOut:
    with db.session(settings.sqlite_path) as conn:
        db.seed_watchlist(conn, settings.default_watchlist)
        symbols = db.list_watchlist(conn)

    return WatchlistOut(
        symbols=symbols,
        captured={symbol: last_capture(settings, symbol) for symbol in symbols},
    )
