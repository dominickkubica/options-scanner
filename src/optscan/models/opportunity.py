"""A scored candidate.

The output of the screener and the input to a human decision. Two design rules:

- It carries its own provenance. An opportunity built from a snapshot captured three
  days ago is a different object from one built on live quotes, and the difference has
  to survive all the way to the screen.
- It carries its score components, not just the score. A single number nobody can take
  apart is a number people learn to distrust or, worse, to obey.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from optscan.models.base import Record
from optscan.models.enums import Right
from optscan.models.market import Symbol


class Strategy(StrEnum):
    """The position shapes this tool knows how to build and evaluate."""

    CASH_SECURED_PUT = "cash_secured_put"
    COVERED_CALL = "covered_call"
    PUT_CREDIT_SPREAD = "put_credit_spread"
    CALL_CREDIT_SPREAD = "call_credit_spread"
    IRON_CONDOR = "iron_condor"
    SHORT_STRANGLE = "short_strangle"

    @property
    def defined_risk(self) -> bool:
        """Whether max loss is bounded by a long leg.

        A covered call's loss is bounded by the stock going to zero, which is bounded
        in arithmetic and not in any sense that matters, so it is grouped with the
        undefined ones here.
        """
        return self in {
            Strategy.PUT_CREDIT_SPREAD,
            Strategy.CALL_CREDIT_SPREAD,
            Strategy.IRON_CONDOR,
        }


class Action(StrEnum):
    SELL = "sell"
    BUY = "buy"


class Leg(Record):
    """One contract in a position, with the quote it was evaluated against."""

    action: Action
    right: Right
    strike: float = Field(gt=0.0)
    expiry: date
    quantity: int = Field(default=1, gt=0)
    contract_symbol: str | None = None

    bid: float | None = Field(default=None, ge=0.0)
    ask: float | None = Field(default=None, ge=0.0)
    mid: float | None = Field(default=None, ge=0.0)
    iv: float | None = Field(default=None, gt=0.0)
    delta: float | None = None
    open_interest: int | None = Field(default=None, ge=0)
    volume: int | None = Field(default=None, ge=0)

    @property
    def signed_mid(self) -> float | None:
        """Positive for a credit taken in, negative for a debit paid."""
        if self.mid is None:
            return None
        return self.mid * self.quantity * (1 if self.action is Action.SELL else -1)

    @property
    def is_short(self) -> bool:
        return self.action is Action.SELL

    def describe(self) -> str:
        verb = "short" if self.is_short else "long"
        return f"{verb} {self.quantity}x {self.strike:g}{self.right}"


class ScoreComponents(Record):
    """Each scored dimension, before weighting.

    Kept so the UI can answer "why is this ranked third" without re-running anything.
    Every value is 0 to 1 and higher is better.
    """

    premium: float = Field(ge=0.0, le=1.0)
    iv_rank: float | None = Field(default=None, ge=0.0, le=1.0)
    liquidity: float = Field(ge=0.0, le=1.0)
    probability: float = Field(ge=0.0, le=1.0)
    event_risk: float = Field(ge=0.0, le=1.0)

    def as_dict(self) -> dict[str, float | None]:
        return {
            "premium": self.premium,
            "iv_rank": self.iv_rank,
            "liquidity": self.liquidity,
            "probability": self.probability,
            "event_risk": self.event_risk,
        }


class Opportunity(Record):
    """A candidate position, priced, scored, and explained."""

    symbol: Symbol
    strategy: Strategy
    expiry: date
    dte: int
    legs: tuple[Leg, ...]
    underlying_price: float = Field(gt=0.0)

    # Economics. All per contract, in dollars, using the margin model in
    # analytics.returns.
    credit: float = Field(gt=0.0)
    max_profit: float
    max_loss: float | None = None
    capital: float | None = None
    commission: float = Field(default=0.0, ge=0.0)
    return_on_capital: float | None = None
    annualized_return: float | None = None

    # Probability. See analytics.probability for what these assume.
    probability_of_profit: float | None = Field(default=None, ge=0.0, le=1.0)
    probability_of_touch: float | None = Field(default=None, ge=0.0, le=1.0)
    short_delta: float | None = None
    net_delta: float | None = None
    #: Strike distance for a defined risk structure. Max loss is width minus credit, so
    #: without it a card states a max loss the reader cannot verify.
    width: float | None = None

    # Volatility context.
    iv: float | None = Field(default=None, gt=0.0)
    iv_rank: float | None = Field(default=None, ge=0.0, le=1.0)
    iv_percentile: float | None = Field(default=None, ge=0.0, le=1.0)
    iv_confidence: str | None = None

    liquidity_score: float | None = Field(default=None, ge=0.0, le=1.0)

    # Events and other reasons to think twice. Never silently filtered out.
    has_earnings: bool = False
    early_assignment_risk: bool = False
    warnings: tuple[str, ...] = ()

    score: float = Field(ge=0.0, le=1.0)
    components: ScoreComponents

    @model_validator(mode="after")
    def _legs_are_coherent(self) -> Self:
        if not self.legs:
            raise ValueError("an opportunity needs at least one leg")
        if not any(leg.is_short for leg in self.legs):
            raise ValueError("this tool only builds short premium positions")
        return self

    @property
    def short_legs(self) -> tuple[Leg, ...]:
        return tuple(leg for leg in self.legs if leg.is_short)

    @property
    def credit_to_max_loss(self) -> float | None:
        if not self.max_loss:
            return None
        return self.max_profit / self.max_loss

    def describe(self) -> str:
        """One line, human readable. What the CLI table prints."""
        legs = " / ".join(leg.describe() for leg in self.legs)
        return f"{self.symbol} {self.expiry} {self.strategy.value}: {legs}"
