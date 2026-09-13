"""Migration 13 re-keys the stored ledger with the six column digest (BUG-0003).

A database built before the fix holds nine column digests, with fill times keyed on
them. After the migration every row carries the digest the importer now computes, the
fill times still find their rows, and importing the same statement again is a no-op.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from optscan.imports import parse_rows
from optscan.models.broker import txn_digest
from optscan.storage import db
from optscan.storage.ledger import all_transactions, import_transactions
from tests.test_imports_robinhood import SPREAD

#: The spread, plus a second fill identical to its first leg: two trades, one digest.
STATEMENT = [*SPREAD, dict(SPREAD[0])]

JOINED = (
    "SELECT COUNT(*) FROM fill_time f JOIN broker_txn b "
    "ON b.source = f.source AND b.digest = f.digest AND b.dup_index = f.dup_index"
)


def test_stored_rows_are_rekeyed_and_their_fill_times_follow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ledger.sqlite"
    before = db.MIGRATIONS[: db.MIGRATIONS.index(db._rekey_broker_ledger)]
    monkeypatch.setattr(db, "MIGRATIONS", before)

    conn = db.connect(path)
    import_transactions(conn, parse_rows(STATEMENT))
    # What the old digest left behind: every row under some other key, index 0, and a
    # fill time on each. The identical pair now has distinct digests and must end up
    # sharing one, told apart by index, or the unique index refuses the migration.
    ids = [row[0] for row in conn.execute("SELECT id FROM broker_txn ORDER BY id")]
    for n, ident in enumerate(ids):
        old = f"{n:032x}"
        conn.execute("UPDATE broker_txn SET digest = ?, dup_index = 0 WHERE id = ?", (old, ident))
        conn.execute(
            "INSERT INTO fill_time VALUES ('robinhood', ?, 0, ?, 'test', '2026-09-05T00:00:00')",
            (old, f"2026-09-04T14:{n:02d}:00+00:00"),
        )
    conn.commit()
    conn.close()

    monkeypatch.undo()
    conn = db.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
        stored = all_transactions(conn)
        for txn in stored:
            assert txn.digest == txn_digest(
                txn.source,
                activity_date=txn.activity_date,
                description=txn.description,
                trans_code=txn.trans_code,
                quantity=txn.quantity,
                is_short=txn.is_short,
                price=txn.price,
                amount=txn.amount,
            )
        assert sorted(txn.dup_index for txn in stored) == [0, 0, 0, 0, 1]
        assert conn.execute(JOINED).fetchone()[0] == len(ids)

        again = import_transactions(conn, parse_rows(STATEMENT))
        assert again.rows_inserted == 0
    finally:
        conn.close()
