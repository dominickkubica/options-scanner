"""Black-Scholes-Merton pricing and greeks.

Pure math: no I/O, no config, no globals. Every input is an argument.

## Conventions

Returned greeks use the units a trader reads on a screen, not the raw partial
derivatives, because mixing the two is the single most common way to be off by 100
or by 365:

- delta: change in option value per 1.00 move in the underlying
- gamma: change in delta per 1.00 move in the underlying
- theta: change in option value per calendar day
- vega:  change in option value per 1 volatility point (0.01 of sigma)
- rho:   change in option value per 1 rate point (0.01 of r)

Values are per share. Multiply by contract_size for per contract.

Time is ACT/365 calendar years. Not trading days: BSM's sigma is an annualized
calendar volatility and the discounting is calendar time. Trading day counts belong
in realized volatility estimation, not here.

## American exercise

Listed US equity and ETF options are American, and this model is European. The
difference is not uniform:

- Calls on a non dividend payer: no early exercise is ever optimal, so European and
  American values agree. This model is correct.
- Puts: early exercise can be optimal when deep in the money, so the true American
  put is worth more than this model says. The error grows with moneyness, rates, and
  time. For the short, out of the money puts this tool is built to rank, it is small.
- Calls on a dividend payer: early exercise can be optimal just before an ex dividend
  date, so this model understates them. Assignment risk around ex dividend is handled
  as an event flag rather than in the pricing.

The practical consequence: treat these greeks as accurate for OTM short premium and
increasingly optimistic as a position goes deep ITM. Phase 8 is where that assumption
gets tested against outcomes rather than argued about.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from optscan.models import Right

DAYS_PER_YEAR = 365.0
SECONDS_PER_YEAR = DAYS_PER_YEAR * 24 * 60 * 60

#: US equity options stop trading at 16:00 New York on their expiry date.
EXPIRY_HOUR_LOCAL = 16
EXPIRY_TIMEZONE = "America/New_York"

#: Below this many years to expiry the model is numerically useless: gamma explodes
#: and vega vanishes. About 30 seconds.
MIN_TIME_TO_EXPIRY = 1e-6

#: Sigma below this is treated as zero volatility, giving the deterministic forward.
MIN_SIGMA = 1e-9


@dataclass(frozen=True, slots=True)
class Greeks:
    """Option value and its sensitivities, in the units documented above."""

    price: float
    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float

    def scaled(self, contracts: float = 1.0, contract_size: int = 100) -> Greeks:
        """Per position rather than per share."""
        factor = contracts * contract_size
        return Greeks(
            price=self.price * factor,
            delta=self.delta * factor,
            gamma=self.gamma * factor,
            theta=self.theta * factor,
            vega=self.vega * factor,
            rho=self.rho * factor,
        )


def norm_cdf(x: float) -> float:
    """Standard normal CDF, via erf. Accurate to double precision and fast."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    """Standard normal density."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def expiry_moment(expiry: date, timezone: str = EXPIRY_TIMEZONE) -> datetime:
    """The instant an expiry stops trading, as a UTC datetime."""
    local = datetime(
        expiry.year,
        expiry.month,
        expiry.day,
        EXPIRY_HOUR_LOCAL,
        tzinfo=ZoneInfo(timezone),
    )
    return local.astimezone(UTC)


def time_to_expiry(
    expiry: date,
    asof: datetime,
    timezone: str = EXPIRY_TIMEZONE,
) -> float:
    """Years to expiry, ACT/365, measured to the 16:00 local close on the expiry date.

    Returns 0.0 once the expiry has passed. Intraday precision matters: on expiry day
    a whole day of time value is the difference between a contract worth something and
    one worth nothing, and rounding a 0DTE to zero or to one day is wrong in both
    directions.
    """
    if asof.tzinfo is None:
        raise ValueError("asof must be timezone aware")
    seconds = (expiry_moment(expiry, timezone) - asof.astimezone(UTC)).total_seconds()
    return max(seconds / SECONDS_PER_YEAR, 0.0)


def d1_d2(
    spot: float,
    strike: float,
    time: float,
    rate: float,
    sigma: float,
    dividend_yield: float = 0.0,
) -> tuple[float, float]:
    """The two BSM terms. Raises on inputs where they are undefined."""
    if spot <= 0:
        raise ValueError(f"spot must be positive, got {spot}")
    if strike <= 0:
        raise ValueError(f"strike must be positive, got {strike}")
    if time <= 0:
        raise ValueError(f"time must be positive, got {time}")
    if sigma <= 0:
        raise ValueError(f"sigma must be positive, got {sigma}")

    variance = sigma * math.sqrt(time)
    first = (
        math.log(spot / strike) + (rate - dividend_yield + 0.5 * sigma * sigma) * time
    ) / variance
    return first, first - variance


def intrinsic_value(right: Right | str, spot: float, strike: float) -> float:
    """Payoff if exercised right now, ignoring time value."""
    if Right.parse(right) is Right.CALL:
        return max(spot - strike, 0.0)
    return max(strike - spot, 0.0)


def bsm_price(
    right: Right | str,
    spot: float,
    strike: float,
    time: float,
    rate: float,
    sigma: float,
    dividend_yield: float = 0.0,
) -> float:
    """Black-Scholes-Merton value per share.

    Degenerate inputs return the discounted deterministic payoff rather than raising,
    because an expired or zero vol contract is a normal thing to be asked about.
    """
    option = Right.parse(right)

    if time <= MIN_TIME_TO_EXPIRY or sigma <= MIN_SIGMA:
        forward = spot * math.exp(-dividend_yield * max(time, 0.0))
        discounted_strike = strike * math.exp(-rate * max(time, 0.0))
        if option is Right.CALL:
            return max(forward - discounted_strike, 0.0)
        return max(discounted_strike - forward, 0.0)

    first, second = d1_d2(spot, strike, time, rate, sigma, dividend_yield)
    carry = math.exp(-dividend_yield * time)
    discount = math.exp(-rate * time)

    if option is Right.CALL:
        return spot * carry * norm_cdf(first) - strike * discount * norm_cdf(second)
    return strike * discount * norm_cdf(-second) - spot * carry * norm_cdf(-first)


def bsm_greeks(
    right: Right | str,
    spot: float,
    strike: float,
    time: float,
    rate: float,
    sigma: float,
    dividend_yield: float = 0.0,
) -> Greeks:
    """Price and all five greeks in one pass, sharing the expensive terms."""
    option = Right.parse(right)

    if time <= MIN_TIME_TO_EXPIRY or sigma <= MIN_SIGMA:
        return _degenerate_greeks(option, spot, strike, time, rate, sigma, dividend_yield)

    first, second = d1_d2(spot, strike, time, rate, sigma, dividend_yield)
    carry = math.exp(-dividend_yield * time)
    discount = math.exp(-rate * time)
    root_time = math.sqrt(time)
    density = norm_pdf(first)

    gamma = carry * density / (spot * sigma * root_time)
    vega_annual = spot * carry * density * root_time

    if option is Right.CALL:
        price = spot * carry * norm_cdf(first) - strike * discount * norm_cdf(second)
        delta = carry * norm_cdf(first)
        theta_annual = (
            -spot * carry * density * sigma / (2.0 * root_time)
            - rate * strike * discount * norm_cdf(second)
            + dividend_yield * spot * carry * norm_cdf(first)
        )
        rho_annual = strike * time * discount * norm_cdf(second)
    else:
        price = strike * discount * norm_cdf(-second) - spot * carry * norm_cdf(-first)
        delta = -carry * norm_cdf(-first)
        theta_annual = (
            -spot * carry * density * sigma / (2.0 * root_time)
            + rate * strike * discount * norm_cdf(-second)
            - dividend_yield * spot * carry * norm_cdf(-first)
        )
        rho_annual = -strike * time * discount * norm_cdf(-second)

    return Greeks(
        price=price,
        delta=delta,
        gamma=gamma,
        theta=theta_annual / DAYS_PER_YEAR,
        vega=vega_annual / 100.0,
        rho=rho_annual / 100.0,
    )


def _degenerate_greeks(
    option: Right,
    spot: float,
    strike: float,
    time: float,
    rate: float,
    sigma: float,
    dividend_yield: float,
) -> Greeks:
    """Expired or zero volatility: the payoff is deterministic, so only delta survives.

    Delta at the money is genuinely undefined here (the payoff has a kink), and 0.5 is
    the conventional split rather than a computed value.
    """
    price = bsm_price(option, spot, strike, time, rate, sigma, dividend_yield)
    if spot == strike:
        delta = 0.5 if option is Right.CALL else -0.5
    elif option is Right.CALL:
        delta = 1.0 if spot > strike else 0.0
    else:
        delta = -1.0 if spot < strike else 0.0
    return Greeks(price=price, delta=delta, gamma=0.0, theta=0.0, vega=0.0, rho=0.0)


def forward_price(
    spot: float,
    time: float,
    rate: float,
    dividend_yield: float = 0.0,
) -> float:
    """Cost of carry forward. The center of the risk neutral distribution."""
    return spot * math.exp((rate - dividend_yield) * time)
