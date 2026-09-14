"""Black-Scholes-Merton over numpy arrays, for the premium study's minute marks.

The same model as `greeks.py` and the same solver refusals as `iv.py`, vectorised,
because the premium study turns tens of thousands of option prints a session into
volatilities and prices hundreds of thousands of leg marks. A scalar Brent solve per
print is minutes per session; this is milliseconds.

Pinned to the scalar functions by tests over a grid of inputs. Two implementations of
one formula are a standing hazard, and the test is the only thing keeping the second
one honest.

## The solver

Bisection on sigma inside `iv.py`'s bracket, a fixed number of halvings. Price is
strictly increasing in sigma, so bisection cannot wander the way a Newton step off a
near zero vega does on the wings, which is exactly where the prints are thinnest. Sixty
halvings of a bracket five wide is far below float resolution.

What the scalar solver refuses with a reason, this returns as NaN: a price outside the
no arbitrage bounds, one the bracket cannot express, and a root whose vega is too small
for the price to pin it down.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import ArrayLike, NDArray
from scipy.special import ndtr

from optscan.analytics.greeks import MIN_SIGMA as DEGENERATE_SIGMA
from optscan.analytics.greeks import MIN_TIME_TO_EXPIRY
from optscan.analytics.iv import DEFAULT_MIN_VEGA, MAX_SIGMA, MIN_SIGMA

Floats = NDArray[np.float64]

#: Bisection steps. The bracket is five wide, so sixty halvings is ~4e-18.
HALVINGS = 60

_ROOT_TWO_PI = float(np.sqrt(2.0 * np.pi))


def _arrays(*values: ArrayLike) -> list[Floats]:
    return [np.asarray(value, dtype=float) for value in np.broadcast_arrays(*values)]


def _terms(
    spot: Floats, strike: Floats, time: Floats, rate: float, sigma: Floats, dividend: float
) -> tuple[Floats, Floats]:
    root = sigma * np.sqrt(time)
    first = (np.log(spot / strike) + (rate - dividend + 0.5 * sigma * sigma) * time) / root
    return first, first - root


def bsm_price(
    is_call: ArrayLike,
    spot: ArrayLike,
    strike: ArrayLike,
    time: ArrayLike,
    rate: float,
    sigma: ArrayLike,
    dividend_yield: float = 0.0,
) -> Floats:
    """Value per share. Expired or zero-vol inputs give the discounted payoff."""
    call, spot_, strike_, time_, sigma_ = _arrays(is_call, spot, strike, time, sigma)
    call = call.astype(bool)
    time_ = np.maximum(time_, 0.0)
    carry = np.exp(-dividend_yield * time_)
    discount = np.exp(-rate * time_)
    degenerate = (time_ <= MIN_TIME_TO_EXPIRY) | (sigma_ <= DEGENERATE_SIGMA)
    first, second = _terms(
        spot_,
        strike_,
        np.where(degenerate, 1.0, time_),
        rate,
        np.where(degenerate, 1.0, sigma_),
        dividend_yield,
    )
    forward = spot_ * carry
    strike_value = strike_ * discount
    model = np.where(
        call,
        forward * ndtr(first) - strike_value * ndtr(second),
        strike_value * ndtr(-second) - forward * ndtr(-first),
    )
    payoff = np.where(
        call, np.maximum(forward - strike_value, 0.0), np.maximum(strike_value - forward, 0.0)
    )
    return np.where(degenerate, payoff, model)


def bsm_delta(
    is_call: ArrayLike,
    spot: ArrayLike,
    strike: ArrayLike,
    time: ArrayLike,
    rate: float,
    sigma: ArrayLike,
    dividend_yield: float = 0.0,
) -> Floats:
    """Delta per 1.00 of the underlying, negative for puts."""
    call, spot_, strike_, time_, sigma_ = _arrays(is_call, spot, strike, time, sigma)
    call = call.astype(bool)
    time_ = np.maximum(time_, 0.0)
    carry = np.exp(-dividend_yield * time_)
    degenerate = (time_ <= MIN_TIME_TO_EXPIRY) | (sigma_ <= DEGENERATE_SIGMA)
    first, _ = _terms(
        spot_,
        strike_,
        np.where(degenerate, 1.0, time_),
        rate,
        np.where(degenerate, 1.0, sigma_),
        dividend_yield,
    )
    model = np.where(call, carry * ndtr(first), carry * (ndtr(first) - 1.0))
    in_the_money = np.where(
        call,
        spot_ * carry > strike_ * np.exp(-rate * time_),
        spot_ * carry < strike_ * np.exp(-rate * time_),
    )
    payoff = np.where(in_the_money, np.where(call, carry, -carry), 0.0)
    return np.where(degenerate, payoff, model)


def bsm_vega(
    spot: ArrayLike,
    strike: ArrayLike,
    time: ArrayLike,
    rate: float,
    sigma: ArrayLike,
    dividend_yield: float = 0.0,
) -> Floats:
    """Vega per volatility point (0.01 of sigma), the unit `iv.py` gates on."""
    spot_, strike_, time_, sigma_ = _arrays(spot, strike, time, sigma)
    usable = (time_ > MIN_TIME_TO_EXPIRY) & (sigma_ > DEGENERATE_SIGMA)
    safe_time = np.where(usable, time_, 1.0)
    first, _ = _terms(
        spot_, strike_, safe_time, rate, np.where(usable, sigma_, 1.0), dividend_yield
    )
    density = np.exp(-0.5 * first * first) / _ROOT_TWO_PI
    vega = spot_ * np.exp(-dividend_yield * safe_time) * density * np.sqrt(safe_time) * 0.01
    return np.where(usable, vega, 0.0)


def implied_vol(
    is_call: ArrayLike,
    price: ArrayLike,
    spot: ArrayLike,
    strike: ArrayLike,
    time: ArrayLike,
    rate: float,
    dividend_yield: float = 0.0,
    min_vega: float = DEFAULT_MIN_VEGA,
) -> Floats:
    """The sigma reproducing each price, or NaN where `iv.implied_vol` would refuse."""
    call, price_, spot_, strike_, time_ = _arrays(is_call, price, spot, strike, time)
    call = call.astype(bool)
    carry = np.exp(-dividend_yield * np.maximum(time_, 0.0))
    discount = np.exp(-rate * np.maximum(time_, 0.0))
    forward = spot_ * carry
    strike_value = strike_ * discount
    lower = np.where(
        call, np.maximum(forward - strike_value, 0.0), np.maximum(strike_value - forward, 0.0)
    )
    upper = np.where(call, forward, strike_value)
    usable = (time_ > MIN_TIME_TO_EXPIRY) & (price_ > 0.0) & (price_ > lower) & (price_ < upper)
    solve_time = np.where(usable, time_, 1.0)

    low = np.full(price_.shape, MIN_SIGMA)
    high = np.full(price_.shape, MAX_SIGMA)
    reachable = (
        usable
        & (bsm_price(call, spot_, strike_, solve_time, rate, low, dividend_yield) <= price_)
        & (bsm_price(call, spot_, strike_, solve_time, rate, high, dividend_yield) >= price_)
    )
    for _ in range(HALVINGS):
        middle = 0.5 * (low + high)
        rich = bsm_price(call, spot_, strike_, solve_time, rate, middle, dividend_yield) > price_
        high = np.where(rich, middle, high)
        low = np.where(rich, low, middle)
    sigma = 0.5 * (low + high)
    identifiable = bsm_vega(spot_, strike_, solve_time, rate, sigma, dividend_yield) >= min_vega
    return np.where(reachable & identifiable, sigma, np.nan)
