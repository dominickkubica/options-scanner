"""Reading and writing a vendor's downloaded daily history.

Append only, like the broker ledger and for a related reason: a row records what a
vendor published on a day that has already happened, so nothing here updates one.

## What a re-import does, and why it is not just a no-op

Exports overlap. Tomorrow's download of the same ticker covers the same twelve years
plus one more day, so almost every row will already be held. The interesting case is
not the duplicate, it is the **conflict**: a session already stored whose values do not
match the file being read.

There are two ways that happens and they need different responses from a human:

  - **The vendor revised the number.** Real, occasional, and the stored value is the
    one every existing rank was computed against.
  - **The file is the wrong ticker.** The format carries no symbol column at all, so a
    mis-saved download files twelve years of one instrument under another's name. The
    prices will disagree on essentially every overlapping row.

Neither is safely resolved by overwriting, and they are trivially distinguished by how
many rows conflict: a revision touches a handful, a wrong ticker touches nearly all of
them. So conflicts are counted, never applied, and the report says which shape it is.
Overwriting silently would turn the second case into a database that looks fine.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from optscan.logging import get_logger
from optscan.models.vendor import VendorDailyBar

log = get_logger("optscan.storage.vendor")

#: Above this share of overlapping rows disagreeing, the file is almost certainly a
#: different instrument rather than a vendor revision. Measured against nothing, since
#: it has never fired; it is a shape test, and the report prints the raw counts beside
#: it so a reader never has to trust the threshold alone.
WRONG_TICKER_CONFLICT_SHARE = 0.5

#: Values compared when deciding whether a stored session matches an incoming one.
#: Prices and the vol, not the volumes: option volume and open interest are the fields
#: a vendor most plausibly restates, and a restated volume is not evidence of anything.
_COMPARED = ("open", "high", "low", "close", "adj_close", "iv30")

#: How many disagreeing sessions to quote in the report. Enough to recognise a
#: wrong ticker at a glance, few enough not to bury the sentence above them.
MAX_CONFLICT_EXAMPLES = 3

#: Floats, so compared with a tolerance. Prices carry four decimals in the export and
#: vol carries two thousandths of a vol point, so this is tighter than either.
_TOLERANCE = 1e-6


@dataclass(frozen=True, slots=True)
class VendorImportReport:
    """What one import actually did."""

    symbol: str
    source: str
    file_name: str | None
    rows_parsed: int
    rows_inserted: int
    rows_duplicate: int
    rows_conflicting: int
    first_session: date | None
    last_session: date | None
    conflict_examples: tuple[str, ...] = field(default_factory=tuple)

    @property
    def already_known(self) -> bool:
        return self.rows_inserted == 0 and self.rows_parsed > 0

    @property
    def looks_like_wrong_ticker(self) -> bool:
        """Whether the conflicts look like a mis-saved file rather than a revision."""
        overlapping = self.rows_duplicate + self.rows_conflicting
        if overlapping == 0 or self.rows_conflicting == 0:
            return False
        return self.rows_conflicting / overlapping > WRONG_TICKER_CONFLICT_SHARE

    def summary(self) -> str:
        span = ""
        if self.first_session and self.last_session:
            span = f", {self.first_session} to {self.last_session}"
        return (
            f"{self.symbol} from {self.source}: {self.rows_parsed} sessions read{span}. "
            f"{self.rows_inserted} new, {self.rows_duplicate} already held, "
            f"{self.rows_conflicting} disagreed."
        )

    def warning(self) -> str | None:
        """The sentence a human needs to see, or None when there is nothing wrong."""
        if not self.rows_conflicting:
            return None
        if self.looks_like_wrong_ticker:
            return (
                f"{self.rows_conflicting} of {self.rows_duplicate + self.rows_conflicting} "
                f"overlapping sessions disagree with what is already stored for "
                f"{self.symbol}. That is too many to be a vendor revision. The Market "
                "Chameleon format has no symbol column, so check that this file is "
                "actually the ticker its name claims. Nothing was overwritten."
            )
        return (
            f"{self.rows_conflicting} stored sessions disagree with this file, which "
            "looks like a vendor revision. The stored values were kept, because they "
            "are the ones every existing rank was computed against. Nothing was "
            "overwritten."
        )


def _matches(row: sqlite3.Row, bar: VendorDailyBar) -> bool:
    for name in _COMPARED:
        stored, incoming = row[name], getattr(bar, name)
        if stored is None or incoming is None:
            if stored is not incoming:  # one side missing, the other not
                return False
            continue
        if abs(stored - incoming) > _TOLERANCE:
            return False
    return True


def import_daily_bars(
    conn: sqlite3.Connection,
    bars: Sequence[VendorDailyBar],
    *,
    file_name: str | None = None,
) -> VendorImportReport:
    """Store what is new, count what is already held, never overwrite.

    Every bar must carry the same source and symbol; mixing them in one call would
    make the report meaningless and is a programming error rather than a data one.
    """
    if not bars:
        raise ValueError("nothing to import")

    seen: dict[date, int] = {}
    for index, bar in enumerate(bars):
        if bar.session_date in seen:
            # The file parser catches this for a download. A bulk API fetch has no
            # parser, so the guard lives here too: two rows for one session would
            # double that day's weight in every average computed over the series, and
            # the unique index turns it into a crash halfway through a batch rather
            # than a message.
            raise ValueError(
                f"{bar.symbol} has two bars for {bar.session_date} in one import "
                f"(rows {seen[bar.session_date]} and {index}). A daily series cannot "
                "hold a session twice."
            )
        seen[bar.session_date] = index

    sources = {bar.source for bar in bars}
    symbols = {bar.symbol for bar in bars}
    if len(sources) != 1 or len(symbols) != 1:
        raise ValueError(f"one source and one symbol per import, got {sources} {symbols}")
    source, symbol = sources.pop(), symbols.pop()

    now = datetime.now(UTC).isoformat()
    existing = {
        row["session_date"]: row
        for row in conn.execute(
            "SELECT * FROM vendor_daily WHERE source = ? AND symbol = ?",
            (source, symbol),
        )
    }

    inserted = duplicate = conflicting = 0
    examples: list[str] = []
    pending: list[tuple] = []

    for bar in bars:
        key = bar.session_date.isoformat()
        row = existing.get(key)
        if row is not None:
            if _matches(row, bar):
                duplicate += 1
            else:
                conflicting += 1
                if len(examples) < MAX_CONFLICT_EXAMPLES:
                    examples.append(f"{key}: stored close {row['close']}, file close {bar.close}")
            continue
        pending.append(
            (
                bar.source,
                bar.symbol,
                key,
                file_name,
                now,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.adj_close,
                bar.volume,
                bar.vwap,
                bar.trade_count,
                bar.iv30,
                bar.call_volume,
                bar.put_volume,
                bar.call_open_interest,
                bar.put_open_interest,
            )
        )
        inserted += 1

    if pending:
        conn.executemany(
            """
            INSERT INTO vendor_daily (
                source, symbol, session_date, source_file, imported_at,
                open, high, low, close, adj_close, volume, vwap, trade_count, iv30,
                call_volume, put_volume, call_open_interest, put_open_interest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            pending,
        )

    report = VendorImportReport(
        symbol=symbol,
        source=source,
        file_name=file_name,
        rows_parsed=len(bars),
        rows_inserted=inserted,
        rows_duplicate=duplicate,
        rows_conflicting=conflicting,
        first_session=bars[0].session_date,
        last_session=bars[-1].session_date,
        conflict_examples=tuple(examples),
    )

    conn.execute(
        """
        INSERT INTO vendor_import (
            source, symbol, file_name, imported_at, rows_parsed, rows_inserted,
            rows_duplicate, rows_conflicting, first_session, last_session
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source,
            symbol,
            file_name,
            now,
            report.rows_parsed,
            report.rows_inserted,
            report.rows_duplicate,
            report.rows_conflicting,
            str(report.first_session),
            str(report.last_session),
        ),
    )
    conn.commit()

    log.info(
        "imported vendor history",
        symbol=symbol,
        source=source,
        inserted=inserted,
        duplicate=duplicate,
        conflicting=conflicting,
    )
    return report


def daily_bars(
    conn: sqlite3.Connection,
    symbol: str,
    *,
    source: str,
    since: date | None = None,
    until: date | None = None,
) -> list[VendorDailyBar]:
    """One vendor's series for one symbol, ascending.

    source is required rather than defaulted, because a defaulted vendor is how two
    series get pooled by accident.
    """
    sql = "SELECT * FROM vendor_daily WHERE source = ? AND symbol = ?"
    params: list = [source, symbol]
    if since is not None:
        sql += " AND session_date >= ?"
        params.append(since.isoformat())
    if until is not None:
        sql += " AND session_date <= ?"
        params.append(until.isoformat())
    sql += " ORDER BY session_date"

    return [
        VendorDailyBar(
            source=row["source"],
            symbol=row["symbol"],
            session_date=date.fromisoformat(row["session_date"]),
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            adj_close=row["adj_close"],
            volume=row["volume"],
            vwap=row["vwap"],
            trade_count=row["trade_count"],
            iv30=row["iv30"],
            call_volume=row["call_volume"],
            put_volume=row["put_volume"],
            call_open_interest=row["call_open_interest"],
            put_open_interest=row["put_open_interest"],
        )
        for row in conn.execute(sql, params)
    ]


def iv30_series(
    conn: sqlite3.Connection,
    symbol: str,
    *,
    source: str,
    until: date | None = None,
) -> list[tuple[date, float]]:
    """Dated IV30 observations, ascending, skipping sessions the vendor left blank.

    The shape analytics/ivrank.py takes. Sessions with no published vol are dropped
    rather than filled: three of them exist in every Market Chameleon export seen so
    far, they are the same three dates in every ticker, and a forward filled vol would
    be a real looking observation the vendor never made.
    """
    sql = """
        SELECT session_date, iv30 FROM vendor_daily
        WHERE source = ? AND symbol = ? AND iv30 IS NOT NULL
    """
    params: list = [source, symbol]
    if until is not None:
        sql += " AND session_date <= ?"
        params.append(until.isoformat())
    sql += " ORDER BY session_date"

    return [(date.fromisoformat(row[0]), row[1]) for row in conn.execute(sql, params)]


def coverage(conn: sqlite3.Connection) -> list[dict]:
    """What is held, per source and symbol. For `optscan status` and the CLI."""
    rows = conn.execute(
        """
        SELECT source, symbol,
               COUNT(*)                AS sessions,
               SUM(iv30 IS NOT NULL)   AS with_iv30,
               MIN(session_date)       AS first_session,
               MAX(session_date)       AS last_session
        FROM vendor_daily
        GROUP BY source, symbol
        ORDER BY symbol, source
        """
    )
    return [dict(row) for row in rows]


def symbols_held(conn: sqlite3.Connection, *, source: str) -> set[str]:
    """Which symbols this vendor has any history for."""
    return {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT symbol FROM vendor_daily WHERE source = ?", (source,)
        )
    }


def import_history(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Recent imports, newest first."""
    rows = conn.execute(
        "SELECT * FROM vendor_import ORDER BY imported_at DESC, id DESC LIMIT ?",
        (limit,),
    )
    return [dict(row) for row in rows]
