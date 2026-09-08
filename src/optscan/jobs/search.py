"""Searching a space of strategies without fooling yourself about the winner.

Running one backtest answers "did this rule work". Running two hundred and reporting the
best answers nothing at all unless the search itself is accounted for, because the
maximum of two hundred noisy numbers is large whether or not anything in the space has an
edge. This module exists to make the second thing safe.

## The three things it does that a loop over backtests does not

**It holds out a period and never looks at it during the search.** History is split by
date. Every candidate is ranked on the training period only; the test period is untouched
until a winner has been chosen. The out-of-sample number is the headline, and the
in-sample number is reported next to it mostly so the gap between them is visible. That
gap is the most informative quantity here: a rule that returns 4% in-sample and 0%
out-of-sample was mined, and no p-value on the training period would have said so.

**It says how many things it tried.** A search over two hundred combinations gets two
hundred chances at a 1-in-20 fluke. `expected_best_under_null` is what the best of that
many looks like when nothing in the space works, and a winner below it is not a finding.

**It refuses to search the test period.** There is no flag to turn that off. A held-out
period that has been peeked at is not held out, and the peeking is never deliberate: it
happens when somebody re-runs the search after seeing the out-of-sample result and
changes the grid. That is why the split is a parameter of the search rather than of the
report, and why re-running with a different grid is recorded as a new search.

## What it still cannot fix

The grid was written by a person who has already looked at this market for a decade, and
that prior selection is invisible to every statistic here. Nor does a held-out period
make a result durable: 2023 to 2026 is one regime, and a rule that survives it has
survived one thing.

Read the out-of-sample column, then read how many candidates were tried, then decide
whether the remaining margin is worth anything. The order matters.
"""

from __future__ import annotations

import math
import statistics
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from optscan.analytics import rules as rule_registry
from optscan.analytics.backtest import (
    MIN_BLOCKS_FOR_A_CLAIM,
    Direction,
    Stats,
    Trade,
    forward_return_table,
    measure_edge,
    null_distribution,
    shift_null,
    simulate,
    summarize,
)
from optscan.config import Settings
from optscan.jobs.backtest import Strategy, load_series, resolve_symbols
from optscan.logging import get_logger
from optscan.models import PriceBar

log = get_logger("optscan.jobs.search")

#: Share of history used to search. The rest is held out. Two thirds leaves roughly three
#: years of test on a ten year history, which is few enough independent blocks that the
#: out-of-sample number is noisy, and still the most honest number available.
DEFAULT_TRAIN_SHARE = 0.67

#: Candidates carried from the search into out-of-sample evaluation. Small on purpose:
#: evaluating twenty finalists out of sample is twenty more chances at a fluke, and the
#: correction for that would undo what the holdout bought.
DEFAULT_FINALISTS = 3

#: Null draws used while ranking. Coarse on purpose: this number only has to order the
#: candidates, and the finalists are re-measured properly afterwards.
SEARCH_DRAWS = 120

#: Null draws for the finalists. The search phase runs no null at all: ranking two
#: hundred candidates by mean return costs nothing, and a null per candidate would make
#: the search take an hour to produce a number that gets discarded.
DEFAULT_DRAWS = 500

#: The default space. Deliberately modest. Every combination added is another chance at a
#: fluke, and a grid large enough to guarantee a winner is a grid that has stopped
#: measuring anything.
DEFAULT_GRID: dict[str, list] = {
    "rsi_below": [{"threshold": t} for t in (25, 30, 35)],
    "rsi_above": [{"threshold": t} for t in (65, 70, 75)],
    "squeeze": [{}],
    "volume_surge": [{"multiple": m} for m in (2.0, 3.0)],
    "above_ma": [{"period": p} for p in (50, 200)],
    "below_ma": [{"period": p} for p in (50, 200)],
}

#: Where the report calls a holdout result significant. Conventional, and it is one test
#: of one finalist rather than a discovery.
SIGNIFICANT = 0.05

#: Below two candidates there is no multiplicity to correct for.
MIN_CANDIDATES_FOR_CORRECTION = 2

#: Share of the in-sample result that may vanish out of sample before the report calls it
#: mining. Half is generous: real edges decay too, and the point is to flag collapse.
MAX_TOLERABLE_DECAY = 0.5

DEFAULT_HORIZONS = (5, 21, 63)
DEFAULT_DIRECTIONS = (Direction.LONG, Direction.SHORT)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One point in the grid, scored in sample."""

    entry: str
    params: dict
    horizon: int
    direction: str
    stats: Stats | None
    mean_return: float
    #: Mean of this candidate's own null. A 63 day hold and a 5 day hold have completely
    #: different baselines, so this is what makes them comparable.
    null_mean: float = 0.0
    #: mean_return - null_mean, in units of the null's own spread. The ranking key.
    #: Ranking on the raw mean instead ranks the horizon: on a first run every one of the
    #: top twelve candidates was a 63 day hold, because three months of drift beats any
    #: rule's contribution regardless of the rule.
    z_score: float = 0.0

    @property
    def edge(self) -> float:
        return self.mean_return - self.null_mean

    @property
    def label(self) -> str:
        detail = ",".join(f"{k}={v}" for k, v in self.params.items())
        return f"{self.entry}({detail}) {self.direction} {self.horizon}d"

    def as_strategy(self, template: Strategy) -> Strategy:
        spec = template.to_dict()
        spec.update(
            entry=self.entry,
            entry_params=dict(self.params),
            horizon=self.horizon,
            direction=self.direction,
            name=self.label,
        )
        return Strategy.from_dict(spec)


@dataclass(frozen=True, slots=True)
class Finalist:
    """A candidate carried through to the held-out period."""

    candidate: Candidate
    in_sample_mean: float
    out_sample_mean: float | None
    out_sample_trades: int
    out_sample_blocks: int
    out_sample_p: float | None
    out_sample_edge: float | None

    @property
    def decay(self) -> float | None:
        """In-sample minus out-of-sample. The number that exposes mining."""
        if self.out_sample_mean is None:
            return None
        return self.in_sample_mean - self.out_sample_mean


@dataclass
class SearchResult:
    train_end: date | None = None
    candidates: list[Candidate] = field(default_factory=list)
    finalists: list[Finalist] = field(default_factory=list)
    tried: int = 0
    expected_best_under_null: float | None = None
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def as_dict(self) -> dict:
        return {
            "train_end": self.train_end.isoformat() if self.train_end else None,
            "tried": self.tried,
            "expected_best_under_null": self.expected_best_under_null,
            "candidates": [
                {
                    "label": c.label,
                    "entry": c.entry,
                    "params": c.params,
                    "horizon": c.horizon,
                    "direction": c.direction,
                    "mean_return": c.mean_return,
                    "null_mean": c.null_mean,
                    "edge": c.edge,
                    "z_score": c.z_score,
                    "trades": c.stats.trades if c.stats else 0,
                    "blocks": c.stats.effective_sample if c.stats else 0,
                }
                for c in self.candidates
            ],
            "finalists": [
                {
                    "label": f.candidate.label,
                    "in_sample_mean": f.in_sample_mean,
                    "out_sample_mean": f.out_sample_mean,
                    "out_sample_trades": f.out_sample_trades,
                    "out_sample_blocks": f.out_sample_blocks,
                    "out_sample_p": f.out_sample_p,
                    "out_sample_edge": f.out_sample_edge,
                    "decay": f.decay,
                }
                for f in self.finalists
            ],
            "notes": self.notes,
            "seconds": self.seconds,
        }


def split_index(bars: Sequence[PriceBar], train_end: date) -> int:
    """First bar index on or after `train_end`. Everything before it is training."""
    for index, bar in enumerate(bars):
        if bar.ts.date() >= train_end:
            return index
    return len(bars)


def boundary_date(
    series: dict[str, list[PriceBar]], share: float = DEFAULT_TRAIN_SHARE
) -> date | None:
    """The date that splits the pooled history by calendar, not per symbol.

    One date for every symbol rather than a per-symbol percentile. A per-symbol split
    would put a short-history symbol's test period in 2019 and a long one's in 2024,
    so the held-out set would span different market regimes for different names and the
    comparison across candidates would be meaningless.
    """
    days = sorted({bar.ts.date() for bars in series.values() for bar in bars})
    if not days:
        return None
    return days[min(len(days) - 1, int(len(days) * share))]


def _entries_in(
    bars: Sequence[PriceBar],
    entries: Sequence[int],
    *,
    low: int,
    high: int,
    horizon: int,
) -> list[int]:
    """Entries whose whole trade falls inside [low, high).

    The `horizon` margin is what keeps a training trade from settling in the test period.
    Without it the last month of training reads the first month of the holdout, which is
    the quiet way a split stops being a split.
    """
    return [i for i in entries if low <= i and i + horizon + 1 < high]


def run_search(
    settings: Settings,
    template: Strategy,
    *,
    grid: dict[str, list] | None = None,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    directions: Sequence[Direction] = DEFAULT_DIRECTIONS,
    train_share: float = DEFAULT_TRAIN_SHARE,
    finalists: int = DEFAULT_FINALISTS,
    draws: int = DEFAULT_DRAWS,
    search_draws: int = SEARCH_DRAWS,
    min_blocks: int = MIN_BLOCKS_FOR_A_CLAIM,
) -> SearchResult:
    """Search the grid on the training period, then evaluate the winners out of sample."""
    started = time.perf_counter()
    result = SearchResult()
    grid = grid or DEFAULT_GRID

    series, skipped = load_series(settings, resolve_symbols(settings, template))
    if not series:
        result.notes.append("No symbol had enough stored history.")
        return result

    split = boundary_date(series, train_share)
    result.train_end = split
    splits = {symbol: split_index(bars, split) for symbol, bars in series.items()}

    # Entries depend only on the rule and its parameters, not on the horizon or the
    # direction, so they are computed once and reused across the rest of the grid. With
    # three horizons and two directions that is six times less work, and the level rules
    # are expensive enough that it is the difference between minutes and an hour.
    fired: dict[tuple[str, str], dict[str, list[int]]] = {}
    for entry, variants in grid.items():
        for params in variants:
            key = (entry, repr(sorted(params.items())))
            rule = rule_registry.resolve(entry, params)
            fired[key] = {symbol: rule(bars) for symbol, bars in series.items()}

    # One forward return table per (horizon, direction), shared by every rule in the
    # grid. The table depends on the exit rule and not on what triggered the entry, so
    # building it per candidate would repeat identical work dozens of times.
    train_bars = {symbol: bars[: splits[symbol]] for symbol, bars in series.items()}
    tables: dict[tuple[int, Direction], dict[str, list[float | None]]] = {}
    for horizon in horizons:
        for direction in directions:
            tables[(horizon, direction)] = {
                symbol: forward_return_table(
                    bars, direction=direction, horizon=horizon, cost=template.cost
                )
                for symbol, bars in train_bars.items()
            }

    result.candidates = _score_grid(
        series,
        splits,
        grid,
        fired,
        tables,
        horizons=horizons,
        directions=directions,
        cost=template.cost,
        block_bars=template.block_bars,
        draws=search_draws,
        seed=template.seed,
    )

    result.tried = len(result.candidates)
    # Ranked on the training period only. The holdout has not been touched yet, and this
    # ordering is the last decision allowed to see it.
    ranked = sorted(
        (c for c in result.candidates if c.stats and c.stats.effective_sample >= min_blocks),
        key=lambda c: c.z_score,
        reverse=True,
    )
    result.candidates = sorted(result.candidates, key=lambda c: c.z_score, reverse=True)

    if not ranked:
        result.notes.append(
            f"No candidate produced at least {min_blocks} independent blocks in the "
            "training period, so there is nothing worth carrying to the holdout."
        )
        result.seconds = round(time.perf_counter() - started, 1)
        return result

    for candidate in ranked[:finalists]:
        result.finalists.append(
            _evaluate_out_of_sample(candidate, template, series, splits, draws=draws)
        )

    result.expected_best_under_null = expected_best_z(result.tried)
    result.notes.extend(_interpretation(result, skipped, min_blocks))
    result.seconds = round(time.perf_counter() - started, 1)
    log.info(
        "strategy search",
        tried=result.tried,
        finalists=len(result.finalists),
        seconds=result.seconds,
    )
    return result


def _score_grid(
    series: dict[str, list[PriceBar]],
    splits: dict[str, int],
    grid: dict[str, list],
    fired: dict[tuple[str, str], dict[str, list[int]]],
    tables: dict[tuple[int, Direction], dict[str, list[float | None]]],
    *,
    horizons: Sequence[int],
    directions: Sequence[Direction],
    cost: float,
    block_bars: int,
    draws: int,
    seed: int,
) -> list[Candidate]:
    """Every cell of the grid, scored on the training period against its own null."""
    candidates: list[Candidate] = []
    for entry, variants in grid.items():
        for params in variants:
            key = (entry, repr(sorted(params.items())))
            for horizon in horizons:
                for direction in directions:
                    trades: list[Trade] = []
                    used: dict[str, list[int]] = {}
                    for symbol, bars in series.items():
                        window = _entries_in(
                            bars,
                            fired[key][symbol],
                            low=0,
                            high=splits[symbol],
                            horizon=horizon,
                        )
                        if not window:
                            continue
                        found = simulate(
                            symbol,
                            bars,
                            window,
                            direction=direction,
                            horizon=horizon,
                            cost=cost,
                        )
                        if found:
                            trades.extend(found)
                            used[symbol] = [t.entry_index - 1 for t in found]

                    stats = summarize(trades, block_bars)
                    null_mean, z_score = _score(
                        stats,
                        used,
                        tables[(horizon, direction)],
                        draws=draws,
                        seed=seed,
                    )
                    candidates.append(
                        Candidate(
                            entry=entry,
                            params=dict(params),
                            horizon=horizon,
                            direction=direction.value,
                            stats=stats,
                            mean_return=stats.mean_return if stats else float("-inf"),
                            null_mean=null_mean,
                            z_score=z_score if stats else float("-inf"),
                        )
                    )

    return candidates


def _score(
    stats: Stats | None,
    used: dict[str, list[int]],
    table: dict[str, list[float | None]],
    *,
    draws: int,
    seed: int,
) -> tuple[float, float]:
    """One candidate's null mean and z-score, from a shared forward return table.

    The z-score is the ranking key rather than the raw mean, because the raw mean ranks
    the holding period: three months of market drift beats any rule's contribution, so a
    grid scored on raw return returns the longest horizon in the grid, whatever the rules
    were. Dividing by the candidate's own null spread makes a five day rule and a sixty
    three day rule comparable.
    """
    if stats is None or not used:
        return 0.0, 0.0
    nulls = shift_null(table, used, draws=draws, seed=seed)
    if not nulls:
        return 0.0, 0.0
    null_mean = sum(nulls) / len(nulls)
    spread = statistics.pstdev(nulls) if len(nulls) > 1 else 0.0
    if spread <= 0:
        return null_mean, 0.0
    return null_mean, (stats.mean_return - null_mean) / spread


def _evaluate_out_of_sample(
    candidate: Candidate,
    template: Strategy,
    series: dict[str, list[PriceBar]],
    splits: dict[str, int],
    *,
    draws: int,
) -> Finalist:
    """Run one finalist on the held-out period, with its own null."""
    rule = rule_registry.resolve(candidate.entry, candidate.params)
    direction = Direction(candidate.direction)

    trades: list[Trade] = []
    fired: dict[str, list[int]] = {}
    for symbol, bars in series.items():
        window = _entries_in(
            bars,
            rule(bars),
            low=splits[symbol],
            high=len(bars),
            horizon=candidate.horizon,
        )
        if not window:
            continue
        found = simulate(
            symbol,
            bars,
            window,
            direction=direction,
            horizon=candidate.horizon,
            cost=template.cost,
        )
        if found:
            trades.extend(found)
            fired[symbol] = [t.entry_index - 1 for t in found]

    stats = summarize(trades, template.block_bars)
    if stats is None:
        return Finalist(
            candidate=candidate,
            in_sample_mean=candidate.mean_return,
            out_sample_mean=None,
            out_sample_trades=0,
            out_sample_blocks=0,
            out_sample_p=None,
            out_sample_edge=None,
        )

    # The null is built from the held-out bars only. Shifting across the whole history
    # would let the training period leak back in through the null's returns.
    holdout = {symbol: bars[splits[symbol] :] for symbol, bars in series.items()}
    shifted = {
        symbol: [i - splits[symbol] for i in indices if i >= splits[symbol]]
        for symbol, indices in fired.items()
    }
    nulls = null_distribution(
        holdout,
        shifted,
        direction=direction,
        horizon=candidate.horizon,
        cost=template.cost,
        draws=draws,
        block_bars=template.block_bars,
    )
    edge = measure_edge(stats.mean_return, nulls)

    return Finalist(
        candidate=candidate,
        in_sample_mean=candidate.mean_return,
        out_sample_mean=stats.mean_return,
        out_sample_trades=stats.trades,
        out_sample_blocks=stats.effective_sample,
        out_sample_p=edge.p_value if edge else None,
        out_sample_edge=edge.edge if edge else None,
    )


def expected_best_z(tried: int) -> float | None:
    """The z-score the best of `tried` candidates reaches when nothing has an edge.

    For n independent draws from a null, the expected maximum sits about sqrt(2 ln n)
    standard deviations above the mean: 2.6 at twenty candidates, 3.0 at eighty, 3.5 at
    five hundred. Comparing the winner's z against this, rather than against zero, is the
    whole correction for having searched.

    Independence is generous here, since grid cells share rules and horizons and
    correlated draws have a smaller maximum. The bar is therefore a little too high,
    which is the direction to be wrong in.
    """
    if tried < MIN_CANDIDATES_FOR_CORRECTION:
        return None
    return math.sqrt(2 * math.log(tried))


def _interpretation(result: SearchResult, skipped: dict[str, str], min_blocks: int) -> list[str]:
    notes = [
        f"{result.tried} combinations were tried and ranked on data up to "
        f"{result.train_end}. Everything after that date was untouched until the "
        f"top {len(result.finalists)} were chosen, and those out-of-sample numbers are "
        "the only ones worth reading as evidence."
    ]

    for finalist in result.finalists:
        if finalist.out_sample_mean is None:
            notes.append(
                f"{finalist.candidate.label} produced no trades in the holdout, so it "
                "cannot be evaluated there."
            )
            continue
        decay = finalist.decay
        if finalist.out_sample_blocks < min_blocks:
            notes.append(
                f"{finalist.candidate.label} has only {finalist.out_sample_blocks} "
                f"independent blocks out of sample, below {min_blocks}. Its holdout "
                "number is not evidence either way."
            )
        elif decay is not None and decay > abs(finalist.in_sample_mean) * MAX_TOLERABLE_DECAY:
            notes.append(
                f"{finalist.candidate.label} returned {finalist.in_sample_mean:.2%} in "
                f"sample and {finalist.out_sample_mean:.2%} out of it. Most of the "
                "in-sample result did not survive, which is what mining looks like."
            )
        elif finalist.out_sample_p is not None and finalist.out_sample_p <= SIGNIFICANT:
            notes.append(
                f"{finalist.candidate.label} held up out of sample: "
                f"{finalist.out_sample_mean:.2%} against its null at "
                f"p={finalist.out_sample_p:.3f}. One holdout in one regime is a reason "
                "to keep watching it, not a reason to trade it."
            )
        else:
            notes.append(
                f"{finalist.candidate.label} returned {finalist.out_sample_mean:.2%} out "
                f"of sample at p={finalist.out_sample_p:.3f}, which is not distinguishable "
                "from entering at other times."
            )

    best = result.candidates[0] if result.candidates else None
    if result.expected_best_under_null is not None and best is not None:
        verdict = "clears" if best.z_score > result.expected_best_under_null else "does not clear"
        notes.append(
            f"Searching {result.tried} combinations gives the best of them a z of about "
            f"{result.expected_best_under_null:.2f} even when nothing in the space works. "
            f"The winner in sample scored {best.z_score:.2f}, which {verdict} that bar. "
            "This is why the holdout exists: the in-sample ranking cannot settle it."
        )

    if len(result.finalists) > 1:
        notes.append(
            f"{len(result.finalists)} finalists were then tested out of sample, so a "
            "p-value here is one of that many. The smallest of three independent "
            "p-values lands below 0.034 about one time in ten by chance."
        )

    notes.append(
        "The grid was written by somebody who has already seen this market, and that "
        "prior selection is invisible to every number here. A held-out period also does "
        "not make a result durable: the holdout is one regime."
    )
    if skipped:
        notes.append(f"{len(skipped)} symbols were skipped for lack of history.")
    return notes
