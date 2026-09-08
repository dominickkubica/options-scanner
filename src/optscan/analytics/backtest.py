"""Does a rule actually have an edge, or does it just trade in a rising market?

Everything in this module exists to answer one question honestly, and almost all of the
difficulty is in the word *honestly*. A backtest is the easiest thing in this repository
to build so that it always says yes.

## The four ways a backtest lies, and what is done about each

**It compares against zero.** A rule that buys on any pretext and holds twenty days made
money over the last decade because the market went up, not because the rule works. Every
number here is therefore reported against a **matched null**: the same symbols, the same
number of entries, the same holding period, entry dates drawn at random. That null
contains the drift, so beating it means the *timing* did something. `edge` is the
strategy mean minus the null mean, and the p-value is the share of null draws that beat
what was observed. See `null_distribution`.

**It counts overlapping trades as independent.** Enter on two hundred days, hold thirty,
and you have two hundred rows covering maybe fifteen genuinely separate market episodes.
A t-statistic on the rows is roughly the square root of the overlap too large, which is
more than enough to manufacture a finding. This is the ninth instance of this repo's
recurring trap, recorded in `calibration.py`, and the answer is the same: the unit of
independence is a **non-overlapping block**, `effective_sample` counts those, and the
bootstrap resamples blocks rather than trades.

**It is the best of many tries.** Sweeping a parameter and reporting the winner is not a
result, it is the maximum of a sample of noise, and with twenty cells the best one looks
good at p below 0.05 by construction. `sweep` therefore reports the best cell *and* what
the best of that many cells looks like under the null, which is the only number that
makes the winner interpretable.

**It trades on information it did not have.** A signal computed from today's close
cannot be filled at today's close. Every entry here fills at the **next bar's open**,
and the delay is not a detail: it is most of the difference between a backtest and a
fantasy for any rule that keys on a closing price.

## Options: what can be tested, and the substitution that is refused

Held to expiry, a short option's profit is `credit - intrinsic at expiry`. The second
term is exact, because the underlying's path is real stored history. Only the credit is
modelled, and it is modelled with **real vendor implied volatility** from
`vendor_daily.iv30`.

That history exists for exactly two symbols, so option backtests run on those two and
refuse elsewhere. The refusal is the important part. The obvious workaround is to
substitute realized volatility for implied, and it is not a workaround, it is the
destruction of the experiment: **the gap between implied and realized vol is the
variance risk premium, which is precisely the edge a premium seller is trying to
measure.** Pricing the entry at realized vol prices it at fair value, and a fair-value
entry has zero expected profit by construction. The backtest would then report no edge
for a strategy that has one, and the natural next move would be to fudge the number
until it looked right. `require_implied_vol` raises instead.

`iv30` is a **thirty day** quote, so pricing a seven day option with it is the tenor
mismatch that this project already walked into once (see `screener/context.py`). Option
backtests are therefore restricted to a DTE band around thirty, and `TENOR_BAND` is that
band.

## What this module will not tell you

Whether a strategy will work. It measures what a rule did on stored history, against a
null, with the sample size stated. Nothing here is a forecast, none of it accounts for
the fact that the rule was chosen by a person who had already seen this data, and a
result that survives everything above is a reason to keep looking rather than a reason
to trade.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from optscan.analytics.greeks import bsm_price
from optscan.models import PriceBar

#: Trades whose holding periods overlap are not independent observations. Blocks are
#: this many bars wide, and the bootstrap resamples blocks. See the module docstring.
DEFAULT_BLOCK_BARS = 21

#: Draws in the null distribution. A thousand puts the resolution of a p-value at 0.001,
#: which is far finer than anything here deserves to claim.
DEFAULT_NULL_DRAWS = 1000

#: Below this many independent blocks, no claim is made. Mirrors the twenty cluster floor
#: in calibration.py and for the same reason: an interval on a handful of episodes is
#: arithmetic rather than evidence.
MIN_BLOCKS_FOR_A_CLAIM = 20

#: Round trip cost as a fraction of notional. Ten basis points is a wide retail spread on
#: a liquid name; it is a parameter because the honest value depends on what is traded.
DEFAULT_COST = 0.0010

#: Days to expiry a 30 day implied vol may honestly price. Half to double, the same band
#: and the same reasoning as `screener/context.py`: outside it the quote and the contract
#: are different tenors and comparing them is the mismatch that bit this project before.
TENOR_BAND = (15, 60)

#: Sessions in a trading year, for annualising.
TRADING_DAYS = 252

#: A series shorter than this cannot be circularly shifted onto anything but itself.
MIN_SHIFTABLE_BARS = 2


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"


class ExitReason(StrEnum):
    HORIZON = "horizon"
    TARGET = "target"
    STOP = "stop"
    EXPIRY = "expiry"
    #: Ran out of history. These are dropped, not counted as flat: keeping them would
    #: silently fill the most recent weeks with truncated winners.
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class Trade:
    """One round trip, with the bar indices that produced it."""

    symbol: str
    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float
    direction: Direction
    reason: ExitReason
    #: Index of the entry bar in the symbol's series. Carried so overlap can be measured
    #: without re-deriving it from dates and a market calendar.
    entry_index: int
    bars_held: int
    #: Net of costs, signed for the direction. The raw move is recoverable from prices.
    net_return: float

    @property
    def year(self) -> int:
        return self.exit_date.year


@dataclass(frozen=True, slots=True)
class Stats:
    """What a set of trades did. Descriptive only; no claim is made here."""

    trades: int
    #: Non-overlapping blocks the trades fall into. The real sample size.
    effective_sample: int
    mean_return: float
    median_return: float
    win_rate: float
    #: Equal weighted, monthly rebalanced. Not the product of the individual trades,
    #: which would compound concurrent positions as though they were consecutive.
    total_return: float
    best: float
    worst: float
    stdev: float
    mean_bars_held: float

    @property
    def enough_to_claim(self) -> bool:
        return self.effective_sample >= MIN_BLOCKS_FOR_A_CLAIM

    def as_dict(self) -> dict[str, object]:
        return {
            "trades": self.trades,
            "effective_sample": self.effective_sample,
            "mean_return": self.mean_return,
            "median_return": self.median_return,
            "win_rate": self.win_rate,
            "total_return": self.total_return,
            "best": self.best,
            "worst": self.worst,
            "stdev": self.stdev,
            "mean_bars_held": self.mean_bars_held,
            "enough_to_claim": self.enough_to_claim,
        }


@dataclass(frozen=True, slots=True)
class Edge:
    """The strategy measured against a null that already contains the drift."""

    strategy_mean: float
    null_mean: float
    #: strategy_mean - null_mean. The part not explained by being in the market.
    edge: float
    #: Share of null draws that matched or beat the strategy. Not a probability that the
    #: strategy works.
    p_value: float
    #: 5th and 95th percentile of the null, so the reader can see the spread the p-value
    #: came from rather than taking it.
    null_low: float
    null_high: float
    draws: int

    def as_dict(self) -> dict[str, object]:
        return {
            "strategy_mean": self.strategy_mean,
            "null_mean": self.null_mean,
            "edge": self.edge,
            "p_value": self.p_value,
            "null_low": self.null_low,
            "null_high": self.null_high,
            "draws": self.draws,
        }


@dataclass(frozen=True, slots=True)
class YearRow:
    """One calendar year. The whole point of asking for edge *over time*."""

    year: int
    trades: int
    mean_return: float
    win_rate: float
    total_return: float


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    stats: Stats | None = None
    edge: Edge | None = None
    by_year: list[YearRow] = field(default_factory=list)
    #: Cumulative compounded return after each trade, in exit order.
    equity: list[tuple[str, float]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "stats": self.stats.as_dict() if self.stats else None,
            "edge": self.edge.as_dict() if self.edge else None,
            "by_year": [
                {
                    "year": row.year,
                    "trades": row.trades,
                    "mean_return": row.mean_return,
                    "win_rate": row.win_rate,
                    "total_return": row.total_return,
                }
                for row in self.by_year
            ],
            "equity": [{"date": when, "value": value} for when, value in self.equity],
            "trades": [
                {
                    "symbol": t.symbol,
                    "entry": t.entry_date.isoformat(),
                    "exit": t.exit_date.isoformat(),
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "direction": t.direction.value,
                    "reason": t.reason.value,
                    "bars_held": t.bars_held,
                    "net_return": t.net_return,
                }
                for t in self.trades
            ],
            "notes": self.notes,
            "skipped": self.skipped,
        }


# --------------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------------


def simulate(
    symbol: str,
    bars: Sequence[PriceBar],
    entry_indices: Sequence[int],
    *,
    direction: Direction = Direction.LONG,
    horizon: int = 21,
    target: float | None = None,
    stop: float | None = None,
    cost: float = DEFAULT_COST,
    allow_overlap: bool = False,
) -> list[Trade]:
    """Turn entry signals into round trips on the underlying.

    **Entry fills at the open of the bar after the signal.** A rule that reads a closing
    price cannot be filled at that close, and letting it be is the single easiest way to
    make a backtest of a closing-price rule look profitable.

    Exits are checked intrabar against the high and the low. Where a bar would have hit
    both the target and the stop, the **stop is taken**: within a daily bar the order is
    unknowable, and assuming the good one is how a backtest quietly earns a few basis
    points a trade that do not exist.

    `allow_overlap=False` is the default because a portfolio cannot hold the same symbol
    five times over. It also happens to reduce the overlap problem at the source, though
    it does not remove it, since separate symbols still move together.
    """
    if horizon < 1:
        raise ValueError("horizon must be at least one bar")

    trades: list[Trade] = []
    busy_until = -1

    for index in sorted(set(entry_indices)):
        if not allow_overlap and index <= busy_until:
            continue
        # Fill on the next bar's open. No next bar means the signal is not tradable.
        fill = index + 1
        if fill >= len(bars):
            continue

        entry_price = bars[fill].open
        if entry_price <= 0:
            continue

        exit_index, exit_price, reason = _walk_forward(bars, fill, direction, horizon, target, stop)
        if reason is ExitReason.INCOMPLETE:
            continue

        gross = (exit_price / entry_price) - 1.0
        signed = gross if direction is Direction.LONG else -gross
        trades.append(
            Trade(
                symbol=symbol,
                entry_date=bars[fill].ts.date(),
                exit_date=bars[exit_index].ts.date(),
                entry_price=entry_price,
                exit_price=exit_price,
                direction=direction,
                reason=reason,
                entry_index=fill,
                bars_held=exit_index - fill,
                net_return=signed - cost,
            )
        )
        busy_until = exit_index

    return trades


def _walk_forward(
    bars: Sequence[PriceBar],
    fill: int,
    direction: Direction,
    horizon: int,
    target: float | None,
    stop: float | None,
) -> tuple[int, float, ExitReason]:
    """Find the exit bar, checking stops and targets intrabar."""
    entry = bars[fill].open
    last = min(fill + horizon, len(bars) - 1)
    if fill + horizon > len(bars) - 1:
        # The trade has not finished inside the stored history. Reporting it at the last
        # close would fill the most recent weeks with trades cut short at whatever the
        # market happened to be doing, which biases the newest period.
        return last, bars[last].close, ExitReason.INCOMPLETE

    for index in range(fill, last + 1):
        bar = bars[index]
        if direction is Direction.LONG:
            stop_hit = stop is not None and bar.low <= entry * (1 - stop)
            target_hit = target is not None and bar.high >= entry * (1 + target)
            stop_price = entry * (1 - stop) if stop is not None else 0.0
            target_price = entry * (1 + target) if target is not None else 0.0
        else:
            stop_hit = stop is not None and bar.high >= entry * (1 + stop)
            target_hit = target is not None and bar.low <= entry * (1 - target)
            stop_price = entry * (1 + stop) if stop is not None else 0.0
            target_price = entry * (1 - target) if target is not None else 0.0

        # Stop wins a tie. See the docstring: the intrabar order is unknowable and
        # assuming the favourable one is a free lunch the market did not serve.
        if stop_hit:
            return index, stop_price, ExitReason.STOP
        if target_hit:
            return index, target_price, ExitReason.TARGET

    return last, bars[last].close, ExitReason.HORIZON


# --------------------------------------------------------------------------------
# The honest part
# --------------------------------------------------------------------------------


def block_of(entry_index: int, block_bars: int = DEFAULT_BLOCK_BARS) -> int:
    return entry_index // block_bars


def effective_sample(trades: Sequence[Trade], block_bars: int = DEFAULT_BLOCK_BARS) -> int:
    """Independent episodes, counted in calendar time and pooled across symbols.

    The obvious key is (symbol, block), and it is wrong in a way that is worth spelling
    out because it looks so reasonable. With overlapping entries suppressed, consecutive
    trades on one symbol are already at least a horizon apart, so each lands in its own
    block and the count comes back equal to the number of trades. It measures nothing.

    The correlation that matters across a universe is **cross-sectional**. Ninety-eight
    technology names oversold in the same week are one observation of one market episode,
    not ninety-eight, because they share almost all of their variance. Pooling by
    calendar block therefore treats everything entered in the same month as a single
    observation.

    That is conservative: two symbols in a week are not *perfectly* correlated either, so
    the truth sits between this and the per-symbol count. Given the choice, this module
    takes the number that makes it harder to claim a finding.
    """
    return len({block_of(t.entry_index, block_bars) for t in trades})


def summarize(trades: Sequence[Trade], block_bars: int = DEFAULT_BLOCK_BARS) -> Stats | None:
    if not trades:
        return None
    returns = [t.net_return for t in trades]
    mean = sum(returns) / len(returns)
    ordered = sorted(returns)
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    variance = (
        sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
        if len(returns) > 1
        else 0.0
    )

    return Stats(
        trades=len(trades),
        effective_sample=effective_sample(trades, block_bars),
        mean_return=mean,
        median_return=median,
        win_rate=sum(1 for value in returns if value > 0) / len(returns),
        total_return=portfolio_total(trades),
        best=max(returns),
        worst=min(returns),
        stdev=math.sqrt(variance),
        mean_bars_held=sum(t.bars_held for t in trades) / len(trades),
    )


def forward_return_table(
    bars: Sequence[PriceBar],
    *,
    direction: Direction = Direction.LONG,
    horizon: int = 21,
    target: float | None = None,
    stop: float | None = None,
    cost: float = DEFAULT_COST,
) -> list[float | None]:
    """Net return of entering on every bar, or None where the trade cannot finish.

    Precomputed once so the null can draw a thousand samples without re-walking the
    series a million times. Entering at index i still fills at i+1 and still respects
    the stop, the target and the horizon, so a value here is exactly what `simulate`
    would have produced for that single entry.
    """
    out: list[float | None] = [None] * len(bars)

    if target is None and stop is None:
        # Without a target or a stop the exit bar is known in advance, so the whole walk
        # collapses to one division. This is not a micro optimisation: a search over a
        # grid needs one of these per horizon and direction, and the general path is
        # O(horizon) per bar, which at 63 bars across three hundred symbols is the
        # difference between a search that runs and one that nobody waits for.
        last = len(bars) - 1
        for index in range(len(bars)):
            fill = index + 1
            exit_index = fill + horizon
            if exit_index > last:
                break
            entry = bars[fill].open
            if entry <= 0:
                continue
            gross = bars[exit_index].close / entry - 1.0
            signed = gross if direction is Direction.LONG else -gross
            out[index] = signed - cost
        return out

    for index in range(len(bars)):
        trades = simulate(
            bars[0].symbol if bars else "",
            bars,
            [index],
            direction=direction,
            horizon=horizon,
            target=target,
            stop=stop,
            cost=cost,
            allow_overlap=True,
        )
        if trades:
            out[index] = trades[0].net_return
    return out


def null_distribution(
    series: dict[str, Sequence[PriceBar]],
    entries: dict[str, Sequence[int]],
    *,
    direction: Direction = Direction.LONG,
    horizon: int = 21,
    target: float | None = None,
    stop: float | None = None,
    cost: float = DEFAULT_COST,
    costs: dict[str, float] | None = None,
    draws: int = DEFAULT_NULL_DRAWS,
    seed: int = 0,
    block_bars: int = DEFAULT_BLOCK_BARS,
) -> list[float]:
    """Mean return of the same entry pattern, slid to a random point in history.

    This is the comparison that makes the whole module worth having. A long rule over the
    last decade makes money because the market rose; the question is whether it made more
    than putting the same capital to work on arbitrary days would have. The null carries
    the drift, the holding period, the exit rules, the costs and the symbol mix, so what
    is left over is timing.

    The null is a **circular shift of the whole entry pattern**: every symbol's entries
    move by the same number of bars, wrapping at the end. Nothing else changes.

    Drawing fresh random days per symbol was the first attempt and it is too generous, in
    a way that inflates every result on a correlated universe. A rule that fires across
    ninety-eight technology names in the same week produces a mean with the variance of
    roughly one observation; a null that scatters each symbol's entries independently
    averages away that co-movement and produces a mean with the variance of ninety-eight.
    Comparing the first against the second is comparing a noisy number to a stable one,
    and almost anything clears it.

    A common shift preserves what the strategy actually has — how many trades per symbol,
    how they cluster in time, and how synchronised they are across symbols — and destroys
    only the alignment between the rule and what the market did next. That alignment is
    the thing being tested, so it is the only thing that should be destroyed.

    The null's spread comes from how much of the series the entry pattern leaves free to
    move. A pattern that covered every tradable bar would shift onto itself and collapse
    to a point, but that case does not arise here: overlap suppression already thins even
    an every-bar rule down to a grid one horizon wide, and a grid has phases to shift
    between. What this does mean is that a very dense rule is tested against a narrow
    null, so its p-value says more about the calendar than about the market.

    `seed` is fixed by default: a p-value that changes on every refresh is not a number
    anybody can act on.
    """
    rng = random.Random(seed)

    # One pass per symbol, then every draw is a lookup. Re-walking the bars inside the
    # sampling loop is a million redundant traversals and makes the null too slow to run,
    # which in practice means it does not get run.
    values: dict[str, list[float | None]] = {}
    span = 0
    for symbol, wanted in entries.items():
        bars = series.get(symbol)
        if not bars or not wanted:
            continue
        values[symbol] = forward_return_table(
            bars,
            direction=direction,
            horizon=horizon,
            target=target,
            stop=stop,
            # The null must be charged what the strategy is charged, per symbol. Using a
            # blended cost here while the strategy pays per symbol would hand whichever
            # side trades the cheaper names an advantage that has nothing to do with
            # timing, which is the only thing the comparison is meant to isolate.
            cost=(costs or {}).get(symbol, cost),
        )
        span = max(span, len(bars))

    # A one bar series has no shift that is not the identity, so there is no null.
    if not values or span < MIN_SHIFTABLE_BARS:
        return []

    return shift_null(values, entries, draws=draws, rng=rng)


def shift_null(
    tables: dict[str, Sequence[float | None]],
    entries: dict[str, Sequence[int]],
    *,
    draws: int = DEFAULT_NULL_DRAWS,
    seed: int = 0,
    rng: random.Random | None = None,
) -> list[float]:
    """The shifting core, over any precomputed table of per-bar returns.

    Split out from `null_distribution` so the option modes can reuse it. Their table is
    built from modelled credits rather than underlying moves, and that is exactly why the
    comparison still works: **the same model prices both the strategy and the null**, so
    whatever the model gets wrong cancels, and what is left is the timing.
    """
    generator = rng or random.Random(seed)
    span = max((len(table) for table in tables.values()), default=0)
    if not tables or span < MIN_SHIFTABLE_BARS:
        return []

    means: list[float] = []
    for _ in range(draws):
        # One shift for the whole portfolio. See null_distribution: shifting each symbol
        # separately would break the co-movement the strategy actually has.
        shift = generator.randrange(1, span)
        returns: list[float] = []
        for symbol, table in tables.items():
            width = len(table)
            if width == 0:
                continue
            for index in entries.get(symbol, ()):
                value = table[(index + shift) % width]
                if value is not None:
                    returns.append(value)
        if returns:
            means.append(sum(returns) / len(returns))

    return means


def option_forward_return_table(
    symbol: str,
    bars: Sequence[PriceBar],
    implied: dict[date, float],
    **kwargs,
) -> list[float | None]:
    """Net return of selling the option on every bar, or None where it cannot be.

    None where there is no stored implied volatility for that session, or where the
    expiry falls past the end of the history. Those gaps are why the null shifts rather
    than resamples: a shifted entry that lands on a gap is dropped, and dropping a few is
    fine, but resampling would quietly concentrate the null on the days that happen to
    have data.
    """
    out: list[float | None] = [None] * len(bars)
    for index in range(len(bars)):
        trades = short_option_trades(symbol, bars, implied, [index], allow_overlap=True, **kwargs)
        if trades:
            out[index] = trades[0].net_return
    return out


def measure_edge(observed_mean: float, null_means: Sequence[float]) -> Edge | None:
    """Where the strategy sits in the null, as a one sided p-value."""
    if not null_means:
        return None
    ordered = sorted(null_means)
    beat = sum(1 for value in null_means if value >= observed_mean)
    null_mean = sum(null_means) / len(null_means)

    def percentile(share: float) -> float:
        position = min(len(ordered) - 1, max(0, int(share * len(ordered))))
        return ordered[position]

    return Edge(
        strategy_mean=observed_mean,
        null_mean=null_mean,
        edge=observed_mean - null_mean,
        # (beat + 1) / (draws + 1): the unbiased small sample form, and it cannot report
        # a p-value of exactly zero, which no finite resample is entitled to claim.
        p_value=(beat + 1) / (len(null_means) + 1),
        null_low=percentile(0.05),
        null_high=percentile(0.95),
        draws=len(null_means),
    )


def portfolio_periods(trades: Sequence[Trade]) -> list[tuple[str, float, int]]:
    """Monthly portfolio returns: (month, equal weighted mean, trade count).

    This exists because the obvious thing is wrong in a way that is not obvious until the
    numbers get silly. Multiplying every trade's return together treats them as a
    sequence, one after another, each staking the whole account. The trades are not a
    sequence: a rule firing across ninety-eight symbols opens ninety-eight concurrent
    positions, and compounding those as if they were consecutive fabricates leverage. On
    a real run it produced a yearly total of +2,737,313 percent, which is what a
    fabricated leverage of about ninety-eight to one looks like.

    An equity curve needs a capital model, so here is the simplest defensible one: hold
    every trade that exits in a month at equal weight, and compound the months. It
    assumes the account is fully invested and rebalanced monthly, which is a real
    assumption and is stated in the report rather than hidden in a multiplication.
    """
    months: dict[str, list[float]] = {}
    for trade in trades:
        key = f"{trade.exit_date.year:04d}-{trade.exit_date.month:02d}"
        months.setdefault(key, []).append(trade.net_return)
    return [
        (month, sum(values) / len(values), len(values)) for month, values in sorted(months.items())
    ]


def portfolio_total(trades: Sequence[Trade]) -> float:
    """Compounded monthly portfolio return over the whole run."""
    value = 1.0
    for _, mean, _ in portfolio_periods(trades):
        value *= 1.0 + mean
    return value - 1.0


def yearly(trades: Sequence[Trade]) -> list[YearRow]:
    """Per calendar year, so a strategy that died in 2022 cannot hide in an average."""
    years: dict[int, list[Trade]] = {}
    for trade in trades:
        years.setdefault(trade.year, []).append(trade)

    rows = []
    for year in sorted(years):
        group = years[year]
        returns = [t.net_return for t in group]
        rows.append(
            YearRow(
                year=year,
                trades=len(group),
                mean_return=sum(returns) / len(returns),
                win_rate=sum(1 for v in returns if v > 0) / len(returns),
                # The equal weighted monthly portfolio, not the product of every trade.
                # See portfolio_periods for why the product is meaningless here.
                total_return=portfolio_total(group),
            )
        )
    return rows


def equity_curve(trades: Sequence[Trade]) -> list[tuple[str, float]]:
    """An equal weighted, monthly rebalanced portfolio, starting at 1.0.

    One point per month rather than one per trade. A per-trade curve implies the trades
    happened one after another, which they did not: see `portfolio_periods`.

    Grouped by exit month because that is when the money actually moved. A curve built on
    entry dates shows profits before they were realised.
    """
    curve = []
    value = 1.0
    for month, mean, _ in portfolio_periods(trades):
        value *= 1.0 + mean
        curve.append((month, value))
    return curve


# --------------------------------------------------------------------------------
# Options
# --------------------------------------------------------------------------------


class MissingImpliedVol(Exception):
    """Raised rather than substituting realized vol. See the module docstring.

    The substitution is not a degraded approximation, it is the removal of the quantity
    being measured: the implied-to-realized gap is the variance risk premium, and pricing
    entry at realized vol prices it at fair value, where expected profit is zero.
    """


def require_implied_vol(symbol: str, available: int, needed: int) -> None:
    if available >= needed:
        return
    raise MissingImpliedVol(
        f"{symbol} has implied volatility on {available} of {needed} sessions. An "
        "option backtest needs a real vendor implied vol to price the entry, and "
        "realized volatility is not a substitute: the gap between the two is the "
        "variance risk premium, which is the edge being measured. Import an IV "
        "history for this symbol, or backtest the underlying instead."
    )


@dataclass(frozen=True, slots=True)
class OptionTrade:
    """One short option held to expiry."""

    symbol: str
    entry_date: date
    expiry: date
    right: str
    strike: float
    spot_at_entry: float
    spot_at_expiry: float
    implied_vol: float
    credit: float
    #: credit - intrinsic at expiry, per share, after the slippage haircut.
    profit: float
    #: Profit as a fraction of the collateral a cash secured seller puts up.
    net_return: float
    dte: int
    entry_index: int

    @property
    def year(self) -> int:
        return self.expiry.year

    @property
    def expired_worthless(self) -> bool:
        return (
            self.spot_at_expiry >= self.strike
            if self.right == "put"
            else (self.spot_at_expiry <= self.strike)
        )


def short_option_trades(
    symbol: str,
    bars: Sequence[PriceBar],
    implied: dict[date, float],
    entry_indices: Sequence[int],
    *,
    right: str = "put",
    dte: int = 30,
    offset: float = 0.05,
    rate: float = 0.043,
    slippage: float = 0.05,
    allow_overlap: bool = False,
) -> list[OptionTrade]:
    """Sell an out of the money option, hold to expiry, settle against the real close.

    Only the **credit** is modelled. The payoff is exact: the underlying's price on the
    expiry date is stored history, so `credit - intrinsic` is what the trade actually
    made, given that entry fill.

    `offset` is how far out of the money to sell, as a fraction of spot. A delta target
    would be more familiar but needs a vol to invert, which is the quantity already being
    modelled; a fixed offset keeps one modelled input rather than two.

    `slippage` haircuts the credit, because a seller receives the bid rather than the
    mid, and no historical spread is stored. It is a parameter and the default is a
    guess: run it twice and look at how much the answer moves.
    """
    if not TENOR_BAND[0] <= dte <= TENOR_BAND[1]:
        raise ValueError(
            f"dte {dte} is outside {TENOR_BAND}, where a 30 day implied vol stops being "
            "the right quote for the contract. This is the tenor mismatch recorded in "
            "screener/context.py."
        )
    if right not in ("put", "call"):
        raise ValueError("right must be put or call")

    by_date = {bar.ts.date(): index for index, bar in enumerate(bars)}
    trades: list[OptionTrade] = []
    busy_until = -1

    for index in sorted(set(entry_indices)):
        if not allow_overlap and index <= busy_until:
            continue
        # Same next-bar rule as the underlying: a signal on today's close is filled
        # tomorrow, not today.
        fill = index + 1
        if fill >= len(bars):
            continue

        entry_bar = bars[fill]
        entry_day = entry_bar.ts.date()
        vol = implied.get(entry_day)
        if vol is None or vol <= 0:
            continue

        spot = entry_bar.open
        if spot <= 0:
            continue

        strike = spot * (1 - offset) if right == "put" else spot * (1 + offset)
        years = dte / 365.0
        # Argument order matters and is easy to get backwards: bsm_price takes the
        # rate before the volatility. Swapping them prices a 30 day option at a
        # 4.3 percent vol and discounts it at 20 percent, which returns a credit
        # near zero rather than an error.
        credit = bsm_price(right, spot, strike, years, rate, vol)
        if credit <= 0:
            continue
        credit *= 1.0 - slippage

        # The settlement bar is the first stored session on or after the expiry date.
        # Calendar arithmetic, then a lookup, because weekends and holidays are not in
        # the bar series and guessing 21 sessions would drift.
        target_day = entry_day.toordinal() + dte
        settle = _first_session_on_or_after(bars, by_date, target_day)
        if settle is None:
            continue

        final = bars[settle].close
        intrinsic = max(strike - final, 0.0) if right == "put" else max(final - strike, 0.0)
        profit = credit - intrinsic
        # Cash secured: the collateral is the strike, which is what a seller actually
        # ties up. Returning profit over the credit would report a 40 percent gain on a
        # trade that risked twenty times that.
        trades.append(
            OptionTrade(
                symbol=symbol,
                entry_date=entry_day,
                expiry=bars[settle].ts.date(),
                right=right,
                strike=strike,
                spot_at_entry=spot,
                spot_at_expiry=final,
                implied_vol=vol,
                credit=credit,
                profit=profit,
                net_return=profit / strike,
                dte=dte,
                entry_index=fill,
            )
        )
        busy_until = settle

    return trades


def _first_session_on_or_after(
    bars: Sequence[PriceBar], by_date: dict[date, int], ordinal: int
) -> int | None:
    """Index of the first stored session at or after a calendar day, or None."""
    for offset in range(8):  # a week's slack covers any holiday run
        found = by_date.get(date.fromordinal(ordinal + offset))
        if found is not None:
            return found
    return None


def option_trades_as_trades(trades: Sequence[OptionTrade]) -> list[Trade]:
    """Adapt option trades onto the shared Trade shape for the common statistics.

    Prices are carried as strike and settlement so the row still reads sensibly, and the
    return is the one computed against collateral rather than being re-derived here.
    """
    return [
        Trade(
            symbol=t.symbol,
            entry_date=t.entry_date,
            exit_date=t.expiry,
            entry_price=t.credit,
            exit_price=max(0.0, t.credit - t.profit),
            direction=Direction.SHORT,
            reason=ExitReason.EXPIRY,
            entry_index=t.entry_index,
            bars_held=0,
            net_return=t.net_return,
        )
        for t in trades
    ]


# --------------------------------------------------------------------------------
# Sweeps
# --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SweepCell:
    label: str
    params: dict[str, object]
    stats: Stats | None
    mean_return: float


@dataclass(frozen=True, slots=True)
class SweepResult:
    cells: list[SweepCell]
    best: SweepCell | None
    #: What the best of this many cells looks like when nothing has an edge. The number
    #: that makes the winner interpretable, and usually the number that deflates it.
    expected_best_under_null: float | None
    note: str

    def as_dict(self) -> dict[str, object]:
        return {
            "cells": [
                {
                    "label": cell.label,
                    "params": cell.params,
                    "mean_return": cell.mean_return,
                    "trades": cell.stats.trades if cell.stats else 0,
                    "effective_sample": cell.stats.effective_sample if cell.stats else 0,
                }
                for cell in self.cells
            ],
            "best": self.best.label if self.best else None,
            "expected_best_under_null": self.expected_best_under_null,
            "note": self.note,
        }


def sweep(
    cells: Sequence[tuple[str, dict[str, object], list[Trade]]],
    null_means: Sequence[float],
    *,
    block_bars: int = DEFAULT_BLOCK_BARS,
) -> SweepResult:
    """Rank parameter cells, and say what the winner is worth.

    A sweep's best cell is the maximum of a sample, and the maximum of twenty draws from
    a distribution centred on nothing still looks like something. So the expected best
    under the null is computed from the same null the single-strategy test uses, by
    taking the maximum of each group of `len(cells)` draws. Comparing the winner to that,
    rather than to zero, is the only reading of a sweep that survives contact with
    statistics.
    """
    scored = [
        SweepCell(
            label=label,
            params=params,
            stats=summarize(trades, block_bars),
            mean_return=(sum(t.net_return for t in trades) / len(trades))
            if trades
            else float("-inf"),
        )
        for label, params, trades in cells
    ]
    ranked = sorted(scored, key=lambda cell: cell.mean_return, reverse=True)
    best = ranked[0] if ranked and ranked[0].mean_return > float("-inf") else None

    expected_best = None
    width = max(1, len(scored))
    if len(null_means) >= width * 2:
        maxima = [
            max(null_means[start : start + width])
            for start in range(0, len(null_means) - width + 1, width)
        ]
        expected_best = sum(maxima) / len(maxima)

    thin = (
        best is not None
        and best.stats is not None
        and best.stats.effective_sample < MIN_BLOCKS_FOR_A_CLAIM
    )

    if best is None:
        note = "No cell produced a trade."
    elif thin:
        # The most important fact about a sweep winner is usually how little it rests on.
        # A tight parameter selects a rare condition, so the best cell is very often the
        # one with the fewest observations, and comparing it to the null misses that.
        note = (
            f"The best of {len(scored)} cells is {best.label} at "
            f"{best.mean_return:.2%}, resting on {best.stats.effective_sample} "
            f"independent blocks. Below the floor of {MIN_BLOCKS_FOR_A_CLAIM} no claim "
            "is made: a tighter parameter selects a rarer condition, so the winning cell "
            "is usually the one with the least evidence behind it."
        )
    elif expected_best is None:
        note = (
            f"{len(scored)} cells tried. Too few null draws to say what the best of "
            "that many is worth by chance, so the winner is uninterpretable."
        )
    elif best.mean_return <= expected_best:
        note = (
            f"The best of {len(scored)} cells returned {best.mean_return:.2%}, and the "
            f"best of {len(scored)} cells returns {expected_best:.2%} when nothing has "
            "an edge. This sweep found noise."
        )
    else:
        note = (
            f"The best of {len(scored)} cells returned {best.mean_return:.2%} against "
            f"{expected_best:.2%} for the best of that many under the null. The margin "
            "is the part a sweep cannot manufacture, and it is smaller than the raw "
            "number suggests."
        )

    return SweepResult(
        cells=ranked,
        best=best,
        expected_best_under_null=expected_best,
        note=note,
    )


# --------------------------------------------------------------------------------
# Entry rules
# --------------------------------------------------------------------------------

#: An entry rule is a function from a bar series to the indices it fires on. Kept as
#: plain callables rather than a class hierarchy: every one of them is a loop over bars
#: returning integers, and there is nothing to inherit.
EntryRule = Callable[[Sequence[PriceBar]], list[int]]


def every_bar(bars: Sequence[PriceBar]) -> list[int]:
    """The control. A strategy that cannot beat this is not a strategy.

    Worth running deliberately rather than only as an internal baseline: it is the
    cheapest way to see what the holding period and the costs do on their own, before
    any rule is involved.
    """
    return list(range(len(bars)))
