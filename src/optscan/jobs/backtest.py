"""Running a strategy against stored history, and saying what the answer is worth.

## The bias this job cannot fix, and therefore states

`universe.yaml` is a list of symbols that are interesting **today**: current S&P
members, current large tech, current miners. Backtesting on it is survivorship biased by
construction, and no amount of care inside the engine repairs that. A rule tested on the
companies that made it to 2026 is tested on a sample that excludes the ones that did
not, and long strategies look better on that sample than they would have looked at the
time.

So every report says so, and the size of it is not small: this is usually a larger
effect than costs, slippage and the entry delay put together.

What *is* fixed here is the smaller cousin. Eight universe symbols stopped trading
between 2024 and early 2026, and the signal scan skips them because a delisted ticker's
last session is not news. A backtest does the opposite and **keeps** them, because the
years they did trade are real history and dropping them would add a second layer of
survivorship on top of the first. Staleness is a bug for an alert and a virtue for a
backtest.

## What a run costs

The indicator rules are a single pass and the whole universe takes a few seconds. The
level rules rebuild support and resistance every twenty-one bars, which is roughly a
hundred rebuilds per symbol, and across three hundred symbols that is minutes rather
than seconds. `MAX_LEVEL_SYMBOLS` caps it and the report says when the cap bit.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from datetime import time as clock

from optscan.analytics import rules as rule_registry
from optscan.analytics.backtest import (
    DEFAULT_BLOCK_BARS,
    DEFAULT_COST,
    DEFAULT_NULL_DRAWS,
    MIN_BLOCKS_FOR_A_CLAIM,
    TENOR_BAND,
    BacktestResult,
    Direction,
    MissingImpliedVol,
    SweepResult,
    Trade,
    equity_curve,
    measure_edge,
    null_distribution,
    option_forward_return_table,
    option_trades_as_trades,
    require_implied_vol,
    shift_null,
    short_option_trades,
    simulate,
    summarize,
    sweep,
    yearly,
)
from optscan.config import Settings
from optscan.logging import get_logger
from optscan.models import PriceBar
from optscan.storage import db
from optscan.storage.vendor import daily_bars, preferred_source

log = get_logger("optscan.jobs.backtest")

#: Bars a symbol needs before it can contribute. Below this the warmup eats the series.
MIN_BARS = 200

#: Symbols a level-rule run will accept. See the module docstring on cost.
MAX_LEVEL_SYMBOLS = 60

#: Rules whose cost is a level rebuild rather than a single pass.
EXPENSIVE_RULES = frozenset({"level_break", "level_approach"})

MODES = ("underlying", "short_put", "short_call")

#: Where the report stops hedging and starts saying the rule beat the null. Conventional
#: rather than tuned, and it is a threshold on one test of one rule, not a discovery.
SIGNIFICANT = 0.05


@dataclass
class Strategy:
    """A strategy as data, so the CLI, the API and the UI all describe one thing.

    Round trips through JSON unchanged, which is what makes a result reproducible: the
    spec that produced a number can be stored beside it and replayed.
    """

    name: str = "unnamed"
    symbols: list[str] = field(default_factory=list)
    group: str | None = None
    entry: str = "every_bar"
    entry_params: dict = field(default_factory=dict)
    direction: str = Direction.LONG.value
    horizon: int = 21
    target: float | None = None
    stop: float | None = None
    cost: float = DEFAULT_COST
    mode: str = "underlying"
    #: Option mode only.
    dte: int = 30
    offset: float = 0.05
    slippage: float = 0.05
    rate: float = 0.043
    #: Statistics.
    draws: int = DEFAULT_NULL_DRAWS
    seed: int = 0
    block_bars: int = DEFAULT_BLOCK_BARS

    def validate(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, not {self.mode!r}")
        if self.horizon < 1:
            raise ValueError("horizon must be at least one bar")
        if self.direction not in (Direction.LONG.value, Direction.SHORT.value):
            raise ValueError("direction must be long or short")
        if self.mode != "underlying" and not TENOR_BAND[0] <= self.dte <= TENOR_BAND[1]:
            raise ValueError(
                f"dte {self.dte} is outside {TENOR_BAND}, where a 30 day implied vol "
                "stops being the right quote for the contract"
            )
        # Resolving now turns a bad rule name or parameter into an error before any bars
        # are read, rather than after a minute of work.
        rule_registry.resolve(self.entry, self.entry_params)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> Strategy:
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(payload) - known
        if unknown:
            raise ValueError(
                f"unknown strategy fields: {', '.join(sorted(unknown))}. "
                f"Known: {', '.join(sorted(known))}"
            )
        return cls(**payload)


def _to_bars(rows: Sequence, source: str) -> list[PriceBar]:
    return [
        PriceBar(
            symbol=row.symbol,
            ts=datetime.combine(row.session_date, clock(), tzinfo=UTC),
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=row.volume,
            fetched_at=datetime.now(UTC),
            source=source,
        )
        for row in rows
    ]


def resolve_symbols(settings: Settings, strategy: Strategy) -> list[str]:
    """Explicit symbols, else a universe group, else everything with stored bars."""
    if strategy.symbols:
        return [symbol.upper() for symbol in strategy.symbols]
    if strategy.group:
        from optscan.universe import UniverseError, load_universe  # noqa: PLC0415

        # load_universe takes a path, not settings, and returns a Universe whose groups
        # live on `.groups`. An unknown group name is an error rather than an empty run:
        # a backtest that silently scanned nothing would report "no trades" and read as
        # a finding about the strategy.
        universe = load_universe()
        if strategy.group not in universe.groups:
            raise UniverseError(
                f"no universe group named {strategy.group!r}. "
                f"Available: {', '.join(universe.names)}"
            )
        return list(universe.groups[strategy.group])
    with db.session(settings.sqlite_path) as conn:
        return [
            row[0]
            for row in conn.execute("SELECT DISTINCT symbol FROM vendor_daily ORDER BY symbol")
        ]


def load_series(
    settings: Settings, symbols: Sequence[str]
) -> tuple[dict[str, list[PriceBar]], dict[str, str]]:
    """Bars per symbol, and why any symbol was left out.

    Delisted symbols are kept. See the module docstring: their trading years are real
    history, and dropping them would stack a second survivorship bias on the one the
    universe already has.
    """
    series: dict[str, list[PriceBar]] = {}
    skipped: dict[str, str] = {}

    with db.session(settings.sqlite_path) as conn:
        for symbol in symbols:
            source = preferred_source(conn, symbol)
            if source is None:
                skipped[symbol] = "no stored price source"
                continue
            bars = _to_bars(daily_bars(conn, symbol, source=source), source)
            if len(bars) < MIN_BARS:
                skipped[symbol] = f"only {len(bars)} sessions stored"
                continue
            series[symbol] = bars
    return series, skipped


def load_implied(settings: Settings, symbol: str) -> dict:
    """Stored vendor implied volatility by session, for the option modes."""
    with db.session(settings.sqlite_path) as conn:
        rows = conn.execute(
            "SELECT session_date, iv30 FROM vendor_daily "
            "WHERE symbol = ? AND iv30 IS NOT NULL ORDER BY session_date",
            (symbol,),
        ).fetchall()
    return {
        datetime.fromisoformat(row[0]).date() if isinstance(row[0], str) else row[0]: row[1]
        for row in rows
    }


def run(settings: Settings, strategy: Strategy) -> BacktestResult:
    """Run one strategy and return everything needed to judge it.

    The order matters: the strategy's own trades first, then a null built from exactly
    the symbols and entry counts the strategy actually used. Building the null from the
    requested symbols instead would compare against a different portfolio whenever
    anything was skipped.
    """
    started = time.perf_counter()
    strategy.validate()
    result = BacktestResult()

    symbols = resolve_symbols(settings, strategy)
    if strategy.entry in EXPENSIVE_RULES and len(symbols) > MAX_LEVEL_SYMBOLS:
        result.notes.append(
            f"{strategy.entry} rebuilds levels roughly a hundred times per symbol, so "
            f"the run is capped at {MAX_LEVEL_SYMBOLS} of {len(symbols)} symbols. "
            "Name symbols or a group explicitly to choose which."
        )
        symbols = symbols[:MAX_LEVEL_SYMBOLS]

    series, skipped = load_series(settings, symbols)
    result.skipped = skipped
    if not series:
        result.notes.append("No symbol had enough stored history. Run `optscan prices sync` first.")
        return result

    rule = rule_registry.resolve(strategy.entry, strategy.entry_params)
    direction = Direction(strategy.direction)

    trades: list[Trade] = []
    # Signal indices that actually produced a trade, per symbol. The null replays this
    # exact pattern at a different point in history, so it has to be the traded entries
    # rather than everything the rule fired on: overlap suppression drops some, and a
    # null with more trades than the strategy has a tighter mean.
    fired: dict[str, list[int]] = {}

    for symbol, bars in series.items():
        entries = rule(bars)
        if not entries:
            continue

        if strategy.mode == "underlying":
            found = simulate(
                symbol,
                bars,
                entries,
                direction=direction,
                horizon=strategy.horizon,
                target=strategy.target,
                stop=strategy.stop,
                cost=strategy.cost,
            )
        else:
            implied = load_implied(settings, symbol)
            try:
                require_implied_vol(symbol, len(implied), MIN_BARS)
            except MissingImpliedVol as error:
                result.skipped[symbol] = str(error)
                continue
            found = option_trades_as_trades(
                short_option_trades(
                    symbol,
                    bars,
                    implied,
                    entries,
                    right="put" if strategy.mode == "short_put" else "call",
                    dte=strategy.dte,
                    offset=strategy.offset,
                    rate=strategy.rate,
                    slippage=strategy.slippage,
                )
            )

        if found:
            trades.extend(found)
            # entry_index is the fill, one bar after the signal, and the forward return
            # table is indexed by the signal.
            fired[symbol] = [t.entry_index - 1 for t in found]

    result.trades = sorted(trades, key=lambda t: t.exit_date)
    result.stats = summarize(trades, strategy.block_bars)
    if result.stats is None:
        result.notes.append(
            "The rule never fired on any symbol with enough history, so there is "
            "nothing to measure. That is a finding about the rule, not an error."
        )
        return result

    result.by_year = yearly(trades)
    result.equity = equity_curve(trades)

    # The null replays the strategy's own entry pattern, slid to a random point in
    # history, so the only difference between it and the strategy is *when* it happened.
    result.edge = measure_edge(
        result.stats.mean_return, _null(settings, strategy, series, fired, direction)
    )

    result.notes.extend(_interpretation(strategy, result))
    log.info(
        "backtest",
        strategy=strategy.name,
        entry=strategy.entry,
        symbols=len(series),
        trades=len(trades),
        seconds=round(time.perf_counter() - started, 1),
    )
    return result


def run_sweep(
    settings: Settings, strategy: Strategy, key: str, values: Sequence[str]
) -> SweepResult:
    """Try several values of one entry parameter and say what the winner is worth.

    The null is computed **once**, from the base strategy, and shared across the cells.
    That is deliberate rather than a shortcut: the question a sweep has to answer is not
    "is this cell good" but "is the best of this many cells better than the best of that
    many draws from the same null", and that comparison needs one null, not one per cell.

    Every cell reuses the same symbols, horizon and costs, so the only thing varying is
    the parameter. A sweep that changed two things at once would not be interpretable.
    """
    if not values:
        raise ValueError("a sweep needs at least one value")

    typed: list = []
    for raw in values:
        try:
            typed.append(int(raw) if raw.lstrip("-").isdigit() else float(raw))
        except ValueError:
            typed.append(raw)

    base = run(settings, strategy)
    nulls: list[float] = []
    if base.edge is not None:
        # measure_edge does not keep the draws, so rebuild them for the best-of-N maths.
        series, _ = load_series(settings, resolve_symbols(settings, strategy))
        fired = {
            symbol: [t.entry_index - 1 for t in base.trades if t.symbol == symbol]
            for symbol in series
        }
        nulls = _null(settings, strategy, series, fired, Direction(strategy.direction))

    cells = []
    for value in typed:
        variant = Strategy.from_dict(strategy.to_dict())
        variant.entry_params = {**strategy.entry_params, key: value}
        variant.name = f"{key}={value}"
        try:
            variant.validate()
        except ValueError as error:
            raise ValueError(f"{key}={value}: {error}") from error
        outcome = run(settings, variant)
        cells.append((variant.name, {key: value}, outcome.trades))

    return sweep(cells, nulls, block_bars=strategy.block_bars)


def _null(
    settings: Settings,
    strategy: Strategy,
    series: dict[str, list[PriceBar]],
    fired: dict[str, list[int]],
    direction: Direction,
) -> list[float]:
    """The null for whichever mode is running.

    For the option modes the same pricing model values the strategy and the null, so
    whatever the model gets wrong cancels between them. That makes the *comparison* sound
    even though the absolute return level is only as good as the model, which is the
    opposite of the intuition and is why the option modes get a null at all.
    """
    if strategy.mode == "underlying":
        return null_distribution(
            series,
            fired,
            direction=direction,
            horizon=strategy.horizon,
            target=strategy.target,
            stop=strategy.stop,
            cost=strategy.cost,
            draws=strategy.draws,
            seed=strategy.seed,
            block_bars=strategy.block_bars,
        )

    tables = {
        symbol: option_forward_return_table(
            symbol,
            series[symbol],
            load_implied(settings, symbol),
            right="put" if strategy.mode == "short_put" else "call",
            dte=strategy.dte,
            offset=strategy.offset,
            rate=strategy.rate,
            slippage=strategy.slippage,
        )
        for symbol, indices in fired.items()
        if indices
    }
    return shift_null(tables, fired, draws=strategy.draws, seed=strategy.seed)


def _interpretation(strategy: Strategy, result: BacktestResult) -> list[str]:
    """The sentences a reader needs before believing any of the numbers above."""
    notes = [
        "The universe is symbols that are interesting today, so these results are "
        "survivorship biased: the companies that did not make it to 2026 are not in "
        "the sample. That is usually a larger effect than costs and slippage together."
    ]

    stats = result.stats
    if stats is not None and not stats.enough_to_claim:
        notes.append(
            f"{stats.trades} trades fall into only {stats.effective_sample} independent "
            f"blocks, below the floor of {MIN_BLOCKS_FOR_A_CLAIM}. Overlapping trades "
            "in one market episode are one observation, so no claim is made here."
        )

    edge = result.edge
    if edge is not None:
        if edge.p_value > SIGNIFICANT:
            notes.append(
                f"Entering at random on the same symbols returned {edge.null_mean:.2%} "
                f"against this rule's {edge.strategy_mean:.2%} (p={edge.p_value:.3f}). "
                "The rule's timing did not do anything the market was not already doing."
            )
        else:
            notes.append(
                f"The rule beat random entry on the same symbols by {edge.edge:.2%} "
                f"(p={edge.p_value:.3f}). That is one test of one rule chosen after "
                "seeing this data, which is not the same as an edge that will persist."
            )

    if strategy.mode != "underlying":
        notes.append(
            f"Entry credits are modelled with Black-Scholes on stored vendor implied "
            f"volatility and haircut {strategy.slippage:.0%} for the spread. The payoff "
            "is exact because the underlying's path is real. The null prices its entries "
            "with the same model, so the comparison against it survives the model being "
            "wrong; the absolute return does not. Run it again with a different slippage "
            "before believing the magnitude."
        )

    return notes
