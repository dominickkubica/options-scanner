"""The watchlist, and when each symbol was last captured.

The capture date is on this payload rather than fetched per symbol because it is what
the sidebar needs to grey out a symbol that has no data yet. A fresh install has a
seeded watchlist and no snapshots, and a list of six symbols that all 404 when clicked
is a worse first run than a list that says so up front.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter

from optscan.api.deps import SettingsDep
from optscan.api.schemas import WatchlistOut
from optscan.config import Settings
from optscan.storage import db, snapshot_files

router = APIRouter(tags=["watchlist"])


def _last_captured(settings: Settings, symbol: str) -> date | None:
    """Latest stored session for a symbol, read from the partition path.

    From the directory name rather than by opening the parquet: the layout is
    `symbol=SPY/session_date=2026-07-30/...`, so the answer is already in the path and
    reading six chains to render a sidebar would be absurd.
    """
    latest: date | None = None
    for path in snapshot_files(settings.snapshot_path, symbol):
        part = path.parent.name
        if not part.startswith("session_date="):
            continue
        try:
            captured = date.fromisoformat(part.removeprefix("session_date="))
        except ValueError:
            continue
        if latest is None or captured > latest:
            latest = captured
    return latest


@router.get("/watchlist", response_model=WatchlistOut)
def watchlist(settings: SettingsDep) -> WatchlistOut:
    with db.session(settings.sqlite_path) as conn:
        db.seed_watchlist(conn, settings.default_watchlist)
        symbols = db.list_watchlist(conn)

    return WatchlistOut(
        symbols=symbols,
        captured={symbol: _last_captured(settings, symbol) for symbol in symbols},
    )
