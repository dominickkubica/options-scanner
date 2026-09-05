"""Does the score mean anything, and is the probability model calibrated.

Pure. Resolved outcomes arrive as arguments.

## The trap this phase brings, which is the same trap in a new costume

Every previous instance was a measure with a structural component read as signal. This
one is a **sample with a structural component read as evidence**: the rows in the
validation log are nowhere near independent, and the naive arithmetic will therefore
produce confident looking results out of nothing.

Three separate correlations, all of which shrink the effective sample:

1. **One scan produces many rows.** Forty candidates off one chain share one underlying,
   one session, and one volatility surface. If the underlying rallies, every short call
   in that scan loses together. They are one observation of a market, not forty.
2. **Consecutive sessions overlap.** The same symbol and expiry scanned on Monday and
   Tuesday is very nearly the same trade recorded twice.
3. **Candidates within a scan are nested.** A 700 and a 705 short put on one chain
   resolve together almost always.

So the unit of independence used here is the **(symbol, expiry) cluster**, not the row.
Win rates are still reported per row because that is what a person wants to read, and
every interval is widened to the cluster count, and the report says both numbers. An
interval computed on the row count would be roughly the square root of the cluster size
too narrow, which on forty rows from three chains is about a factor of three and is more
than enough to turn noise into a finding.

## Why the intervals are Wilson and not normal

At the sample sizes this will have for months, the textbook normal interval on a
proportion is wrong in the specific direction that matters: near a win rate of 0.9,
which is where short premium lives, it produces upper bounds above 1.0 and understates
the lower bound. Wilson behaves at the extremes and is barely more code.

## What "calibrated" means here, and what it does not

The probability model claims a short strike finishes out of the money with probability
p. Calibration asks whether the strikes it called 80 percent actually finished out of
the money 80 percent of the time. That is a question about the model, and it is
deliberately measured against `finished_beyond` rather than against profit: a position
can make money after being breached, and scoring that as a hit would let a model be
wrong about the event and still look calibrated.

`probability.py` already states the direction it is expected to be wrong: lognormal
tails are too thin, so it should prove optimistic on the far strikes. Finding exactly
that is the expected result rather than a surprise, and finding the opposite would mean
something is wrong with this file rather than with the market.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

#: Resolved outcomes below which nothing is reported at all. Twenty independent
#: clusters is already generous for a claim about a win rate; below it the interval is
#: wider than the range of plausible answers and printing a point estimate invites
#: somebody to read it.
MIN_CLUSTERS_FOR_A_CLAIM = 20

#: Buckets the score is split into. Deciles are the roadmap's word and they are wrong
#: for this sample size: ten buckets over a hundred outcomes is ten per bucket, which
#: cannot separate anything. Quartiles are reported and the function takes the count.
DEFAULT_BUCKETS = 4

#: Standard normal quantile for a 95 percent two sided interval.
Z_95 = 1.959963985

#: Fewest buckets that can compare anything, and the fewest resolved rows a halves
#: comparison can be built from.
MIN_BUCKETS = 2
MIN_ROWS_FOR_HALVES = 4

#: Above this win rate, a negative mean profit is the classic premium selling trap
#: rather than a rounding artifact, and the report says so.
HIGH_WIN_RATE = 0.6


@dataclass(frozen=True, slots=True)
class Interval:
    """A proportion with an honest interval around it."""

    value: float
    low: float
    high: float
    observations: int
    clusters: int

    @property
    def width(self) -> float:
        return self.high - self.low

    def __str__(self) -> str:
        return f"{self.value:.0%} ({self.low:.0%} to {self.high:.0%})"


@dataclass(frozen=True, slots=True)
class Bucket:
    """One slice of the score range, and how its candidates actually did."""

    label: str
    low: float
    high: float
    count: int
    clusters: int
    wins: int
    win_rate: Interval | None
    mean_profit: float
    total_profit: float
    mean_score: float

    @property
    def usable(self) -> bool:
        return self.clusters >= MIN_CLUSTERS_FOR_A_CLAIM


@dataclass(frozen=True, slots=True)
class CalibrationPoint:
    """One predicted probability band against what happened in it."""

    low: float
    high: float
    predicted: float
    actual: float
    count: int
    clusters: int

    @property
    def error(self) -> float:
        """Predicted minus actual. Positive means the model was optimistic."""
        return self.predicted - self.actual


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """The answer to the only question Phase 8 asks, or a refusal to give one."""

    resolved: int
    clusters: int
    buckets: tuple[Bucket, ...] = ()
    calibration: tuple[CalibrationPoint, ...] = ()
    brier: float | None = None
    overall_win_rate: Interval | None = None
    mean_profit: float | None = None
    total_profit: float | None = None
    #: The headline. None when the sample cannot support an answer, which it will be
    #: for months, and that is the correct output rather than a placeholder.
    scores_separate: bool | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def conclusive(self) -> bool:
        return self.scores_separate is not None


def wilson_interval(successes: int, trials: int, *, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Behaves at the extremes, which is where short premium lives. A normal interval on a
    win rate of 0.95 from 40 trials puts its upper bound above 1.0, and an upper bound
    above certainty is a visible sign of a formula being used outside its range.
    """
    if trials <= 0:
        return 0.0, 1.0
    proportion = successes / trials
    denominator = 1.0 + z * z / trials
    centre = proportion + z * z / (2 * trials)
    spread = z * math.sqrt(proportion * (1 - proportion) / trials + z * z / (4 * trials * trials))
    return max((centre - spread) / denominator, 0.0), min((centre + spread) / denominator, 1.0)


def cluster_key(item) -> tuple[str, object]:
    """The unit of independence: one symbol and one expiry.

    Not the row, and not the scan. Two scans of the same symbol and expiry on
    consecutive days resolve to the same settlement price, so they are one observation
    however many rows they produced.
    """
    return (item.symbol, item.expiry)


def count_clusters(items: Sequence) -> int:
    return len({cluster_key(item) for item in items})


def proportion_interval(items: Sequence, predicate) -> Interval | None:
    """A proportion widened to the cluster count rather than the row count.

    The widening is the point of this function. Successes are counted per row because
    that is the quantity being estimated, but the interval is computed as though the
    sample were only as large as the number of independent clusters, which it is.
    """
    if not items:
        return None
    trials = len(items)
    successes = sum(1 for item in items if predicate(item))
    clusters = count_clusters(items)

    proportion = successes / trials
    # Scale the observed proportion onto the cluster sample, rounding to a whole number
    # of successes so the interval is one a binomial could actually have produced.
    effective_successes = round(proportion * clusters)
    low, high = wilson_interval(effective_successes, clusters)

    return Interval(
        value=proportion,
        low=low,
        high=high,
        observations=trials,
        clusters=clusters,
    )


def score_buckets(items: Sequence, buckets: int = DEFAULT_BUCKETS) -> list[Bucket]:
    """Split resolved candidates by score and report each slice.

    Split on equal counts rather than on equal score ranges. The score distribution is
    heavily bunched, so fixed width bands put almost everything in one of them and leave
    the rest empty, which looks like a finding about the score and is a fact about the
    binning.
    """
    usable = [item for item in items if item.profit is not None]
    if not usable or buckets < MIN_BUCKETS:
        return []

    ordered = sorted(usable, key=lambda item: item.score)
    size = len(ordered) / buckets
    result: list[Bucket] = []

    for index in range(buckets):
        start = round(index * size)
        end = round((index + 1) * size)
        slice_ = ordered[start:end]
        if not slice_:
            continue

        wins = sum(1 for item in slice_ if item.profit > 0)
        profits = [item.profit for item in slice_]
        result.append(
            Bucket(
                label=f"Q{index + 1}",
                low=slice_[0].score,
                high=slice_[-1].score,
                count=len(slice_),
                clusters=count_clusters(slice_),
                wins=wins,
                win_rate=proportion_interval(slice_, lambda item: item.profit > 0),
                mean_profit=sum(profits) / len(profits),
                total_profit=sum(profits),
                mean_score=sum(item.score for item in slice_) / len(slice_),
            )
        )
    return result


def probability_calibration(
    items: Sequence,
    *,
    bands: Sequence[tuple[float, float]] = (
        (0.50, 0.70),
        (0.70, 0.80),
        (0.80, 0.90),
        (0.90, 1.01),
    ),
) -> list[CalibrationPoint]:
    """Predicted probability of profit against how often it actually happened.

    Measured on `finished_beyond` inverted, not on profit. The model predicts an event
    about where price finishes; whether that event was survivable is a separate
    question, and scoring the model on profit would let it be wrong about the event and
    still look right.
    """
    usable = [
        item
        for item in items
        if item.probability_of_profit is not None and item.finished_beyond is not None
    ]
    points: list[CalibrationPoint] = []

    for low, high in bands:
        inside = [item for item in usable if low <= item.probability_of_profit < high]
        if not inside:
            continue
        predicted = sum(item.probability_of_profit for item in inside) / len(inside)
        # The model's claim is that the short strike is NOT breached, so the observed
        # equivalent is the share that did not finish beyond it.
        actual = sum(1 for item in inside if not item.finished_beyond) / len(inside)
        points.append(
            CalibrationPoint(
                low=low,
                high=high,
                predicted=predicted,
                actual=actual,
                count=len(inside),
                clusters=count_clusters(inside),
            )
        )
    return points


def brier_score(items: Sequence) -> float | None:
    """Mean squared error of the probability forecasts. Lower is better, 0.25 is a coin.

    Reported because a reliability table can look fine while the forecasts carry no
    information: predicting 0.8 for everything is perfectly calibrated on average and
    completely useless, and the Brier score notices while the table does not.
    """
    usable = [
        item
        for item in items
        if item.probability_of_profit is not None and item.finished_beyond is not None
    ]
    if not usable:
        return None
    total = sum(
        (item.probability_of_profit - (0.0 if item.finished_beyond else 1.0)) ** 2
        for item in usable
    )
    return total / len(usable)


def validate(items: Sequence, *, buckets: int = DEFAULT_BUCKETS) -> ValidationReport:
    """The whole report, including a refusal when the sample cannot support one."""
    resolved = [item for item in items if item.profit is not None]
    clusters = count_clusters(resolved)
    notes: list[str] = []

    if not resolved:
        return ValidationReport(
            resolved=0,
            clusters=0,
            notes=(
                "No resolved outcomes yet. Candidates are logged at scan time and "
                "settle after their expiry passes, so this report stays empty until "
                "the first logged expiry has come and gone.",
            ),
        )

    slices = score_buckets(resolved, buckets)
    profits = [item.profit for item in resolved]
    report_notes = _sample_notes(resolved, clusters)
    notes.extend(report_notes)

    separates = None
    if clusters >= MIN_CLUSTERS_FOR_A_CLAIM and len(resolved) >= MIN_ROWS_FOR_HALVES:
        # Top half against bottom half, not top bucket against bottom bucket.
        #
        # The extreme bucket comparison is the obvious version and it is worse in a way
        # that took a demonstration to see: each bucket holds a quarter of the sample,
        # so both intervals are wide, and it throws away the middle half entirely. On a
        # sample where the top two quartiles won 95 and 87 percent against 67 and 40 in
        # the bottom two, comparing only Q4 against Q1 reported no separation, because
        # Q4 happened not to be the best bucket. Halves use every observation and
        # answer the question actually asked, which is whether high scores do better
        # than low ones rather than whether the very top beats the very bottom.
        ordered = sorted(resolved, key=lambda item: item.score)
        middle = len(ordered) // 2
        low_half, high_half = ordered[:middle], ordered[middle:]
        top = proportion_interval(high_half, lambda item: item.profit > 0)
        bottom = proportion_interval(low_half, lambda item: item.profit > 0)

        if top and bottom:
            # Non overlapping intervals rather than a two proportion test. Deliberately
            # the more conservative of the two: it will call a real effect inconclusive
            # before it calls noise a finding, which is the correct direction for a
            # study whose whole purpose is to stop this tool over claiming.
            separates = top.low > bottom.high
            notes.append(
                f"Top half by score {top} against bottom half {bottom}, intervals "
                f"widened to {clusters} independent clusters. "
                + (
                    "They do not overlap, so the score separates outcomes on this sample."
                    if separates
                    else "They overlap, so this sample does not show the score separating "
                    "outcomes. That is not evidence that it does not, only that there "
                    "is not enough here to tell."
                )
            )

    mean_profit = sum(profits) / len(profits)
    win_rate = proportion_interval(resolved, lambda item: item.profit > 0)
    if win_rate is not None and win_rate.value > HIGH_WIN_RATE and mean_profit < 0:
        # The classic premium selling trap, and the single most useful thing this report
        # can say. Short premium wins most of the time by construction, so a high win
        # rate is not evidence of anything; the losses are simply larger than the wins.
        notes.append(
            f"A {win_rate.value:.0%} win rate alongside a mean profit of "
            f"{mean_profit:+.2f} means the losers are bigger than the winners. Win rate "
            "and expectancy are different questions, and short premium is designed to "
            "win often. Read the profit column, not the win rate."
        )

    return ValidationReport(
        resolved=len(resolved),
        clusters=clusters,
        buckets=tuple(slices),
        calibration=tuple(probability_calibration(resolved)),
        brier=brier_score(resolved),
        overall_win_rate=win_rate,
        mean_profit=mean_profit,
        total_profit=sum(profits),
        scores_separate=separates,
        notes=tuple(notes),
    )


def _sample_notes(resolved: Sequence, clusters: int) -> list[str]:
    """Everything about the sample a reader has to know before reading a number."""
    notes = [
        f"{len(resolved)} resolved candidates over {clusters} independent "
        f"(symbol, expiry) clusters. Rows from one chain resolve together, so the "
        f"cluster count is the sample size and every interval below uses it."
    ]
    if clusters < MIN_CLUSTERS_FOR_A_CLAIM:
        notes.append(
            f"Below {MIN_CLUSTERS_FOR_A_CLAIM} clusters no conclusion is drawn. The "
            "numbers below are printed so the pipeline can be seen working, not "
            "because they mean anything yet."
        )
    notes.append(
        "Profit assumes the position was opened at the recorded credit and held to "
        "expiry with no management. Nobody trades that way, and Phase 7 exists to "
        "close early. It is measured this way because probability of profit is defined "
        "at expiry, so it is the only policy under which calibration means anything."
    )
    return notes
