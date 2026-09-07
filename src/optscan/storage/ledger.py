"""Reading and writing the broker ledger.

Append only. A statement row records something that already happened at a broker, so
nothing here updates or deletes one. Re-importing a file that overlaps an earlier one
inserts the rows that are new and reports the rest as duplicates rather than raising,
because overlapping exports are the normal case and not an error.

Identity is `(source, digest, dup_index)`. The digest is a hash of the row's own text,
and the index counts identical rows within one file. Both halves are needed: without
the digest a re-import doubles the account, and without the index two identical fills
on one day collapse into one and a real trade disappears.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime

from optscan.logging import get_logger
from optscan.models.broker import BrokerTxn, Effect, TxnKind
from optscan.models.enums import Right
from optscan.models.opportunity import Action

log = get_logger("optscan.storage.ledger")

_COLUMNS = (
    "source",
    "source_file",
    "imported_at",
    "activity_date",
    "process_date",
    "settle_date",
    "instrument",
    "description",
    "trans_code",
    "quantity",
    "price",
    "amount",
    "kind",
    "symbol",
    "expiry",
    "right",
    "strike",
    "action",
    "effect",
    "is_short",
    "fee",
    "digest",
    "dup_index",
)


@dataclass(frozen=True, slots=True)
class ImportReport:
    """What one import actually did."""

    file_name: str | None
    rows_parsed: int
    rows_inserted: int
    rows_duplicate: int
    first_activity: str | None
    last_activity: str | None

    @property
    def already_known(self) -> bool:
        return self.rows_inserted == 0 and self.rows_parsed > 0

    def summary(self) -> str:
        if self.rows_parsed == 0:
            return "nothing to import: the file held no transaction rows"
        span = (
            f" covering {self.first_activity} to {self.last_activity}"
            if self.first_activity
            else ""
        )
        return (
            f"{self.rows_parsed} rows parsed{span}: "
            f"{self.rows_inserted} new, {self.rows_duplicate} already held"
        )


def import_transactions(
    conn: sqlite3.Connection,
    txns: Iterable[BrokerTxn],
    *,
    now: datetime | None = None,
) -> ImportReport:
    """Insert every transaction not already held, and record the import."""
    rows = list(txns)
    stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()

    inserted = 0
    for txn in rows:
        cursor = conn.execute(
            f"INSERT OR IGNORE INTO broker_txn ({', '.join(_COLUMNS)}) "
            f"VALUES ({', '.join('?' * len(_COLUMNS))})",
            (
                txn.source,
                txn.source_file,
                stamp,
                txn.activity_date.isoformat(),
                txn.process_date.isoformat() if txn.process_date else None,
                txn.settle_date.isoformat() if txn.settle_date else None,
                txn.instrument,
                txn.description,
                txn.trans_code,
                txn.quantity,
                txn.price,
                txn.amount,
                str(txn.kind),
                txn.symbol,
                txn.expiry.isoformat() if txn.expiry else None,
                str(txn.right) if txn.right else None,
                txn.strike,
                str(txn.action) if txn.action else None,
                str(txn.effect) if txn.effect else None,
                int(txn.is_short),
                txn.fee,
                txn.digest,
                txn.dup_index,
            ),
        )
        inserted += cursor.rowcount or 0

    dates = sorted(t.activity_date for t in rows)
    report = ImportReport(
        file_name=rows[0].source_file if rows else None,
        rows_parsed=len(rows),
        rows_inserted=inserted,
        rows_duplicate=len(rows) - inserted,
        first_activity=dates[0].isoformat() if dates else None,
        last_activity=dates[-1].isoformat() if dates else None,
    )
    conn.execute(
        """
        INSERT INTO broker_import
            (source, file_name, imported_at, rows_parsed, rows_inserted,
             rows_duplicate, first_activity, last_activity)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rows[0].source if rows else "unknown",
            report.file_name,
            stamp,
            report.rows_parsed,
            report.rows_inserted,
            report.rows_duplicate,
            report.first_activity,
            report.last_activity,
        ),
    )
    conn.commit()
    log.info(
        "broker import",
        file=report.file_name,
        parsed=report.rows_parsed,
        inserted=report.rows_inserted,
        duplicate=report.rows_duplicate,
    )
    return report


def all_transactions(conn: sqlite3.Connection, source: str | None = None) -> list[BrokerTxn]:
    """Every stored transaction, oldest first.

    Ordered by activity date then by insertion id, which preserves the order the rows
    appeared in the file. Several fills on one day carry no timestamp, so file order is
    the only sequencing information that exists.
    """
    sql = "SELECT * FROM broker_txn"
    params: tuple[str, ...] = ()
    if source:
        sql += " WHERE source = ?"
        params = (source,)
    sql += " ORDER BY activity_date, id"
    return [_to_model(row) for row in conn.execute(sql, params)]


def imports(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    """Import history, newest first."""
    return conn.execute(
        "SELECT * FROM broker_import ORDER BY imported_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def _to_model(row: sqlite3.Row) -> BrokerTxn:
    return BrokerTxn(
        source=row["source"],
        source_file=row["source_file"],
        activity_date=_date(row["activity_date"]),
        process_date=_date(row["process_date"]),
        settle_date=_date(row["settle_date"]),
        instrument=row["instrument"],
        description=row["description"],
        trans_code=row["trans_code"],
        quantity=row["quantity"],
        price=row["price"],
        amount=row["amount"],
        kind=TxnKind(row["kind"]),
        symbol=row["symbol"],
        expiry=_date(row["expiry"]),
        right=Right(row["right"]) if row["right"] else None,
        strike=row["strike"],
        action=Action(row["action"]) if row["action"] else None,
        effect=Effect(row["effect"]) if row["effect"] else None,
        is_short=bool(row["is_short"]),
        fee=row["fee"],
        digest=row["digest"],
        dup_index=row["dup_index"],
    )


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None
