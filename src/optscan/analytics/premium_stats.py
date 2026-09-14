"""What the premium study's trades are worth as evidence.

## The unit is the session, not the trade

A put spread on QQQ and one on SPY entered the same morning share almost all of their
variance, and so do a condor's two sides. Every interval here resamples whole blocks of
sessions, carrying every trade in them, so a day with four positions counts as one
observation of one market. Blocks are a trading week, because volatility clusters and
a bad Monday is not independent of the Tuesday after it.

## Rules are compared on the same entries

Two exit rules applied to identical positions differ only by what the rule did, so
their difference is taken session by session and resampled as one series. That paired
difference is far more precise than comparing two independent means, and it is the
honest way to answer "does a stop help": the market noise both share cancels.

## Many cells, one correction

A grid of rules is hundreds of tests. Every p-value is reported beside its
Benjamini-Hochberg q-value over the grid it came from, and a cell with fewer than
`MIN_SESSIONS` sessions reports nothing at all.

## The tail is stated, not averaged away

Short premium has a fat left tail by construction, so every cell reports its worst day
and week, its drawdown, and what the total would be without its best and worst five
days. A result that turns on five days says so in its own row.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Callable, Hashable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date

from optscan.analytics.backtest import MIN_BOOTSTRAP_BLOCKS, weekday_ordinal

#: Sessions a cell needs before it reports anything.
MIN_SESSIONS = 30

#: Sessions per resampled block.
BLOCK_SESSIONS = 5

BOOTSTRAP_DRAWS = 2000
CONFIDENCE = 0.95

#: How many of the best and worst days the tail rows take out.
TAIL_DAYS = 5


@dataclass(frozen=True, slots=True)
class CellStats:
    trades: int
    sessions: int
    reportable: bool
    mean_pnl: float
    ci_low: float | None
    ci_high: float | None
    p_value: float | None
    mean_r: float
    win_rate: float
    total: float
    worst_day: float
    worst_week: float
    max_drawdown: float
    without_worst: float
    without_best: float
    #: Mean per trade with each calendar month left out, lowest and highest.
    month_low: float | None
    month_high: float | None


@dataclass(frozen=True, slots=True)
class Difference:
    sessions: int
    reportable: bool
    mean: float
    ci_low: float | None
    ci_high: float | None
    p_value: float | None


def _block(day: date) -> int:
    return weekday_ordinal(day) // BLOCK_SESSIONS


def _bootstrap(
    groups: dict[int, tuple[float, int]], *, seed: int, draws: int = BOOTSTRAP_DRAWS
) -> tuple[float, float, float] | None:
    """Interval and two sided p on a mean, resampling whole blocks.

    `groups` maps a block to (sum, count). Returns (low, high, p) or None with fewer
    than two blocks, where no resampling can say anything.
    """
    if len(groups) < MIN_BOOTSTRAP_BLOCKS:
        return None
    blocks = list(groups.values())
    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        picked = rng.choices(blocks, k=len(blocks))
        count = sum(n for _, n in picked)
        means.append(sum(total for total, _ in picked) / count)
    means.sort()
    tail = (1.0 - CONFIDENCE) / 2.0
    low = means[int(tail * draws)]
    high = means[min(draws - 1, int((1.0 - tail) * draws))]
    below = sum(1 for value in means if value <= 0.0) / draws
    above = sum(1 for value in means if value >= 0.0) / draws
    return low, high, min(1.0, 2.0 * min(below, above))


def daily_totals(trades: Iterable) -> dict[date, float]:
    totals: dict[date, float] = defaultdict(float)
    for trade in trades:
        totals[trade.session] += trade.pnl
    return dict(sorted(totals.items()))


def _drawdown(daily: Sequence[float]) -> float:
    peak = running = worst = 0.0
    for value in daily:
        running += value
        peak = max(peak, running)
        worst = min(worst, running - peak)
    return -worst


def cell_stats(trades: Sequence, *, seed: int = 0) -> CellStats | None:
    """Everything a grid cell reports. None for an empty cell."""
    if not trades:
        return None
    pnls = [trade.pnl for trade in trades]
    daily = daily_totals(trades)
    sessions = len(daily)
    groups: dict[int, tuple[float, int]] = defaultdict(lambda: (0.0, 0))
    for trade in trades:
        total, count = groups[_block(trade.session)]
        groups[_block(trade.session)] = (total + trade.pnl, count + 1)
    reportable = sessions >= MIN_SESSIONS
    interval = _bootstrap(groups, seed=seed) if reportable else None

    weeks: dict[int, float] = defaultdict(float)
    for day, value in daily.items():
        weeks[_block(day)] += value
    ordered = sorted(daily.values())
    total = sum(pnls)

    months: dict[tuple[int, int], list[float]] = defaultdict(list)
    for trade in trades:
        months[(trade.session.year, trade.session.month)].append(trade.pnl)
    leave_out = [
        (total - sum(values)) / (len(pnls) - len(values))
        for values in months.values()
        if len(pnls) > len(values)
    ]

    return CellStats(
        trades=len(trades),
        sessions=sessions,
        reportable=reportable,
        mean_pnl=total / len(pnls),
        ci_low=interval[0] if interval else None,
        ci_high=interval[1] if interval else None,
        p_value=interval[2] if interval else None,
        mean_r=sum(trade.r for trade in trades) / len(trades),
        win_rate=sum(1 for value in pnls if value > 0) / len(pnls),
        total=total,
        worst_day=ordered[0],
        worst_week=min(weeks.values()),
        max_drawdown=_drawdown(list(daily.values())),
        without_worst=total - sum(ordered[:TAIL_DAYS]),
        without_best=total - sum(ordered[-TAIL_DAYS:]),
        month_low=min(leave_out) if leave_out and reportable else None,
        month_high=max(leave_out) if leave_out and reportable else None,
    )


def paired_difference(
    first: Sequence,
    second: Sequence,
    key: Callable[[object], Hashable] = lambda trade: (trade.session, trade.underlying),
    *,
    seed: int = 0,
) -> Difference | None:
    """Mean of `first - second` over the positions both rules traded.

    Positions are matched on `key`, which by default is the session and underlying: two
    rules applied to one grid of entries trade the same positions. A position only one
    of them traded, which happens when a fill assumption refuses it, is left out of both.
    """
    left = {key(trade): trade.pnl for trade in first}
    right = {key(trade): trade.pnl for trade in second}
    shared = sorted(set(left) & set(right), key=str)
    if not shared:
        return None
    groups: dict[int, tuple[float, int]] = defaultdict(lambda: (0.0, 0))
    days: set[date] = set()
    for item in shared:
        session = item[0] if isinstance(item, tuple) else item
        days.add(session)
        total, count = groups[_block(session)]
        groups[_block(session)] = (total + left[item] - right[item], count + 1)
    reportable = len(days) >= MIN_SESSIONS
    interval = _bootstrap(groups, seed=seed) if reportable else None
    mean = sum(left[item] - right[item] for item in shared) / len(shared)
    return Difference(
        sessions=len(days),
        reportable=reportable,
        mean=mean,
        ci_low=interval[0] if interval else None,
        ci_high=interval[1] if interval else None,
        p_value=interval[2] if interval else None,
    )


def benjamini_hochberg(p_values: Sequence[float | None]) -> list[float | None]:
    """q-values, in the input order. A None p stays None and is not counted as a test."""
    indexed = [(p, i) for i, p in enumerate(p_values) if p is not None]
    count = len(indexed)
    out: list[float | None] = [None] * len(p_values)
    running = 1.0
    for rank, (p, i) in sorted(
        ((rank, item) for rank, item in enumerate(sorted(indexed), start=1)), reverse=True
    ):
        running = min(running, p * count / rank)
        out[i] = running
    return out
