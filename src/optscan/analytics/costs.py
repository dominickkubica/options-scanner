"""What it actually costs to trade a symbol, estimated from the bars already stored.

Every backtest in this project has been charging a flat ten basis points a round trip,
which was a guess. That guess is load bearing: the strongest result found so far is a
short-horizon reversion worth roughly seventy to a hundred basis points a trade, so if
the true cost is thirty rather than ten, most of it is gone. A number that decides
whether a finding survives should not be a placeholder.

## Why an estimator rather than real quotes

No historical bid-ask spread is stored. Alpaca does publish NBBO, but pulling ten years
of quotes for three hundred symbols to answer one question is the wrong trade, and the
quotes would still need converting into a per-trade cost.

The high-low estimators solve exactly this. Corwin and Schultz (2012) start from an
observation about what a daily bar contains: the high is very likely a buy at the ask and
the low a sell at the bid, so a single day's high-low range holds both the true
volatility and one spread, while a two day range holds twice the volatility and still
only one spread. Volatility scales with time and the spread does not, so the two can be
separated.

## What the estimate is and is not

It is a **quoted** proportional spread, and a round trip that crosses it on entry and
again on exit pays about one full spread. `round_trip_cost` therefore returns the spread
itself rather than half of it.

It is not the whole cost. It excludes market impact, which matters for size this project
will never trade, and it excludes commissions, which are zero at the broker in use. It
also ignores that opening auctions are wider than mid-session, and every backtest here
fills at the open, so the honest reading is that this **understates** the cost of these
particular strategies.

Negative estimates are set to zero before averaging, which is what the authors specify.
The estimator is noisy day to day and only meaningful averaged over months.

## How wrong it is, and in which direction

Measured against names whose real spreads are known: it puts GLD at 13.6 basis points
where the true figure is nearer one, and HYG at 8.3 where the truth is a basis point or
two. So on very liquid instruments it **overstates**, because the flooring of negative
estimates removes the left tail of a noisy distribution centred near zero.

On illiquid names it is far more useful, and that is where the question actually bites: a
uranium microcap coming out at 200 basis points is not being flattered by truncation,
because its spread genuinely dominates its noise. Read the ordering as reliable and the
absolute level as an upper bound that gets tighter as the symbol gets thinner.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from optscan.models import PriceBar

#: Bars needed before an estimate is worth reporting. The daily estimator is very noisy;
#: a quarter of sessions is the point where the average settles down.
MIN_BARS = 60

#: Constant from the Corwin-Schultz derivation: 3 - 2*sqrt(2).
_K = 3 - 2 * math.sqrt(2)

#: Spreads above this are not a spread, they are a broken bar. A 25% quoted spread on a
#: listed equity means a split, a halt, or bad data, and averaging it in would swamp
#: everything around it.
MAX_CREDIBLE_SPREAD = 0.25

#: Share of daily estimates that come out negative and get floored. Reported, never used
#: to reject. Negative daily values are an expected feature of this estimator rather than
#: a fault: a two day range is only a little larger than a one day range when the spread
#: is small, so noise pushes the difference below zero routinely. Measured here, every
#: one of 188 symbols sits between 37% and 63%, and the *tightest* names are the most
#: negative, which is the diagnostic working: a small spread is the hardest to resolve.
TYPICAL_NEGATIVE_SHARE = (0.35, 0.65)


@dataclass(frozen=True, slots=True)
class SpreadEstimate:
    """A symbol's estimated proportional spread, and how much it rests on."""

    symbol: str
    spread: float
    sessions: int
    #: Share of daily estimates that came out negative before flooring. High values mean
    #: the estimator is struggling on this symbol rather than finding a tight spread.
    negative_share: float

    @property
    def round_trip_cost(self) -> float:
        """Crossing the spread on the way in and again on the way out."""
        return self.spread

    @property
    def basis_points(self) -> float:
        return self.spread * 10_000

    @property
    def trustworthy(self) -> bool:
        # Only the sample size gates. An earlier version also rejected anything above 35%
        # negative and threw away all 188 symbols, because that share is normal for this
        # estimator rather than a sign of trouble.
        return self.sessions >= MIN_BARS


def corwin_schultz_daily(first: PriceBar, second: PriceBar) -> float | None:
    """One two-day spread estimate, or None if the pair cannot produce one.

    beta is the sum of the two single-day squared log ranges, which contains two days of
    volatility and two spreads. gamma is the squared log range across both days together,
    which contains two days of volatility and one spread. The difference isolates the
    spread.
    """
    if min(first.low, second.low) <= 0:
        return None
    if first.high < first.low or second.high < second.low:
        return None

    beta = math.log(first.high / first.low) ** 2 + math.log(second.high / second.low) ** 2
    high = max(first.high, second.high)
    low = min(first.low, second.low)
    gamma = math.log(high / low) ** 2

    alpha = (math.sqrt(2 * beta) - math.sqrt(beta)) / _K - math.sqrt(gamma / _K)
    spread = 2 * (math.exp(alpha) - 1) / (1 + math.exp(alpha))
    if not math.isfinite(spread):
        return None
    return spread


def estimate_spread(symbol: str, bars: Sequence[PriceBar]) -> SpreadEstimate | None:
    """Average proportional spread across consecutive session pairs.

    Negative daily estimates are floored at zero rather than dropped. Dropping them keeps
    only the upward noise and biases the average high, which for a cost estimate is the
    wrong direction to be wrong in twice over: it would make every strategy look worse
    than it is and hide the ones that work.
    """
    if len(bars) < MIN_BARS:
        return None

    values: list[float] = []
    negatives = 0
    for index in range(len(bars) - 1):
        estimate = corwin_schultz_daily(bars[index], bars[index + 1])
        if estimate is None:
            continue
        if estimate < 0:
            negatives += 1
            estimate = 0.0
        if estimate <= MAX_CREDIBLE_SPREAD:
            values.append(estimate)

    if len(values) < MIN_BARS:
        return None

    return SpreadEstimate(
        symbol=symbol,
        spread=sum(values) / len(values),
        sessions=len(values),
        negative_share=negatives / len(values),
    )


def blended_cost(estimates: Sequence[SpreadEstimate], floor: float = 0.0005) -> float:
    """One cost for a portfolio of symbols: the mean of the trustworthy estimates.

    A floor because the estimator can return implausibly small numbers on very liquid
    names, and no round trip is free even there. Five basis points is roughly a penny on
    a twenty dollar stock.
    """
    usable = [e.round_trip_cost for e in estimates if e.trustworthy]
    if not usable:
        return floor
    return max(floor, sum(usable) / len(usable))
