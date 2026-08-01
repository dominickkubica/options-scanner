"""Position and portfolio risk: profit, greeks, and aggregate exposure.

Pure. Marks and greeks arrive as arguments; fetching them is somebody else's job.

## Greeks aggregate, but only in the units they aggregate in

Delta adds across positions in share terms and nowhere else. A 0.30 delta on a 740
dollar index and a 0.30 delta on a 40 dollar name are the same number and completely
different exposures, so everything here converts to shares first and reports the raw
per contract greek only alongside its position.

Theta is reported per calendar day and vega per volatility point, matching greeks.py.
Those are the units a person manages in, and they are not the units the maths produces,
which is why the convention is stated in one place and followed everywhere.

## Beta weighting is the honest version of "net delta"

Adding delta across symbols pretends a share of one thing equals a share of another. It
does not. Beta weighting converts each position's exposure into the equivalent exposure
in a reference symbol, usually SPY, which is the only way a portfolio level delta means
anything at all.

It brings its own problems and they are stated rather than hidden:

- **Beta is estimated, not known.** It comes from a regression over a finite window and
  it moves. `beta` below returns the window and the observation count with the number,
  and refuses outright below a minimum sample rather than returning a figure computed
  from three weeks of data.
- **Beta is a linear fit, and the tail is where it fails.** Correlations go to one in a
  crash, which is precisely when a beta weighted delta is being relied on to size risk.
  A portfolio that looks flat on beta weighted delta is not hedged against a market
  wide move; it is hedged against the average relationship of the last year.
- **Beta weighting a short option position is doubly indirect,** because the position's
  delta is itself a local derivative that changes as spot moves. The result is a
  first order estimate of a first order estimate, useful for spotting that a book is
  heavily one way and not for deciding a hedge ratio to two decimal places.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from itertools import pairwise

from optscan.models import PriceBar, Right
from optscan.models.position import Position, PositionLeg

#: Fewest overlapping returns before a beta is published. Sixty sessions is about a
#: quarter, which is the shortest window anyone quotes, and below it the standard error
#: on the slope is large enough that the estimate carries no information.
MIN_BETA_OBSERVATIONS = 60

#: Beyond this the fit is almost certainly picking up a data problem rather than a
#: relationship. A single mismatched split between the two series produces betas in the
#: tens, and a silently absurd beta poisons the portfolio total it feeds.
MAX_PLAUSIBLE_BETA = 5.0

#: Below this the symbol barely tracks the reference, so beta weighting its delta
#: says less than the number suggests. Reported as a caveat rather than a refusal.
LOOSE_FIT_R_SQUARED = 0.25


@dataclass(frozen=True, slots=True)
class Beta:
    """A regression slope against a reference symbol, with its own sample size."""

    #: None when the sample was too small to estimate one. Not 1.0: a default of 1.0
    #: is a measured looking number for an unmeasured thing, and it would quietly weight
    #: a thinly traded name as if it moved exactly with the index.
    value: float | None
    observations: int
    span_days: int
    r_squared: float
    reference: str

    @property
    def usable(self) -> bool:
        return self.value is not None and self.observations >= MIN_BETA_OBSERVATIONS

    def caveat(self) -> str | None:
        """A sentence for the UI when the fit does not support much weight."""
        if self.observations < MIN_BETA_OBSERVATIONS:
            return (
                f"Only {self.observations} overlapping sessions against "
                f"{self.reference}, which is too few to estimate a beta."
            )
        if self.r_squared < LOOSE_FIT_R_SQUARED:
            return (
                f"This symbol only tracks {self.reference} loosely "
                f"(r squared {self.r_squared:.2f}), so beta weighting its delta says "
                "less than the number suggests."
            )
        return None


@dataclass(frozen=True, slots=True)
class LegRisk:
    """One leg's contribution, already converted to share terms."""

    leg: PositionLeg
    mark: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    iv: float | None = None

    def _scaled(self, greek: float | None) -> float | None:
        """A per share greek in position terms: signed, and times the share count.

        The sign is the one thing that is easy to get wrong and impossible to see
        afterwards. A short call has positive delta per contract and negative delta as
        a position, and a book that added the first would read as long when it is short.
        """
        if greek is None:
            return None
        return -self.leg.direction * greek * self.leg.shares

    @property
    def position_delta(self) -> float | None:
        return self._scaled(self.delta)

    @property
    def position_gamma(self) -> float | None:
        return self._scaled(self.gamma)

    @property
    def position_theta(self) -> float | None:
        return self._scaled(self.theta)

    @property
    def position_vega(self) -> float | None:
        return self._scaled(self.vega)

    @property
    def extrinsic(self) -> float | None:
        """Time value left in the leg. What early assignment risk is measured against."""
        return self.mark


@dataclass(frozen=True, slots=True)
class PositionRisk:
    """One position, marked and measured."""

    position: Position
    spot: float | None
    legs: tuple[LegRisk, ...]
    asof: date
    marks_complete: bool
    beta: Beta | None = None
    notes: tuple[str, ...] = ()

    @property
    def unrealized(self) -> float | None:
        if not self.marks_complete:
            return None
        return self.position.unrealized(self._marks())

    @property
    def profit_fraction(self) -> float | None:
        if not self.marks_complete:
            return None
        return self.position.profit_fraction(self._marks())

    def _marks(self) -> dict[tuple[float, Right], float | None]:
        return {(item.leg.strike, item.leg.right): item.mark for item in self.legs}

    def _sum(self, attribute: str) -> float | None:
        """Add a greek across legs, or refuse if any leg is missing it.

        Refusing is the point. A four legged condor summed over the three legs that
        solved reports a delta that is not the position's delta, and it is off in the
        direction of whichever leg failed, which is usually the far wing.
        """
        total = 0.0
        for item in self.legs:
            value = getattr(item, attribute)
            if value is None:
                return None
            total += value
        return total

    @property
    def delta(self) -> float | None:
        return self._sum("position_delta")

    @property
    def gamma(self) -> float | None:
        return self._sum("position_gamma")

    @property
    def theta(self) -> float | None:
        return self._sum("position_theta")

    @property
    def vega(self) -> float | None:
        return self._sum("position_vega")

    @property
    def beta_weighted_delta(self) -> float | None:
        """Reference equivalent exposure, in dollars.

        Share delta times spot gives the position's dollar exposure; times beta
        restates it as the dollar exposure of the reference symbol that would move the
        same way. Dollars rather than reference shares, so the number does not go stale
        when the reference's own price moves and so it adds across symbols without
        needing the reference quoted at all.
        """
        delta = self.delta
        if delta is None or self.beta is None or not self.beta.usable or self.spot is None:
            return None
        return delta * self.beta.value * self.spot  # type: ignore[operator]

    @property
    def tested(self) -> bool:
        return self.spot is not None and self.position.tested(self.spot)

    @property
    def dte(self) -> int:
        return self.position.dte(self.asof)


@dataclass(frozen=True, slots=True)
class PortfolioRisk:
    """Every open position, and what they add up to."""

    positions: tuple[PositionRisk, ...]
    asof: date
    reference: str = "SPY"
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def unrealized(self) -> float:
        """Total open profit across positions that could be marked.

        A sum rather than a refusal, unlike the greeks, because an incomplete profit
        total is still the profit of the positions in it and `unmarked` says how many
        are missing. A delta that silently omits a position is a different kind of
        wrong: it is used to decide a hedge.
        """
        return sum(item.unrealized or 0.0 for item in self.positions)

    @property
    def unmarked(self) -> int:
        return sum(1 for item in self.positions if not item.marks_complete)

    def _sum(self, attribute: str) -> float | None:
        values = [getattr(item, attribute) for item in self.positions]
        if any(value is None for value in values):
            return None
        return sum(values)  # type: ignore[arg-type]

    @property
    def delta(self) -> float | None:
        """Raw share delta. Only meaningful within one symbol, so mostly a diagnostic.

        Published anyway because its absence would be more confusing than its presence,
        and because for a single symbol book it is exactly the right number.
        """
        return self._sum("delta")

    @property
    def theta(self) -> float | None:
        """Dollars of decay per calendar day, if nothing moves. The premium seller's
        headline number, and the one most often quoted without the qualifier."""
        return self._sum("theta")

    @property
    def vega(self) -> float | None:
        return self._sum("vega")

    @property
    def gamma(self) -> float | None:
        return self._sum("gamma")

    @property
    def beta_weighted_delta(self) -> float | None:
        """Portfolio delta in reference symbol dollars. None if any position lacks one."""
        values = [item.beta_weighted_delta for item in self.positions]
        if not values or any(value is None for value in values):
            return None
        return sum(values)  # type: ignore[arg-type]

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted({item.position.symbol for item in self.positions}))


def beta(
    symbol_bars: Sequence[PriceBar],
    reference_bars: Sequence[PriceBar],
    *,
    reference: str = "SPY",
    min_observations: int = MIN_BETA_OBSERVATIONS,
) -> Beta | None:
    """Ordinary least squares slope of the symbol's returns on the reference's.

    Matched by session date rather than by position, because two vendors' series can
    disagree about holidays and a one day offset turns a beta of 1.0 into noise. Only
    dates present in both are used, and the count that survives is reported.

    Returns None rather than a number when there is too little overlap or the fit is
    degenerate. A beta computed from twenty sessions is not a weak beta, it is a
    different quantity with the same name.
    """
    symbol_returns = _returns_by_date(symbol_bars)
    reference_returns = _returns_by_date(reference_bars)
    shared = sorted(set(symbol_returns) & set(reference_returns))

    if len(shared) < min_observations:
        return Beta(
            value=None,
            observations=len(shared),
            span_days=(shared[-1] - shared[0]).days if len(shared) > 1 else 0,
            r_squared=0.0,
            reference=reference,
        )

    xs = [reference_returns[day] for day in shared]
    ys = [symbol_returns[day] for day in shared]
    count = len(shared)

    mean_x = sum(xs) / count
    mean_y = sum(ys) / count
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    variance_x = sum((x - mean_x) ** 2 for x in xs)
    if variance_x <= 0:
        return None

    slope = covariance / variance_x
    if not math.isfinite(slope) or abs(slope) > MAX_PLAUSIBLE_BETA:
        # Almost always a corporate action mismatch between the two series rather than
        # a real relationship, and a beta of 40 would swamp every other position.
        return None

    variance_y = sum((y - mean_y) ** 2 for y in ys)
    r_squared = (covariance**2) / (variance_x * variance_y) if variance_y > 0 else 0.0

    return Beta(
        value=slope,
        observations=count,
        span_days=(shared[-1] - shared[0]).days,
        r_squared=r_squared,
        reference=reference,
    )


def _returns_by_date(bars: Sequence[PriceBar]) -> dict[date, float]:
    """Log returns keyed by session date. Log rather than simple, so the regression is
    over a symmetric quantity and a 50 percent fall is the same size as the doubling
    that undoes it."""
    ordered = sorted((bar for bar in bars if bar.close > 0), key=lambda bar: bar.ts)
    returns: dict[date, float] = {}
    for previous, current in pairwise(ordered):
        returns[current.ts.date()] = math.log(current.close / previous.close)
    return returns
