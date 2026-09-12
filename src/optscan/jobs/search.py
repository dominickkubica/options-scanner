"""Searching a space of strategies without fooling yourself about the winner.

Running one backtest answers "did this rule work". Running two hundred and reporting the
best answers nothing at all unless the search itself is accounted for, because the
maximum of two hundred noisy numbers is large whether or not anything in the space has an
edge. This module exists to make the second thing safe.

## What it does that a loop over backtests does not

**It holds out a contiguous period and never looks at it during the search.** History
is split by date, one date for every symbol. Every candidate is ranked on the training
period only; the test period is untouched until a winner has been chosen. A randomly
shuffled split would feel more rigorous and be less so: nearby days are autocorrelated,
so shuffling puts one market episode on both sides of the line. The out-of-sample number
is the headline, and the gap between it and the in-sample one is the most informative
quantity here: a rule that returns 4% in-sample and 0% out-of-sample was mined, and no
p-value on the training period would have said so.

**Nothing about the test period reaches the ranking.** Training trades must settle
before the boundary, and each symbol's trading cost is measured on the training bars
alone. It used to be measured over the whole history, so the ranking was charged spreads
partly observed in the holdout: small, and exactly the kind of leak that is invisible
from inside the result.

**It can walk forward.** One holdout is one regime. `run_walk_forward` repeats the
whole search on an expanding training window and scores each fold's winner on the next
stretch alone, so every test window is used once and no choice ever saw its own test.
A search that picks a different winner every fold is choosing noise.

**It says how many things it tried, and how many times the holdout was looked at.** A
search over two hundred combinations gets two hundred chances at a fluke;
`expected_best_z` is what the best of that many looks like when nothing works. And every
out-of-sample evaluation is written to a ledger (`storage/holdout.py`), because the
peeking that ruins a holdout is never deliberate: it is somebody re-running the search
after a disappointing result. The significance bar rises with every finalist ever tested
on the same data.

## What it still cannot fix

The grid was written by a person who has already looked at this market for a decade, and
that prior selection is invisible to every statistic here. Read the out-of-sample
column, then how many candidates were tried and how often the holdout has been used,
then decide whether the remaining margin is worth anything. The order matters.
"""

from __future__ import annotations

import math
import statistics
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

from optscan.analytics import rules as rule_registry
from optscan.analytics.backtest import (
    MIN_BLOCKS_FOR_A_CLAIM,
    Direction,
    Stats,
    Trade,
    forward_return_table,
    measure_edge,
    null_distribution,
    session_positions,
    shift_null,
    simulate,
    summarize,
)
from optscan.config import Settings
from optscan.jobs.backtest import (
    Strategy,
    load_series,
    resolve_symbols,
    symbol_costs,
)
from optscan.logging import get_logger
from optscan.models import PriceBar
from optscan.storage import db
from optscan.storage import holdout as ledger

log = get_logger("optscan.jobs.search")

#: Share of history used to search. The rest is held out. Two thirds leaves roughly three
#: years of test on a ten year history. Left where it is on purpose: moving the boundary
#: after results have been read turns data the earlier searches trained on into "test",
#: which is a holdout assembled from things already seen.
DEFAULT_TRAIN_SHARE = 0.67

#: Candidates carried from the search into out-of-sample evaluation. Small on purpose:
#: evaluating twenty finalists out of sample is twenty more chances at a fluke, and the
#: correction for that would undo what the holdout bought.
DEFAULT_FINALISTS = 3

#: Null draws used while ranking. Coarse on purpose: this number only has to order the
#: candidates, and the finalists are re-measured properly afterwards.
SEARCH_DRAWS = 120

#: Null draws for the finalists.
DEFAULT_DRAWS = 500

#: Walk-forward test windows. Four on a ten year history is about fifteen months each.
DEFAULT_FOLDS = 4

#: One fold is a single holdout, which is what `run_search` already is.
MIN_FOLDS = 2

#: Share of history the first walk-forward fold trains on. Each later fold trains on
#: everything before its own test window, so training grows and every window is tested
#: exactly once.
DEFAULT_FIRST_TRAIN_SHARE = 0.5

#: The default space. Deliberately modest. Every combination added is another chance at a
#: fluke, and a grid large enough to guarantee a winner is a grid that has stopped
#: measuring anything.
DEFAULT_GRID: dict[str, list] = {
    # Short-horizon reversion, the family that has actually shown something here.
    "rsi_below": [{"threshold": t} for t in (25, 30, 35)],
    "ibs_below": [{"threshold": t} for t in (0.1, 0.2, 0.3)],
    "down_days": [{"count": c} for c in (2, 3, 4)],
    "gap_down": [{"size": s} for s in (0.02, 0.04)],
    # The other side of each, so the grid cannot only confirm reversion.
    "rsi_above": [{"threshold": t} for t in (70, 75)],
    "ibs_above": [{"threshold": 0.8}],
    "up_days": [{"count": 3}],
    # Momentum and trend.
    "near_52w_high": [{"within": 0.02}],
    "above_ma": [{"period": p} for p in (50, 200)],
    "below_ma": [{"period": 200}],
    # Volatility and volume.
    "squeeze": [{}],
    "volume_surge": [{"multiple": m} for m in (2.0, 3.0)],
    # A calendar rule with no market input, as a second control alongside every_bar.
    "turn_of_month": [{"days": 3}],
}

#: Where a single, untouched out-of-sample test is called significant. The bar actually
#: applied is lower whenever more than one finalist has been tested on the same data.
SIGNIFICANT = 0.05

#: Below two candidates there is no multiplicity to correct for.
MIN_CANDIDATES_FOR_CORRECTION = 2

#: Share of the in-sample result that may vanish out of sample before the report calls it
#: mining. Half is generous: real edges decay too, and the point is to flag collapse.
MAX_TOLERABLE_DECAY = 0.5

DEFAULT_HORIZONS = (1, 3, 5, 21)
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
    """A candidate carried through to a held-out period."""

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

    def as_dict(self) -> dict:
        return {
            "label": self.candidate.label,
            "in_sample_mean": self.in_sample_mean,
            "in_sample_z": self.candidate.z_score,
            "out_sample_mean": self.out_sample_mean,
            "out_sample_trades": self.out_sample_trades,
            "out_sample_blocks": self.out_sample_blocks,
            "out_sample_p": self.out_sample_p,
            "out_sample_edge": self.out_sample_edge,
            "decay": self.decay,
        }


@dataclass
class SearchResult:
    train_end: date | None = None
    candidates: list[Candidate] = field(default_factory=list)
    finalists: list[Finalist] = field(default_factory=list)
    tried: int = 0
    expected_best_under_null: float | None = None
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0
    #: Earlier out-of-sample evaluations on overlapping data, read from the ledger.
    prior_uses: int = 0
    prior_finalists: int = 0
    #: The p-value a finalist must beat for "significant" to mean what 0.05 means for a
    #: single untouched test, counting every finalist ever tested on this data.
    threshold: float = SIGNIFICANT

    def as_dict(self) -> dict:
        return {
            "train_end": self.train_end.isoformat() if self.train_end else None,
            "tried": self.tried,
            "expected_best_under_null": self.expected_best_under_null,
            "prior_uses": self.prior_uses,
            "prior_finalists": self.prior_finalists,
            "threshold": self.threshold,
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
            "finalists": [f.as_dict() for f in self.finalists],
            "notes": self.notes,
            "seconds": self.seconds,
        }


@dataclass(frozen=True, slots=True)
class Fold:
    """One walk-forward step: trained on everything before `train_end`, tested after."""

    train_end: date
    #: Exclusive. None for the last fold, which runs to the end of the data.
    test_end: date | None
    #: The best candidate on this fold's training data, scored on its test window. None
    #: when nothing in the grid cleared the block floor in training.
    winner: Finalist | None


@dataclass
class WalkForwardResult:
    folds: list[Fold] = field(default_factory=list)
    tried: int = 0
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0
    prior_uses: int = 0
    prior_finalists: int = 0
    threshold: float = SIGNIFICANT

    def as_dict(self) -> dict:
        return {
            "tried": self.tried,
            "prior_uses": self.prior_uses,
            "prior_finalists": self.prior_finalists,
            "threshold": self.threshold,
            "folds": [
                {
                    "train_end": fold.train_end.isoformat(),
                    "test_end": fold.test_end.isoformat() if fold.test_end else None,
                    "winner": fold.winner.as_dict() if fold.winner else None,
                }
                for fold in self.folds
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


def sidak(alpha: float, tests: int) -> float:
    """The per-test threshold that keeps the chance of any false positive at `alpha`."""
    if tests <= 1:
        return alpha
    return 1.0 - (1.0 - alpha) ** (1.0 / tests)


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


def _key(entry: str, params: dict) -> tuple[str, str]:
    return entry, repr(sorted(params.items()))


def _fire_all(
    series: dict[str, list[PriceBar]], grid: dict[str, list]
) -> dict[tuple[str, str], dict[str, list[int]]]:
    """Every rule in the grid, run once on each symbol's whole history.

    Computing a rule on the full series and then slicing it into training and test is
    only sound because no rule reads a bar after the one it fires on, and
    `tests/test_rules.py` checks exactly that for every rule in the registry. Entries do
    not depend on the horizon or the direction, so this is also six times less work.
    """
    fired: dict[tuple[str, str], dict[str, list[int]]] = {}
    for entry, variants in grid.items():
        for params in variants:
            rule = rule_registry.resolve(entry, params)
            fired[_key(entry, params)] = {symbol: rule(bars) for symbol, bars in series.items()}
    return fired


def _rank(
    series: dict[str, list[PriceBar]],
    ends: dict[str, int],
    grid: dict[str, list],
    fired: dict[tuple[str, str], dict[str, list[int]]],
    template: Strategy,
    *,
    horizons: Sequence[int],
    directions: Sequence[Direction],
    draws: int,
) -> list[Candidate]:
    """Score every cell of the grid on the bars before `ends`, ordered by z.

    Everything here, costs included, is computed from the training bars alone.
    """
    train = {symbol: bars[: ends[symbol]] for symbol, bars in series.items() if ends[symbol] > 0}
    costs = symbol_costs(template, train)
    positions, span = session_positions(train)

    # One forward return table per (horizon, direction), shared by every rule. It depends
    # on the exit and not on what triggered the entry.
    tables = {
        (horizon, direction): {
            symbol: forward_return_table(
                bars, direction=direction, horizon=horizon, cost=costs[symbol]
            )
            for symbol, bars in train.items()
        }
        for horizon in horizons
        for direction in directions
    }

    candidates: list[Candidate] = []
    for entry, variants in grid.items():
        for params in variants:
            key = _key(entry, params)
            for horizon in horizons:
                for direction in directions:
                    trades: list[Trade] = []
                    used: dict[str, list[int]] = {}
                    for symbol, bars in train.items():
                        window = _entries_in(
                            bars, fired[key][symbol], low=0, high=len(bars), horizon=horizon
                        )
                        if not window:
                            continue
                        found = simulate(
                            symbol,
                            bars,
                            window,
                            direction=direction,
                            horizon=horizon,
                            cost=costs[symbol],
                        )
                        if found:
                            trades.extend(found)
                            used[symbol] = [t.entry_index - 1 for t in found]

                    stats = summarize(trades, template.block_bars, interval=False)
                    null_mean, z_score = _score(
                        stats,
                        used,
                        tables[(horizon, direction)],
                        positions=positions,
                        span=span,
                        draws=draws,
                        seed=template.seed,
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

    return sorted(candidates, key=lambda c: c.z_score, reverse=True)


def _score(
    stats: Stats | None,
    used: dict[str, list[int]],
    table: dict[str, list[float | None]],
    *,
    positions: dict[str, list[int]],
    span: int,
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
    nulls = shift_null(table, used, draws=draws, seed=seed, positions=positions, span=span)
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
    lows: dict[str, int],
    highs: dict[str, int],
    *,
    draws: int,
) -> Finalist:
    """Run one finalist on the window [lows, highs) alone, against its own null."""
    rule = rule_registry.resolve(candidate.entry, candidate.params)
    direction = Direction(candidate.direction)

    window_bars = {
        symbol: bars[lows[symbol] : highs[symbol]]
        for symbol, bars in series.items()
        if highs[symbol] > lows[symbol]
    }
    # Charged what the test window cost to trade, measured on the test window.
    costs = symbol_costs(template, window_bars)

    trades: list[Trade] = []
    fired: dict[str, list[int]] = {}
    for symbol in window_bars:
        bars = series[symbol]
        window = _entries_in(
            bars,
            rule(bars),
            low=lows[symbol],
            high=highs[symbol],
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
            cost=costs[symbol],
        )
        if found:
            trades.extend(found)
            # Relative to the window, because the null below is built on the window.
            fired[symbol] = [t.entry_index - 1 - lows[symbol] for t in found]

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

    # The null is built from the test window only. Shifting across the whole history
    # would let the training period leak back in through the null's returns.
    nulls = null_distribution(
        window_bars,
        fired,
        direction=direction,
        horizon=candidate.horizon,
        costs=costs,
        draws=draws,
        block_bars=template.block_bars,
    )
    edge = measure_edge(stats.mean_return, nulls, blocks=stats.effective_sample)

    return Finalist(
        candidate=candidate,
        in_sample_mean=candidate.mean_return,
        out_sample_mean=stats.mean_return,
        out_sample_trades=stats.trades,
        out_sample_blocks=stats.effective_sample,
        out_sample_p=edge.p_value if edge else None,
        out_sample_edge=edge.edge if edge else None,
    )


def _read_ledger(
    settings: Settings, start: date, end: date, symbols: Sequence[str]
) -> tuple[int, int]:
    """(earlier evaluations, finalists they tested) on any of this data."""
    with db.session(settings.sqlite_path) as conn:
        uses = ledger.prior_uses(conn, test_start=start, test_end=end, symbols=symbols)
    return len(uses), sum(use.finalists for use in uses)


def _write_ledger(
    settings: Settings,
    *,
    kind: str,
    start: date,
    end: date,
    symbols: Sequence[str],
    tried: int,
    finalists: Sequence[Finalist],
) -> None:
    with db.session(settings.sqlite_path) as conn:
        ledger.record_use(
            conn,
            kind=kind,
            test_start=start,
            test_end=end,
            symbols=symbols,
            tried=tried,
            finalists=[(f.candidate.label, f.out_sample_p) for f in finalists],
        )


def _last_day(series: dict[str, list[PriceBar]]) -> date:
    return max(bars[-1].ts.date() for bars in series.values())


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
    record: bool = True,
) -> SearchResult:
    """Search the grid on the training period, then evaluate the winners out of sample.

    `record` writes the out-of-sample evaluation to the holdout ledger. There is no
    reason to turn it off outside a test: an unrecorded look at a holdout is exactly the
    kind that makes the next one overconfident.
    """
    started = time.perf_counter()
    result = SearchResult()
    grid = grid or DEFAULT_GRID

    series, skipped = load_series(settings, resolve_symbols(settings, template))
    if not series:
        result.notes.append("No symbol had enough stored history.")
        return result

    split = boundary_date(series, train_share)
    if split is None:
        result.notes.append("No stored sessions to split.")
        return result
    result.train_end = split
    splits = {symbol: split_index(bars, split) for symbol, bars in series.items()}
    ends = {symbol: len(bars) for symbol, bars in series.items()}

    fired = _fire_all(series, grid)
    result.candidates = _rank(
        series,
        splits,
        grid,
        fired,
        template,
        horizons=horizons,
        directions=directions,
        draws=search_draws,
    )
    result.tried = len(result.candidates)

    # Ranked on the training period only. The holdout has not been touched yet, and this
    # ordering is the last decision allowed to see it.
    ranked = [c for c in result.candidates if c.stats and c.stats.effective_sample >= min_blocks]
    if not ranked:
        # Nothing was carried to the holdout, so the holdout was not looked at and there
        # is nothing to record.
        result.notes.append(
            f"No candidate produced at least {min_blocks} independent blocks in the "
            "training period, so there is nothing worth carrying to the holdout."
        )
        result.seconds = round(time.perf_counter() - started, 1)
        return result

    for candidate in ranked[:finalists]:
        result.finalists.append(
            _evaluate_out_of_sample(candidate, template, series, splits, ends, draws=draws)
        )

    tested = sorted(symbol for symbol in series if splits[symbol] < ends[symbol])
    end = _last_day(series) + timedelta(days=1)
    result.prior_uses, result.prior_finalists = _read_ledger(settings, split, end, tested)
    result.threshold = sidak(SIGNIFICANT, result.prior_finalists + len(result.finalists))
    if record:
        _write_ledger(
            settings,
            kind="search",
            start=split,
            end=end,
            symbols=tested,
            tried=result.tried,
            finalists=result.finalists,
        )

    result.expected_best_under_null = expected_best_z(result.tried)
    result.notes.extend(_interpretation(result, skipped, min_blocks))
    result.seconds = round(time.perf_counter() - started, 1)
    log.info(
        "strategy search",
        tried=result.tried,
        finalists=len(result.finalists),
        prior_uses=result.prior_uses,
        seconds=result.seconds,
    )
    return result


def run_walk_forward(
    settings: Settings,
    template: Strategy,
    *,
    grid: dict[str, list] | None = None,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    directions: Sequence[Direction] = DEFAULT_DIRECTIONS,
    folds: int = DEFAULT_FOLDS,
    first_train_share: float = DEFAULT_FIRST_TRAIN_SHARE,
    draws: int = DEFAULT_DRAWS,
    search_draws: int = SEARCH_DRAWS,
    min_blocks: int = MIN_BLOCKS_FOR_A_CLAIM,
    record: bool = True,
) -> WalkForwardResult:
    """The search, repeated on an expanding window, each winner tested on what follows.

    Fold k trains on everything before its boundary, picks the single best candidate
    there, and scores it on the stretch up to the next boundary. Only one winner per fold
    is carried forward, so each window is one test rather than several.
    """
    if folds < MIN_FOLDS:
        raise ValueError("a walk-forward needs at least two folds; use run_search for one")
    if not 0.0 < first_train_share < 1.0:
        raise ValueError("first_train_share must be between 0 and 1")

    started = time.perf_counter()
    result = WalkForwardResult()
    grid = grid or DEFAULT_GRID

    series, skipped = load_series(settings, resolve_symbols(settings, template))
    if not series:
        result.notes.append("No symbol had enough stored history.")
        return result

    step = (1.0 - first_train_share) / folds
    bounds = [boundary_date(series, first_train_share + k * step) for k in range(folds)]
    if any(bound is None for bound in bounds) or len(set(bounds)) < folds:
        result.notes.append("Too little history to cut into that many distinct windows.")
        return result

    fired = _fire_all(series, grid)
    tested_windows: list[tuple[date, date, list[str], Finalist]] = []
    last = _last_day(series) + timedelta(days=1)

    for k, start in enumerate(bounds):
        stop = bounds[k + 1] if k + 1 < len(bounds) else None
        lows = {symbol: split_index(bars, start) for symbol, bars in series.items()}
        highs = {
            symbol: split_index(bars, stop) if stop else len(bars)
            for symbol, bars in series.items()
        }
        candidates = _rank(
            series,
            lows,
            grid,
            fired,
            template,
            horizons=horizons,
            directions=directions,
            draws=search_draws,
        )
        result.tried = len(candidates)
        ranked = [c for c in candidates if c.stats and c.stats.effective_sample >= min_blocks]
        winner = (
            _evaluate_out_of_sample(ranked[0], template, series, lows, highs, draws=draws)
            if ranked
            else None
        )
        result.folds.append(Fold(train_end=start, test_end=stop, winner=winner))
        if winner is not None:
            symbols = sorted(s for s in series if highs[s] > lows[s])
            tested_windows.append((start, stop or last, symbols, winner))

    everything = sorted({s for _, _, symbols, _ in tested_windows for s in symbols})
    if tested_windows:
        result.prior_uses, result.prior_finalists = _read_ledger(
            settings, bounds[0], last, everything
        )
    result.threshold = sidak(SIGNIFICANT, result.prior_finalists + len(tested_windows))
    if record:
        for start, stop, symbols, winner in tested_windows:
            _write_ledger(
                settings,
                kind="walk_forward",
                start=start,
                end=stop,
                symbols=symbols,
                tried=result.tried,
                finalists=[winner],
            )

    result.notes.extend(_walk_forward_notes(result, min_blocks))
    if skipped:
        result.notes.append(f"{len(skipped)} symbols were skipped for lack of history.")
    result.seconds = round(time.perf_counter() - started, 1)
    log.info("walk forward", folds=len(result.folds), seconds=result.seconds)
    return result


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


def _ledger_note(prior_uses: int, prior_finalists: int, tested: int, threshold: float) -> str:
    if prior_uses:
        return (
            f"This held-out data has been evaluated {prior_uses} time(s) before, testing "
            f"{prior_finalists} finalists. Counting those and these {tested}, a result "
            f"needs p below {threshold:.4f} to mean what p=0.05 means for one untouched "
            f"test. Searches before {ledger.LEDGER_STARTED} are not in the ledger, so "
            "that count is a floor."
        )
    return (
        f"First recorded use of this held-out data. With {tested} tested on it, the bar "
        f"is p below {threshold:.4f}. Searches before {ledger.LEDGER_STARTED} are not in "
        "the ledger, and the 2026-09-08 searches used this history, so treat it as a floor."
    )


def _interpretation(result: SearchResult, skipped: dict[str, str], min_blocks: int) -> list[str]:
    notes = [
        f"{result.tried} combinations were tried and ranked on data up to "
        f"{result.train_end}, with trading costs measured on that period alone. "
        f"Everything after that date was untouched until the top "
        f"{len(result.finalists)} were chosen, and those out-of-sample numbers are the "
        "only ones worth reading as evidence."
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
        elif finalist.out_sample_p is None:
            notes.append(
                f"{finalist.candidate.label} returned {finalist.out_sample_mean:.2%} out "
                "of sample, but its null could not be built, so it is not comparable."
            )
        elif finalist.out_sample_p <= result.threshold:
            notes.append(
                f"{finalist.candidate.label} held up out of sample: "
                f"{finalist.out_sample_mean:.2%} against its null at "
                f"p={finalist.out_sample_p:.3f}, below the bar of {result.threshold:.4f}. "
                "One holdout in one regime is a reason to keep watching it, not a "
                "reason to trade it."
            )
        else:
            notes.append(
                f"{finalist.candidate.label} returned {finalist.out_sample_mean:.2%} out "
                f"of sample at p={finalist.out_sample_p:.3f}, above the bar of "
                f"{result.threshold:.4f}, which is not distinguishable from entering at "
                "other times."
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
        _ledger_note(
            result.prior_uses, result.prior_finalists, len(result.finalists), result.threshold
        )
    )
    notes.append(
        "The grid was written by somebody who has already seen this market, and that "
        "prior selection is invisible to every number here. A held-out period also does "
        "not make a result durable: the holdout is one regime. `--folds` walks forward "
        "across several."
    )
    if skipped:
        notes.append(f"{len(skipped)} symbols were skipped for lack of history.")
    return notes


def _walk_forward_notes(result: WalkForwardResult, min_blocks: int) -> list[str]:
    notes = [
        f"Each of {len(result.folds)} folds chose its winner from {result.tried} "
        "combinations using only the data before its own test window, with costs "
        "measured there too, then was scored on that window alone. Every window is "
        "tested once and no fold's choice saw it."
    ]
    scored = [
        fold.winner
        for fold in result.folds
        if fold.winner is not None and fold.winner.out_sample_edge is not None
    ]
    if not scored:
        notes.append("No fold produced a winner with a measurable out-of-sample result.")
        return notes

    positive = sum(1 for w in scored if (w.out_sample_edge or 0.0) > 0)
    held = sum(
        1 for w in scored if w.out_sample_p is not None and w.out_sample_p <= result.threshold
    )
    notes.append(
        f"{positive} of {len(scored)} winners had a positive edge out of sample, and "
        f"{held} beat their null at p below {result.threshold:.4f}, the bar after counting "
        "every winner tested on this data."
    )

    labels = [w.candidate.label for w in scored]
    distinct = len(set(labels))
    if len(labels) > 1 and distinct == len(labels):
        notes.append(
            "A different candidate won every fold. A search that changes its mind each "
            "time it sees more data is choosing noise, whatever any single fold says."
        )
    elif distinct == 1 and len(labels) > 1:
        notes.append(
            f"The same candidate won every fold: {labels[0]}. Stable selection is "
            "necessary for a real edge, not sufficient."
        )
    elif len(labels) > 1:
        common = max(set(labels), key=labels.count)
        notes.append(
            f"{distinct} different candidates won across {len(labels)} folds; "
            f"{common} won {labels.count(common)} of them."
        )

    blocks = sum(w.out_sample_blocks for w in scored)
    if blocks:
        pooled = sum((w.out_sample_edge or 0.0) * w.out_sample_blocks for w in scored) / blocks
        notes.append(
            f"Pooled over {blocks} out-of-sample blocks, the winners' edge was "
            f"{pooled:+.2%}: what the search's own choices were worth, measured only "
            "where they had not been fitted."
        )
    thin = [w for w in scored if w.out_sample_blocks < min_blocks]
    if thin:
        notes.append(
            f"{len(thin)} of the test windows hold fewer than {min_blocks} independent "
            "blocks, so a single fold's p-value is weak. Read the stability and the pooled "
            "edge rather than any one fold."
        )

    notes.append(
        _ledger_note(result.prior_uses, result.prior_finalists, len(scored), result.threshold)
    )
    notes.append(
        "Walk-forward removes the fitting from every test window. It does not remove the "
        "hindsight of whoever wrote the grid."
    )
    return notes
