"""Price levels from daily bars: where the underlying has actually spent time.

Backward looking by construction. Everything here describes what price has already
done; projection.py handles what it might do next.

## Why almost all of this file is filtering

Support and resistance is the single easiest place in this project to build a detector
that fires on everything, and this codebase has made that mistake enough times to
treat it as the default assumption rather than an edge case. Three specific traps live
here, and each one is why a function below takes a threshold rather than just
returning what it found:

- **Every bar is a pivot at some lookback.** A strict local maximum over a five bar
  window occurs roughly once every nine bars, so four years of SPY yields 123 of them.
  Drawn unfiltered that is not a set of levels, it is a hairball.
- **A touch count is a density, not a piece of evidence.** This is the one that took
  measuring to see. Scatter those 123 pivots across a 390 point range and cluster them
  in 4.5 point bands: about 1.45 land in each band by arithmetic alone. So "this level
  was touched twice" is not evidence of anything, and clustering with a touch floor
  publishes 31 levels of which almost all are counting noise that happens to share a
  price. The fix is a baseline: each cluster's touch count is compared against how many
  touches its own band would have collected by chance given how much time price
  actually spent there. On the real capture that takes 31 candidates down to 1.
- **A volume profile over a trending window finds the trend, not support.** Over four
  years in which SPY went from 400 to 740, the point of control lands at 413, down
  where the advance began. That is a fact about the path and not about where buyers
  wait. The window is therefore an argument, the default is short, and `volume_profile`
  states the bias in its own docstring rather than leaving the caller to find it.

## A filter that looked right and did nothing

`min_prominence_atr` was written to be the significance test and defaults to zero,
because measuring it showed it is not one. Requiring a swing to be deeper than some
multiple of ATR sounds like it selects real turning points; in practice the prominence
of a local extreme inside an eleven bar window *is* roughly an eleven bar range, which
is roughly one ATR by construction. Thresholding it in ATR units therefore thresholds a
constant: 123 pivots at 0.0, still 123 at 0.5, 118 at 0.75. Worse, turning it up far
enough to bite starved the test that does work, taking the surviving level count from 1
to 0. It is kept as an argument and documented as a trap rather than deleted, so the
next person to reach for it finds the measurement instead of repeating it.

## Strength is only comparable within a kind

Every level carries a `strength` in 0 to 1, and it means something different for each
kind: for a swing level it restates the p-value, for a volume node it is the bin's
share of the peak bin, for a round number it is a constant standing in for the fact
that nothing was measured at all. Sorting levels of different kinds against each other
by strength would be comparing three unrelated quantities that happen to share a scale.
`Levels.all()` returns them tagged rather than merged and ranked.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from itertools import pairwise

from scipy.stats import poisson

from optscan.models import PriceBar

#: Bars either side of a candidate that must not exceed it. Five trading days is one
#: week, which is the shortest swing most people would point at on a daily chart.
DEFAULT_PIVOT_LOOKBACK = 5

#: How deep a swing must be, in ATR, before it is a pivot rather than a wiggle.
#:
#: Off by default, and that is a measured decision rather than an oversight. On the
#: real SPY capture this filter does almost nothing at any setting a person would
#: reach for: 123 raw pivots at 0.0 ATR, 123 at 0.5, 118 at 0.75. The reason is
#: mechanical. The prominence of a local extreme inside an eleven bar window is the
#: depth of an eleven bar range, and an eleven bar range is about one ATR by
#: construction, so thresholding it in ATR units is thresholding a constant. It looks
#: like a significance filter and selects nothing.
#:
#: Turning it up does not rescue it, it makes things worse. At 1.5 ATR it cuts the
#: pivot count to 66, and since the significance test below needs touch counts to work
#: with, starving it that way took the surviving level count from 1 to 0. Two filters
#: aimed at the same thing, one principled and one confounded, and the confounded one
#: silently disabling the other. It is kept as an argument because a caller with a
#: specific reason may want it, and it defaults to off.
DEFAULT_MIN_PROMINENCE_ATR = 0.0

#: Pivots closer together than this many ATR are the same level seen more than once.
DEFAULT_CLUSTER_ATR = 0.5

#: Touches needed before a cluster is published at all. A floor, not the test: see
#: max_p_value. One touch is a place price turned once, which is not what anyone
#: means by support, so it is excluded before the arithmetic starts.
DEFAULT_MIN_TOUCHES = 2

#: How unlikely a cluster's touch count must be under the null before it is published.
#:
#: This is the filter that matters, and it exists because raw touch count measures
#: pivot density rather than significance. Scatter 123 pivots over a 390 point range
#: and ask how many land in a 4.5 point cluster: the answer is about 1.45 on average,
#: so finding 2 is not evidence of anything and finding 7 is. Without a baseline this
#: module publishes thirty levels on four years of SPY, most of which are counting
#: noise that happens to share a price.
DEFAULT_MAX_P_VALUE = 0.05

DEFAULT_ATR_PERIOD = 14
DEFAULT_BOLLINGER_PERIOD = 20
DEFAULT_BOLLINGER_DEVIATIONS = 2.0
#: ATRs either side of the EMA for a Keltner channel. 1.5 is the value the indicator's
#: author specified and the one the frontend draws.
DEFAULT_KELTNER_MULTIPLE = 1.5
DEFAULT_VOLUME_BINS = 60

#: A volume bin must reach this share of the busiest bin to count as a high volume
#: node. Without it every local bump in the histogram is a "node".
DEFAULT_MIN_NODE_SHARE = 0.55

#: Share of total traded volume that defines the value area around the point of
#: control. 70 percent is the market profile convention, roughly one standard
#: deviation if the distribution were normal.
DEFAULT_VALUE_AREA_SHARE = 0.70

#: Trading days in a year, for annualizing realized volatility.
TRADING_DAYS_PER_YEAR = 252

#: Two returns is the fewest that can have a deviation about their own mean.
MIN_RETURNS_FOR_DEVIATION = 2

#: Two bars and two bins is the fewest a histogram can be built from.
MIN_PROFILE_BARS = 2
MIN_PROFILE_BINS = 2

#: Price equality tolerance, for not drawing the point of control twice.
PRICE_EPSILON = 1e-9

#: Sessions in a realized volatility window. 30 is the conventional comparison against
#: 30 day implied vol, which is what IV rank is measured at.
DEFAULT_REALIZED_VOL_WINDOW = 30

#: Round number ladders, as a fraction of spot. A 740 dollar index wants lines every
#: 10 and 50; a 30 dollar name wants them every 1. Expressing the step as a share of
#: price rather than as a fixed dollar amount is what makes one rule work for both.
ROUND_NUMBER_STEPS = (0.01, 0.05, 0.10)


class LevelKind(StrEnum):
    """What produced a level. Strength is only comparable inside one of these."""

    SWING_HIGH = "swing_high"
    SWING_LOW = "swing_low"
    POINT_OF_CONTROL = "point_of_control"
    VOLUME_NODE = "volume_node"
    VALUE_AREA = "value_area"
    ROUND_NUMBER = "round_number"


@dataclass(frozen=True, slots=True)
class Level:
    """One horizontal line, and enough context to judge whether to believe it."""

    price: float
    kind: LevelKind
    #: 0 to 1, comparable only against other levels of the same kind. See the module
    #: docstring: these are three different quantities sharing one scale.
    strength: float
    touches: int = 0
    first_touch: date | None = None
    last_touch: date | None = None
    #: For swing levels, the probability of seeing at least this many touches by
    #: chance. Low is good. None for kinds where there is nothing to test: a round
    #: number has no touch count, and a volume node's height is not a count of events.
    p_value: float | None = None
    #: Touches expected by chance in a cluster this wide at this price. Carried so the
    #: UI can show "5 touches against 1.4 expected" rather than a bare 5, which reads
    #: as far more evidence than it is.
    expected_touches: float | None = None

    def distance_from(self, spot: float) -> float | None:
        """Signed distance as a fraction of spot. Positive means above."""
        if spot <= 0:
            return None
        return self.price / spot - 1.0


@dataclass(frozen=True, slots=True)
class Pivot:
    """A raw swing point, before clustering."""

    price: float
    when: date
    high: bool
    prominence: float


@dataclass(frozen=True, slots=True)
class BollingerBands:
    middle: float
    upper: float
    lower: float
    period: int
    deviations: float

    @property
    def width(self) -> float:
        """Band width as a fraction of the middle. The usual squeeze measure."""
        return (self.upper - self.lower) / self.middle if self.middle > 0 else 0.0


@dataclass(frozen=True, slots=True)
class KeltnerChannels:
    """An EMA with a band of ATRs either side.

    Kept next to BollingerBands because the pair is only interesting together: one
    measures the spread of closes, the other the size of the daily range, and the
    squeeze is the statement that the first has fallen inside the second.
    """

    middle: float
    upper: float
    lower: float
    period: int
    multiple: float


@dataclass(frozen=True, slots=True)
class VolumeProfile:
    """Where volume traded, binned by price."""

    edges: tuple[float, ...]
    volumes: tuple[float, ...]
    point_of_control: float
    value_area_low: float
    value_area_high: float

    def total_volume(self) -> float:
        return sum(self.volumes)


@dataclass(frozen=True, slots=True)
class Levels:
    """Everything backward looking about one symbol's recent price action."""

    spot: float
    atr: float | None = None
    realized_vol: float | None = None
    moving_averages: dict[int, float] = field(default_factory=dict)
    bollinger: BollingerBands | None = None
    profile: VolumeProfile | None = None
    swing: tuple[Level, ...] = ()
    volume: tuple[Level, ...] = ()
    round_numbers: tuple[Level, ...] = ()
    #: Stated rather than inferred: how many sessions the levels were built from.
    sessions: int = 0
    #: Swing candidates that were clustered and tested. `swing` holds the survivors.
    #: Both are reported because "no levels" and "no candidates" are different facts,
    #: and only one of them is a finding about the price action.
    swing_candidates: int = 0
    notes: tuple[str, ...] = ()

    def all(self) -> tuple[Level, ...]:
        """Every level, tagged by kind and deliberately not ranked against each other."""
        return self.swing + self.volume + self.round_numbers

    def nearest(self, price: float, *, above: bool) -> Level | None:
        """The closest level above or below a price, across every kind."""
        candidates = [
            level for level in self.all() if (level.price > price if above else level.price < price)
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda level: abs(level.price - price))


# --------------------------------------------------------------------------------
# Indicators
# --------------------------------------------------------------------------------


def true_ranges(bars: Sequence[PriceBar]) -> list[float]:
    """True range per bar, starting at the second one.

    Wilder's definition: the greatest of today's range, today's high against
    yesterday's close, and today's low against yesterday's close. The two gap terms are
    the point of it. A stock that gaps up and trades quietly has a small range and a
    large true range, and only the second describes the risk that was taken.
    """
    ranges: list[float] = []
    for previous, current in pairwise(bars):
        ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return ranges


def atr(bars: Sequence[PriceBar], period: int = DEFAULT_ATR_PERIOD) -> float | None:
    """Average true range, Wilder smoothed. None when there is not enough history.

    None rather than a partial average: an ATR computed from four bars is not a
    fourteen day ATR wearing the same name, and every threshold in this module is
    expressed as a multiple of it.
    """
    if period < 1:
        raise ValueError("period must be at least 1")
    ranges = true_ranges(bars)
    if len(ranges) < period:
        return None

    # Wilder's smoothing: seed with a simple mean, then a running update. Equivalent to
    # an exponential average with alpha = 1/period, and it is what every charting
    # package means by ATR, which matters when someone checks this against one.
    value = sum(ranges[:period]) / period
    for current in ranges[period:]:
        value = (value * (period - 1) + current) / period
    return value


def moving_average(bars: Sequence[PriceBar], period: int) -> float | None:
    """Simple moving average of closes. None when history is shorter than the period."""
    if period < 1:
        raise ValueError("period must be at least 1")
    if len(bars) < period:
        return None
    return sum(bar.close for bar in bars[-period:]) / period


def bollinger_bands(
    bars: Sequence[PriceBar],
    period: int = DEFAULT_BOLLINGER_PERIOD,
    deviations: float = DEFAULT_BOLLINGER_DEVIATIONS,
) -> BollingerBands | None:
    """Bands at the moving average plus and minus a multiple of the standard deviation.

    Population standard deviation, not sample. That is what the indicator's author
    specified and what charting packages compute, and at a 20 bar window the two differ
    by about 2.5 percent, which is enough to move a band visibly.
    """
    middle = moving_average(bars, period)
    if middle is None:
        return None

    closes = [bar.close for bar in bars[-period:]]
    variance = sum((close - middle) ** 2 for close in closes) / period
    spread = math.sqrt(variance) * deviations
    return BollingerBands(
        middle=middle,
        upper=middle + spread,
        lower=middle - spread,
        period=period,
        deviations=deviations,
    )


def ema(bars: Sequence[PriceBar], period: int) -> float | None:
    """Exponential moving average of closes. None when history is shorter than period.

    Seeded with the simple average of the first window rather than the first close, so
    the series does not spend its early bars converging from an arbitrary value. This
    matches the frontend's `compute.js` deliberately: the chart draws the channel and
    the alert fires on it, and the two disagreeing about where the band sits would be
    a bug nobody could see.
    """
    if period < 1:
        raise ValueError("period must be at least 1")
    if len(bars) < period:
        return None
    k = 2.0 / (period + 1)
    value = sum(bar.close for bar in bars[:period]) / period
    for bar in bars[period:]:
        value = bar.close * k + value * (1 - k)
    return value


def keltner_channels(
    bars: Sequence[PriceBar],
    period: int = DEFAULT_BOLLINGER_PERIOD,
    multiple: float = DEFAULT_KELTNER_MULTIPLE,
) -> KeltnerChannels | None:
    """An EMA centre with a band of `multiple` ATRs.

    None unless both the centre and the width exist. A centre line without a width is
    not a channel, and returning one would be a bare EMA wearing the Keltner label.
    """
    middle = ema(bars, period)
    width = atr(bars, period)
    if middle is None or width is None:
        return None
    return KeltnerChannels(
        middle=middle,
        upper=middle + multiple * width,
        lower=middle - multiple * width,
        period=period,
        multiple=multiple,
    )


def rsi(bars: Sequence[PriceBar], period: int = 14) -> float | None:
    """Relative strength index of the most recent bar, or None without enough history.

    Wilder's recursive smoothing, not a rolling mean. The distinction is not pedantry:
    a simple mean gives an RSI that disagrees with every other platform by a point or
    two, which is exactly the size of disagreement nobody investigates and everybody
    eventually trades on. The chart's JavaScript implementation is seeded and smoothed
    the same way so the two cannot report different numbers for the same bars.

    Returns the latest value only. Nothing here plots a series; the caller is asking
    whether a condition holds right now.
    """
    closes = [bar.close for bar in bars]
    if len(closes) < period + 1:
        return None

    gains: list[float] = []
    losses: list[float] = []
    for previous, current in pairwise(closes):
        change = current - previous
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period
    for index in range(period, len(gains)):
        average_gain = (average_gain * (period - 1) + gains[index]) / period
        average_loss = (average_loss * (period - 1) + losses[index]) / period

    # No losses in the window is the definition's limit rather than a divide by zero,
    # and it is a state a strong trend really reaches.
    if average_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1 + average_gain / average_loss)


def realized_volatility(
    bars: Sequence[PriceBar],
    window: int = DEFAULT_REALIZED_VOL_WINDOW,
    trading_days: int = TRADING_DAYS_PER_YEAR,
) -> float | None:
    """Annualized close to close realized volatility over the last `window` returns.

    The quantity to hold against implied vol when asking what the variance risk premium
    is paying. Close to close rather than a range based estimator such as Parkinson:
    range estimators are more efficient per observation, but implied vol is quoted as a
    close to close annualized standard deviation, so this is the like for like
    comparison and the efficiency is not worth the mismatch.

    Uses the sample standard deviation about the mean return. Some references drop the
    mean on the grounds that daily drift is negligible; over a 30 day window on a
    trending index it is not quite, and keeping it costs nothing.
    """
    if window < MIN_RETURNS_FOR_DEVIATION:
        raise ValueError("window must be at least 2 to have a deviation")
    closes = [bar.close for bar in bars if bar.close > 0]
    if len(closes) < window + 1:
        return None

    recent = closes[-(window + 1) :]
    returns = [math.log(b / a) for a, b in pairwise(recent)]
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    return math.sqrt(variance) * math.sqrt(trading_days)


# --------------------------------------------------------------------------------
# Swing pivots
# --------------------------------------------------------------------------------


def swing_pivots(
    bars: Sequence[PriceBar],
    lookback: int = DEFAULT_PIVOT_LOOKBACK,
    *,
    min_prominence: float = 0.0,
) -> list[Pivot]:
    """Local extremes that stand out by at least `min_prominence` in price terms.

    Prominence is the depth of the swing, not the height of the bar: for a swing high
    it is the distance down to the lowest low on either side within the window. That is
    what separates a turning point from a bar that happened to be the tallest of eleven
    in a quiet drift, and without it this returns roughly one pivot per eleven bars
    forever.

    The caller passes prominence in price units rather than in ATR so this stays a pure
    geometric function. `support_resistance` is what converts an ATR multiple into it.
    """
    if lookback < 1:
        raise ValueError("lookback must be at least 1")
    if len(bars) < 2 * lookback + 1:
        return []

    pivots: list[Pivot] = []
    for index in range(lookback, len(bars) - lookback):
        window = bars[index - lookback : index + lookback + 1]
        bar = bars[index]
        before = window[:lookback]
        after = window[lookback + 1 :]

        others = before + after
        # Strictly greater than every other bar in the window, not greater or equal.
        # With a tie allowed, a flat stretch makes every one of its bars both a swing
        # high and a swing low, which is the degenerate case a levels module must not
        # get wrong: it manufactures a wall of levels out of a market doing nothing.
        if bar.high > max(other.high for other in others):
            trough = max(
                min(other.low for other in before),
                min(other.low for other in after),
            )
            prominence = bar.high - trough
            if prominence >= min_prominence:
                pivots.append(
                    Pivot(price=bar.high, when=bar.ts.date(), high=True, prominence=prominence)
                )

        if bar.low < min(other.low for other in others):
            peak = min(
                max(other.high for other in before),
                max(other.high for other in after),
            )
            prominence = peak - bar.low
            if prominence >= min_prominence:
                pivots.append(
                    Pivot(price=bar.low, when=bar.ts.date(), high=False, prominence=prominence)
                )

    return pivots


def cluster_pivots(
    pivots: Sequence[Pivot],
    tolerance: float,
    *,
    min_touches: int = DEFAULT_MIN_TOUCHES,
) -> list[Level]:
    """Group pivots within `tolerance` of each other into single levels.

    Five pivots inside a dollar of each other are one level that price has tested five
    times, not five levels. Collapsing them is what turns a hairball into something
    worth drawing, and the touch count that falls out is the honest strength measure:
    it is literally how many times price turned there.

    Clusters are grown greedily from the lowest price upward. A cluster admits a pivot
    while it stays within `tolerance` of the cluster's running mean, so a long shallow
    drift of pivots does not chain into one implausibly wide level.
    """
    if tolerance <= 0:
        raise ValueError("tolerance must be positive")
    if not pivots:
        return []

    ordered = sorted(pivots, key=lambda pivot: pivot.price)
    groups: list[list[Pivot]] = [[ordered[0]]]
    for pivot in ordered[1:]:
        current = groups[-1]
        centre = sum(item.price for item in current) / len(current)
        if abs(pivot.price - centre) <= tolerance:
            current.append(pivot)
        else:
            groups.append([pivot])

    kept = [group for group in groups if len(group) >= min_touches]
    if not kept:
        return []

    best = max(len(group) for group in kept)
    levels: list[Level] = []
    for group in kept:
        dates = sorted(item.when for item in group)
        highs = sum(1 for item in group if item.high)
        levels.append(
            Level(
                price=sum(item.price for item in group) / len(group),
                # A cluster can hold both highs and lows, which is the classic flip from
                # resistance to support. It is labelled by whichever it mostly was.
                kind=LevelKind.SWING_HIGH if highs * 2 >= len(group) else LevelKind.SWING_LOW,
                strength=len(group) / best,
                touches=len(group),
                first_touch=dates[0],
                last_touch=dates[-1],
            )
        )
    return sorted(levels, key=lambda level: level.price)


def occupancy_share(bars: Sequence[PriceBar], low: float, high: float) -> float:
    """Fraction of the window's sessions whose range overlapped [low, high].

    The null model for how often price *should* turn near a price, and the reason this
    is not a uniform distribution over the range. Price does not visit all prices
    equally: it spends months in a congestion zone and crosses a gap in a day. A
    uniform null would therefore call every level inside the congestion zone
    significant and miss every level outside it, which is the constant offset trap
    wearing a statistics hat.

    Each session contributes the fraction of its own high to low range that falls
    inside the window, so a bar straddling the edge counts partially. Computed exactly
    rather than binned, since the cost is one pass per query and there are only ever a
    few dozen queries.
    """
    if high < low:
        raise ValueError("high must not be below low")
    usable = [bar for bar in bars if bar.high > bar.low]
    if not usable:
        return 0.0

    total = 0.0
    for bar in usable:
        overlap = min(bar.high, high) - max(bar.low, low)
        if overlap > 0:
            total += overlap / (bar.high - bar.low)
    return total / len(usable)


def touch_p_value(touches: int, expected: float) -> float:
    """P(at least `touches` pivots land here by chance), Poisson with mean `expected`.

    Poisson because the question is how many of a fixed number of scattered points fall
    in a small window, and that is what Poisson counts. The approximation is good
    exactly where it is being used, which is small expected counts in narrow bands.
    """
    if touches <= 0:
        return 1.0
    if expected <= 0:
        # Nothing was expected here at all, so any touch at all is as surprising as
        # this test can express. Zero would claim more certainty than a finite sample
        # supports.
        return 0.0
    return float(poisson.sf(touches - 1, expected))


def candidate_levels(
    bars: Sequence[PriceBar],
    *,
    lookback: int = DEFAULT_PIVOT_LOOKBACK,
    min_prominence_atr: float = DEFAULT_MIN_PROMINENCE_ATR,
    cluster_atr: float = DEFAULT_CLUSTER_ATR,
    min_touches: int = DEFAULT_MIN_TOUCHES,
    atr_period: int = DEFAULT_ATR_PERIOD,
) -> list[Level]:
    """Every clustered candidate with its p-value attached, unfiltered.

    Exposed separately from `support_resistance` so a caller can see what was rejected
    and how narrowly. An empty level list is much easier to trust when the alternative
    reading, that thirty candidates were tested and two survived, is also available.
    """
    average_range = atr(bars, atr_period)
    if average_range is None or average_range <= 0:
        return []

    width = cluster_atr * average_range
    pivots = swing_pivots(bars, lookback, min_prominence=min_prominence_atr * average_range)
    clusters = cluster_pivots(pivots, width, min_touches=min_touches)

    scored: list[Level] = []
    for level in clusters:
        share = occupancy_share(bars, level.price - width, level.price + width)
        expected = len(pivots) * share
        p_value = touch_p_value(level.touches, expected)
        scored.append(
            Level(
                price=level.price,
                kind=level.kind,
                # Strength restates the p-value rather than the raw touch count, since
                # the test is exactly what showed the raw count to be uninformative on
                # its own. 1.0 is a certainty, 0.0 is indistinguishable from chance.
                strength=max(0.0, 1.0 - p_value),
                touches=level.touches,
                first_touch=level.first_touch,
                last_touch=level.last_touch,
                p_value=p_value,
                expected_touches=expected,
            )
        )
    return scored


def support_resistance(
    bars: Sequence[PriceBar],
    *,
    max_p_value: float = DEFAULT_MAX_P_VALUE,
    **kwargs,
) -> list[Level]:
    """Swing levels that price respected more often than chance explains.

    Every threshold is in ATR rather than dollars or percent, so one setting works on a
    30 dollar name and a 740 dollar index without retuning.

    The significance test is the step that matters, and it is worth knowing how much it
    removes. On four years of SPY, clustering alone publishes 31 levels; requiring them
    to beat their own price band's expected touch count leaves 8 at p below 0.20 and 1
    at p below 0.05. The 30 that fall away are not weak levels, they are prices where
    the number of pivots is what time spent there already predicts.

    Returns an empty list when there is not enough history for an ATR, because a level
    built on a threshold that could not be computed is not a level.
    """
    return [
        level
        for level in candidate_levels(bars, **kwargs)
        if level.p_value is not None and level.p_value <= max_p_value
    ]


# --------------------------------------------------------------------------------
# Volume profile
# --------------------------------------------------------------------------------


def volume_profile(
    bars: Sequence[PriceBar],
    bins: int = DEFAULT_VOLUME_BINS,
    *,
    value_area_share: float = DEFAULT_VALUE_AREA_SHARE,
) -> VolumeProfile | None:
    """Traded volume binned by price, with the point of control and value area.

    Each bar's volume is spread uniformly across the bins its range covers rather than
    dropped entirely at its typical price. Neither is what actually happened, since the
    tape is not in a daily bar, but spreading does not invent a spike at a price that
    may have seen one trade.

    **Read the window before reading the profile.** Over a long trending window the
    point of control lands wherever the trend spent the most time, which describes the
    path rather than a price buyers defend. That is a structural bias and not a signal,
    so prefer a window short enough to sit inside one regime, and treat the value area
    as a range statement about that window rather than as support.
    """
    usable = [bar for bar in bars if bar.volume and bar.high > bar.low]
    if len(usable) < MIN_PROFILE_BARS or bins < MIN_PROFILE_BINS:
        return None

    low = min(bar.low for bar in usable)
    high = max(bar.high for bar in usable)
    if high <= low:
        return None

    width = (high - low) / bins
    edges = [low + width * index for index in range(bins + 1)]
    volumes = [0.0] * bins

    for bar in usable:
        first = min(int((bar.low - low) / width), bins - 1)
        last = min(int((bar.high - low) / width), bins - 1)
        share = bar.volume / (last - first + 1)
        for index in range(first, last + 1):
            volumes[index] += share

    peak = max(range(bins), key=lambda index: volumes[index])
    point_of_control = (edges[peak] + edges[peak + 1]) / 2.0

    # The value area grows outward from the point of control, taking whichever
    # neighbouring bin is busier, until it holds the requested share of total volume.
    # That is the market profile construction, and it is directional rather than
    # symmetric because volume distributions rarely are.
    total = sum(volumes)
    target = total * value_area_share
    lower, upper = peak, peak
    captured = volumes[peak]
    while captured < target and (lower > 0 or upper < bins - 1):
        below = volumes[lower - 1] if lower > 0 else -1.0
        above = volumes[upper + 1] if upper < bins - 1 else -1.0
        if below >= above:
            lower -= 1
            captured += below
        else:
            upper += 1
            captured += above

    return VolumeProfile(
        edges=tuple(edges),
        volumes=tuple(volumes),
        point_of_control=point_of_control,
        value_area_low=edges[lower],
        value_area_high=edges[upper + 1],
    )


def volume_levels(
    profile: VolumeProfile | None,
    *,
    min_share: float = DEFAULT_MIN_NODE_SHARE,
) -> list[Level]:
    """The point of control, the value area edges, and any other high volume nodes.

    A node has to be a local maximum of the histogram *and* reach `min_share` of the
    busiest bin. The local maximum test alone accepts every bump, which on sixty bins
    is a dozen "nodes" that are mostly binning noise.
    """
    if profile is None:
        return []

    volumes = profile.volumes
    peak = max(volumes)
    if peak <= 0:
        return []

    levels = [
        Level(
            price=profile.point_of_control,
            kind=LevelKind.POINT_OF_CONTROL,
            strength=1.0,
        ),
        Level(price=profile.value_area_low, kind=LevelKind.VALUE_AREA, strength=0.6),
        Level(price=profile.value_area_high, kind=LevelKind.VALUE_AREA, strength=0.6),
    ]

    for index in range(1, len(volumes) - 1):
        share = volumes[index] / peak
        if share < min_share:
            continue
        if volumes[index] < volumes[index - 1] or volumes[index] < volumes[index + 1]:
            continue
        price = (profile.edges[index] + profile.edges[index + 1]) / 2.0
        # The point of control is already published as its own kind, and republishing
        # it as an ordinary node would draw the same line twice.
        if abs(price - profile.point_of_control) < PRICE_EPSILON:
            continue
        levels.append(Level(price=price, kind=LevelKind.VOLUME_NODE, strength=share))

    return sorted(levels, key=lambda level: level.price)


# --------------------------------------------------------------------------------
# Round numbers
# --------------------------------------------------------------------------------


def round_numbers(
    low: float,
    high: float,
    spot: float,
    *,
    steps: Sequence[float] = ROUND_NUMBER_STEPS,
    max_lines: int = 12,
) -> list[Level]:
    """Round price levels inside a range, spaced relative to the price.

    The step is a fraction of spot rather than a fixed amount, which is what makes one
    rule work at 30 dollars and at 740. Each step is snapped to a human round number
    (1, 2, 5 and their powers of ten) because 7.40 is not a round number and 10 is.

    **These carry no evidence.** A round number is a place people put orders, which is
    a real effect and a weak one, and unlike a swing level nothing here counts how
    often price actually respected it. They are reference lines. Strength is a constant
    for exactly that reason: there is nothing to measure, and inventing a score would
    make them look comparable to levels that earned one.
    """
    if spot <= 0 or high <= low:
        return []

    chosen: float | None = None
    for fraction in sorted(steps, reverse=True):
        candidate = _snap_to_round(spot * fraction)
        if candidate > 0 and (high - low) / candidate <= max_lines:
            chosen = candidate
            break
    if chosen is None:
        return []

    first = math.ceil(low / chosen) * chosen
    levels: list[Level] = []
    price = first
    while price <= high and len(levels) < max_lines:
        levels.append(Level(price=round(price, 4), kind=LevelKind.ROUND_NUMBER, strength=0.35))
        price += chosen
    return levels


def _snap_to_round(value: float) -> float:
    """Nearest 1, 2 or 5 times a power of ten, at or below `value`."""
    if value <= 0:
        return 0.0
    exponent = math.floor(math.log10(value))
    base = 10.0**exponent
    for multiple in (5.0, 2.0, 1.0):
        if value >= multiple * base:
            return multiple * base
    return base


# --------------------------------------------------------------------------------
# The aggregate
# --------------------------------------------------------------------------------


def build_levels(
    bars: Sequence[PriceBar],
    *,
    spot: float | None = None,
    ma_periods: Sequence[int] = (20, 50, 200),
    profile_sessions: int = 120,
    atr_period: int = DEFAULT_ATR_PERIOD,
    realized_vol_window: int = DEFAULT_REALIZED_VOL_WINDOW,
    max_p_value: float = DEFAULT_MAX_P_VALUE,
    **kwargs,
) -> Levels:
    """Everything backward looking, in one pass, with what could not be computed named.

    `profile_sessions` deliberately trims the volume profile to a shorter window than
    the rest. See volume_profile: over a long trend the point of control describes the
    path rather than a defended price, and the moving averages and swing levels do not
    have that problem, so they keep the full history.
    """
    if not bars:
        return Levels(spot=spot or 0.0, notes=("No price history, so there are no levels.",))

    ordered = sorted(bars, key=lambda bar: bar.ts)
    price = spot if spot is not None else ordered[-1].close
    notes: list[str] = []

    average_range = atr(ordered, atr_period)
    if average_range is None:
        notes.append(
            f"Not enough history for a {atr_period} day ATR, so swing levels were "
            f"not computed: {len(ordered)} sessions available."
        )

    averages = {}
    for period in ma_periods:
        value = moving_average(ordered, period)
        if value is None:
            notes.append(f"The {period} day moving average needs {period} sessions.")
        else:
            averages[period] = value

    window = ordered[-profile_sessions:] if profile_sessions > 0 else ordered
    profile = volume_profile(window)
    if profile is None:
        notes.append("No usable volume, so there is no volume profile.")
    elif len(window) < len(ordered):
        notes.append(
            f"The volume profile covers the last {len(window)} sessions, not all "
            f"{len(ordered)}. A profile over a long trend finds the trend."
        )

    candidates = candidate_levels(ordered, atr_period=atr_period, **kwargs)
    swing = [
        level for level in candidates if level.p_value is not None and level.p_value <= max_p_value
    ]
    if candidates and not swing:
        notes.append(
            f"None of the {len(candidates)} swing candidates beat chance at p below "
            f"{max_p_value}. Price turned near each of them about as often as the time "
            f"it spent there already predicts, which is a finding rather than a gap."
        )
    elif candidates:
        notes.append(
            f"{len(swing)} of {len(candidates)} swing candidates beat chance at p below "
            f"{max_p_value}. The rest are prices where the pivot count is what time "
            f"spent there already predicts."
        )

    volume = volume_levels(profile)

    low = min(bar.low for bar in window)
    high = max(bar.high for bar in window)

    return Levels(
        spot=price,
        atr=average_range,
        realized_vol=realized_volatility(ordered, realized_vol_window),
        moving_averages=averages,
        bollinger=bollinger_bands(ordered),
        profile=profile,
        swing=tuple(swing),
        volume=tuple(volume),
        round_numbers=tuple(round_numbers(low, high, price)),
        sessions=len(ordered),
        swing_candidates=len(candidates),
        notes=tuple(notes),
    )


def levels_near(levels: Sequence[Level], price: float, tolerance: float) -> list[Level]:
    """Every level within `tolerance` in price terms, nearest first.

    Used to answer the question a strike selection screen actually asks, which is not
    "where are the levels" but "is this strike sitting on one".
    """
    if tolerance < 0:
        raise ValueError("tolerance must not be negative")
    near = [level for level in levels if abs(level.price - price) <= tolerance]
    return sorted(near, key=lambda level: abs(level.price - price))


def _sorted_prices(levels: Sequence[Level]) -> list[float]:
    return sorted(level.price for level in levels)


def bracketing_levels(
    levels: Sequence[Level],
    price: float,
) -> tuple[float | None, float | None]:
    """The nearest level below and above a price, as bare prices.

    Returns None on either side when there is nothing there, which is a real answer:
    a strike above every level this window found has no resistance between it and
    wherever price went last time, and that is worth seeing rather than filling in.
    """
    prices = _sorted_prices(levels)
    if not prices:
        return None, None
    index = bisect_left(prices, price)
    below = prices[index - 1] if index > 0 else None
    above = prices[index] if index < len(prices) else None
    if above is not None and above == price and index + 1 < len(prices):
        above = prices[index + 1]
    return below, above
