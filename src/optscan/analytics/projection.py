"""Forward projections: expected move cones and terminal distributions.

The mirror of levels.py. That module says where price has been; this one says where the
options market currently thinks it might go, and draws the two on one chart so a strike
can be judged against both.

## The cone uses each expiry's own volatility

A cone drawn from a single volatility over a sqrt(t) curve is the version everybody
draws and it is wrong in a way that matters to anyone selling more than one expiry.
Implied vol is not flat across expiries: the whole reason surface.py exists is that the
term structure slopes, and it inverts around events. Projecting the front week's vol out
to ninety days therefore draws a cone that is too narrow at the back when the curve is
in contango and too wide when it is inverted, and the error is largest exactly around
the events a premium seller most wants to see.

So the cone here is a polyline through one point per expiry, each computed from that
expiry's own ATM implied vol. Between expiries it is a straight line, which is an
interpolation and is labelled as one, rather than a curve implying resolution the
inputs do not have.

## Lognormal, not symmetric

`ExpectedMove.band` gives a symmetric price band because that is what people read off a
chart. This module uses `lognormal_band` instead. Over a week the two are
indistinguishable; past about ninety days the asymmetry is visible and real, because a
stock cannot fall more than 100 percent and can rise without limit. A symmetric cone
drawn a year out puts its lower edge at a price the model assigns almost no probability
to, which looks like precision and is an artifact of the wrong band.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np

from optscan.analytics.greeks import MIN_TIME_TO_EXPIRY
from optscan.analytics.montecarlo import DEFAULT_PATHS, terminal_distribution

#: Standard deviations the cone is drawn at. One sigma is the conventional expected
#: move; two is where a premium seller's short strikes usually sit.
DEFAULT_DEVIATIONS = (1.0, 2.0)

#: Bins in the terminal distribution histogram. Enough to show the shape's skew
#: without turning sampling noise into structure.
DEFAULT_HISTOGRAM_BINS = 40

#: Fewest bins a histogram can meaningfully have.
MIN_HISTOGRAM_BINS = 2


@dataclass(frozen=True, slots=True)
class ConePoint:
    """One expiry's projected band, from that expiry's own implied vol."""

    expiry: date
    dte: int
    time: float
    sigma: float
    #: Band edges keyed by the number of standard deviations, so the caller can draw
    #: one sigma and two sigma without recomputing.
    bands: dict[float, tuple[float, float]]

    def band(self, deviations: float) -> tuple[float, float] | None:
        return self.bands.get(deviations)


@dataclass(frozen=True, slots=True)
class Cone:
    """The expected move cone across every expiry that could be priced."""

    spot: float
    asof: date
    points: tuple[ConePoint, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def deviations(self) -> tuple[float, ...]:
        return tuple(sorted(self.points[0].bands)) if self.points else ()

    def widest(self, deviations: float) -> tuple[float, float] | None:
        """Outer edges across the whole cone, for scaling an axis."""
        bands = [point.band(deviations) for point in self.points]
        usable = [band for band in bands if band is not None]
        if not usable:
            return None
        return min(low for low, _ in usable), max(high for _, high in usable)


@dataclass(frozen=True, slots=True)
class HistogramBin:
    low: float
    high: float
    probability: float

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0


@dataclass(frozen=True, slots=True)
class TerminalDistribution:
    """Simulated terminal prices, binned, with the quantiles that matter."""

    bins: tuple[HistogramBin, ...]
    paths: int
    median: float
    quantiles: dict[float, float]

    @property
    def mode(self) -> float | None:
        """Busiest bin's midpoint. The peak of a lognormal sits below its mean."""
        if not self.bins:
            return None
        return max(self.bins, key=lambda item: item.probability).mid


def build_cone(
    spot: float,
    asof: date,
    expiries: Sequence[tuple[date, float]],
    *,
    deviations: Sequence[float] = DEFAULT_DEVIATIONS,
    trading_days_per_year: float = 365.0,
) -> Cone:
    """A cone point per expiry, each from that expiry's own ATM implied vol.

    `expiries` is (expiry date, ATM implied vol). An expiry whose vol could not be
    solved is skipped and named in the notes rather than filled from a neighbour:
    interpolating a vol across a gap is exactly how a cone comes to imply a precision
    the chain never supported.

    Calendar days rather than trading days, matching the rest of this project's time to
    expiry convention, since options decay over weekends.
    """
    if spot <= 0:
        raise ValueError(f"spot must be positive, got {spot}")

    points: list[ConePoint] = []
    skipped: list[str] = []

    for expiry, sigma in sorted(expiries, key=lambda item: item[0]):
        dte = (expiry - asof).days
        time = dte / trading_days_per_year
        if sigma is None or sigma <= 0:
            skipped.append(f"{expiry} has no solved at the money volatility")
            continue
        if time <= MIN_TIME_TO_EXPIRY:
            skipped.append(f"{expiry} has already expired or expires today")
            continue

        bands: dict[float, tuple[float, float]] = {}
        for count in deviations:
            width = count * sigma * math.sqrt(time)
            bands[count] = (spot * math.exp(-width), spot * math.exp(width))

        points.append(ConePoint(expiry=expiry, dte=dte, time=time, sigma=sigma, bands=bands))

    notes: list[str] = []
    if skipped:
        notes.append(
            f"{len(skipped)} expiries are missing from the cone: {'; '.join(skipped)}. "
            "They are left out rather than interpolated from their neighbours."
        )
    if len(points) > 1:
        notes.append(
            "Each point uses its own expiry's implied volatility. The lines between "
            "points are straight interpolation, not a modelled path."
        )

    return Cone(spot=spot, asof=asof, points=tuple(points), notes=tuple(notes))


def terminal_histogram(
    spot: float,
    time: float,
    sigma: float,
    *,
    rate: float = 0.0,
    dividend_yield: float = 0.0,
    paths: int = DEFAULT_PATHS,
    bins: int = DEFAULT_HISTOGRAM_BINS,
    quantiles: Sequence[float] = (0.05, 0.25, 0.5, 0.75, 0.95),
    seed: int | None = 12345,
) -> TerminalDistribution | None:
    """Simulate terminal prices and bin them into a probability histogram.

    Seeded by default, so the same inputs draw the same picture. An unseeded histogram
    that shifts slightly on every page load invites the reader to see movement in the
    market that is only sampling noise.

    Returns None rather than an empty histogram when the inputs cannot support one,
    which is the same refusal convention the rest of the analytics use.
    """
    if spot <= 0 or time <= MIN_TIME_TO_EXPIRY or sigma <= 0 or bins < MIN_HISTOGRAM_BINS:
        return None

    prices = terminal_distribution(
        spot,
        time,
        sigma,
        rate=rate,
        dividend_yield=dividend_yield,
        paths=paths,
        seed=seed,
    )
    counts, edges = np.histogram(prices, bins=bins)
    total = counts.sum()
    if total <= 0:
        return None

    return TerminalDistribution(
        bins=tuple(
            HistogramBin(
                low=float(edges[index]),
                high=float(edges[index + 1]),
                probability=float(counts[index]) / float(total),
            )
            for index in range(len(counts))
        ),
        paths=int(prices.size),
        median=float(np.median(prices)),
        quantiles={float(q): float(np.quantile(prices, q)) for q in quantiles if 0.0 < q < 1.0},
    )
