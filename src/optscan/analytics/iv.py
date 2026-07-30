"""Implied volatility.

Solving for sigma is the easy part. The hard part is refusing to solve, because
every bad quote in a chain will happily produce a number that looks like a
volatility and is not one. A 0.00 by 0.05 quote on a far out of the money contract
has a mathematically valid implied vol and no informational content whatsoever.

So the gates come first and the solver second, and a rejected contract carries the
reason it was rejected rather than a None the caller has to guess about.

Everything here is pure. The thresholds are arguments, not config reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from itertools import pairwise
from math import exp

from scipy.optimize import brentq

from optscan.analytics.greeks import (
    MIN_TIME_TO_EXPIRY,
    bsm_greeks,
    bsm_price,
    time_to_expiry,
)
from optscan.models import OptionContract, Right

#: Bracket for the solver. Below the floor a quote is indistinguishable from zero
#: time value; above the ceiling it is not a market, it is a typo or a lottery ticket.
MIN_SIGMA = 1e-4
MAX_SIGMA = 5.0

#: Defaults for the gates. Callers pass their own from config.
DEFAULT_MAX_SPREAD_PCT = 0.25
DEFAULT_MIN_PRICE = 0.01

#: Minimum vega, in dollars per volatility point, for a solved sigma to mean anything.
#: Far out of the money and very short dated contracts are worth so little that a wide
#: range of volatilities produces the same price to double precision. Brent will still
#: return a root there, and that root is noise. Rejecting is the honest answer, because
#: a fabricated vol in the IV history is worse than a gap in it.
DEFAULT_MIN_VEGA = 1e-6


class VolReason(StrEnum):
    """Why a contract does or does not have a usable implied vol."""

    OK = "ok"
    NO_TWO_SIDED_MARKET = "no_two_sided_market"
    CROSSED = "crossed"
    SPREAD_TOO_WIDE = "spread_too_wide"
    PRICE_TOO_SMALL = "price_too_small"
    EXPIRED = "expired"
    BELOW_INTRINSIC = "below_intrinsic"
    ABOVE_MAXIMUM = "above_maximum"
    NO_SOLUTION = "no_solution"
    OUTSIDE_BRACKET = "outside_bracket"
    NOT_IDENTIFIABLE = "not_identifiable"


@dataclass(frozen=True, slots=True)
class VolResult:
    """An implied vol, or a stated reason there is not one."""

    sigma: float | None
    reason: VolReason
    price_used: float | None = None
    time_to_expiry: float | None = None

    @property
    def ok(self) -> bool:
        return self.sigma is not None

    def __bool__(self) -> bool:
        return self.ok


def price_bounds(
    right: Right | str,
    spot: float,
    strike: float,
    time: float,
    rate: float,
    dividend_yield: float = 0.0,
) -> tuple[float, float]:
    """No arbitrage bounds on an option's price.

    A quote at or below the lower bound implies zero or negative volatility, and one
    at or above the upper bound implies infinite volatility. Neither has a solution,
    and both are common in real chains from stale marks.
    """
    carry = spot * exp(-dividend_yield * time)
    discounted_strike = strike * exp(-rate * time)
    if Right.parse(right) is Right.CALL:
        return max(carry - discounted_strike, 0.0), carry
    return max(discounted_strike - carry, 0.0), discounted_strike


def implied_vol(
    right: Right | str,
    price: float,
    spot: float,
    strike: float,
    time: float,
    rate: float,
    dividend_yield: float = 0.0,
    min_vega: float = DEFAULT_MIN_VEGA,
) -> VolResult:
    """Solve for the sigma that reproduces `price`.

    Brent's method on the bracket, which is safe because price is strictly increasing
    in sigma. A Newton step off vega would be faster and would also wander off on the
    wings where vega is near zero, which is precisely where the bad quotes are.

    The solution is then checked for identifiability: if vega at the root is
    negligible, many volatilities produce this price and the root is an artifact of
    where the search happened to land.
    """
    option = Right.parse(right)

    if time <= MIN_TIME_TO_EXPIRY:
        return VolResult(None, VolReason.EXPIRED, price, time)
    if price <= 0:
        return VolResult(None, VolReason.PRICE_TOO_SMALL, price, time)

    lower, upper = price_bounds(option, spot, strike, time, rate, dividend_yield)
    if price <= lower:
        return VolResult(None, VolReason.BELOW_INTRINSIC, price, time)
    if price >= upper:
        return VolResult(None, VolReason.ABOVE_MAXIMUM, price, time)

    def objective(sigma: float) -> float:
        return bsm_price(option, spot, strike, time, rate, sigma, dividend_yield) - price

    low, high = objective(MIN_SIGMA), objective(MAX_SIGMA)
    if low > 0 or high < 0:
        # The price is inside the no arbitrage bounds but outside what this bracket
        # can express. Almost always a vol above 500 percent, which is not a market.
        return VolResult(None, VolReason.OUTSIDE_BRACKET, price, time)

    try:
        sigma = brentq(objective, MIN_SIGMA, MAX_SIGMA, xtol=1e-8, rtol=1e-10, maxiter=200)
    except (ValueError, RuntimeError):
        return VolResult(None, VolReason.NO_SOLUTION, price, time)

    vega = bsm_greeks(option, spot, strike, time, rate, float(sigma), dividend_yield).vega
    if vega < min_vega:
        return VolResult(None, VolReason.NOT_IDENTIFIABLE, price, time)

    return VolResult(float(sigma), VolReason.OK, price, time)


def screen_quote(
    contract: OptionContract,
    *,
    max_spread_pct: float = DEFAULT_MAX_SPREAD_PCT,
    min_price: float = DEFAULT_MIN_PRICE,
) -> VolReason:
    """Decide whether a contract's quote is worth solving from.

    Returns OK, or the first reason it fails. Order matters: a crossed market is a
    more useful thing to report than a wide one, and both are more useful than
    "no solution" from the solver later.
    """
    if contract.is_crossed:
        return VolReason.CROSSED
    if not contract.has_two_sided_market:
        return VolReason.NO_TWO_SIDED_MARKET

    mid = contract.mid
    if mid is None or mid < min_price:
        return VolReason.PRICE_TOO_SMALL

    spread_pct = contract.spread_pct_of_mid
    if spread_pct is not None and spread_pct > max_spread_pct:
        return VolReason.SPREAD_TOO_WIDE

    return VolReason.OK


def contract_implied_vol(
    contract: OptionContract,
    spot: float,
    asof: datetime,
    *,
    rate: float,
    dividend_yield: float = 0.0,
    max_spread_pct: float = DEFAULT_MAX_SPREAD_PCT,
    min_price: float = DEFAULT_MIN_PRICE,
    min_vega: float = DEFAULT_MIN_VEGA,
) -> VolResult:
    """Screen a real contract and solve from its mid price.

    Mid rather than last: last can be hours old on a thin strike, and an implied vol
    computed from a stale trade against a live spot is a fabrication.
    """
    reason = screen_quote(contract, max_spread_pct=max_spread_pct, min_price=min_price)
    time = time_to_expiry(contract.expiry, asof)
    if reason is not VolReason.OK:
        return VolResult(None, reason, contract.mid, time)

    return implied_vol(
        contract.right,
        contract.mid,  # type: ignore[arg-type]  # screen_quote guarantees this is a float
        spot,
        contract.strike,
        time,
        rate,
        dividend_yield,
        min_vega,
    )


def atm_strike(strikes: list[float], spot: float) -> float | None:
    """The listed strike closest to spot. None for an empty ladder.

    Ties go to the lower strike, which only matters when spot sits exactly between
    two strikes and is chosen for determinism rather than for any market reason.
    """
    if not strikes:
        return None
    return min(sorted(strikes), key=lambda strike: (abs(strike - spot), strike))


def interpolate_atm_vol(
    strike_vols: dict[float, float],
    spot: float,
) -> float | None:
    """ATM implied vol, linearly interpolated between the two strikes bracketing spot.

    Interpolated rather than nearest strike, because on a 5 dollar strike ladder the
    nearest listed strike can be 2.50 away, and with any skew at all that is a
    meaningfully different vol. Falls back to the single nearest when spot is outside
    the ladder entirely.
    """
    usable = {strike: vol for strike, vol in strike_vols.items() if vol is not None and vol > 0}
    if not usable:
        return None

    strikes = sorted(usable)
    if spot <= strikes[0]:
        return usable[strikes[0]]
    if spot >= strikes[-1]:
        return usable[strikes[-1]]

    for lower, upper in pairwise(strikes):
        if lower <= spot <= upper:
            if upper == lower:
                return usable[lower]
            weight = (spot - lower) / (upper - lower)
            return usable[lower] * (1 - weight) + usable[upper] * weight

    return None


def chain_implied_vols(
    contracts: list[OptionContract],
    spot: float,
    asof: datetime,
    *,
    rate: float,
    dividend_yield: float = 0.0,
    max_spread_pct: float = DEFAULT_MAX_SPREAD_PCT,
    min_price: float = DEFAULT_MIN_PRICE,
    min_vega: float = DEFAULT_MIN_VEGA,
) -> dict[tuple[date, float, Right], VolResult]:
    """Solve a whole chain, keyed by expiry, strike, and right.

    Rejections are kept in the mapping rather than dropped, so a caller can report
    that 300 of 800 contracts were unusable and why, which is a fact about the
    underlying's liquidity worth surfacing.
    """
    results: dict[tuple[date, float, Right], VolResult] = {}
    for contract in contracts:
        results[(contract.expiry, contract.strike, contract.right)] = contract_implied_vol(
            contract,
            spot,
            asof,
            rate=rate,
            dividend_yield=dividend_yield,
            max_spread_pct=max_spread_pct,
            min_price=min_price,
            min_vega=min_vega,
        )
    return results
