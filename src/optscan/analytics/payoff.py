"""Payoff diagrams.

Two curves, and the difference between them is the point of drawing either:

- At expiry: piecewise linear, made of the legs' intrinsic values. This is the shape
  everyone pictures when they think about a position.
- At T plus zero: what the position is worth right now if the underlying moves but
  time does not. Smooth, because it still contains time value.

A short premium position's T+0 curve sits below its expiry curve across most of the
range, which is the visual form of the fact that the money is made by waiting rather
than by being right immediately. People who have only ever seen the expiry diagram are
routinely surprised by an early mark to market loss on a position that is working.

Pure. Legs come in with the vol they were priced at, and nothing here fetches anything.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from optscan.analytics.greeks import bsm_price, intrinsic_value
from optscan.models import Action, Leg, Right

CONTRACT_SIZE = 100

#: How far either side of spot to draw, as a fraction. Wide enough to show both wings
#: of a condor and the whole useful range of a vertical.
DEFAULT_RANGE = 0.20

#: Points across that range. Enough for a smooth T+0 curve without sending a thousand
#: floats to a browser per position.
DEFAULT_POINTS = 121

#: A curve needs two points to be a curve.
MIN_POINTS = 2


@dataclass(frozen=True, slots=True)
class PayoffPoint:
    """Profit and loss at one underlying price, in dollars per position."""

    price: float
    at_expiry: float
    at_now: float | None = None


@dataclass(frozen=True, slots=True)
class Payoff:
    """The whole diagram, plus the numbers a table would show beside it."""

    points: tuple[PayoffPoint, ...]
    breakevens: tuple[float, ...]
    max_profit: float | None
    max_loss: float | None
    spot: float
    net_credit: float

    @property
    def prices(self) -> tuple[float, ...]:
        return tuple(point.price for point in self.points)


def leg_sign(leg: Leg) -> int:
    """+1 for a long leg, -1 for a short one.

    A short leg gains when the option loses value, which is the whole sign convention
    that makes short premium payoffs confusing to read.
    """
    return -1 if leg.action is Action.SELL else 1


def net_credit(legs: Sequence[Leg]) -> float:
    """Credit taken in across the position, in dollars, positive for a net credit."""
    total = 0.0
    for leg in legs:
        if leg.mid is None:
            raise ValueError(f"leg {leg.describe()} has no price, so it has no payoff")
        total += -leg_sign(leg) * leg.mid * leg.quantity
    return total * CONTRACT_SIZE


def pnl_at_expiry(legs: Sequence[Leg], price: float) -> float:
    """Position profit and loss at expiry, at one underlying price.

    Intrinsic value only. Every leg is either exercised or worthless, and the credit
    or debit paid at entry is already banked.
    """
    total = 0.0
    for leg in legs:
        if leg.mid is None:
            raise ValueError(f"leg {leg.describe()} has no price, so it has no payoff")
        sign = leg_sign(leg)
        settlement = intrinsic_value(leg.right, price, leg.strike)
        total += sign * (settlement - leg.mid) * leg.quantity
    return total * CONTRACT_SIZE


def pnl_at_time(
    legs: Sequence[Leg],
    price: float,
    time: float,
    rate: float,
    dividend_yield: float = 0.0,
) -> float | None:
    """Position profit and loss right now, with `time` still left on the clock.

    Each leg is repriced at the implied vol it was entered at, which is the honest
    thing to hold constant when the question is "what if the underlying moves". It is
    also the biggest simplification in this module: a move of any size usually comes
    with a change in volatility, and for a short premium position that change works
    against you on the way down. The T+0 curve is therefore optimistic in exactly the
    direction that matters.

    Returns None when any leg lacks a volatility, because half a curve is worse than
    none.
    """
    total = 0.0
    for leg in legs:
        if leg.mid is None or leg.iv is None:
            return None
        value = bsm_price(leg.right, price, leg.strike, time, rate, leg.iv, dividend_yield)
        total += leg_sign(leg) * (value - leg.mid) * leg.quantity
    return total * CONTRACT_SIZE


def breakevens(legs: Sequence[Leg]) -> tuple[float, ...]:
    """Underlying prices where the position breaks even at expiry.

    Solved exactly rather than sampled. The expiry payoff is piecewise linear with
    kinks only at the strikes, so every zero crossing lies on a straight segment
    between two of them and can be found by interpolation. Scanning a fixed grid would
    miss a crossing that falls between samples and would report the others slightly
    wrong.
    """
    if not legs:
        return ()

    strikes = sorted({leg.strike for leg in legs})
    span = max(strikes) - min(strikes)
    margin = max(span, max(strikes) * 0.5, 1.0)
    knots = [max(min(strikes) - margin, 0.0), *strikes, max(strikes) + margin]

    found: list[float] = []
    for low, high in pairwise(knots):
        low_pnl = pnl_at_expiry(legs, low)
        high_pnl = pnl_at_expiry(legs, high)

        if low_pnl == 0.0:
            found.append(low)
        if low_pnl * high_pnl < 0:
            # Linear on this segment, so one interpolation is exact.
            weight = low_pnl / (low_pnl - high_pnl)
            found.append(low + weight * (high - low))

    if pnl_at_expiry(legs, knots[-1]) == 0.0:
        found.append(knots[-1])

    return tuple(sorted({round(value, 6) for value in found}))


def expiry_extremes(legs: Sequence[Leg]) -> tuple[float | None, float | None]:
    """Max profit and max loss at expiry, or None where the position is unbounded.

    A piecewise linear function takes its extremes at a kink or off at infinity, so
    evaluating at zero and at every strike finds everything except a runaway tail.

    Only the upside can actually run away. A stock cannot fall below zero, so the
    downside is always bounded, and a short put's maximum loss is a real number even
    though it is a large and rarely quoted one. That asymmetry is why a short put has
    a stated maximum loss here and a short call does not.
    """
    if not legs:
        return None, None

    strikes = sorted({leg.strike for leg in legs})
    knots = [0.0, *strikes, max(strikes) * 2 + 1.0]
    values = [pnl_at_expiry(legs, price) for price in knots]

    upside_slope = _upside_slope(legs)
    max_profit: float | None = max(values)
    max_loss: float | None = min(values)

    if upside_slope > 0:
        max_profit = None
    elif upside_slope < 0:
        max_loss = None

    return max_profit, max_loss


def _upside_slope(legs: Sequence[Leg]) -> float:
    """Net contracts of exposure above every strike, which is what runs to infinity.

    Puts are all worthless up there, so only calls contribute.
    """
    return float(sum(leg_sign(leg) * leg.quantity for leg in legs if leg.right is Right.CALL))


def build_payoff(
    legs: Sequence[Leg],
    spot: float,
    *,
    time: float | None = None,
    rate: float = 0.0,
    dividend_yield: float = 0.0,
    price_range: float = DEFAULT_RANGE,
    points: int = DEFAULT_POINTS,
) -> Payoff:
    """The full diagram over a band around spot.

    time is what remains on the clock for the T+0 curve. Pass None, or zero, for a
    position at expiry, where the two curves coincide and only one is drawn.
    """
    if not legs:
        raise ValueError("a payoff needs at least one leg")
    if spot <= 0:
        raise ValueError(f"spot must be positive, got {spot}")
    if points < MIN_POINTS:
        raise ValueError("a curve needs at least two points")

    low = max(spot * (1.0 - price_range), 0.01)
    high = spot * (1.0 + price_range)
    step = (high - low) / (points - 1)

    curve: list[PayoffPoint] = []
    for index in range(points):
        price = low + step * index
        curve.append(
            PayoffPoint(
                price=price,
                at_expiry=pnl_at_expiry(legs, price),
                at_now=(
                    pnl_at_time(legs, price, time, rate, dividend_yield)
                    if time and time > 0
                    else None
                ),
            )
        )

    max_profit, max_loss = expiry_extremes(legs)
    return Payoff(
        points=tuple(curve),
        breakevens=breakevens(legs),
        max_profit=max_profit,
        max_loss=max_loss,
        spot=spot,
        net_credit=net_credit(legs),
    )
