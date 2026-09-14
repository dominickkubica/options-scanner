"""The short premium study, run over the stored option tape.

Replays put credit spreads, call credit spreads and iron condors on QQQ and SPY at 0
and 1 DTE, from February 2024, on every stored session. Each session opens one position
per structure and DTE, and every exit rule and every fill assumption is applied to the
**same** positions, so rules are compared on identical entries and their difference is
what the rule did.

## The grid is the spec's first tier, not a cross product

Tier 1 asks three questions, and they are asked separately rather than crossed, because
crossing them is how a grid manufactures a winner:

1. **Stop policy.** No stop, 1x, 1.5x and 2x credit, a short strike touch, and a short
   delta of 0.30, 0.40 and 0.50, each both flattened before the close and held to
   expiry. With no profit target, so the stop is the only thing that differs.
2. **Profit target.** 25, 50 and 75 percent of the credit, with no stop.
3. **Side.** Put spreads and call spreads are opened exactly as a condor's two sides
   are, so the condor decomposes into them, and each is split by trend.

The baseline every rule is paired against is no stop, no target, flattened five minutes
before the close.

## What this cannot say

Everything `analytics/premium.py` says about marks and fills, plus: two and a half
years is not a regime sample. It holds the August 2024 unwind and the April 2025
selloff and nothing like February 2018 or March 2020, and every result says so.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime

import numpy as np
import pandas as pd

from optscan.analytics.ledger import build_trades
from optscan.analytics.positions import attach, build_positions
from optscan.analytics.premium import (
    EntryRule,
    ExitRule,
    SessionMarks,
    SpreadModel,
    Structure,
    Trade,
    build_marks,
    build_path,
    expiry_seconds,
    run_exit,
    select_entry,
)
from optscan.analytics.premium_stats import (
    CellStats,
    Difference,
    benjamini_hochberg,
    cell_stats,
    paired_difference,
)
from optscan.analytics.vector_bsm import bsm_delta, bsm_price, implied_vol
from optscan.config import Settings
from optscan.jobs.option_tape import following_sessions
from optscan.logging import get_logger
from optscan.market_calendar import session_bounds
from optscan.storage import db, journal_book, ledger
from optscan.storage.option_tape import read_session, sessions_held, tape_root
from optscan.storage.snapshots import read_snapshots
from optscan.storage.vendor import daily_bars, preferred_source

log = get_logger("optscan.jobs.premium")

DEFAULT_SYMBOLS = ("QQQ", "SPY")
DEFAULT_DTES = (0, 1)

#: Fill assumptions: the share of the modelled half spread paid on every leg, both ways.
DEFAULT_KS = (0.0, 0.25, 0.5, 1.0)

#: The fill assumption headline tables are printed at. Every other k is reported too.
HEADLINE_K = 0.5

#: Sessions in the moving average that labels an entry's trend.
TREND_WINDOW = 20

#: Frozen, so one instance can safely stand as every default.
DEFAULT_ENTRY = EntryRule()
DEFAULT_SPREADS = SpreadModel()

#: Paired against every rule.
BASELINE = ExitRule()

SUPPORTED_DTES = frozenset({0, 1})

STOPS = (
    ExitRule(),
    ExitRule(stop=1.0),
    ExitRule(stop=1.5),
    ExitRule(stop=2.0),
    ExitRule(touch=True),
    ExitRule(delta_stop=0.30),
    ExitRule(delta_stop=0.40),
    ExitRule(delta_stop=0.50),
)
STOP_RULES = STOPS + tuple(replace(rule, settle=True) for rule in STOPS)
TARGET_RULES = tuple(ExitRule(target=share) for share in (0.25, 0.5, 0.75))

CellKey = tuple[int, Structure, str, float]


@dataclass(frozen=True, slots=True)
class StudySpec:
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS
    start: date | None = None
    end: date | None = None
    dtes: tuple[int, ...] = DEFAULT_DTES
    structures: tuple[Structure, ...] = tuple(Structure)
    entry: EntryRule = DEFAULT_ENTRY
    ks: tuple[float, ...] = DEFAULT_KS
    rules: tuple[ExitRule, ...] = STOP_RULES + TARGET_RULES
    spreads: SpreadModel = DEFAULT_SPREADS


@dataclass
class StudyResult:
    spec: StudySpec
    trades: dict[CellKey, list[Trade]] = field(default_factory=lambda: defaultdict(list))
    #: "above" or "below" the 20 session average, per (symbol, entry session).
    trend: dict[tuple[str, date], str] = field(default_factory=dict)
    opened: Counter = field(default_factory=Counter)
    refused: Counter = field(default_factory=Counter)
    unmarked: Counter = field(default_factory=Counter)
    #: Per symbol: (first session, last session, sessions read).
    coverage: dict[str, tuple[date, date, int]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CellRow:
    dte: int
    structure: Structure
    rule: str
    k: float
    stats: CellStats
    versus_baseline: Difference | None
    q: float | None = None
    q_versus_baseline: float | None = None


def _marks(settings: Settings, symbol: str, session: date) -> SessionMarks | None:
    stored = read_session(tape_root(settings.data_path), symbol, session)
    bounds = session_bounds(session, settings.market_calendar)
    if stored is None or bounds is None:
        return None
    return build_marks(
        symbol,
        session,
        stored.minutes,
        stored.options,
        close=bounds[1],
        rate=settings.risk_free_rate,
    )


def trend_labels(settings: Settings, symbol: str, sessions: Sequence[date]) -> dict[date, str]:
    """Whether the prior close sat above or below its own 20 session average.

    From the stored daily bars, which are adjusted; the comparison is within that one
    series, so the adjustment cancels. Only closes before the session are used.
    """
    with db.session(settings.sqlite_path) as conn:
        source = preferred_source(conn, symbol)
        bars = daily_bars(conn, symbol, source=source) if source else []
    closes = [(bar.session_date, bar.close) for bar in bars]
    labels: dict[date, str] = {}
    cursor = 0
    history: list[float] = []
    for session in sessions:
        while cursor < len(closes) and closes[cursor][0] < session:
            history.append(closes[cursor][1])
            cursor += 1
        if len(history) >= TREND_WINDOW:
            average = sum(history[-TREND_WINDOW:]) / TREND_WINDOW
            labels[session] = "above" if history[-1] >= average else "below"
    return labels


def run_study(
    settings: Settings,
    spec: StudySpec | None = None,
    *,
    progress: Callable[[int, int, str], None] | None = None,
) -> StudyResult:
    """Open, manage and close every position the spec describes, on every held session."""
    spec = spec or StudySpec()
    unsupported = set(spec.dtes) - SUPPORTED_DTES
    if unsupported:
        raise ValueError(
            f"DTE {sorted(unsupported)} is not supported: the tape holds each session's own "
            "expiry and the next one, so 0 and 1 DTE are what it can mark."
        )
    result = StudyResult(spec=spec)
    root = tape_root(settings.data_path)
    plan = {
        symbol: [
            day
            for day in sessions_held(root, symbol)
            if (spec.start is None or day >= spec.start) and (spec.end is None or day <= spec.end)
        ]
        for symbol in spec.symbols
    }
    total = sum(len(days) for days in plan.values())
    done = 0
    for symbol, days in plan.items():
        if not days:
            continue
        result.coverage[symbol] = (days[0], days[-1], len(days))
        labels = trend_labels(settings, symbol, days)
        previous: SessionMarks | None = None
        for session in days:
            marks = _marks(settings, symbol, session)
            done += 1
            if marks is None:
                previous = None
                continue
            if session in labels:
                result.trend[(symbol, session)] = labels[session]
            for dte in spec.dtes:
                if dte == 0:
                    chain = [marks]
                elif previous is not None and following_sessions(
                    previous.session, 1, settings.market_calendar
                ) == [session]:
                    chain = [previous, marks]
                    if previous.session in labels:
                        result.trend[(symbol, previous.session)] = labels[previous.session]
                else:
                    continue
                _open_and_manage(result, spec, chain, dte, settings.risk_free_rate)
            previous = marks
            if progress is not None:
                progress(done, total, f"{symbol} {session}")
    return result


def _open_and_manage(
    result: StudyResult,
    spec: StudySpec,
    chain: Sequence[SessionMarks],
    dte: int,
    rate: float,
) -> None:
    entry_rule = replace(spec.entry, dte=dte)
    expiry = chain[-1].session
    for structure in spec.structures:
        entry = select_entry(chain[0], expiry, structure, entry_rule, spec.spreads, rate)
        if isinstance(entry, str):
            result.refused[(dte, structure, entry)] += 1
            continue
        path = build_path(entry, chain, entry_rule.minute, spec.spreads, rate)
        if path is None:
            result.unmarked[(dte, structure)] += 1
            continue
        result.opened[(dte, structure)] += 1
        for k in spec.ks:
            credit_gone = False
            for rule in spec.rules:
                trade = run_exit(entry, path, rule, k)
                if trade is None:
                    credit_gone = True
                    continue
                result.trades[(dte, structure, rule.label, k)].append(trade)
            if credit_gone:
                result.refused[(dte, structure, f"no credit left at k={k:g}")] += 1


def cell_rows(result: StudyResult) -> list[CellRow]:
    """Every cell, with its interval, its paired difference from the baseline, and
    Benjamini-Hochberg q-values over the whole grid for both."""
    raw: list[tuple[CellKey, CellStats, Difference | None]] = []
    for key, trades in sorted(result.trades.items(), key=lambda item: str(item[0])):
        stats = cell_stats(trades)
        if stats is None:
            continue
        dte, structure, rule, k = key
        baseline = result.trades.get((dte, structure, BASELINE.label, k), [])
        difference = paired_difference(trades, baseline) if rule != BASELINE.label else None
        raw.append((key, stats, difference))

    q_cells = benjamini_hochberg([stats.p_value for _, stats, _ in raw])
    q_differences = benjamini_hochberg(
        [difference.p_value if difference else None for _, _, difference in raw]
    )
    return [
        CellRow(
            dte=key[0],
            structure=key[1],
            rule=key[2],
            k=key[3],
            stats=stats,
            versus_baseline=difference,
            q=q_cell,
            q_versus_baseline=q_difference,
        )
        for (key, stats, difference), q_cell, q_difference in zip(
            raw, q_cells, q_differences, strict=True
        )
    ]


def by_trend(
    result: StudyResult, dte: int, rule: str, k: float
) -> dict[tuple[Structure, str], CellStats]:
    """Spec question three: each structure's baseline split by the entry's trend."""
    out: dict[tuple[Structure, str], CellStats] = {}
    for structure in result.spec.structures:
        trades = result.trades.get((dte, structure, rule, k), [])
        for label in ("above", "below"):
            subset = [t for t in trades if result.trend.get((t.underlying, t.session)) == label]
            stats = cell_stats(subset)
            if stats is not None:
                out[(structure, label)] = stats
    return out


# --------------------------------------------------------------------------------
# The spread model, refit from captured quotes
# --------------------------------------------------------------------------------

#: A bucket needs this many captured quotes before its median replaces the default.
MIN_SPREAD_SAMPLES = 20

#: Captured quotes this many days or fewer from expiry inform the spread model.
MAX_SPREAD_DTE = 3

#: ACT/365 in seconds, the year `greeks.py` measures time in.
YEAR_SECONDS = 365.0 * 24 * 60 * 60

_EPOCH = pd.Timestamp("1970-01-01", tz="UTC")


@dataclass(frozen=True, slots=True)
class SpreadFit:
    model: SpreadModel
    #: Quotes behind each bucket, same-day then later, in `model.edges` order.
    samples_same_day: tuple[int, ...]
    samples_later: tuple[int, ...]
    sessions: int


def fit_spread_model(settings: Settings, symbols: Sequence[str] = DEFAULT_SYMBOLS) -> SpreadFit:
    """The half spread per leg, measured on this project's own captured quotes.

    Every stored 0 to 3 DTE quote with a two sided market is solved for its delta and
    bucketed; each bucket takes its median half spread. A bucket with fewer than
    `MIN_SPREAD_SAMPLES` quotes keeps the default and is reported as unmeasured.

    The captures are all taken at 15:45, so this is the late afternoon spread of calm
    and ordinary sessions. It says nothing about how far spreads widen in a fast
    market, which is exactly when a stop fills; the k sweep is what covers that.
    """
    edges = np.asarray(DEFAULT_SPREADS.edges)
    halves: dict[tuple[bool, int], list[float]] = defaultdict(list)
    sessions: set[date] = set()
    rate = settings.risk_free_rate
    for symbol in symbols:
        frame = read_snapshots(settings.snapshot_path, symbol)
        if frame.empty:
            continue
        session = frame["session_date"].dt.date.to_numpy()
        expiry = frame["expiry"].dt.date.to_numpy()
        dte = np.array([(e - s).days for e, s in zip(expiry, session, strict=True)])
        bid = frame["bid"].to_numpy(dtype=float)
        ask = frame["ask"].to_numpy(dtype=float)
        spot = frame["underlying_price"].to_numpy(dtype=float)
        keep = (bid > 0) & (ask > bid) & (spot > 0) & (dte >= 0) & (dte <= MAX_SPREAD_DTE)
        if not keep.any():
            continue
        closes = np.array([expiry_seconds(day) for day in expiry[keep]], dtype=float)
        seconds = ((frame["captured_at"][keep] - _EPOCH) // pd.Timedelta(seconds=1)).to_numpy(
            dtype=float
        )
        years = np.maximum((closes - seconds) / YEAR_SECONDS, 0.0)
        calls = frame["right"].to_numpy()[keep] == "C"
        strikes = frame["strike"].to_numpy(dtype=float)[keep]
        mid = (bid[keep] + ask[keep]) / 2
        sigma = implied_vol(calls, mid, spot[keep], strikes, years, rate)
        delta = np.abs(bsm_delta(calls, spot[keep], strikes, years, rate, sigma))
        bucket = np.clip(
            np.searchsorted(edges, np.nan_to_num(delta), side="right"), 0, len(edges) - 1
        )
        half = (ask[keep] - bid[keep]) / 2
        same_day = dte[keep] == 0
        for index in np.flatnonzero(np.isfinite(sigma)):
            halves[(bool(same_day[index]), int(bucket[index]))].append(float(half[index]))
        sessions.update(session[keep])

    same_values, same_counts = _fitted(halves, True, DEFAULT_SPREADS.same_day)
    later_values, later_counts = _fitted(halves, False, DEFAULT_SPREADS.later)
    return SpreadFit(
        model=SpreadModel(edges=DEFAULT_SPREADS.edges, same_day=same_values, later=later_values),
        samples_same_day=same_counts,
        samples_later=later_counts,
        sessions=len(sessions),
    )


def _fitted(
    halves: dict[tuple[bool, int], list[float]], same_day: bool, fallback: tuple[float, ...]
) -> tuple[tuple[float, ...], tuple[int, ...]]:
    values, counts = [], []
    for index, default_value in enumerate(fallback):
        sample = sorted(halves.get((same_day, index), []))
        counts.append(len(sample))
        values.append(
            sample[len(sample) // 2] if len(sample) >= MIN_SPREAD_SAMPLES else default_value
        )
    return tuple(values), tuple(counts)


# --------------------------------------------------------------------------------
# Reconciliation: the account's own fills, replayed on the tape
# --------------------------------------------------------------------------------

#: A position whose opening (or closing) fills span more than this was scaled in (or
#: out), and one minute's marks cannot price it.
MAX_FILL_SPREAD_SECONDS = 120


@dataclass(frozen=True, slots=True)
class Replay:
    """One real position, priced at the study's marks at the minutes it was filled."""

    key: str
    symbol: str
    session: date
    minute: int
    legs: int
    actual_credit: float
    model_credit: float
    entry_half: float
    exit_minute: int | None = None
    actual_debit: float | None = None
    model_debit: float | None = None
    exit_half: float | None = None

    @property
    def entry_cost(self) -> float:
        """What the entry gave up against the mark, per share. Positive is worse."""
        return self.model_credit - self.actual_credit

    @property
    def entry_k(self) -> float | None:
        return self.entry_cost / self.entry_half if self.entry_half > 0 else None

    @property
    def exit_cost(self) -> float | None:
        if self.actual_debit is None or self.model_debit is None:
            return None
        return self.actual_debit - self.model_debit

    @property
    def exit_k(self) -> float | None:
        cost = self.exit_cost
        return cost / self.exit_half if cost is not None and self.exit_half else None


@dataclass
class Reconciliation:
    replays: list[Replay] = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)


def _minute(marks: SessionMarks, moment: datetime) -> int | None:
    stamp = int(moment.timestamp()) // 60 * 60
    index = int(np.searchsorted(marks.times, stamp))
    return index if index < len(marks.times) and marks.times[index] == stamp else None


def _price_legs(
    marks: SessionMarks,
    expiry: date,
    legs: Sequence[tuple[bool, float, float, float]],
    minute: int,
    *,
    rate: float,
    spreads: SpreadModel,
) -> tuple[float, float] | None:
    """(net credit at the mid, summed half spread), each leg weighted, or None."""
    smile = marks.smiles.get(expiry)
    if smile is None:
        return None
    spot = float(marks.spot[minute])
    years = float(smile.years[minute])
    net = half = 0.0
    for is_call, strike, sign, weight in legs:
        column = smile.column(strike)
        if column is None or not np.isfinite(column[minute]):
            return None
        sigma = float(column[minute])
        price = float(bsm_price(is_call, spot, strike, years, rate, sigma))
        delta = abs(float(bsm_delta(is_call, spot, strike, years, rate, sigma)))
        net += sign * weight * price
        half += weight * float(spreads.half(np.array([0]), np.array([delta]))[0])
    return net, half


def _filled_together(legs: Sequence, side: str, times: dict) -> bool:
    """Whether every fill on one side of a position landed within two minutes."""
    fills = [times.get((t.digest, t.dup_index)) for leg in legs for t in getattr(leg, side)]
    if not fills or any(moment is None for moment in fills):
        return False
    stamps = [moment.timestamp() for moment in fills]
    return max(stamps) - min(stamps) <= MAX_FILL_SPREAD_SECONDS


def _unreplayable(position, held: dict[str, set[date]], times: dict) -> str | None:
    """Why a position cannot be replayed, or None. Checked in order, lazily, because
    the later checks assume the earlier ones passed."""
    checks: tuple[tuple[Callable[[], bool], str], ...] = (
        (lambda: not position.is_option, "not an option position"),
        (lambda: position.is_open, "still open"),
        (lambda: position.symbol not in held, "not an underlying in the tape"),
        (lambda: position.contract_expiry != position.opened_at, "not 0DTE"),
        (lambda: position.entry_at is None, "no fill time"),
        (lambda: position.opened_at not in held[position.symbol], "session not in the tape"),
        (
            lambda: any(
                leg.right is None or leg.strike is None or leg.open_price is None
                for leg in position.legs
            ),
            "a leg without a price",
        ),
        (
            lambda: not _filled_together(position.legs, "opening", times),
            "opened over more than two minutes",
        ),
    )
    return next((reason for failed, reason in checks if failed()), None)


def reconcile(
    settings: Settings,
    symbols: Sequence[str] = DEFAULT_SYMBOLS,
    spreads: SpreadModel = DEFAULT_SPREADS,
) -> Reconciliation:
    """Replay the account's own 0DTE fills on the tape.

    The spec's best single sanity check: price each real position's legs at the study's
    marks in the minute it was filled, and see what the fill gave up against the mark.
    If real fills sit further from the mark than the k a result is read at, that result
    is optimistic by the difference, whatever else it says.
    """
    with db.session(settings.sqlite_path) as conn:
        txns = ledger.all_transactions(conn)
        times = journal_book.fill_times(conn)
        notes = journal_book.annotations(conn)
    positions = build_positions(build_trades(txns))
    attach(positions, annotations=notes, fill_times=times)

    root = tape_root(settings.data_path)
    held = {symbol: set(sessions_held(root, symbol)) for symbol in symbols}
    cache: dict[tuple[str, date], SessionMarks | None] = {}
    out = Reconciliation()
    rate = settings.risk_free_rate
    for position in positions:
        reason = _unreplayable(position, held, times)
        if reason:
            out.skipped[reason] += 1
            continue
        where = (position.symbol, position.opened_at)
        if where not in cache:
            cache[where] = _marks(settings, *where)
        marks = cache[where]
        minute = _minute(marks, position.entry_at) if marks is not None else None
        if marks is None or minute is None:
            out.skipped["entry outside the stored session"] += 1
            continue
        units = position.units or 1.0
        legs = [
            (
                leg.right == "call",
                float(leg.strike),
                1.0 if leg.side == "short" else -1.0,
                leg.size / units,
            )
            for leg in position.legs
        ]
        priced = _price_legs(
            marks, position.contract_expiry, legs, minute, rate=rate, spreads=spreads
        )
        if priced is None:
            out.skipped["a leg has no mark at entry"] += 1
            continue
        actual = sum(
            sign * weight * float(leg.open_price)
            for (_, _, sign, weight), leg in zip(legs, position.legs, strict=True)
        )
        replay = Replay(
            key=position.key,
            symbol=position.symbol,
            session=position.opened_at,
            minute=minute,
            legs=len(legs),
            actual_credit=actual,
            model_credit=priced[0],
            entry_half=priced[1],
        )
        out.replays.append(
            _with_exit(replay, position, marks, legs, times=times, rate=rate, spreads=spreads)
        )
    return out


def _with_exit(
    replay: Replay,
    position,
    marks: SessionMarks,
    legs: Sequence[tuple[bool, float, float, float]],
    *,
    times: dict,
    rate: float,
    spreads: SpreadModel,
) -> Replay:
    """The replay with its exit priced too, when the position closed by a trade."""
    if position.exit_at is None or any(
        leg.close_price is None or leg.ended_by_event for leg in position.legs
    ):
        return replay
    minute = _minute(marks, position.exit_at)
    if (
        minute is None
        or minute <= replay.minute
        or not _filled_together(position.legs, "closing", times)
    ):
        return replay
    priced = _price_legs(marks, position.contract_expiry, legs, minute, rate=rate, spreads=spreads)
    if priced is None:
        return replay
    debit = sum(
        sign * weight * float(leg.close_price)
        for (_, _, sign, weight), leg in zip(legs, position.legs, strict=True)
    )
    return replace(
        replay, exit_minute=minute, actual_debit=debit, model_debit=priced[0], exit_half=priced[1]
    )


# --------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------

#: `statistics.quantiles` needs at least this many values to split into quarters.
QUARTILES = 4


def row_payload(row: CellRow) -> dict:
    return asdict(row)


def _clock(minute: int) -> str:
    total = 9 * 60 + 30 + minute
    return f"{total // 60}:{total % 60:02d}"


def _money(value: float | None) -> str:
    return "-" if value is None else f"{value:+.2f}"


def _q(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def _quartiles(values: Sequence[float]) -> str:
    if len(values) < QUARTILES:
        return "n/a"
    low, middle, high = statistics.quantiles(values, n=QUARTILES)
    return f"{middle:+.3f} (middle half {low:+.3f} to {high:+.3f})"


def reconcile_lines(result: Reconciliation) -> list[str]:
    replays = result.replays
    lines = [
        f"Replayed {len(replays)} of your 0DTE positions on the tape "
        f"({sum(result.skipped.values())} skipped: "
        + ", ".join(f"{reason} {count}" for reason, count in result.skipped.most_common())
        + ")."
    ]
    if not replays:
        lines.append("Nothing to compare. Run `optscan premium sync` over your trading dates.")
        return lines
    entry_costs = [replay.entry_cost for replay in replays]
    entry_ks = [replay.entry_k for replay in replays if replay.entry_k is not None]
    lines += [
        "",
        "Entry: what each fill gave up against the study's mark, per share",
        f"  median {_quartiles(entry_costs)}",
        f"  as k   {_quartiles(entry_ks)}",
        f"  {sum(1 for cost in entry_costs if cost < 0)} of {len(entry_costs)} filled better "
        "than the mark",
    ]
    exits = [replay for replay in replays if replay.exit_cost is not None]
    if exits:
        exit_ks = [replay.exit_k for replay in exits if replay.exit_k is not None]
        lines += [
            "",
            f"Exit, for the {len(exits)} closed by a trade inside two minutes",
            f"  median {_quartiles([replay.exit_cost for replay in exits])}",
            f"  as k   {_quartiles(exit_ks)}",
        ]
    lines += [
        "",
        "Read the study at the k your fills match. A mark is a trade-print smile, not a",
        "quoted mid, so a small cost either way is inside the mark's own noise.",
        "",
        f"  {'session':<11}{'sym':<5}{'time':>6}{'legs':>5}"
        f"{'filled':>9}{'mark':>9}{'cost':>8}{'k':>7}",
    ]
    for replay in sorted(replays, key=lambda item: (item.session, item.minute)):
        k = "-" if replay.entry_k is None else f"{replay.entry_k:+.1f}"
        lines.append(
            f"  {replay.session.isoformat():<11}{replay.symbol:<5}{_clock(replay.minute):>6}"
            f"{replay.legs:>5}{replay.actual_credit:>9.3f}{replay.model_credit:>9.3f}"
            f"{replay.entry_cost:>+8.3f}{k:>7}"
        )
    return lines


def report_lines(
    result: StudyResult, rows: Sequence[CellRow], *, notes: Sequence[str] = ()
) -> list[str]:
    spec = result.spec
    k = HEADLINE_K if HEADLINE_K in spec.ks else spec.ks[0]
    lines = [
        "Short premium study: "
        + ", ".join(
            f"{symbol} {n} sessions {first} to {last}"
            for symbol, (first, last, n) in result.coverage.items()
        ),
        f"Entry at {_clock(spec.entry.minute)} New York, short strike nearest "
        f"{spec.entry.short_delta:.2f} delta, ${spec.entry.width:g} wings.",
        "Opened: "
        + ", ".join(
            f"{structure.value} {dte}DTE {count}"
            for (dte, structure), count in sorted(result.opened.items(), key=str)
        ),
    ]
    if result.refused:
        lines.append(
            "Refused: "
            + "; ".join(
                f"{dte}DTE {structure.value}: {reason} ({count})"
                for (dte, structure, reason), count in result.refused.most_common(6)
            )
        )
    if result.unmarked:
        lines.append(f"Could not be marked to expiry: {sum(result.unmarked.values())}")
    lines += list(notes)

    order = {rule.label: index for index, rule in enumerate(spec.rules)}
    for dte in spec.dtes:
        for structure in spec.structures:
            cells = sorted(
                (r for r in rows if r.dte == dte and r.structure is structure and r.k == k),
                key=lambda r: order.get(r.rule, len(order)),
            )
            if not cells:
                continue
            lines += [
                "",
                f"{structure.value}, {dte} DTE, k={k:g}. Dollars per one-lot position after fees;",
                "95% interval by trading-week blocks; q is Benjamini-Hochberg over every cell.",
                f"  {'rule':<44}{'trades':>7}{'days':>6}{'mean':>8}{'interval':>17}{'q':>6}"
                f"{'vs base':>9}{'q':>6}{'win':>6}{'R':>7}{'worst day':>10}{'w/o worst 5':>12}",
            ]
            lines += [_row_line(row) for row in cells]

    lines += ["", f"What the fill assumption costs, baseline rule ({BASELINE.label})"]
    for dte in spec.dtes:
        for structure in spec.structures:
            means = {
                row.k: row.stats.mean_pnl
                for row in rows
                if row.dte == dte and row.structure is structure and row.rule == BASELINE.label
            }
            if means:
                lines.append(
                    f"  {structure.value:<13}{dte}DTE  "
                    + "  ".join(f"k={key:g} {value:+7.2f}" for key, value in sorted(means.items()))
                )

    lines += [
        "",
        f"Side by trend, baseline rule, k={k:g} (prior close against its 20-session average)",
    ]
    for dte in spec.dtes:
        split = by_trend(result, dte, BASELINE.label, k)
        for (structure, label), stats in sorted(split.items(), key=str):
            interval = (
                f"[{stats.ci_low:+.2f}, {stats.ci_high:+.2f}]"
                if stats.ci_low is not None
                else "too few"
            )
            lines.append(
                f"  {dte}DTE {structure.value:<13}{label:<6}{stats.trades:>6} trades "
                f"{stats.mean_pnl:+8.2f} {interval:>18}  win {stats.win_rate:.0%}"
            )

    lines += [
        "",
        "What this cannot tell you:",
        "  Marks are Alpaca trade prints turned into a per-minute smile, not quoted mids.",
        "  Fills are the mark plus or minus k half spreads, the half spread measured on this",
        "  project's own 15:45 captures in ordinary conditions. `optscan premium reconcile`",
        "  shows which k your own fills match; read the table at that k.",
        "  February 2024 onward holds the August 2024 unwind and the April 2025 selloff and",
        "  nothing like February 2018 or March 2020. A stop's worth in a real crash is untested.",
        "  QQQ and SPY move together: sessions, not trades, are the unit of evidence.",
        "  Held to expiry settles at the last regular minute; after hours moves and",
        "  assignment are not modelled. One entry rule was run; tiers 2 to 4 were not.",
    ]
    return lines


def _row_line(row: CellRow) -> str:
    stats = row.stats
    if not stats.reportable:
        return f"  {row.rule:<44}{stats.trades:>7}{stats.sessions:>6}  too few sessions to report"
    interval = f"[{stats.ci_low:+.2f}, {stats.ci_high:+.2f}]"
    versus = row.versus_baseline
    return (
        f"  {row.rule:<44}{stats.trades:>7}{stats.sessions:>6}{stats.mean_pnl:>+8.2f}"
        f"{interval:>17}{_q(row.q):>6}{_money(versus.mean if versus else None):>9}"
        f"{_q(row.q_versus_baseline):>6}{stats.win_rate:>6.0%}{stats.mean_r:>+7.2f}"
        f"{stats.worst_day:>+10.0f}{stats.without_worst:>+12.0f}"
    )
