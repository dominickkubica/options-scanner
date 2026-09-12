"""Stored closes of the Cboe volatility indices. See migration 12 in `storage/db.py`.

Kept out of `vendor_daily` on purpose: that table's rows are read as prices of the symbol
they name, and a volatility index is not a price of anything tradeable here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, date, datetime


def save_closes(conn: sqlite3.Connection, index: str, rows: Iterable[tuple[date, float]]) -> int:
    """Store (session, close in vol points). Re-fetching a day replaces it."""
    now = datetime.now(UTC).isoformat()
    count = 0
    for day, close in rows:
        if close is None or close <= 0:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO vol_index (index_symbol, session_date, close, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            (index, day.isoformat(), float(close), now),
        )
        count += 1
    return count


def series(
    conn: sqlite3.Connection, index: str, *, until: date | None = None
) -> list[tuple[date, float]]:
    """Closes in vol points, oldest first, never past `until`."""
    query = "SELECT session_date, close FROM vol_index WHERE index_symbol = ?"
    params: list[object] = [index]
    if until is not None:
        query += " AND session_date <= ?"
        params.append(until.isoformat())
    query += " ORDER BY session_date"
    return [(date.fromisoformat(day), close) for day, close in conn.execute(query, params)]


def latest(conn: sqlite3.Connection) -> dict[str, date]:
    """The newest stored session per index."""
    return {
        index: date.fromisoformat(day)
        for index, day in conn.execute(
            "SELECT index_symbol, MAX(session_date) FROM vol_index GROUP BY index_symbol"
        )
    }
