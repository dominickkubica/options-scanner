"""Expected move.

Two ways to ask the same question, which are routinely compared incorrectly:

1. The ATM straddle price. What the market charges to own the move.
2. spot * IV * sqrt(t). What the model says one standard deviation is.

## The conversion, which the common rule of thumb gets wrong

Under the model these two are not the same quantity and there is an exact factor
between them. The ATM straddle equals the mean absolute move, and for a normal
distribution the mean absolute deviation is sqrt(2/pi) of the standard deviation:

    straddle = spot * IV * sqrt(t) * sqrt(2/pi) = one_sigma_move * 0.79788

That is a first order identity, exact in the limit of small sigma*sqrt(t) and off by
a percent or so once total volatility gets large. It gives the conversion factor
sqrt(pi/2) = 1.25331, not the widely quoted 0.85.

The inversion does not need the approximation at all. With rates set aside, an ATM
straddle is exactly 2 * spot * (2 * N(x/2) - 1) where x = sigma * sqrt(t), so

    one_sigma_move = 2 * spot * Phi_inverse((1 + straddle / (2 * spot)) / 2)

recovers the move exactly at any volatility. That is what this module uses. The
constant is kept only to document the relationship and to show what the folklore
multiplier is actually claiming.

The 0.85 rule of thumb goes the wrong direction: straddle * 0.85 lands at 0.68 of a
standard deviation. Comparing that against spot * IV * sqrt(t) produces a permanent
30 percent gap that has nothing to do with the market and swamps whatever real signal
the comparison was supposed to carry.

## What the disagreement actually means

Once both sides are the same quantity, the residual is informative. It is near zero
when the ATM vol and the ATM straddle describe the same distribution, which they
should when the vol was solved from those same options. A large positive value means
the straddle is richer than the ATM vol implies, and the usual causes are worth
distinguishing: an ATM vol interpolated across a skewed smile rather than read off the
straddle strike, a strike ladder too coarse to have anything near the money, or a
genuine event bid concentrated in the at the money options.

It is a consistency check first and an event detector second. Backwardation in the
term structure is the better event detector, and it lives in surface.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from scipy.stats import norm

from optscan.analytics.greeks import MIN_TIME_TO_EXPIRY

#: Mean absolute deviation of a normal, as a fraction of its standard deviation.
#: The ATM straddle sits at exactly this multiple of the one sigma move.
MEAN_ABSOLUTE_OVER_SIGMA = math.sqrt(2.0 / math.pi)  # 0.7978845608

#: The inverse. Multiply a straddle by this to get the market implied one sigma move.
STRADDLE_TO_ONE_SIGMA = 1.0 / MEAN_ABSOLUTE_OVER_SIGMA  # 1.2533141373

#: The rule of thumb seen in most retail material. Kept only so the difference can be
#: shown and explained. Not used for anything.
FOLKLORE_STRADDLE_MULTIPLIER = 0.85


@dataclass(frozen=True, slots=True)
class ExpectedMove:
    """The market's price of the move and the model's estimate of it.

    Three numbers, deliberately named for what they are:

    - expected_absolute_move: the straddle itself, which is the average size of the
      move the market is pricing. This is what "expected move" literally means and it
      is the one to draw on a chart.
    - market_sigma_move: that straddle converted to one standard deviation.
    - model_sigma_move: one standard deviation from spot, ATM vol, and time.
    """

    straddle_price: float | None
    model_sigma_move: float | None
    spot: float
    time: float

    @property
    def expected_absolute_move(self) -> float | None:
        """The average absolute move the market is pricing, which is the straddle."""
        return self.straddle_price

    @property
    def market_sigma_move(self) -> float | None:
        """One standard deviation implied by the straddle, inverted exactly."""
        return straddle_implied_sigma_move(self.straddle_price, self.spot)

    @property
    def model_absolute_move(self) -> float | None:
        """The model's average absolute move, for comparing against a straddle."""
        if self.model_sigma_move is None:
            return None
        return self.model_sigma_move * MEAN_ABSOLUTE_OVER_SIGMA

    @property
    def disagreement(self) -> float | None:
        """Market one sigma over model one sigma, minus one.

        Both sides are the same quantity, so a reading near zero is the healthy case
        rather than a coincidence. See the module docstring for how to read a large one.
        """
        market = self.market_sigma_move
        if not market or not self.model_sigma_move:
            return None
        return market / self.model_sigma_move - 1.0

    @property
    def best_sigma_move(self) -> float | None:
        """The straddle derived one sigma when there is one, else the model's.

        A transactable price beats a model output. The model is the fallback for
        expiries with no quotable at the money straddle.
        """
        return (
            self.market_sigma_move
            if self.market_sigma_move is not None
            else (self.model_sigma_move)
        )

    def band(self, deviations: float = 1.0) -> tuple[float, float] | None:
        """Symmetric price band, `deviations` standard deviations wide.

        Symmetric in price terms, which is what traders read off a chart, even though
        the underlying distribution is lognormal and therefore not symmetric. Use
        lognormal_band when the asymmetry matters, which it does past about 90 days.
        """
        size = self.best_sigma_move
        if size is None:
            return None
        return max(self.spot - size * deviations, 0.0), self.spot + size * deviations

    def lognormal_band(self, sigma: float, deviations: float = 1.0) -> tuple[float, float] | None:
        """Multiplicative band: spot * exp(+/- deviations * sigma * sqrt(t)).

        The honest version. The downside is compressed and the upside stretched,
        because a stock cannot fall more than 100 percent and can rise without limit.
        """
        if self.time <= MIN_TIME_TO_EXPIRY or sigma <= 0:
            return None
        width = deviations * sigma * math.sqrt(self.time)
        return self.spot * math.exp(-width), self.spot * math.exp(width)


def sigma_move(spot: float, sigma: float, time: float, deviations: float = 1.0) -> float | None:
    """One standard deviation move in price terms: spot * sigma * sqrt(t)."""
    if spot <= 0 or sigma <= 0 or time <= MIN_TIME_TO_EXPIRY:
        return None
    return spot * sigma * math.sqrt(time) * deviations


def straddle_price(call_mid: float | None, put_mid: float | None) -> float | None:
    """ATM straddle. None if either leg is unquotable, never half a straddle."""
    if call_mid is None or put_mid is None or call_mid <= 0 or put_mid <= 0:
        return None
    return call_mid + put_mid


def straddle_implied_sigma_move(straddle: float | None, spot: float) -> float | None:
    """Invert an ATM straddle to the one sigma move it implies.

    Exact rather than approximate, by inverting the ATM straddle formula directly.
    Two assumptions remain and both are small over the horizons this tool screens:
    the strike is taken as spot rather than as the forward, and the discount rate is
    ignored. Both matter more the further out the expiry, and neither is worth the
    complexity at 45 days.

    Returns None when the straddle is too rich to invert, meaning it exceeds twice
    the spot, which is not a market.
    """
    if straddle is None or straddle <= 0 or spot <= 0:
        return None
    ratio = straddle / (2.0 * spot)
    if ratio >= 1.0:
        return None
    return float(2.0 * spot * norm.ppf((1.0 + ratio) / 2.0))


def straddle_implied_sigma(
    call_mid: float | None,
    put_mid: float | None,
    spot: float,
) -> float | None:
    """One sigma move implied by an ATM straddle's two legs."""
    return straddle_implied_sigma_move(straddle_price(call_mid, put_mid), spot)


def expected_move(
    spot: float,
    time: float,
    *,
    atm_iv: float | None = None,
    call_mid: float | None = None,
    put_mid: float | None = None,
) -> ExpectedMove:
    """Both estimates, whichever of them the inputs support."""
    return ExpectedMove(
        straddle_price=straddle_price(call_mid, put_mid),
        model_sigma_move=sigma_move(spot, atm_iv, time) if atm_iv else None,
        spot=spot,
        time=time,
    )
