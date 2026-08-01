"""Positions the user actually holds.

Deliberately not a `Record`. Everything else in models/ came from a vendor and carries
`fetched_at` and `source` so its age can be judged. A position came from a person: it
has a fill price they got, at a time they chose, and there is no vendor to attribute it
to. Forcing it into the same base would mean inventing a source for a fact that has
none.

## The fill is the anchor, and it is not the mark

Profit and loss is measured against what was actually paid or received, never against
what the position was theoretically worth at the time. Those differ by the spread, and
on the wide contracts a premium seller lives in they differ a lot. A tool that opens a
position at the mid and then reports P/L against the mid shows a trader flat at the
moment they have already lost half the spread, which is exactly the fiction this
project refuses everywhere else.

So `fill_price` is required on every leg. There is no default and no "use the mid",
because the whole point of storing it is that it is the number the model cannot guess.

## Sign convention

`signed_fill` and `signed_value` are both positive for a credit and negative for a
debit, so profit is always `signed_fill - signed_value`:

- A put sold at 2.00 now marked 1.00: 2.00 minus 1.00 is a 1.00 gain.
- A call bought at 3.00 now marked 5.00: -3.00 minus -5.00 is a 2.00 gain.

One rule for both directions, which is what keeps the aggregate arithmetic honest when
a position holds legs of each kind.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from optscan.models.base import UtcDatetime
from optscan.models.enums import Right
from optscan.models.market import Symbol
from optscan.models.opportunity import Action, Strategy

#: Shares per option contract. Overridable per leg, because adjusted contracts after a
#: split or a special dividend are not 100 and silently assuming they are turns every
#: number on the screen into a lie for that position.
DEFAULT_CONTRACT_SIZE = 100


class PositionStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class UserRecord(BaseModel):
    """Base for things a person entered rather than a vendor supplied."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class PositionLeg(UserRecord):
    """One contract in a held position, at the price it was actually filled."""

    action: Action
    right: Right
    strike: float = Field(gt=0.0)
    expiry: date
    quantity: int = Field(default=1, gt=0)
    #: Per share, always positive. The direction lives in `action`, so a fill price is
    #: never negative and a negative one is a data entry error rather than a short.
    fill_price: float = Field(ge=0.0)
    contract_size: int = Field(default=DEFAULT_CONTRACT_SIZE, gt=0)
    contract_symbol: str | None = None

    @field_validator("right", mode="before")
    @classmethod
    def _parse_right(cls, value: str | Right) -> Right:
        return Right.parse(value)

    @property
    def direction(self) -> int:
        """+1 for a short leg, -1 for a long one. The sign of every credit."""
        return 1 if self.action is Action.SELL else -1

    @property
    def shares(self) -> int:
        """Total shares this leg controls."""
        return self.quantity * self.contract_size

    @property
    def signed_fill(self) -> float:
        """Cash taken in when the leg was opened. Negative for a debit."""
        return self.fill_price * self.direction * self.shares

    def signed_value(self, mark: float | None) -> float | None:
        """What the leg is worth now, in the same sign convention as `signed_fill`.

        None when there is no usable mark, and None has to propagate: a position whose
        legs cannot all be marked has no honest P/L, only a partial one that would read
        as a real number.
        """
        if mark is None:
            return None
        return mark * self.direction * self.shares

    def profit(self, mark: float | None) -> float | None:
        """Gain in cash terms, before commissions."""
        value = self.signed_value(mark)
        return None if value is None else self.signed_fill - value

    def dte(self, asof: date) -> int:
        return (self.expiry - asof).days


class Position(UserRecord):
    """A position as held: what was opened, when, at what price, and whether it is on."""

    symbol: Symbol
    legs: tuple[PositionLeg, ...] = Field(min_length=1)
    opened_at: UtcDatetime
    #: The database identity. None until stored, which is what distinguishes a position
    #: being built from one that has been written down.
    id: int | None = None
    strategy: Strategy | None = None
    #: Charged on the way in. Kept separate from the closing commission because a
    #: position that is still open has only paid half of its round trip, and folding
    #: both in up front would overstate the loss on every open position.
    commission_open: float = Field(default=0.0, ge=0.0)
    commission_close: float = Field(default=0.0, ge=0.0)
    status: PositionStatus = PositionStatus.OPEN
    closed_at: UtcDatetime | None = None
    #: Net cash on closing, in the same convention as `signed_fill`. Recorded rather
    #: than derived so a closed position's P/L is history and not a re-marked estimate.
    close_value: float | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _closed_positions_say_when(self) -> Self:
        if self.status is PositionStatus.CLOSED and self.closed_at is None:
            raise ValueError("a closed position must record when it was closed")
        if self.status is PositionStatus.OPEN and self.closed_at is not None:
            raise ValueError("an open position cannot have a closing time")
        return self

    @model_validator(mode="after")
    def _legs_share_the_symbol(self) -> Self:
        sizes = {leg.contract_size for leg in self.legs}
        if len(sizes) > 1:
            # Not forbidden in principle, but it is almost always a data entry mistake
            # and it makes every aggregate silently wrong, so it has to be deliberate.
            raise ValueError(
                f"legs have different contract sizes {sorted(sizes)}. If that is really "
                "an adjusted contract, open it as its own position."
            )
        return self

    @property
    def expiry(self) -> date:
        """The nearest expiry the position carries. What DTE triggers measure against."""
        return min(leg.expiry for leg in self.legs)

    @property
    def is_open(self) -> bool:
        return self.status is PositionStatus.OPEN

    @property
    def entry_credit(self) -> float:
        """Net cash received on opening, before commissions. Negative for a debit."""
        return sum(leg.signed_fill for leg in self.legs)

    @property
    def net_credit(self) -> float:
        """Entry credit after the opening commission.

        Commissions are an eighth of a narrow spread's maximum profit, which is why
        this project counts them everywhere rather than treating them as rounding.
        """
        return self.entry_credit - self.commission_open

    def dte(self, asof: date) -> int:
        return (self.expiry - asof).days

    def current_value(self, marks: dict[tuple[float, Right], float | None]) -> float | None:
        """What it would cost to close, in the credit positive convention.

        Returns None when any leg is unmarkable, because a three legged position priced
        on two legs is not worth two thirds of a number, it is worth nothing at all.
        """
        total = 0.0
        for leg in self.legs:
            value = leg.signed_value(marks.get((leg.strike, leg.right)))
            if value is None:
                return None
            total += value
        return total

    def unrealized(self, marks: dict[tuple[float, Right], float | None]) -> float | None:
        """Open profit, after the commission already paid to get in.

        The closing commission is deliberately not subtracted. It has not been paid and
        may never be: the position could expire worthless, which is the outcome most of
        these are opened for. Charging it early would understate every winner.
        """
        value = self.current_value(marks)
        return None if value is None else self.net_credit - value

    @property
    def realized(self) -> float | None:
        """Final profit on a closed position, after both commissions."""
        if self.status is not PositionStatus.CLOSED or self.close_value is None:
            return None
        return self.net_credit - self.close_value - self.commission_close

    def max_profit(self) -> float | None:
        """Credit kept if every short leg expires worthless.

        Only meaningful for a net credit position. A net debit position's maximum is a
        function of where price goes, which is the payoff diagram's job, not this one's.
        """
        return self.entry_credit if self.entry_credit > 0 else None

    def profit_fraction(self, marks: dict[tuple[float, Right], float | None]) -> float | None:
        """Open profit as a share of maximum profit. The number a 50 percent rule reads.

        None when the position was opened for a debit, since there is no maximum to take
        a fraction of, and a rule written for credit positions must not silently produce
        a number for one it was not written for.
        """
        maximum = self.max_profit()
        gain = self.unrealized(marks)
        if maximum is None or maximum <= 0 or gain is None:
            return None
        return gain / maximum

    def short_legs(self) -> tuple[PositionLeg, ...]:
        return tuple(leg for leg in self.legs if leg.action is Action.SELL)

    def tested(self, spot: float) -> bool:
        """True when spot has crossed any short strike.

        The trigger most people actually manage on, and it is about the underlying
        rather than about the option's price: a short put is tested when spot trades
        below its strike, whatever the position is currently worth.
        """
        for leg in self.short_legs():
            if leg.right is Right.PUT and spot <= leg.strike:
                return True
            if leg.right is Right.CALL and spot >= leg.strike:
                return True
        return False

    def with_id(self, identifier: int) -> Position:
        return self.model_copy(update={"id": identifier})

    def close(
        self,
        close_value: float,
        *,
        commission: float = 0.0,
        when: datetime | None = None,
    ) -> Position:
        """Return this position closed at a stated value."""
        return self.model_copy(
            update={
                "status": PositionStatus.CLOSED,
                "closed_at": when or datetime.now(UTC),
                "close_value": close_value,
                "commission_close": commission,
            }
        )
