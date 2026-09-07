"""Turning a broker ledger into holdings and round trips.

Pure: takes transactions, returns derived records. No I/O, no clock, no database.

## Why expirations are resolved here and not at parse time

An expiration row states a contract and a date and nothing about direction. Robinhood
marks one leg of an expiring spread with a trailing S on the quantity, but in the
reference export that marker sat on the bought leg and on the higher strike in every
observed case, and three spreads cannot tell those two rules apart. Guessing would be a
coin flip that silently inverts a closed spread into two open contracts.

The ledger does not have to guess. Every open and close before the expiration is
already recorded, so the holding at that moment is known, and an expiration closes
whatever is open. That is correct regardless of what the S means, and it stays correct
if Robinhood changes it.

## What a round trip is here

A `Trade` is one contract's life: the opens that built a holding and the closes that
took it back to flat, with the cash each leg moved. It is deliberately per contract
rather than per strategy. Grouping four legs into "an iron condor" is inference, and
inference belongs above this layer where it can be shown to the user as a guess. What
this module reports is arithmetic.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from optscan.models.broker import BrokerTxn, TxnKind
from optscan.models.enums import Right

#: A holding smaller than this is flat. Share quantities can be fractional, so an exact
#: equality against zero would leave dust positions open forever.
FLAT = 1e-9


@dataclass(frozen=True, slots=True)
class ContractKey:
    """What makes one tradeable thing distinct from another."""

    symbol: str
    expiry: date | None = None
    right: Right | None = None
    strike: float | None = None

    @property
    def is_option(self) -> bool:
        return self.expiry is not None

    def __str__(self) -> str:
        if not self.is_option:
            return self.symbol
        return f"{self.symbol} {self.expiry} {self.right} {self.strike:g}"


@dataclass
class Trade:
    """One contract taken from flat to flat, or still open."""

    key: ContractKey
    opened_at: date
    closed_at: date | None = None
    #: Signed cash across every leg of this round trip, fees included, as the broker
    #: reported it. Positive is money kept.
    cash: float = 0.0
    fees: float = 0.0
    #: Peak absolute quantity held, which is the size the trade actually carried.
    size: float = 0.0
    legs: list[BrokerTxn] = field(default_factory=list)
    #: True when the position was still open at the end of the ledger. Its cash is a
    #: partial figure and must never be counted as a result.
    open_at_end: bool = False
    #: True when an expiration, assignment or exercise ended it rather than a trade.
    ended_by_event: bool = False

    @property
    def realized(self) -> float | None:
        """Profit, or None while the trade is still open.

        An open trade's cash is money received so far, not a result. Returning it as a
        profit is how a losing position that has not been closed reads as a winner.
        """
        return None if self.open_at_end else self.cash

    @property
    def held_days(self) -> int | None:
        if self.closed_at is None:
            return None
        return (self.closed_at - self.opened_at).days


def build_trades(txns: list[BrokerTxn]) -> list[Trade]:
    """Walk the ledger in order and cut it into round trips, one per contract.

    Transactions are sorted by activity date, then by the order they appeared in the
    file. The file order matters: several fills on one day have no timestamp, and the
    export lists them newest first within a day, so the original sequence is the only
    ordering information available and reversing it within a day would open positions
    after closing them.
    """
    ordered = sorted(
        enumerate(txns),
        key=lambda pair: (pair[1].activity_date, -pair[0]),
    )

    holding: dict[ContractKey, float] = defaultdict(float)
    live: dict[ContractKey, Trade] = {}
    done: list[Trade] = []

    for _, txn in ordered:
        if txn.kind is TxnKind.CASH:
            continue
        key = _key(txn)
        if key is None:
            continue

        before = holding[key]
        delta = txn.signed_quantity
        ends_by_event = txn.kind in {
            TxnKind.OPTION_EXPIRATION,
            TxnKind.OPTION_ASSIGNMENT,
            TxnKind.OPTION_EXERCISE,
        }
        if delta is None:
            if not ends_by_event:
                continue
            # An expiration closes whatever was open. See the module docstring.
            delta = -before

        trade = live.get(key)
        if trade is None:
            trade = Trade(key=key, opened_at=txn.activity_date)
            live[key] = trade

        trade.legs.append(txn)
        trade.cash += txn.amount or 0.0
        trade.fees += txn.fee or 0.0
        if ends_by_event:
            trade.ended_by_event = True

        after = before + delta
        holding[key] = after
        trade.size = max(trade.size, abs(after), abs(before))

        if abs(after) <= FLAT:
            trade.closed_at = txn.activity_date
            done.append(trade)
            del live[key]

    for trade in live.values():
        trade.open_at_end = True
        done.append(trade)

    done.sort(key=lambda t: (t.opened_at, str(t.key)))
    return done


def _key(txn: BrokerTxn) -> ContractKey | None:
    symbol = txn.symbol or txn.instrument
    if not symbol:
        return None
    if txn.is_option:
        return ContractKey(symbol, txn.expiry, txn.right, txn.strike)
    return ContractKey(symbol)


@dataclass(frozen=True, slots=True)
class LedgerSummary:
    """Headline figures, separated by what they can honestly claim."""

    option_realized: float
    option_fees: float
    option_trades: int
    equity_realized: float
    equity_trades: int
    open_trades: int
    cash_flows: dict[str, float]

    @property
    def realized(self) -> float:
        return self.option_realized + self.equity_realized


def summarize(txns: list[BrokerTxn], trades: list[Trade]) -> LedgerSummary:
    """Realized results and cash movements, kept apart.

    Deposits are not profit and interest is not trading. They are reported so an equity
    curve can reconcile against the account, never folded into a result.
    """
    cash: dict[str, float] = defaultdict(float)
    for txn in txns:
        if txn.kind is TxnKind.CASH:
            cash[txn.trans_code] += txn.amount or 0.0

    closed = [t for t in trades if not t.open_at_end]
    opt = [t for t in closed if t.key.is_option]
    eq = [t for t in closed if not t.key.is_option]
    return LedgerSummary(
        option_realized=round(sum(t.cash for t in opt), 2),
        option_fees=round(sum(t.fees for t in opt), 2),
        option_trades=len(opt),
        equity_realized=round(sum(t.cash for t in eq), 2),
        equity_trades=len(eq),
        open_trades=sum(1 for t in trades if t.open_at_end),
        cash_flows={k: round(v, 2) for k, v in sorted(cash.items())},
    )
