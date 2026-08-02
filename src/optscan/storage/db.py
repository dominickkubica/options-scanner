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
    # 3: the validation log, added in Phase 8.
    #
    # Every scored candidate is written at scan time whether or not it is traded. That
    # is the whole design: a study that only records the trades somebody took measures
    # the trader, not the score, and it will confirm whatever they already believed.
    #
    # The score and its components are denormalized onto the row rather than joined
    # from config, because the weights will change and a resolved outcome has to stay
    # attached to the score it was actually given. Re-scoring history under new weights
    # is a different question and it must not silently overwrite this one.
    #
    # scan_id groups the rows written by one run. It is what makes the correlation
    # between candidates visible: forty rows from one scan of one chain are not forty
    # independent observations, and the calibration report needs to be able to say so.
    """
    CREATE TABLE scan_run (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ran_at          TEXT NOT NULL,
        session_date    TEXT NOT NULL,
        symbols         TEXT NOT NULL,
        config_digest   TEXT,
        candidates      INTEGER NOT NULL DEFAULT 0,
        note            TEXT
    );
    CREATE TABLE opportunity_log (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        scan_id                 INTEGER NOT NULL REFERENCES scan_run(id) ON DELETE CASCADE,
        recorded_at             TEXT NOT NULL,
        session_date            TEXT NOT NULL,
        symbol                  TEXT NOT NULL,
        strategy                TEXT NOT NULL,
        expiry                  TEXT NOT NULL,
        dte                     INTEGER NOT NULL,
        underlying_price        REAL NOT NULL,
        legs                    TEXT NOT NULL,
        short_strike            REAL,
        short_right             TEXT,
        long_strike             REAL,
        width                   REAL,
        credit                  REAL NOT NULL,
        max_profit              REAL NOT NULL,
        max_loss                REAL,
        capital                 REAL,
        commission              REAL NOT NULL DEFAULT 0,
        probability_of_profit   REAL,
        probability_of_touch    REAL,
        short_delta             REAL,
        iv                      REAL,
        iv_rank                 REAL,
        iv_confidence           TEXT,
        liquidity_score         REAL,
        has_earnings            INTEGER NOT NULL DEFAULT 0,
        score                   REAL NOT NULL,
        component_premium       REAL,
        component_iv_rank       REAL,
        component_liquidity     REAL,
        component_probability   REAL,
        component_event_risk    REAL
    );
    CREATE INDEX idx_opportunity_expiry ON opportunity_log (expiry);
    CREATE INDEX idx_opportunity_symbol ON opportunity_log (symbol, expiry);
    CREATE INDEX idx_opportunity_scan ON opportunity_log (scan_id);

    -- Resolution is separate from the log so that re-resolving is possible without
    -- touching what was recorded. The scored row is the claim; this is the umpire.
    CREATE TABLE opportunity_outcome (
        opportunity_id      INTEGER PRIMARY KEY REFERENCES opportunity_log(id) ON DELETE CASCADE,
        resolved_at         TEXT NOT NULL,
        settlement_date     TEXT NOT NULL,
        settlement_price    REAL NOT NULL,
        settlement_source   TEXT NOT NULL,
        outcome             TEXT NOT NULL,
        finished_beyond     INTEGER NOT NULL,
        profit              REAL NOT NULL,
        profit_fraction     REAL,
        note                TEXT
    );
    CREATE INDEX idx_outcome_resolved ON opportunity_outcome (settlement_date);
    """,
    # 4: the job run log, added in Phase 9 hardening.
    #
    # Windows already knows whether it started a process and what the process returned.
    # It does not know whether the work happened, and those differ in the case that
    # matters: a job that runs, finds nothing to do, and exits 0. Task Scheduler calls
    # that a success. So this table records the work, and health reads both.
    #
    # A row is written when a job starts and closed when it finishes. That is the point
    # of two writes rather than one: a job killed by the execution time limit, or by the
    # machine going away, leaves a row with a start and no finish. One row written at the
    # end would leave nothing at all, which is indistinguishable from never having run.
    """
    CREATE TABLE job_run (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        job         TEXT NOT NULL,
        started_at  TEXT NOT NULL,
        finished_at TEXT,
        ok          INTEGER,
        detail      TEXT
    );
    CREATE INDEX idx_job_run_job ON job_run (job, started_at);
    """,
    # 5: when the run log started.
    #
    # Without this the health report blames every job for every day before the log
    # existed, which on the day it ships is every day. That is the exact failure the
    # report is supposed to prevent in the other direction: a monitor that opens by
    # crying wolf teaches its reader to ignore it before it has ever been right.
    #
    # A separate migration rather than folded into 4 because 4 has already been applied
    # to a database holding the validation study, and re-running a migration to add a
    # row is not a thing this scheme does. Additive only, always.
    """
    CREATE TABLE meta (
        key     TEXT PRIMARY KEY,
        value   TEXT NOT NULL
    );
    INSERT INTO meta (key, value)
    VALUES ('job_log_started_at', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));
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
