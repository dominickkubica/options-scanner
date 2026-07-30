"""IV rank and IV percentile.

The central signal for premium selling, and the one number in this tool that cannot
be computed from today's data. It needs history, the history has to be built one day
at a time, and for the first few months there is not enough of it.

That is the whole reason the confidence field exists. An IV rank of 85 computed from
three weeks of observations is not a weak signal, it is a different quantity wearing
the same name: over three weeks the range it normalizes against is whatever happened
to occur in three weeks. Reporting it without a qualifier invites exactly the trade
it does not support.

The two measures answer different questions and disagree in a useful way:

- IV rank normalizes against the range: where does today sit between the lowest and
  highest vol observed? One outlier spike compresses every subsequent reading.
- IV percentile counts days: what fraction of days had lower vol than today? Immune
  to outliers, blind to how far away the extremes are.

A high rank with a low percentile means vol is near its highs but has spent most of
its time even higher recently. Both are reported.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

#: Below this many observations no rank is published at all.
MIN_OBSERVATIONS = 20

#: Thresholds for how much history counts as how much confidence, in observations.
#: 60 trading days is the usual minimum quoted for IV percentile. 180 is most of a
#: year and starts to cover more than one volatility regime.
LOW_CONFIDENCE_OBSERVATIONS = 60
MEDIUM_CONFIDENCE_OBSERVATIONS = 180

#: Fraction of expected trading days that must actually be present. A window with
#: holes in it covers less than its span suggests.
MIN_COMPLETENESS = 0.8

#: Rough trading days per calendar day, for judging completeness.
TRADING_DAY_RATIO = 252 / 365


class Confidence(StrEnum):
    """How much history stands behind a rank."""

    INSUFFICIENT = "insufficient"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class IVRank:
    """Today's vol placed against its own history, with the caveats attached."""

    iv: float
    rank: float | None
    percentile: float | None
    observations: int
    span_days: int
    completeness: float
    confidence: Confidence
    low: float | None = None
    high: float | None = None
    median: float | None = None

    @property
    def usable(self) -> bool:
        """Whether there is enough history to act on this."""
        return self.confidence is not Confidence.INSUFFICIENT

    def caveat(self) -> str | None:
        """A sentence the UI can show next to the number, or None when there is none."""
        if self.confidence is Confidence.INSUFFICIENT:
            return f"Only {self.observations} observations, need {MIN_OBSERVATIONS}. No rank yet."
        if self.confidence is Confidence.LOW:
            return (
                f"{self.observations} observations over {self.span_days} days. "
                "The range this is measured against is short enough to be one regime."
            )
        if self.confidence is Confidence.MEDIUM:
            return f"{self.observations} observations, less than a full year of history."
        return None


def assess_confidence(observations: int, completeness: float) -> Confidence:
    """How much to trust a rank from this much history.

    Count first, then a downgrade for gaps: 100 observations scattered across two
    years cover less than 100 consecutive days do, because the missing days are not
    missing at random. They are the days the job failed, and the job fails on days
    the vendor is struggling, which correlates with days the market is moving.
    """
    if observations < MIN_OBSERVATIONS:
        return Confidence.INSUFFICIENT

    if observations >= MEDIUM_CONFIDENCE_OBSERVATIONS:
        level = Confidence.HIGH
    elif observations >= LOW_CONFIDENCE_OBSERVATIONS:
        level = Confidence.MEDIUM
    else:
        level = Confidence.LOW

    if completeness < MIN_COMPLETENESS:
        return {
            Confidence.HIGH: Confidence.MEDIUM,
            Confidence.MEDIUM: Confidence.LOW,
            Confidence.LOW: Confidence.LOW,
        }[level]
    return level


def completeness_ratio(observations: int, span_days: int) -> float:
    """Observations present over trading days the span should have contained.

    Capped at 1.0, since more than one observation per day is a recapture rather than
    extra coverage.
    """
    if span_days <= 0:
        return 1.0 if observations > 0 else 0.0
    expected = max(span_days * TRADING_DAY_RATIO, 1.0)
    return min(observations / expected, 1.0)


def iv_rank(
    current_iv: float,
    history: Sequence[float],
    *,
    span_days: int | None = None,
) -> IVRank:
    """Rank and percentile of `current_iv` against `history`.

    history should be the ATM implied vols of prior sessions, not including today.
    Including today would make the current value part of its own range, which biases
    the rank toward the middle in exactly the calm periods where it should read low.
    """
    if current_iv <= 0:
        raise ValueError(f"current_iv must be positive, got {current_iv}")

    values = sorted(v for v in history if v is not None and v > 0)
    observations = len(values)
    span = span_days if span_days is not None else observations
    completeness = completeness_ratio(observations, span)
    confidence = assess_confidence(observations, completeness)

    if confidence is Confidence.INSUFFICIENT:
        return IVRank(
            iv=current_iv,
            rank=None,
            percentile=None,
            observations=observations,
            span_days=span,
            completeness=completeness,
            confidence=confidence,
        )

    low, high = values[0], values[-1]
    # A zero spread means every observation was identical, which makes a rank
    # meaningless. A percentile is still well defined.
    spread = high - low
    rank = None if spread <= 0 else min(max((current_iv - low) / spread, 0.0), 1.0)

    below = sum(1 for value in values if value < current_iv)
    percentile = below / observations

    middle = observations // 2
    median = values[middle] if observations % 2 else (values[middle - 1] + values[middle]) / 2.0

    return IVRank(
        iv=current_iv,
        rank=rank,
        percentile=percentile,
        observations=observations,
        span_days=span,
        completeness=completeness,
        confidence=confidence,
        low=low,
        high=high,
        median=median,
    )


def iv_rank_from_series(
    current_iv: float,
    history: Sequence[tuple[date, float]],
    *,
    asof: date | None = None,
    lookback_days: int = 365,
) -> IVRank:
    """Rank against dated observations inside a lookback window.

    The dated form is what real snapshot history looks like, and it is what makes
    completeness measurable: a bare list of numbers cannot tell you whether it covers
    a year with gaps or two months solid.
    """
    if not history:
        return iv_rank(current_iv, [], span_days=0)

    latest = asof or max(day for day, _ in history)
    cutoff_days = lookback_days

    window = [
        (day, value)
        for day, value in history
        if value is not None and value > 0 and 0 <= (latest - day).days <= cutoff_days
    ]
    if not window:
        return iv_rank(current_iv, [], span_days=0)

    days = [day for day, _ in window]
    span = (max(days) - min(days)).days
    return iv_rank(current_iv, [value for _, value in window], span_days=span)
