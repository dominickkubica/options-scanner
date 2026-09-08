"""Persisting which market signals have fired, so each one notifies once.

The rule and the reason are `alerts.py`'s: an alerting tool that repeats itself gets
muted, and a muted alerting tool is worse than none because it is still believed to be
working. What differs here is the key.

A position trigger fires once for the life of that position, so `alert_sent` keys on
(position_id, kind). A market signal has no position and "once ever" would be wrong for
it: a level break in March and another in July are two events, not a repeat. The key is
(symbol, kind, session), so the same condition on a later day is a new alert and the
same condition rechecked four times that afternoon is not.

Uniqueness lives in the index rather than in a read then a write, so two runs racing
cannot both decide they are the first.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, date, datetime

from optscan.analytics.signals import Signal


def already_signalled(conn: sqlite3.Connection, symbol: str, kind: str, session: date) -> bool:
    row = conn.execute(
        "SELECT 1 FROM signal_sent WHERE symbol = ? AND kind = ? AND session_date = ? LIMIT 1",
        (symbol, kind, session.isoformat()),
    ).fetchone()
    return row is not None


def record_signal(conn: sqlite3.Connection, signal: Signal, *, now: datetime | None = None) -> bool:
    """Note that a signal fired. Returns False if it had already fired that session."""
    try:
        conn.execute(
            "INSERT INTO signal_sent "
            "(symbol, kind, session_date, fired_at, severity, price, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                signal.symbol,
                signal.kind.value,
                signal.session.isoformat(),
                (now or datetime.now(UTC)).isoformat(),
                signal.severity,
                signal.price,
                signal.message,
            ),
        )
    except sqlite3.IntegrityError:
        return False
    conn.commit()
    return True


def recent_signals(
    conn: sqlite3.Connection,
    *,
    since: date | None = None,
    symbol: str | None = None,
    min_severity: int = 0,
    limit: int = 200,
) -> list[dict[str, object]]:
    """What fired lately, newest and most severe first.

    Returned as plain dicts rather than `Signal` objects: these are the record of what
    was delivered, not a fresh evaluation, and rebuilding a `Signal` from them would
    invite treating a stored row as a current condition.
    """
    sql = "SELECT * FROM signal_sent WHERE severity >= ?"
    params: list = [min_severity]
    if since is not None:
        sql += " AND session_date >= ?"
        params.append(since.isoformat())
    if symbol is not None:
        sql += " AND symbol = ?"
        params.append(symbol)
    sql += " ORDER BY session_date DESC, severity DESC, symbol LIMIT ?"
    params.append(limit)

    return [
        {
            "symbol": row["symbol"],
            "kind": row["kind"],
            "session": row["session_date"],
            "fired_at": row["fired_at"],
            "severity": row["severity"],
            "price": row["price"],
            "message": row["detail"],
        }
        for row in conn.execute(sql, params)
    ]


def signal_counts(conn: sqlite3.Connection, *, since: date | None = None) -> dict[str, int]:
    """How many of each kind fired since `since`.

    The health check this project keeps asking of its detectors: a kind that fires on
    everything is describing the market rather than finding anything in it.
    """
    sql = "SELECT kind, COUNT(*) c FROM signal_sent"
    params: Sequence = ()
    if since is not None:
        sql += " WHERE session_date >= ?"
        params = (since.isoformat(),)
    sql += " GROUP BY kind ORDER BY c DESC"
    return {row["kind"]: row["c"] for row in conn.execute(sql, params)}
