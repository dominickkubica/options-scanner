"""A ledger of every time a held-out period was looked at.

A holdout is only held out once. The first search to evaluate finalists on 2023 to 2026
used it up; a second search on the same period, run after reading the first one's
answer, chooses its grid knowing what that period rewards, and nothing in its own
statistics can see it. The peeking is never deliberate. It is what happens when a result
disappoints and somebody adjusts the grid and runs it again.

Nothing can un-see a holdout, but the looks can be counted. Every out-of-sample
evaluation is recorded here, and a later one on overlapping data is told how many
finalists have been tested on it before, so the significance bar can rise with them.

Overlap is by data, not by label: a search of the tech group and a search of the whole
universe share ninety-eight symbols' worth of holdout, so they share a count. Windows
use exclusive end dates.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

#: The day this ledger started. Searches before it evaluated finalists on the same
#: history -- the 2026-09-08 search, trained to 2023-05-17, among them -- and are not
#: recorded, so every count read from here is a floor.
LEDGER_STARTED = date(2026, 9, 11)


@dataclass(frozen=True, slots=True)
class HoldoutUse:
    used_at: str
    kind: str
    test_start: date
    test_end: date
    #: Symbols this use shares with the one asking. Any overlap counts.
    shared_symbols: int
    finalists: int
    best_p: float | None


def record_use(
    conn: sqlite3.Connection,
    *,
    kind: str,
    test_start: date,
    test_end: date,
    symbols: Sequence[str],
    tried: int,
    finalists: Sequence[tuple[str, float | None]],
) -> None:
    """Record one out-of-sample evaluation. `finalists` is (label, p) per candidate."""
    conn.execute(
        "INSERT INTO holdout_use "
        "(used_at, kind, test_start, test_end, symbols, tried, finalists) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            datetime.now(UTC).isoformat(),
            kind,
            test_start.isoformat(),
            test_end.isoformat(),
            json.dumps(sorted(symbols)),
            tried,
            json.dumps([[label, p] for label, p in finalists]),
        ),
    )


def prior_uses(
    conn: sqlite3.Connection,
    *,
    test_start: date,
    test_end: date,
    symbols: Sequence[str],
) -> list[HoldoutUse]:
    """Earlier evaluations that looked at any of these symbols on any of these days."""
    wanted = set(symbols)
    rows = conn.execute(
        "SELECT used_at, kind, test_start, test_end, symbols, finalists FROM holdout_use "
        "WHERE test_start < ? AND ? < test_end ORDER BY used_at",
        (test_end.isoformat(), test_start.isoformat()),
    ).fetchall()

    out = []
    for row in rows:
        shared = wanted & set(json.loads(row["symbols"]))
        if not shared:
            continue
        tested = json.loads(row["finalists"])
        values = [p for _, p in tested if p is not None]
        out.append(
            HoldoutUse(
                used_at=row["used_at"],
                kind=row["kind"],
                test_start=date.fromisoformat(row["test_start"]),
                test_end=date.fromisoformat(row["test_end"]),
                shared_symbols=len(shared),
                finalists=len(tested),
                best_p=min(values) if values else None,
            )
        )
    return out
