"""Monte Carlo path simulation.

Used for the one number that has no closed form: P50, the probability of reaching
half of maximum profit at some point before expiry. Managing winners early is the
core mechanic of premium selling, so the probability of getting the chance to do it
is worth more than the probability of holding to expiry.

Deterministic given a seed. Every estimate carries its standard error, because an
unqualified "62 percent" from 2000 paths is a number with a plus or minus 1.1 on it
and the caller deserves to know that.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from optscan.analytics.greeks import DAYS_PER_YEAR, MIN_SIGMA, MIN_TIME_TO_EXPIRY, bsm_price
from optscan.models import Right

DEFAULT_PATHS = 4000
DEFAULT_STEPS_PER_DAY = 1


@dataclass(frozen=True, slots=True)
class MonteCarloEstimate:
    """A simulated probability and how much to trust it."""

    probability: float
    standard_error: float
    paths: int

    @property
    def interval_95(self) -> tuple[float, float]:
        """Roughly two standard errors either side. Report this, not the point estimate."""
        margin = 1.96 * self.standard_error
        return max(self.probability - margin, 0.0), min(self.probability + margin, 1.0)

    def __str__(self) -> str:
        return f"{self.probability:.1%} +/- {1.96 * self.standard_error:.1%}"


def simulate_paths(
    spot: float,
    time: float,
    sigma: float,
    *,
    rate: float = 0.0,
    dividend_yield: float = 0.0,
    paths: int = DEFAULT_PATHS,
    steps: int = 30,
    seed: int | None = None,
) -> np.ndarray:
    """GBM price paths, shape (paths, steps + 1), starting at spot.

    Exact lognormal stepping rather than an Euler discretization, so the step size
    only affects path resolution and never introduces drift error.
    """
    if spot <= 0:
        raise ValueError(f"spot must be positive, got {spot}")
    if time <= 0:
        raise ValueError(f"time must be positive, got {time}")
    if paths < 1 or steps < 1:
        raise ValueError("paths and steps must both be at least 1")

    rng = np.random.default_rng(seed)
    dt = time / steps
    drift = (rate - dividend_yield - 0.5 * sigma * sigma) * dt
    diffusion = sigma * np.sqrt(dt)

    shocks = rng.standard_normal((paths, steps))
    increments = drift + diffusion * shocks
    log_paths = np.cumsum(increments, axis=1)
    prices = spot * np.exp(log_paths)
    return np.hstack([np.full((paths, 1), spot), prices])


def terminal_distribution(
    spot: float,
    time: float,
    sigma: float,
    *,
    rate: float = 0.0,
    dividend_yield: float = 0.0,
    paths: int = DEFAULT_PATHS,
    seed: int | None = None,
) -> np.ndarray:
    """Just the terminal prices. One step, since nothing in between is needed."""
    return simulate_paths(
        spot,
        time,
        sigma,
        rate=rate,
        dividend_yield=dividend_yield,
        paths=paths,
        steps=1,
        seed=seed,
    )[:, -1]


def probability_of_target(
    right: Right | str,
    strike: float,
    credit: float,
    spot: float,
    time: float,
    sigma: float,
    *,
    target_fraction: float = 0.5,
    rate: float = 0.0,
    dividend_yield: float = 0.0,
    paths: int = DEFAULT_PATHS,
    steps_per_day: int = DEFAULT_STEPS_PER_DAY,
    seed: int | None = 12345,
) -> MonteCarloEstimate:
    """P(a short option can be closed at `target_fraction` of max profit before expiry).

    target_fraction of 0.5 is the conventional P50. Max profit on a short option is
    the credit, so the target is buying it back at credit * (1 - target_fraction).

    The option is re-priced at every step at the same constant sigma it was sold at.
    That is the model's biggest simplification: in reality the vol that lets you buy
    the option back cheaply is usually falling at the same time, so this understates
    P50 on a position sold into elevated IV. Understating is the safe direction.
    """
    option = Right.parse(right)
    if not 0 < target_fraction <= 1:
        raise ValueError("target_fraction must be in (0, 1]")
    if credit <= 0:
        raise ValueError("credit must be positive")

    if time <= MIN_TIME_TO_EXPIRY or sigma <= MIN_SIGMA:
        return MonteCarloEstimate(0.0, 0.0, 0)

    steps = max(round(time * DAYS_PER_YEAR * steps_per_day), 1)
    prices = simulate_paths(
        spot,
        time,
        sigma,
        rate=rate,
        dividend_yield=dividend_yield,
        paths=paths,
        steps=steps,
        seed=seed,
    )

    target_price = credit * (1.0 - target_fraction)
    reached = np.zeros(prices.shape[0], dtype=bool)

    for index in range(1, prices.shape[1]):
        remaining = time * (1.0 - index / steps)
        step_prices = prices[:, index]
        values = _price_vector(option, step_prices, strike, remaining, rate, sigma, dividend_yield)
        reached |= values <= target_price
        if reached.all():
            break

    hits = int(reached.sum())
    probability = hits / prices.shape[0]
    error = float(np.sqrt(probability * (1 - probability) / prices.shape[0]))
    return MonteCarloEstimate(probability, error, prices.shape[0])


def _price_vector(
    option: Right,
    spots: np.ndarray,
    strike: float,
    time: float,
    rate: float,
    sigma: float,
    dividend_yield: float,
) -> np.ndarray:
    """BSM across a vector of spots. Loops in python, which is fast enough here.

    Vectorizing this properly is the obvious optimization if P50 ever needs to run
    across a whole chain rather than a handful of shortlisted candidates.
    """
    return np.array(
        [bsm_price(option, float(s), strike, time, rate, sigma, dividend_yield) for s in spots]
    )
