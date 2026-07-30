"""Probability metrics for short premium positions.

Everything here assumes lognormal terminal prices under geometric Brownian motion at
a constant volatility. That assumption is wrong in a specific and consistent
direction: real return distributions have fatter tails and negative skew, so a model
that says a strike has a 90 percent chance of expiring worthless is systematically a
little optimistic. It is optimistic in exactly the scenario a premium seller cares
about, which is the large adverse move.

Treat these numbers as a consistent ranking device, not as a forecast. Phase 8 exists
to find out how well calibrated they actually are.

The delta proxy is also here, because delta is what most platforms display as
"probability ITM" and the difference between it and the real number is worth seeing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from optscan.analytics.greeks import MIN_SIGMA, MIN_TIME_TO_EXPIRY, norm_cdf
from optscan.models import Right


@dataclass(frozen=True, slots=True)
class TouchAndFinish:
    """The two probabilities that matter for a short strike.

    touch is roughly double finish for a strike far from spot, which is the single
    most underappreciated fact in premium selling: a 20 percent chance of finishing
    ITM is about a 40 percent chance of being tested at some point along the way.
    """

    finish_beyond: float
    touch: float


def _log_drift(rate: float, dividend_yield: float, sigma: float) -> float:
    """Drift of log price under the risk neutral measure."""
    return rate - dividend_yield - 0.5 * sigma * sigma


def prob_finish_below(
    barrier: float,
    spot: float,
    time: float,
    sigma: float,
    rate: float = 0.0,
    dividend_yield: float = 0.0,
) -> float:
    """P(S_T < barrier). This is N(-d2) with the barrier in place of the strike."""
    _validate(barrier, spot, time, sigma)
    if time <= MIN_TIME_TO_EXPIRY or sigma <= MIN_SIGMA:
        return 1.0 if spot < barrier else 0.0
    drift = _log_drift(rate, dividend_yield, sigma)
    z = (math.log(barrier / spot) - drift * time) / (sigma * math.sqrt(time))
    return norm_cdf(z)


def prob_finish_above(
    barrier: float,
    spot: float,
    time: float,
    sigma: float,
    rate: float = 0.0,
    dividend_yield: float = 0.0,
) -> float:
    """P(S_T > barrier)."""
    return 1.0 - prob_finish_below(barrier, spot, time, sigma, rate, dividend_yield)


def prob_of_touch(
    barrier: float,
    spot: float,
    time: float,
    sigma: float,
    rate: float = 0.0,
    dividend_yield: float = 0.0,
) -> float:
    """P(the underlying trades through `barrier` at any point before expiry).

    First passage probability for geometric Brownian motion, not the "double the
    finish probability" rule of thumb. The rule of thumb is exact only when the log
    drift is zero, and it is off by several points at longer horizons where the drift
    term has time to matter.
    """
    _validate(barrier, spot, time, sigma)
    if barrier == spot:
        return 1.0
    if time <= MIN_TIME_TO_EXPIRY or sigma <= MIN_SIGMA:
        return 0.0

    drift = _log_drift(rate, dividend_yield, sigma)
    level = math.log(barrier / spot)
    root = sigma * math.sqrt(time)
    exponent = 2.0 * drift * level / (sigma * sigma)

    if level > 0:  # upper barrier
        first = norm_cdf((-level + drift * time) / root)
        second = norm_cdf((-level - drift * time) / root)
    else:  # lower barrier
        first = norm_cdf((level - drift * time) / root)
        second = norm_cdf((level + drift * time) / root)

    # exp can overflow for a distant barrier with a large drift, where the second
    # term is vanishing anyway. Clamp rather than raise.
    try:
        scaled = math.exp(exponent) * second
    except OverflowError:
        scaled = 0.0

    return min(max(first + scaled, 0.0), 1.0)


def touch_and_finish(
    right: Right | str,
    strike: float,
    spot: float,
    time: float,
    sigma: float,
    rate: float = 0.0,
    dividend_yield: float = 0.0,
) -> TouchAndFinish:
    """Both probabilities for a short strike, in the direction that hurts."""
    option = Right.parse(right)
    if option is Right.PUT:
        finish = prob_finish_below(strike, spot, time, sigma, rate, dividend_yield)
    else:
        finish = prob_finish_above(strike, spot, time, sigma, rate, dividend_yield)
    return TouchAndFinish(
        finish_beyond=finish,
        touch=prob_of_touch(strike, spot, time, sigma, rate, dividend_yield),
    )


def breakeven(right: Right | str, strike: float, credit: float) -> float:
    """Underlying price at which a short single leg position breaks even at expiry."""
    if credit < 0:
        raise ValueError("credit must not be negative for a short position")
    if Right.parse(right) is Right.PUT:
        return strike - credit
    return strike + credit


def probability_of_profit(
    right: Right | str,
    strike: float,
    credit: float,
    spot: float,
    time: float,
    sigma: float,
    rate: float = 0.0,
    dividend_yield: float = 0.0,
) -> float:
    """P(a short option expires profitable), measured at the breakeven, not the strike.

    Measuring at the strike is the common shortcut and it understates POP, because a
    short put assigned one cent in the money still keeps almost all of the credit. The
    gap is largest exactly where the credit is largest, which is where it matters.
    """
    option = Right.parse(right)
    level = breakeven(option, strike, credit)
    if level <= 0:
        # Credit exceeds the strike, so the position cannot lose at expiry.
        return 1.0
    if option is Right.PUT:
        return prob_finish_above(level, spot, time, sigma, rate, dividend_yield)
    return prob_finish_below(level, spot, time, sigma, rate, dividend_yield)


def probability_of_profit_spread(
    right: Right | str,
    short_strike: float,
    long_strike: float,
    credit: float,
    spot: float,
    time: float,
    sigma: float,
    rate: float = 0.0,
    dividend_yield: float = 0.0,
) -> float:
    """P(a vertical credit spread expires profitable).

    Same breakeven logic as a single leg. The long strike does not change where
    breakeven is, only how bad the loss gets beyond it.
    """
    option = Right.parse(right)
    if option is Right.PUT and long_strike >= short_strike:
        raise ValueError("a put credit spread buys a lower strike than it sells")
    if option is Right.CALL and long_strike <= short_strike:
        raise ValueError("a call credit spread buys a higher strike than it sells")
    return probability_of_profit(
        option, short_strike, credit, spot, time, sigma, rate, dividend_yield
    )


def delta_as_probability(delta: float) -> float:
    """The trader shortcut: |delta| as the chance of finishing in the money.

    It is a decent approximation and a biased one. Delta is N(d1) for a call and the
    ITM probability is N(d2), and d1 exceeds d2 by sigma*sqrt(t), so delta always
    overstates the probability of finishing ITM. The gap grows with volatility and
    time, reaching several points on a high IV name at 45 days.

    Provided so the UI can show both and make the difference visible, not so anything
    downstream can use it in place of the real calculation.
    """
    return min(abs(delta), 1.0)


def _validate(barrier: float, spot: float, time: float, sigma: float) -> None:
    if barrier <= 0:
        raise ValueError(f"barrier must be positive, got {barrier}")
    if spot <= 0:
        raise ValueError(f"spot must be positive, got {spot}")
    if time < 0:
        raise ValueError(f"time must not be negative, got {time}")
    if sigma < 0:
        raise ValueError(f"sigma must not be negative, got {sigma}")
