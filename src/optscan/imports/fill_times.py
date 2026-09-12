"""Matching Robinhood's order history to the ledger, to put a time on every fill.

The account activity CSV says what happened on which day and nothing about when in the
day. The order history says exactly when, to the second, but it is a different record
with a different shape: orders and executions rather than statement rows. This module
is the join between them, and it is pure, so the matching can be tested without a
login.

## How a fill finds its row

A statement row is one leg of one order on one day: "STO 1 QQQ 9/10/2026 Put $500.00 at
$0.50". An order leg's executions, grouped by the Eastern trading day they happened on,
are the same thing from the other side. So the key is the contract, the direction, the
day, and then the quantity and the price: the statement prints the leg's average price
to the cent, the executions carry it to more places, and a cent of tolerance covers the
rounding without admitting a different fill.

Matching is one-to-one. Two identical fills on one day are two rows in the ledger (see
`dup_index`) and two executions in the history, and each is used once.

## What is never guessed

A row with no matching fill keeps no time. Expirations have no execution at all and are
left alone, and a fill that matches nothing is reported rather than forced onto the
nearest row: a wrong time is worse than a missing one, because it silently moves a
trade between the time-of-day buckets.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from optscan.models.broker import BrokerTxn, Effect, TxnKind
from optscan.models.enums import Right
from optscan.models.opportunity import Action

MARKET_TZ = ZoneInfo("America/New_York")

#: The statement prints a leg's average price to the cent; executions carry more places.
PRICE_TOLERANCE = 0.011

#: Quantities are whole contracts, or fractional shares printed to several places.
QUANTITY_TOLERANCE = 1e-6

#: Where a time came from, as stored in `fill_time.origin`.
ORIGIN = "robinhood order history"

_TRADES = frozenset({TxnKind.OPTION_TRADE, TxnKind.EQUITY_TRADE})


@dataclass(frozen=True, slots=True)
class ExecutedFill:
    """One order leg's executions on one trading day, from the order history."""

    symbol: str
    #: None for shares.
    expiry: date | None
    right: Right | None
    strike: float | None
    action: Action
    #: Open or close for an option leg. None for shares, where the history does not say.
    effect: Effect | None
    quantity: float
    #: Quantity-weighted average across the executions.
    price: float
    #: The first execution. Timezone aware.
    executed_at: datetime
    order_id: str

    @property
    def trading_day(self) -> date:
        return self.executed_at.astimezone(MARKET_TZ).date()


@dataclass
class MatchReport:
    matched: list[tuple[BrokerTxn, ExecutedFill]] = field(default_factory=list)
    #: Fills from the history with no row in the ledger: usually trades outside the
    #: imported statements' date range.
    unmatched_fills: list[ExecutedFill] = field(default_factory=list)
    #: Trade rows that still have no time after matching.
    untimed_rows: list[BrokerTxn] = field(default_factory=list)


def _key(
    day: date,
    symbol: str,
    *,
    expiry: date | None,
    right: Right | None,
    strike: float | None,
    action: Action | None,
) -> tuple:
    return (day, symbol.upper(), expiry, right, round(strike, 4) if strike else None, action)


def match_fills(fills: Sequence[ExecutedFill], txns: Iterable[BrokerTxn]) -> MatchReport:
    """Pair each fill with the ledger row it produced. See the module docstring."""
    candidates: dict[tuple, list[BrokerTxn]] = defaultdict(list)
    rows = [t for t in txns if t.kind in _TRADES and t.symbol and t.action is not None]
    for txn in rows:
        key = _key(
            txn.activity_date,
            txn.symbol or "",
            expiry=txn.expiry,
            right=txn.right,
            strike=txn.strike,
            action=txn.action,
        )
        candidates[key].append(txn)

    report = MatchReport()
    used: set[tuple[str, int]] = set()
    for fill in sorted(fills, key=lambda f: f.executed_at):
        key = _key(
            fill.trading_day,
            fill.symbol,
            expiry=fill.expiry,
            right=fill.right,
            strike=fill.strike,
            action=fill.action,
        )
        best: BrokerTxn | None = None
        best_gap = PRICE_TOLERANCE
        for txn in candidates.get(key, ()):
            if (txn.digest, txn.dup_index) in used:
                continue
            if fill.effect is not None and txn.effect is not None and fill.effect is not txn.effect:
                continue
            if abs((txn.quantity or 0.0) - fill.quantity) > QUANTITY_TOLERANCE:
                continue
            gap = abs((txn.price or 0.0) - fill.price)
            if gap <= best_gap:
                best, best_gap = txn, gap
        if best is None:
            report.unmatched_fills.append(fill)
            continue
        used.add((best.digest, best.dup_index))
        report.matched.append((best, fill))

    report.untimed_rows = [t for t in rows if (t.digest, t.dup_index) not in used]
    return report
