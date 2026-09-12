"""Journal reporting over settled candidates: the numbers a trade log is read for.

This is the report surface a trading journal has - a calendar of daily profit, an
equity curve, win rate and expectancy, and the same broken out by strategy, symbol,
days to expiry and score. What it is reporting on is **not** trades that were taken.
Nothing here has been to a broker. Every row is a candidate the screen surfaced and
`optscan resolve` later settled at expiry, so this measures the screen, not a trader.
`analytics/outcomes.py` settles them; this only aggregates.

## The one thing that makes this honest

Row counts are not sample sizes. Candidates from one chain settle against one
settlement price, so a hundred rows off six chains is six observations wearing a
hundred hats. `calibration.py` established the (symbol, expiry) cluster as the unit of
independence and every interval here is widened to it, using that module's own helper
rather than a second copy of the reasoning.

That matters more here than anywhere else in the project, because a calendar and an
equity curve are the two most persuasive objects a tool like this can draw. A curve
climbing to a six figure total across two thousand "trades" reads as a track record. At
fourteen independent clusters it is not one, and every aggregate this module returns
carries its cluster count so the view has no way to render the number without it.

## Expectancy is measured per cluster, not per row

Mean profit per row is the number a person wants, so it is what `expectancy` reports.
Its interval, though, is computed from cluster totals: profits are summed within each
(symbol, expiry) and the spread of those cluster means is what the standard error comes
from. Taking the standard error across rows would divide by the square root of two
thousand when the real denominator is fourteen, which is the specific error that turns
noise into a finding.

## Dates are settlement dates

A candidate is scored on one day and settles on another, and the money moves on the
second. So the calendar and the curve are keyed on expiry, not on the session the
screen ran. Keying on the scan date would draw profit on days nothing was realized.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from optscan.analytics.calibration import (
    MIN_CLUSTERS_FOR_A_CLAIM,
    Z_95,
    Interval,
    cluster_key,
    count_clusters,
    proportion_interval,
)

#: Days to expiry bands for the breakdown. Fixed rather than quantiled: the screen's
#: own window is 21 to 60, so these are the thirds of it a reader already thinks in,
#: and quantiles would move the edges every time the sample changed.
#: How long a position was carried, in sessions.
#:
#: Retuned when the journal moved from settled candidates to real fills. The old bands
#: were 0-20, 21-30, 31-45, 46-60, 61+, which were the thirds of the screen's own 21 to
#: 60 window. Against this account they are useless: it closes almost everything the day
#: it opens, so every entry landed in a single "0-20" bucket and the breakdown said
#: nothing. Separating the same session from the overnight is the distinction that
#: actually exists in the data.
DTE_BANDS: tuple[tuple[str, int, int], ...] = (
    ("same session", 0, 0),
    ("overnight", 1, 1),
    ("2-4 days", 2, 4),
    ("5-9 days", 5, 9),
    ("10+ days", 10, 10_000),
)

#: Fewest clusters that have any spread between them. One cluster is a single
#: observation, and an interval drawn around it would be zero wide, which reads as
#: certainty rather than as the absence of evidence it actually is.
MIN_CLUSTERS_FOR_A_SPREAD = 2

#: Score bands, on the 0 to 1 composite. Fixed for the same reason as the DTE bands.
SCORE_BANDS: tuple[tuple[str, float, float], ...] = (
    ("0.00-0.60", 0.0, 0.60),
    ("0.60-0.75", 0.60, 0.75),
    ("0.75-0.85", 0.75, 0.85),
    ("0.85-1.00", 0.85, 1.01),
)


@dataclass(frozen=True, slots=True)
class Estimate:
    """A mean with an interval that respects the cluster count.

    Separate from `calibration.Interval`, which is a proportion. This one is dollars,
    can be negative, and its interval comes from the spread between clusters rather
    than from a binomial.
    """

    value: float
    low: float | None
    high: float | None
    observations: int
    clusters: int

    @property
    def indistinguishable_from_zero(self) -> bool:
        """Whether zero sits inside the interval.

        The question a premium seller is actually asking. A positive mean with zero
        inside its interval has not been shown to be positive, and this is the flag
        that stops a view from printing it as though it had.
        """
        if self.low is None or self.high is None:
            return True
        return self.low <= 0.0 <= self.high


@dataclass(frozen=True, slots=True)
class DayPoint:
    """One settlement date on the calendar and the curve."""

    day: date
    profit: float
    trades: int
    clusters: int
    cumulative: float


@dataclass(frozen=True, slots=True)
class Breakdown:
    """One row of a grouped report."""

    key: str
    trades: int
    clusters: int
    win_rate: Interval | None
    total_profit: float
    mean_profit: float

    @property
    def reportable(self) -> bool:
        """Whether this group has enough independent observations to mean anything."""
        return self.clusters >= MIN_CLUSTERS_FOR_A_CLAIM


@dataclass(frozen=True, slots=True)
class JournalReport:
    trades: int
    clusters: int
    wins: int
    losses: int
    scratches: int
    win_rate: Interval | None
    expectancy: Estimate | None
    avg_win: float | None
    avg_loss: float | None
    profit_factor: float | None
    total_profit: float
    max_drawdown: float
    best_day: DayPoint | None
    worst_day: DayPoint | None
    settlement_dates: int
    days: list[DayPoint]
    by_strategy: list[Breakdown]
    by_symbol: list[Breakdown]
    by_dte: list[Breakdown]
    by_score: list[Breakdown]
    notes: list[str]

    @property
    def reportable(self) -> bool:
        return self.clusters >= MIN_CLUSTERS_FOR_A_CLAIM


def _won(item) -> bool:
    return item.profit > 0


def cluster_totals(items: Sequence) -> list[float]:
    """Total profit per (symbol, expiry), which is the independent observation.

    Summed rather than averaged: the cluster is one settlement, and several candidates
    on it are several positions that would all have been carried into it.
    """
    totals: dict[tuple, float] = defaultdict(float)
    for item in items:
        totals[cluster_key(item)] += item.profit
    return list(totals.values())


def expectancy(items: Sequence) -> Estimate | None:
    """Mean profit per candidate, with an interval built from the cluster spread.

    The point estimate is per row because that is the unit a person sizes in. The
    interval is per cluster because that is the unit that varies independently. A
    single cluster gets no interval at all rather than a zero width one: one
    observation has no spread, and drawing that as certainty would be the worst
    possible reading of it.
    """
    if not items:
        return None

    profits = [item.profit for item in items]
    mean_per_row = statistics.fmean(profits)
    totals = cluster_totals(items)
    clusters = len(totals)

    if clusters < MIN_CLUSTERS_FOR_A_SPREAD:
        return Estimate(
            value=mean_per_row,
            low=None,
            high=None,
            observations=len(items),
            clusters=clusters,
        )

    # Scale the cluster spread back onto the per row mean, so the interval is in the
    # same units as the point estimate it surrounds.
    rows_per_cluster = len(items) / clusters
    standard_error = statistics.stdev(totals) / math.sqrt(clusters) / rows_per_cluster
    margin = Z_95 * standard_error

    return Estimate(
        value=mean_per_row,
        low=mean_per_row - margin,
        high=mean_per_row + margin,
        observations=len(items),
        clusters=clusters,
    )


def daily_series(items: Sequence) -> list[DayPoint]:
    """Profit by settlement date, with the running total already accumulated."""
    by_day: dict[date, list] = defaultdict(list)
    for item in items:
        by_day[item.expiry].append(item)

    points: list[DayPoint] = []
    running = 0.0
    for day in sorted(by_day):
        rows = by_day[day]
        profit = sum(row.profit for row in rows)
        running += profit
        points.append(
            DayPoint(
                day=day,
                profit=profit,
                trades=len(rows),
                clusters=count_clusters(rows),
                cumulative=running,
            )
        )
    return points


def max_drawdown(points: Sequence[DayPoint]) -> float:
    """Largest peak to trough fall in the cumulative curve, as a positive number.

    Measured from the start rather than from the first peak, so a curve that only ever
    loses reports its full fall instead of zero.
    """
    peak = 0.0
    worst = 0.0
    for point in points:
        peak = max(peak, point.cumulative)
        worst = max(worst, peak - point.cumulative)
    return worst


def _breakdown(key: str, rows: Sequence) -> Breakdown:
    profits = [row.profit for row in rows]
    return Breakdown(
        key=key,
        trades=len(rows),
        clusters=count_clusters(rows),
        win_rate=proportion_interval(rows, _won),
        total_profit=sum(profits),
        mean_profit=statistics.fmean(profits) if profits else 0.0,
    )


def group_by(items: Sequence, attribute: str) -> list[Breakdown]:
    """Break down by a plain attribute, worst total profit last."""
    groups: dict[str, list] = defaultdict(list)
    for item in items:
        groups[str(getattr(item, attribute))].append(item)
    return sorted(
        (_breakdown(key, rows) for key, rows in groups.items()),
        key=lambda row: -row.total_profit,
    )


def _band(value: float | None, bands) -> str | None:
    """Which band a value falls in, or None if it falls in none of them.

    A missing value is banded as None rather than compared, because not every source
    carries every attribute: a real trade imported from a broker has no score, and
    comparing that None against a float raises rather than reporting an empty
    breakdown.
    """
    if value is None:
        return None
    for label, low, high in bands:
        if low <= value <= high:
            return label
    return None


def group_by_band(items: Sequence, attribute: str, bands) -> list[Breakdown]:
    """Break down into fixed bands, kept in band order rather than sorted by profit.

    Order carries meaning here that it does not for a symbol list: these read as a
    gradient, and re-sorting them by profit would hide whether the gradient is
    monotonic, which is the only interesting thing about it.
    """
    groups: dict[str, list] = defaultdict(list)
    for item in items:
        label = _band(getattr(item, attribute), bands)
        if label is not None:
            groups[label].append(item)
    return [_breakdown(label, groups[label]) for label, _low, _high in bands if groups.get(label)]


def build_report(items: Sequence) -> JournalReport:
    """Aggregate settled candidates into the whole report.

    Anything unresolved is dropped rather than counted as a scratch. An open position
    is not a flat one, and letting them through would dilute every mean here toward
    zero by exactly the number of candidates still waiting on an expiry.
    """
    settled = [item for item in items if item.profit is not None]

    if not settled:
        return JournalReport(
            trades=0,
            clusters=0,
            wins=0,
            losses=0,
            scratches=0,
            win_rate=None,
            expectancy=None,
            avg_win=None,
            avg_loss=None,
            profit_factor=None,
            total_profit=0.0,
            max_drawdown=0.0,
            best_day=None,
            worst_day=None,
            settlement_dates=0,
            days=[],
            by_strategy=[],
            by_symbol=[],
            by_dte=[],
            by_score=[],
            notes=["Nothing has settled yet. `optscan resolve` fills this in after an expiry."],
        )

    wins = [item.profit for item in settled if item.profit > 0]
    losses = [item.profit for item in settled if item.profit < 0]
    scratches = len(settled) - len(wins) - len(losses)

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    points = daily_series(settled)
    clusters = count_clusters(settled)

    notes: list[str] = []
    if clusters < MIN_CLUSTERS_FOR_A_CLAIM:
        notes.append(
            f"{len(settled)} settled candidates, but only {clusters} independent "
            f"(symbol, expiry) clusters. Below {MIN_CLUSTERS_FOR_A_CLAIM} nothing here "
            "supports a conclusion; the figures are shown so the pipeline can be seen "
            "working."
        )
    # The (symbol, expiry) cluster is the project's unit of independence and it is the
    # right one for a question about one chain. It is still too generous for a question
    # about the whole screen: six symbols settling on the same Friday share one market
    # move, so they are nowhere near six independent draws. The count of distinct
    # settlement dates is the pessimistic bound, and when it is small it is the number
    # that actually governs, so it is stated next to the cluster count rather than left
    # for a reader to work out.
    if len(points) < MIN_CLUSTERS_FOR_A_CLAIM:
        notes.append(
            f"Those clusters fall on only {len(points)} distinct settlement "
            f"{'date' if len(points) == 1 else 'dates'}. Symbols expiring together share "
            "one market move, so the effective sample is closer to that number than to "
            "the cluster count, and the intervals below are optimistic even so."
        )
    # No provenance note here any more. The report used to describe screen candidates
    # settled at expiry and said so; it now reads real broker fills, and that sentence
    # sat under the trader's own trades telling them none of it had been traded.

    return JournalReport(
        trades=len(settled),
        clusters=clusters,
        wins=len(wins),
        losses=len(losses),
        scratches=scratches,
        win_rate=proportion_interval(settled, _won),
        expectancy=expectancy(settled),
        avg_win=statistics.fmean(wins) if wins else None,
        avg_loss=statistics.fmean(losses) if losses else None,
        # None rather than infinity when nothing lost: a factor with an empty
        # denominator is undefined, and rendering it as a huge number would read as an
        # extraordinary result rather than an absent one.
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else None,
        total_profit=sum(item.profit for item in settled),
        max_drawdown=max_drawdown(points),
        best_day=max(points, key=lambda point: point.profit),
        worst_day=min(points, key=lambda point: point.profit),
        settlement_dates=len(points),
        days=points,
        by_strategy=group_by(settled, "strategy"),
        by_symbol=group_by(settled, "symbol"),
        by_dte=group_by_band(settled, "dte", DTE_BANDS),
        by_score=group_by_band(settled, "score", SCORE_BANDS),
        notes=notes,
    )
