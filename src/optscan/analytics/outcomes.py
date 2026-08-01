"""Settling a short premium position from the underlying's closing price.

Pure. The price arrives as an argument, and fetching it is jobs/resolve.py's problem.

## Why this works without a historical option chain

A short option's value at expiry is not a market quote, it is arithmetic: a put is worth
max(strike - spot, 0) and a call max(spot - strike, 0), and everything in between has
decayed to nothing. So settling a logged candidate needs one number, the underlying's
close on expiry day, which yfinance gives away. That is the whole reason Phase 8 can
run at all without the historical options backfill: the scoring needed a chain, the
settling does not.

## What the resulting profit is, and what it is not

It is the profit of a specific policy: **open at the recorded credit, hold to expiry,
never manage.** Nobody actually trades that way. The 50 percent rule exists, and the
whole of Phase 7 is about closing early, so the number here is deliberately not "what
you would have made".

It is still the right thing to measure, for two reasons. It is the policy the
probability model actually describes, since probability of profit is defined at expiry,
so it is the only policy under which calibration means anything. And it is the
pessimistic bound on a managed position for the cases that matter: a position closed at
half profit banks less than one that expires worthless, but a position closed early on a
loser loses less than one held to the end. Held to expiry is the worse tail, which is
the correct direction to be wrong in when the question is whether a score is safe.

The report says all of this rather than printing a win rate and letting it be read as a
track record.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from optscan.models import Right

#: Shares per contract. The logged credit is per share, as everywhere else in the
#: project, so settling multiplies by this to get cash.
CONTRACT_SIZE = 100


class Outcome(StrEnum):
    """How a short premium position finished.

    Deliberately more than win and lose. "Expired worthless" and "finished barely in the
    money but still profitable" are different events even though both make money, and
    collapsing them would hide exactly the near misses a calibration check is about.
    """

    #: Finished out of the money. The credit is kept in full.
    EXPIRED_WORTHLESS = "expired_worthless"
    #: Finished in the money but inside the credit, so still profitable.
    BREACHED_PROFITABLE = "breached_profitable"
    #: Finished in the money past the breakeven. A loss, but not the maximum.
    BREACHED_LOSS = "breached_loss"
    #: Past the long strike on a defined risk position, or unbounded on a naked one.
    MAX_LOSS = "max_loss"

    @property
    def profitable(self) -> bool:
        return self in {Outcome.EXPIRED_WORTHLESS, Outcome.BREACHED_PROFITABLE}


@dataclass(frozen=True, slots=True)
class Settlement:
    """One resolved position, in cash terms per contract."""

    outcome: Outcome
    #: Whether the underlying finished past the short strike. The event the probability
    #: model predicts, which is not the same as whether the trade made money.
    finished_beyond: bool
    profit: float
    #: Profit as a share of maximum profit. 1.0 is a full win, negative is a loss.
    profit_fraction: float | None
    intrinsic: float

    @property
    def won(self) -> bool:
        return self.profit > 0


def intrinsic_value(right: Right | str, strike: float, spot: float) -> float:
    """What the option is worth at expiry. Arithmetic, not a quote."""
    option = Right.parse(right)
    if option is Right.PUT:
        return max(strike - spot, 0.0)
    return max(spot - strike, 0.0)


def settle(
    *,
    short_right: Right | str,
    short_strike: float,
    credit: float,
    settlement_price: float,
    width: float | None = None,
    max_loss: float | None = None,
    commission: float = 0.0,
    contract_size: int = CONTRACT_SIZE,
) -> Settlement:
    """Resolve one short premium position held to expiry.

    `credit` and `width` are per share, matching how the screener records them.
    `max_loss` is in cash and is used only as a floor, because the margin model already
    computed it and recomputing it here from the width would risk the two disagreeing.

    A naked short with no width has no bounded loss, so a deep breach simply produces a
    large negative number rather than being clamped. Clamping it would flatter every
    undefined risk strategy in the report, which is the one place this study must not
    put a thumb on the scale.
    """
    option = Right.parse(short_right)
    intrinsic = intrinsic_value(option, short_strike, settlement_price)
    finished_beyond = intrinsic > 0.0

    # Cash profit: the credit taken in, less what the short is worth at expiry, less
    # whatever the long leg recovers, less commission.
    recovered = 0.0
    if width is not None and width > 0:
        long_strike = short_strike - width if option is Right.PUT else short_strike + width
        recovered = intrinsic_value(option, long_strike, settlement_price)

    profit = (credit - intrinsic + recovered) * contract_size - commission

    if max_loss is not None and max_loss > 0:
        # The margin model's number wins. A floor rather than a recomputation, so the
        # two cannot drift apart.
        profit = max(profit, -max_loss)

    maximum = credit * contract_size - commission
    fraction = profit / maximum if maximum > 0 else None

    return Settlement(
        outcome=_classify(finished_beyond, profit, width, intrinsic, max_loss, credit),
        finished_beyond=finished_beyond,
        profit=profit,
        profit_fraction=fraction,
        intrinsic=intrinsic,
    )


def _classify(
    finished_beyond: bool,
    profit: float,
    width: float | None,
    intrinsic: float,
    max_loss: float | None,
    credit: float,
) -> Outcome:
    """Which of the four states this is.

    The maximum loss test compares against the width rather than against the profit,
    because a position can be within a cent of maximum loss without reaching it and
    calling that "max loss" would overstate how often the worst case actually happens.
    """
    if not finished_beyond:
        return Outcome.EXPIRED_WORTHLESS
    if profit > 0:
        return Outcome.BREACHED_PROFITABLE
    if width is not None and width > 0 and intrinsic >= width:
        return Outcome.MAX_LOSS
    if width is None and max_loss is not None and profit <= -max_loss:
        return Outcome.MAX_LOSS
    del credit
    return Outcome.BREACHED_LOSS
