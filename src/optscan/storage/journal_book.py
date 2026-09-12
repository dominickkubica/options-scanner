"""What the trader adds to the journal: tags, notes, screenshots, fill times, VIX, balance.

None of this comes from a broker statement, and none of it is written to the ledger.
The ledger stays exactly what Robinhood exported; this module is everything layered on
top, keyed so that a re-import cannot orphan it. See migration 11 in `storage/db.py`.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, date, datetime

from optscan.analytics.positions import Annotation

#: Where the account balance before the first imported statement is kept.
BALANCE_KEY = "journal_starting_balance"


def _now() -> str:
    return datetime.now(UTC).isoformat()


# -- annotations ------------------------------------------------------------------


def annotations(conn: sqlite3.Connection) -> dict[str, Annotation]:
    rows = conn.execute(
        "SELECT position_key, strategy, notes, tags, planned_exit, entry_time, exit_time "
        "FROM journal_annotation"
    ).fetchall()
    return {
        row["position_key"]: Annotation(
            strategy=row["strategy"],
            notes=row["notes"],
            tags=tuple(json.loads(row["tags"] or "[]")),
            planned_exit=row["planned_exit"],
            entry_time=row["entry_time"],
            exit_time=row["exit_time"],
        )
        for row in rows
    }


def save_annotation(conn: sqlite3.Connection, key: str, note: Annotation) -> None:
    """Replace a position's annotation whole. The editor always sends every field."""
    conn.execute(
        "INSERT INTO journal_annotation "
        "(position_key, strategy, notes, tags, planned_exit, entry_time, exit_time, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(position_key) DO UPDATE SET strategy = excluded.strategy, "
        "notes = excluded.notes, tags = excluded.tags, planned_exit = excluded.planned_exit, "
        "entry_time = excluded.entry_time, exit_time = excluded.exit_time, "
        "updated_at = excluded.updated_at",
        (
            key,
            note.strategy,
            note.notes,
            json.dumps(list(note.tags)),
            note.planned_exit,
            note.entry_time,
            note.exit_time,
            _now(),
        ),
    )


# -- screenshots ------------------------------------------------------------------


def screenshots(conn: sqlite3.Connection) -> dict[str, list[int]]:
    out: dict[str, list[int]] = defaultdict(list)
    for row in conn.execute("SELECT id, position_key FROM journal_screenshot ORDER BY id"):
        out[row["position_key"]].append(row["id"])
    return dict(out)


def add_screenshot(
    conn: sqlite3.Connection, *, key: str, file_name: str | None, content_type: str, path: str
) -> int:
    cursor = conn.execute(
        "INSERT INTO journal_screenshot (position_key, file_name, content_type, path, stored_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (key, file_name, content_type, path, _now()),
    )
    return int(cursor.lastrowid or 0)


def screenshot(conn: sqlite3.Connection, shot_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, position_key, file_name, content_type, path FROM journal_screenshot "
        "WHERE id = ?",
        (shot_id,),
    ).fetchone()


def delete_screenshot(conn: sqlite3.Connection, shot_id: int) -> str | None:
    """Remove the record and return the file path, so the caller can delete the file."""
    row = screenshot(conn, shot_id)
    if row is None:
        return None
    conn.execute("DELETE FROM journal_screenshot WHERE id = ?", (shot_id,))
    return row["path"]


# -- fill times -------------------------------------------------------------------


def fill_times(conn: sqlite3.Connection) -> dict[tuple[str, int], datetime]:
    return {
        (row["digest"], row["dup_index"]): datetime.fromisoformat(row["executed_at"])
        for row in conn.execute("SELECT digest, dup_index, executed_at FROM fill_time")
    }


def save_fill_times(
    conn: sqlite3.Connection,
    rows: Iterable[tuple[str, str, int, datetime, str]],
) -> int:
    """(source, digest, dup_index, executed_at, origin). Replaces what was there."""
    count = 0
    for source, digest, dup_index, executed_at, origin in rows:
        conn.execute(
            "INSERT OR REPLACE INTO fill_time "
            "(source, digest, dup_index, executed_at, origin, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (source, digest, dup_index, executed_at.isoformat(), origin, _now()),
        )
        count += 1
    return count


# -- regime -----------------------------------------------------------------------


def vix(conn: sqlite3.Connection) -> dict[date, float]:
    return {
        date.fromisoformat(row["session_date"]): row["vix_close"]
        for row in conn.execute("SELECT session_date, vix_close FROM market_regime")
    }


def save_vix(conn: sqlite3.Connection, rows: Iterable[tuple[date, float]]) -> int:
    count = 0
    for day, close in rows:
        conn.execute(
            "INSERT OR REPLACE INTO market_regime (session_date, vix_close, fetched_at) "
            "VALUES (?, ?, ?)",
            (day.isoformat(), close, _now()),
        )
        count += 1
    return count


# -- starting balance -------------------------------------------------------------


def starting_balance(conn: sqlite3.Connection) -> float | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (BALANCE_KEY,)).fetchone()
    return float(row["value"]) if row else None


def set_starting_balance(conn: sqlite3.Connection, value: float | None) -> None:
    if value is None:
        conn.execute("DELETE FROM meta WHERE key = ?", (BALANCE_KEY,))
        return
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (BALANCE_KEY, str(value)),
    )
