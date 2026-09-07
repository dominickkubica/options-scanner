"""Broker activity: one row of a statement, parsed but not interpreted.

## Why a ledger rather than positions

A broker export is a transaction log, and this model keeps it one. Positions, round
trips, realized profit and a cash balance are all **derived** from these rows rather
than written at import time, for three reasons that only show up on the second import:

  - **Exports overlap.** The next download will cover this month and the last one. An
    importer that wrote positions directly would have to decide, per position, whether
    it had seen it before. Against an append only ledger with a stable identity per
    row, a re-import is a no-op and needs no such judgement.
  - **One table holds everything.** Option legs, share trades, deposits, interest and
    fees are all the same shape here. Forcing them into an options position model
    would drop the ones that do not fit, which is most of the account.
  - **Derivation improves.** Strategy inference and round trip matching will get better.
    Recomputing them from a ledger is free; recomputing them from rows that were already
    collapsed into positions is impossible, because the collapsing is lossy.

## The two rules this file exists to hold

**An unrecognised transaction code is an error, not a skipped row.** A future export
containing an assignment or an exercise must fail the import loudly rather than quietly
dropping the trade and reporting a profit that is missing a leg. The known codes are
enumerated and anything else raises.

**A fee is derived, never assumed.** The broker states a price and an amount, and the
gap between them is what the trade actually cost in fees. That number is measured per
row rather than taken from a config default, because the whole point of importing real
fills is that the real numbers are available.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from optscan.models.enums import Right
from optscan.models.opportunity import Action

#: Shares per option contract. Robinhood exports do not state it.
OPTION_MULTIPLIER = 100


class TxnKind(StrEnum):
    """What a row is about, decided from its transaction code and description."""

    OPTION_TRADE = "option_trade"
    OPTION_EXPIRATION = "option_expiration"
    OPTION_ASSIGNMENT = "option_assignment"
    OPTION_EXERCISE = "option_exercise"
    EQUITY_TRADE = "equity_trade"
    CASH = "cash"


class Effect(StrEnum):
    """Whether a trade opened exposure or closed it."""

    OPEN = "open"
    CLOSE = "close"


class BrokerTxn(BaseModel):
    """One statement row.

    Frozen, because a ledger entry is a record of something that already happened. The
    derived views may be recomputed as often as they like; the row may not change.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    #: Which broker's export this came from. Present from the first row so a second
    #: broker never requires a migration to tell the two apart.
    source: str
    #: The file this row was read from, for tracing a surprising number back to paper.
    source_file: str | None = None

    activity_date: date
    process_date: date | None = None
    settle_date: date | None = None

    #: Verbatim, so a row can always be shown to the user as the broker wrote it.
    instrument: str | None = None
    description: str
    trans_code: str

    quantity: float | None = None
    #: Per share for equities, per share of the contract for options. Always positive.
    price: float | None = None
    #: Signed as the broker signed it: negative is cash leaving the account.
    amount: float | None = None

    kind: TxnKind
    symbol: str | None = None
    expiry: date | None = None
    right: Right | None = None
    strike: float | None = Field(default=None, gt=0.0)
    action: Action | None = None
    effect: Effect | None = None
    #: Robinhood puts a trailing S on the quantity of one leg of an expiring spread.
    #: Captured verbatim, and deliberately NOT used to decide direction. Across the
    #: three expiring spreads in the reference export the S always sat on the leg that
    #: had been bought, but it also always sat on the higher strike, and three cases
    #: cannot separate those two explanations. An expiration's direction is derived
    #: from the holding the ledger says was open at that moment instead, which is
    #: correct whatever the S turns out to mean.
    is_short: bool = False

    #: price x multiplier x quantity, less what actually moved. None when the row does
    #: not carry both numbers, which is every expiration and every cash movement.
    fee: float | None = None

    #: Stable identity for this row within its source. See `storage/ledger.py`.
    digest: str
    #: Which occurrence of an identical row this is, within one file. Two genuinely
    #: identical fills on one day are two trades, not one, and the digest alone cannot
    #: tell them apart.
    dup_index: int = 0

    @property
    def multiplier(self) -> int:
        """Shares per unit of `quantity`."""
        return OPTION_MULTIPLIER if self.kind.startswith("option") else 1

    @property
    def is_option(self) -> bool:
        return self.kind.startswith("option")

    @property
    def signed_quantity(self) -> float | None:
        """Change in contracts or shares held, from this row alone.

        Positive is long, negative is short. None means the row cannot answer on its
        own, which is true of three things and for two different reasons:

          - a cash movement, which touches no holding at all
          - an expiration, assignment or exercise, which closes whatever was open and
            therefore depends on prior state rather than on anything in the row

        The second case is why this is a property of a row and not the whole story.
        `analytics/ledger.py` walks the ledger in order and resolves them against the
        holding that was actually open, which is correct without having to decode what
        Robinhood's trailing S means.
        """
        if self.quantity is None or self.kind is TxnKind.CASH:
            return None
        if self.kind in {
            TxnKind.OPTION_EXPIRATION,
            TxnKind.OPTION_ASSIGNMENT,
            TxnKind.OPTION_EXERCISE,
        }:
            return None
        if self.action is None:
            return None
        return self.quantity if self.action is Action.BUY else -self.quantity
