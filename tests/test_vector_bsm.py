"""The vectorised pricing agrees with the scalar pricing it restates.

Two implementations of one formula is the hazard test_rules.py describes: the day they
drift, the study prices one way and the screener another, and nothing fails loudly. So
the array versions are checked against `greeks.py` and `iv.py` over a grid wide enough
to reach the wings and the last hour of a 0DTE, where the prints this is built for live.
"""

from __future__ import annotations

import itertools
import math

import numpy as np
import pytest

from optscan.analytics import vector_bsm
from optscan.analytics.greeks import bsm_greeks, bsm_price
from optscan.analytics.iv import implied_vol

RATE = 0.043
HOUR = 1 / (365 * 24)
TIMES = (HOUR, 6 * HOUR, 1 / 365, 7 / 365, 45 / 365, 1.0)
STRIKES = (80.0, 95.0, 100.0, 103.0, 120.0)
SIGMAS = (0.08, 0.25, 0.9)
GRID = list(itertools.product((True, False), STRIKES, TIMES, SIGMAS))


def test_prices_match_the_scalar_model() -> None:
    calls, strikes, times, sigmas = (np.array(column) for column in zip(*GRID, strict=True))
    vector = vector_bsm.bsm_price(calls, 100.0, strikes, times, RATE, sigmas)
    for value, (call, strike, time, sigma) in zip(vector, GRID, strict=True):
        scalar = bsm_price("C" if call else "P", 100.0, strike, time, RATE, sigma)
        assert value == pytest.approx(scalar, rel=1e-10, abs=1e-12)


def test_deltas_match_the_scalar_model() -> None:
    calls, strikes, times, sigmas = (np.array(column) for column in zip(*GRID, strict=True))
    vector = vector_bsm.bsm_delta(calls, 100.0, strikes, times, RATE, sigmas)
    for value, (call, strike, time, sigma) in zip(vector, GRID, strict=True):
        scalar = bsm_greeks("C" if call else "P", 100.0, strike, time, RATE, sigma).delta
        assert value == pytest.approx(scalar, rel=1e-9, abs=1e-12)


def test_vega_is_per_volatility_point() -> None:
    vector = vector_bsm.bsm_vega(100.0, 103.0, 7 / 365, RATE, 0.25)
    assert float(vector) == pytest.approx(bsm_greeks("C", 100.0, 103.0, 7 / 365, RATE, 0.25).vega)


def test_implied_vol_solves_and_refuses_where_the_scalar_solver_does() -> None:
    """Prices from the model, then back to sigma. Where the scalar solver refuses a
    far wing as unidentifiable, the vector solver must refuse too, or noise gets into
    the smile."""
    calls, strikes, times, sigmas = (np.array(column) for column in zip(*GRID, strict=True))
    prices = vector_bsm.bsm_price(calls, 100.0, strikes, times, RATE, sigmas)
    solved = vector_bsm.implied_vol(calls, prices, 100.0, strikes, times, RATE)
    for value, price, (call, strike, time, _) in zip(solved, prices, GRID, strict=True):
        scalar = implied_vol("C" if call else "P", float(price), 100.0, strike, time, RATE)
        if scalar.ok:
            assert value == pytest.approx(scalar.sigma, rel=1e-6)
        else:
            assert math.isnan(value), f"scalar refused ({scalar.reason}) and vector solved"


@pytest.mark.parametrize(
    ("price", "strike", "time"),
    [
        (0.0, 100.0, 1 / 365),  # nothing to solve from
        (1.0, 90.0, 1 / 365),  # a 90 put worth 1.00 with spot at 100 is fine...
        (9.0, 110.0, 1 / 365),  # ...but a 110 put below its 10.00 intrinsic is not
        (1.0, 100.0, 0.0),  # expired
        (150.0, 100.0, 1 / 365),  # above the strike's present value
    ],
)
def test_the_refusals(price: float, strike: float, time: float) -> None:
    vector = float(vector_bsm.implied_vol(False, price, 100.0, strike, time, RATE))
    scalar = implied_vol("P", price, 100.0, strike, time, RATE)
    assert math.isnan(vector) == (not scalar.ok)
