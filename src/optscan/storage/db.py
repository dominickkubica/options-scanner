"""SQLite: watchlist, and a manifest of every snapshot run.

Migrations are a plain ordered list of SQL statements guarded by user_version. No
migration framework, because the schema is small, local, and single writer. Each
migration must be additive: this database will be years old before the tool is done.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

from optscan.logging import get_logger

log = get_logger("optscan.storage.db")

MIGRATIONS: tuple[str, ...] = (
    # 1
    """
    CREATE TABLE watchlist (
        symbol      TEXT PRIMARY KEY,
        added_at    TEXT NOT NULL,
        note        TEXT
    );
    CREATE TABLE snapshot_run (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol          TEXT NOT NULL,
        session_date    TEXT NOT NULL,
        captured_at     TEXT NOT NULL,
        provider        TEXT NOT NULL,
        expiries        INTEGER NOT NULL,
        contracts       INTEGER NOT NULL,
        partial         INTEGER NOT NULL DEFAULT 0,
        path            TEXT,
        error           TEXT
    );
    CREATE INDEX idx_snapshot_run_symbol_date ON snapshot_run (symbol, session_date);
    """,
    # 2: positions, added in Phase 7.
    #
    # Legs are a child table rather than a JSON blob on the position. A blob would be
    # less code today and would make every later question awkward: "which positions
    # have a short strike inside this expiry" is the query a roll trigger wants, and
    # against JSON it is a full scan and a parse.
    #
    # fill_price is NOT NULL with no default on purpose. It is the one number the tool
    # cannot reconstruct, and a column that quietly accepted a null would let a
    # position exist whose profit and loss is fiction.
    """
    CREATE TABLE position (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol              TEXT NOT NULL,
        strategy            TEXT,
        opened_at           TEXT NOT NULL,
        commission_open     REAL NOT NULL DEFAULT 0,
        commission_close    REAL NOT NULL DEFAULT 0,
        status              TEXT NOT NULL DEFAULT 'open',
        closed_at           TEXT,
        close_value         REAL,
        note                TEXT
    );
    CREATE TABLE position_leg (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        position_id     INTEGER NOT NULL REFERENCES position(id) ON DELETE CASCADE,
        action          TEXT NOT NULL,
        right           TEXT NOT NULL,
        strike          REAL NOT NULL,
        expiry          TEXT NOT NULL,
        quantity        INTEGER NOT NULL,
        fill_price      REAL NOT NULL,
        contract_size   INTEGER NOT NULL DEFAULT 100,
        contract_symbol TEXT
    );
    CREATE INDEX idx_position_symbol_status ON position (symbol, status);
    CREATE INDEX idx_position_leg_position ON position_leg (position_id);

    -- One row per alert that has already fired, so a triggered condition notifies once
    -- rather than on every poll. The uniqueness is the whole point of the table: an
    -- alerting tool that repeats itself every thirty seconds gets muted, and a muted
    -- alert is worse than none because it is believed to be working.
    CREATE TABLE alert_sent (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        position_id     INTEGER NOT NULL REFERENCES position(id) ON DELETE CASCADE,
        kind            TEXT NOT NULL,
        fired_at        TEXT NOT NULL,
        detail          TEXT
    );
    CREATE UNIQUE INDEX idx_alert_once ON alert_sent (position_id, kind);
    """,
)


def connect(path: Path) -> sqlite3.Connection:
    """Open the database, creating the file and schema if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, detect_types=0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


@contextmanager
def session(path: Path) -> Iterator[sqlite3.Connection]:
    """Connection as a context manager, committing on clean exit."""
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def migrate(conn: sqlite3.Connection) -> int:
    """Apply outstanding migrations. Returns the resulting schema version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, script in enumerate(MIGRATIONS, start=1):
        if version <= current:
            continue
        conn.executescript(script)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
        log.info("applied migration", version=version)
        current = version
    return current


def seed_watchlist(conn: sqlite3.Connection, symbols: Iterable[str]) -> int:
    """Insert the default symbols only if the watchlist has never been populated.

    Once the user has edited the list, config no longer touches it. A default that
    keeps reasserting itself is worse than no default.
    """
    count = conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
    if count:
        return 0
    added = 0
    for symbol in symbols:
        added += add_symbol(conn, symbol, note="seeded from default_watchlist")
    conn.commit()
    return added


def add_symbol(conn: sqlite3.Connection, symbol: str, note: str | None = None) -> int:
    """Add a symbol. Returns 1 if added, 0 if it was already there."""
    normalized = symbol.strip().upper()
    if not normalized:
        raise ValueError("symbol must not be blank")
    cursor = conn.execute(
        "INSERT OR IGNORE INTO watchlist (symbol, added_at, note) VALUES (?, ?, ?)",
        (normalized, datetime.now(UTC).isoformat(), note),
    )
    return cursor.rowcount


def remove_symbol(conn: sqlite3.Connection, symbol: str) -> int:
    """Remove a symbol. Returns 1 if removed, 0 if it was not present."""
    cursor = conn.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol.strip().upper(),))
    return cursor.rowcount


def list_watchlist(conn: sqlite3.Connection) -> list[str]:
    """Watchlist symbols, alphabetical."""
    rows = conn.execute("SELECT symbol FROM watchlist ORDER BY symbol").fetchall()
    return [row["symbol"] for row in rows]


def record_run(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    session_date: date,
    captured_at: datetime,
    provider: str,
    expiries: int,
    contracts: int,
    partial: bool = False,
    path: Path | None = None,
    error: str | None = None,
) -> int:
    """Log one capture attempt, successful or not.

    Failures are rows too. A gap in the IV history needs to be explainable later,
    and "there is no row" cannot distinguish a crash from a holiday.
    """
    cursor = conn.execute(
        """
        INSERT INTO snapshot_run
            (symbol, session_date, captured_at, provider, expiries, contracts,
             partial, path, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            symbol.strip().upper(),
            session_date.isoformat(),
            captured_at.astimezone(UTC).isoformat(),
            provider,
            expiries,
            contracts,
            int(partial),
            str(path) if path else None,
            error,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def recent_runs(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    """Most recent capture attempts, newest first."""
    return conn.execute(
        "SELECT * FROM snapshot_run ORDER BY captured_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def has_run(conn: sqlite3.Connection, symbol: str, session_date: date) -> bool:
    """True if a successful capture already exists for this symbol and session."""
    row = conn.execute(
        """
        SELECT 1 FROM snapshot_run
        WHERE symbol = ? AND session_date = ? AND error IS NULL
        LIMIT 1
        """,
        (symbol.strip().upper(), session_date.isoformat()),
    ).fetchone()
    return row is not None
