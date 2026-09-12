"""Positions: the per-contract round trips folded back into the trades a person placed.

`analytics/ledger.py` cuts the broker ledger into one round trip per contract, because
that is the arithmetic. Nobody places a trade that way. A condor is four contracts and
one decision, so this module groups every contract opened in one underlying, on one
expiry, on one day, into a single `Position`, and derives what a journal row shows: the
legs, the net credit or debit per unit, DTE at entry, risk at entry, the result in
dollars and in R, and a guess at the strategy.

## What is inferred, and says so

The strategy is a guess from the shape of the legs. Verticals, condors, butterflies,
straddles and single legs are recognised; anything else -- a position scaled into at
several strikes, a condor with a side added later -- is labelled "multi-leg" and marked
unconfident, so the journal asks the trader to tag it rather than inventing a name. A
trader's own tag always wins over the guess.

Risk at entry is the worst the position could lose if every leg were held to expiry,
found by evaluating the payoff at each strike. Exact for legs held together, and only
computed for shapes the journal can name: a position scaled into during the day has
each leg at its peak at a different moment, and treating those peaks as held at once
netted three real positions into naked shorts worth $70,000 to $76,000 of "risk".
Sizing therefore measures 1R, which the credit bounds, not risk at entry.

## R is the trader's own stop, not the theoretical maximum

A premium seller who buys back at twice the credit never loses the width of the spread,
so dividing by max loss makes every result look tiny. R here is what the stop actually
risks: `(STOP_MULTIPLE - 1) x credit`. With the stop at 2x, 1R is the credit received and
a trade stopped exactly on its rule is -1R. A debit trade risks what it paid.

## Times

The Robinhood CSV carries dates only. Entry and exit times come from `fill_time` rows
(the order history pulled from Robinhood's API) or from what the trader typed, and every
time-of-day breakdown stays empty until they exist. A time is never estimated.

## The unit of independence

Positions in one underlying closed on one day share one market move, so breakdowns
here cluster on (symbol, closing day), the same grain the journal's headline uses.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from optscan.analytics.calibration import count_clusters, proportion_interval
from optscan.analytics.journal import Breakdown, Estimate, expectancy
from optscan.analytics.ledger import JournalEntry, Trade
from optscan.models.broker import OPTION_MULTIPLIER, BrokerTxn, Effect, TxnKind
from optscan.models.enums import Right
from optscan.models.opportunity import Action

MARKET_TZ = ZoneInfo("America/New_York")

#: The trader's stop: buy back when the position costs this multiple of the credit.
STOP_MULTIPLE = 2.0

#: A close above the stop by more than this share is flagged "held past stop". Some
#: slippage through a stop is normal on a fast 0DTE move; this is past slippage.
STOP_TOLERANCE = 0.10

#: Risk at entry above this multiple of the trader's median risk share is flagged.
SIZE_FLAG_MULTIPLE = 2.0

#: Smallest 1R, in dollars, that a result is measured against. Below it the ratio is
#: about the denominator. Ten dollars is a dime a share on one contract.
MIN_RISK_UNIT = 10.0

#: The trader's own clock. Everything a person types or reads here is on it, and so is
#: the 12:44 rule: measured on 27 timed 0DTE exits (2026-09-12), they cluster at 15:40 to
#: 15:44 Eastern, which is 12:40 to 12:44 Pacific, sixteen minutes before the close. The
#: first version read the rule as Eastern and called nearly every rule-following close
#: "after 12:44".
TRADER_TZ = ZoneInfo("America/Los_Angeles")

#: The trader's time rule: manual closes by 12:44, on the trader's clock. See TRADER_TZ.
EXIT_RULE_TIME = time(12, 44)
EXIT_BY = f"closed by {EXIT_RULE_TIME:%H:%M} PT"
EXIT_AFTER = f"closed after {EXIT_RULE_TIME:%H:%M} PT"

#: Tags a trader can put on a position. Free text is allowed too; these are offered.
MISTAKE_TAGS: tuple[str, ...] = (
    "held past stop",
    "sized too big",
    "no plan",
    "chased entry",
    "revenge trade",
    "exited early",
    "broke time rule",
    "fat finger",
)

DTE_CLASSES: tuple[tuple[str, int, int], ...] = (
    ("0DTE", 0, 0),
    ("1-7 DTE", 1, 7),
    ("8+ DTE", 8, 100_000),
)

#: Entry time of day on the trader's clock, Pacific. Half-open intervals [start, end).
#: The last runs to 13:15 because SPY and QQQ options trade until 4:15 Eastern.
ENTRY_BUCKETS: tuple[tuple[str, time, time], ...] = (
    ("open 6:30-7:30 PT", time(6, 30), time(7, 30)),
    ("morning 7:30-9:00 PT", time(7, 30), time(9, 0)),
    ("midday 9:00-11:00 PT", time(9, 0), time(11, 0)),
    ("afternoon 11:00-12:00 PT", time(11, 0), time(12, 0)),
    ("last hour 12:00-13:15 PT", time(12, 0), time(13, 16)),
)

VIX_BANDS: tuple[tuple[str, float, float], ...] = (
    ("VIX under 15", 0.0, 15.0),
    ("VIX 15-20", 15.0, 20.0),
    ("VIX 20-30", 20.0, 30.0),
    ("VIX 30+", 30.0, 1_000.0),
)

#: Twenty sessions of SPY moving less than this either way is "flat".
TREND_LOOKBACK = 20
TREND_FLAT = 0.02

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

_EVENTS = frozenset({TxnKind.OPTION_EXPIRATION, TxnKind.OPTION_ASSIGNMENT, TxnKind.OPTION_EXERCISE})


# --------------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Leg:
    """One contract within a position, possibly traded more than once that day."""

    right: str | None
    strike: float | None
    side: str
    size: float
    open_price: float | None
    close_price: float | None
    cash: float
    #: Ended by expiration, assignment or exercise rather than a closing trade.
    ended_by_event: bool
    opening: tuple[BrokerTxn, ...]
    closing: tuple[BrokerTxn, ...]


@dataclass(frozen=True, slots=True)
class Annotation:
    """What the trader added to a position. Nothing here comes from the broker."""

    strategy: str | None = None
    notes: str | None = None
    tags: tuple[str, ...] = ()
    #: Per-unit price the trader meant to exit at, for the slippage comparison.
    planned_exit: float | None = None
    #: "HH:MM" on the trader's clock (Pacific). Overrides times from the order history.
    entry_time: str | None = None
    exit_time: str | None = None


@dataclass
class Position:
    key: str
    symbol: str
    #: The contracts' expiry. None for a stock position.
    contract_expiry: date | None
    opened_at: date
    closed_at: date | None
    is_open: bool
    legs: list[Leg]
    trades: list[Trade]
    opening_cash: float
    closing_cash: float
    fees: float
    units: float
    max_loss: float | None
    guess: str
    confident: bool
    annotation: Annotation = field(default_factory=Annotation)
    screenshots: list[int] = field(default_factory=list)
    entry_at: datetime | None = None
    exit_at: datetime | None = None
    time_source: str | None = None
    flags: list[str] = field(default_factory=list)
    vix: float | None = None
    trend: str | None = None
    account: float | None = None
    #: The result had the trade risked no more than the account's median 1R. Set only
    #: in the journal's capped view, and only on trades above it. See
    #: `cap_to_median_risk`.
    scaled: float | None = None

    # -- derived ------------------------------------------------------------------

    @property
    def is_option(self) -> bool:
        return self.contract_expiry is not None

    @property
    def strategy(self) -> str:
        return self.annotation.strategy or self.guess

    @property
    def realized(self) -> float | None:
        return None if self.is_open else round(self.opening_cash + self.closing_cash, 2)

    @property
    def outcome(self) -> float:
        """What the statistics count: the capped result in the capped view, else the
        real one. The trade table always shows `realized`."""
        if self.scaled is not None:
            return self.scaled
        return self.realized or 0.0

    @property
    def dte_at_entry(self) -> int | None:
        if self.contract_expiry is None:
            return None
        return (self.contract_expiry - self.opened_at).days

    @property
    def dte_class(self) -> str:
        dte = self.dte_at_entry
        if dte is None:
            return "stock"
        for label, low, high in DTE_CLASSES:
            if low <= dte <= high:
                return label
        return DTE_CLASSES[-1][0]

    @property
    def held_days(self) -> int | None:
        return None if self.closed_at is None else (self.closed_at - self.opened_at).days

    @property
    def multiplier(self) -> int:
        return OPTION_MULTIPLIER if self.is_option else 1

    @property
    def entry_price(self) -> float | None:
        """Net per unit, positive for a credit received."""
        if self.units <= 0:
            return None
        return self.opening_cash / (self.units * self.multiplier)

    @property
    def exit_price(self) -> float | None:
        """Net per unit to close, positive for a debit paid."""
        if self.is_open or self.units <= 0:
            return None
        return -self.closing_cash / (self.units * self.multiplier)

    @property
    def is_credit(self) -> bool:
        return self.is_option and self.opening_cash > 0

    @property
    def risk_unit(self) -> float | None:
        """1R in dollars. See the module docstring.

        Never more than the most the position could lose, and never less than
        `MIN_RISK_UNIT`. On the real ledger a put spread with a long call took in three
        cents and made $17.70, which read as +6.19R: a statement about the denominator
        rather than the trade. Below the floor R is left blank, not forced.
        """
        if self.is_credit:
            risk = (STOP_MULTIPLE - 1.0) * self.opening_cash
        elif self.opening_cash < 0:
            risk = -self.opening_cash
        else:
            return None
        if self.max_loss is not None:
            risk = min(risk, self.max_loss)
        return risk if risk >= MIN_RISK_UNIT else None

    @property
    def r_multiple(self) -> float | None:
        risk = self.risk_unit
        realized = self.realized
        if risk is None or risk <= 0 or realized is None:
            return None
        return realized / risk

    @property
    def close_multiple(self) -> float | None:
        """For a credit trade closed for a debit, what it cost as a multiple of credit."""
        if not self.is_credit or self.is_open or self.closing_cash >= 0:
            return None
        return -self.closing_cash / self.opening_cash

    @property
    def exit_slippage(self) -> float | None:
        """Actual exit against the planned one, per unit. Positive is worse than planned."""
        planned = self.annotation.planned_exit
        actual = self.exit_price
        if planned is None or actual is None:
            return None
        return actual - planned if self.is_credit else planned - actual

    @property
    def weekday(self) -> str:
        return WEEKDAYS[self.opened_at.weekday()]

    @property
    def expired(self) -> bool:
        """Every option leg ran to expiration rather than being closed.

        At least one leg is required: `all()` of nothing is true, which would call a
        position with no legs "held to expiry" and skip the exit-time test entirely.
        """
        return (
            self.is_option
            and not self.is_open
            and bool(self.legs)
            and all(leg.ended_by_event for leg in self.legs)
        )

    @property
    def entry_bucket(self) -> str | None:
        if self.entry_at is None:
            return None
        moment = self.entry_at.astimezone(TRADER_TZ).time()
        for label, start, end in ENTRY_BUCKETS:
            if start <= moment < end:
                return label
        return "outside regular hours"

    @property
    def exit_bucket(self) -> str | None:
        if self.expired:
            return "held to expiry"
        if self.exit_at is None or self.is_open:
            return None
        # The session is the market's date; the rule is on the trader's clock.
        if (
            self.contract_expiry is not None
            and self.exit_at.astimezone(MARKET_TZ).date() != self.contract_expiry
        ):
            return "closed before expiry day"
        local = self.exit_at.astimezone(TRADER_TZ)
        return EXIT_BY if local.time() <= EXIT_RULE_TIME else EXIT_AFTER

    @property
    def vix_band(self) -> str | None:
        if self.vix is None:
            return None
        for label, low, high in VIX_BANDS:
            if low <= self.vix < high:
                return label
        return None


# --------------------------------------------------------------------------------
# Building positions
# --------------------------------------------------------------------------------


def position_key(symbol: str, expiry: date | None, opened: date) -> str:
    """Stable across re-imports, so annotations survive a new statement."""
    return f"{symbol}|{expiry.isoformat() if expiry else '-'}|{opened.isoformat()}"


def _right_name(right: Right | None) -> str | None:
    if right is None:
        return None
    return "call" if right is Right.CALL else "put"


def _is_opening(txn: BrokerTxn, first_sign: float) -> bool:
    if txn.kind in _EVENTS:
        return False
    if txn.kind is TxnKind.OPTION_TRADE and txn.effect is not None:
        return txn.effect is Effect.OPEN
    signed = txn.signed_quantity
    return signed is not None and (signed > 0) == (first_sign > 0)


def _average_price(txns: Iterable[BrokerTxn]) -> float | None:
    weighted = total = 0.0
    for txn in txns:
        if txn.price is None or not txn.quantity:
            continue
        weighted += txn.price * txn.quantity
        total += txn.quantity
    return weighted / total if total else None


def _leg(trades: Sequence[Trade]) -> Leg:
    """One contract's round trips within a position, merged."""
    txns = [txn for trade in trades for txn in trade.legs]
    first = next((t.signed_quantity for t in txns if t.signed_quantity), 1.0) or 1.0
    opening = tuple(t for t in txns if _is_opening(t, first))
    closing = tuple(t for t in txns if t not in opening)
    first_open = opening[0] if opening else txns[0]
    side = "short" if first_open.action is Action.SELL else "long"
    closed_by_trade = [t for t in closing if t.kind not in _EVENTS]
    ended = any(trade.ended_by_event for trade in trades)
    is_open = any(trade.open_at_end for trade in trades)

    close_price = _average_price(closed_by_trade)
    if close_price is None and ended and not is_open:
        close_price = 0.0

    key = trades[0].key
    return Leg(
        right=_right_name(key.right),
        strike=key.strike,
        side=side,
        size=max(trade.size for trade in trades),
        open_price=_average_price(opening),
        close_price=close_price,
        cash=round(sum(trade.cash for trade in trades), 2),
        ended_by_event=ended and not closed_by_trade,
        opening=opening,
        closing=closing,
    )


def risk_at_entry(legs: Sequence[Leg], opening_cash: float) -> float | None:
    """Worst loss if every leg is held to expiry. None when it is unbounded.

    The payoff of a set of options at expiry is piecewise linear with kinks only at the
    strikes, so its minimum is at zero, at a strike, or off to infinity. Net short calls
    lose without limit as the price rises, which is the one case reported as None.
    """
    options = [leg for leg in legs if leg.right is not None]
    if not options:
        return -opening_cash if opening_cash < 0 else None

    def sign(leg: Leg) -> int:
        return 1 if leg.side == "long" else -1

    if sum(sign(leg) * leg.size for leg in options if leg.right == "call") < 0:
        return None

    def pnl(spot: float) -> float:
        total = opening_cash
        for leg in options:
            strike = leg.strike or 0.0
            intrinsic = max(0.0, spot - strike) if leg.right == "call" else max(0.0, strike - spot)
            total += sign(leg) * leg.size * OPTION_MULTIPLIER * intrinsic
        return total

    points = [0.0, *sorted({leg.strike or 0.0 for leg in options})]
    return max(0.0, -min(pnl(point) for point in points))


def guess_strategy(legs: Sequence[Leg], opening_cash: float) -> tuple[str, bool]:
    """A name for the shape of the legs, and whether the name is a confident one."""
    options = [leg for leg in legs if leg.right is not None]
    if not options:
        return "stock", True
    if len(options) == 1:
        leg = options[0]
        return f"{leg.side} {leg.right}", True

    shorts = [leg for leg in options if leg.side == "short"]
    longs = [leg for leg in options if leg.side == "long"]
    calls = [leg for leg in options if leg.right == "call"]
    puts = [leg for leg in options if leg.right == "put"]
    same_size = len({leg.size for leg in options}) == 1
    pair = 2
    condor = 4

    if len(options) == pair and len({leg.right for leg in options}) == 1 and len(shorts) == 1:
        kind = "credit" if opening_cash > 0 else "debit"
        return f"{options[0].right} {kind} spread", same_size
    if len(options) == pair and len(calls) == 1 and len(puts) == 1:
        side = "short" if len(shorts) == pair else "long" if len(longs) == pair else None
        if side is not None:
            name = "straddle" if calls[0].strike == puts[0].strike else "strangle"
            return f"{side} {name}", same_size
    if len(options) == condor and len(calls) == pair and len(puts) == pair:
        short_call = [leg for leg in calls if leg.side == "short"]
        short_put = [leg for leg in puts if leg.side == "short"]
        if len(short_call) == 1 and len(short_put) == 1:
            if short_call[0].strike == short_put[0].strike:
                return "iron butterfly", same_size
            return "iron condor", same_size
    return "multi-leg", False


def build_positions(trades: Sequence[Trade]) -> list[Position]:
    """Group contract round trips into positions: one underlying, one expiry, one day."""
    groups: dict[tuple[str, date | None, date], list[Trade]] = defaultdict(list)
    for trade in trades:
        groups[(trade.key.symbol, trade.key.expiry, trade.opened_at)].append(trade)

    positions = []
    for (symbol, expiry, opened), group in groups.items():
        by_contract: dict[object, list[Trade]] = defaultdict(list)
        for trade in group:
            by_contract[trade.key].append(trade)
        legs = sorted(
            (_leg(items) for items in by_contract.values()),
            key=lambda leg: (leg.right or "", leg.strike or 0.0, leg.side),
        )
        opening_cash = round(sum(t.amount or 0.0 for leg in legs for t in leg.opening), 2)
        closing_cash = round(sum(t.amount or 0.0 for leg in legs for t in leg.closing), 2)
        is_open = any(trade.open_at_end for trade in group)
        closes = [trade.closed_at for trade in group if trade.closed_at is not None]
        units = min((leg.size for leg in legs), default=0.0) if expiry else legs[0].size
        guess, confident = guess_strategy(legs, opening_cash)
        positions.append(
            Position(
                key=position_key(symbol, expiry, opened),
                symbol=symbol,
                contract_expiry=expiry,
                opened_at=opened,
                closed_at=None if is_open else max(closes, default=None),
                is_open=is_open,
                legs=legs,
                trades=list(group),
                opening_cash=opening_cash,
                closing_cash=closing_cash,
                fees=round(sum(trade.fees for trade in group), 2),
                units=units,
                # Only for a shape the journal can name. A position scaled into over the
                # day holds each leg at its own peak at a different moment, and treating
                # those peaks as one simultaneous position nets them into a naked short:
                # on the real ledger three such positions came out at $70,000 to $76,000
                # of "risk" against a median of $151. None rather than that number.
                max_loss=risk_at_entry(legs, opening_cash) if confident else None,
                guess=guess,
                confident=confident,
            )
        )
    positions.sort(key=lambda p: (p.opened_at, p.symbol, p.key))
    return positions


def _clock(day: date, text: str | None) -> datetime | None:
    """A typed "HH:MM" on the trader's clock on a given day, or None if it does not parse."""
    if not text:
        return None
    try:
        hours, minutes = (int(part) for part in text.strip().split(":")[:2])
        return datetime.combine(day, time(hours, minutes), tzinfo=TRADER_TZ)
    except (ValueError, TypeError):
        return None


def attach(
    positions: Sequence[Position],
    *,
    annotations: dict[str, Annotation] | None = None,
    screenshots: dict[str, list[int]] | None = None,
    fill_times: dict[tuple[str, int], datetime] | None = None,
    vix: dict[date, float] | None = None,
    trend: dict[date, str] | None = None,
) -> None:
    """Hang everything the broker did not supply onto the positions, in place.

    Typed times win over the order history, because a trader who corrected a time did
    so on purpose. Exit time is the last closing fill: a position is closed when its
    final leg is.
    """
    annotations = annotations or {}
    fill_times = fill_times or {}
    for position in positions:
        position.annotation = annotations.get(position.key, Annotation())
        position.screenshots = list((screenshots or {}).get(position.key, []))
        position.vix = (vix or {}).get(position.opened_at)
        position.trend = (trend or {}).get(position.opened_at)

        opened = [
            fill_times[(t.digest, t.dup_index)]
            for leg in position.legs
            for t in leg.opening
            if (t.digest, t.dup_index) in fill_times
        ]
        closed = [
            fill_times[(t.digest, t.dup_index)]
            for leg in position.legs
            for t in leg.closing
            if t.kind not in _EVENTS and (t.digest, t.dup_index) in fill_times
        ]
        position.entry_at = min(opened, default=None)
        position.exit_at = max(closed, default=None)
        position.time_source = "order history" if opened or closed else None

        typed_entry = _clock(position.opened_at, position.annotation.entry_time)
        typed_exit = _clock(position.closed_at or position.opened_at, position.annotation.exit_time)
        if typed_entry or typed_exit:
            position.entry_at = typed_entry or position.entry_at
            position.exit_at = typed_exit or position.exit_at
            position.time_source = "typed"


def trend_labels(closes: Sequence[tuple[date, float]]) -> dict[date, str]:
    """SPY's twenty session move for each day: up, down, or flat within two percent."""
    ordered = sorted(closes)
    labels: dict[date, str] = {}
    for index in range(TREND_LOOKBACK, len(ordered)):
        day, close = ordered[index]
        before = ordered[index - TREND_LOOKBACK][1]
        if before <= 0:
            continue
        move = close / before - 1.0
        labels[day] = (
            "SPY flat" if abs(move) < TREND_FLAT else "SPY uptrend" if move > 0 else "SPY downtrend"
        )
    return labels


# --------------------------------------------------------------------------------
# The book: everything reported over positions
# --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StatRow:
    """A closed position shaped for the shared cluster statistics.

    `expiry` holds the closing day because that is the key `calibration.cluster_key`
    reads, and (symbol, closing day) is this journal's unit of independence.
    """

    symbol: str
    expiry: date
    profit: float


@dataclass(frozen=True, slots=True)
class WeekdayRow:
    breakdown: Breakdown
    avg_credit: float | None


@dataclass(frozen=True, slots=True)
class Streaks:
    #: Positive for a run of wins, negative for a run of losses.
    current: int
    longest_win: int
    longest_loss: int


@dataclass(frozen=True, slots=True)
class StopReport:
    planned_multiple: float
    stopped: int
    mean_multiple: float | None
    worst_multiple: float | None
    beyond_stop: int
    planned_exits: int
    mean_slippage: float | None


@dataclass(frozen=True, slots=True)
class SizingPoint:
    key: str
    day: date
    risk: float
    #: None until the balance before the first statement is known. See `sizing`.
    account: float | None
    share: float | None


@dataclass(frozen=True, slots=True)
class Sizing:
    points: list[SizingPoint]
    #: "share" of the account once a starting balance is set, "dollars" of risk until
    #: then. The three figures below are in that unit.
    basis: str
    median_risk: float | None
    after_win_risk: float | None
    after_win_n: int
    after_loss_risk: float | None
    after_loss_n: int
    starting_balance: float | None
    net_deposits: float


@dataclass
class Book:
    positions: list[Position]
    by_strategy: list[Breakdown]
    by_dte_class: list[Breakdown]
    by_weekday: list[WeekdayRow]
    by_tag: list[Breakdown]
    by_vix: list[Breakdown]
    by_trend: list[Breakdown]
    by_entry_time: list[Breakdown]
    by_exit_time: list[Breakdown]
    r_expectancy: Estimate | None
    day_streaks: Streaks
    position_streaks: Streaks
    stops: StopReport
    sizing: Sizing
    timed: int
    notes: list[str]
    #: Plain-English findings per panel. Filled by `analytics.insights`, which needs the
    #: headline report as well as the book, so it is set after the book is built.
    insights: dict[str, list[str]] = field(default_factory=dict)


def _stat(position: Position) -> StatRow:
    return StatRow(position.symbol, position.closed_at or position.opened_at, position.outcome)


def _breakdown(key: str, rows: Sequence[StatRow]) -> Breakdown:
    profits = [row.profit for row in rows]
    return Breakdown(
        key=key,
        trades=len(rows),
        clusters=count_clusters(rows),
        win_rate=proportion_interval(rows, lambda row: row.profit > 0),
        total_profit=sum(profits),
        mean_profit=statistics.fmean(profits) if profits else 0.0,
    )


def group(
    positions: Sequence[Position],
    label: Callable[[Position], str | None],
    order: Sequence[str] | None = None,
) -> list[Breakdown]:
    """Breakdown by a label. Unlabelled positions are left out rather than lumped."""
    buckets: dict[str, list[StatRow]] = defaultdict(list)
    for position in positions:
        name = label(position)
        if name is not None:
            buckets[name].append(_stat(position))
    rows = [_breakdown(name, items) for name, items in buckets.items()]
    if order is not None:
        rank = {name: index for index, name in enumerate(order)}
        return sorted(rows, key=lambda row: rank.get(row.key, len(rank)))
    return sorted(rows, key=lambda row: -row.total_profit)


def by_tag(positions: Sequence[Position]) -> list[Breakdown]:
    """One row per tag, plus the untagged. A position with two tags counts in both."""
    buckets: dict[str, list[StatRow]] = defaultdict(list)
    for position in positions:
        tags = position.annotation.tags or ("untagged",)
        for tag in tags:
            buckets[tag].append(_stat(position))
    return sorted(
        (_breakdown(name, rows) for name, rows in buckets.items()),
        key=lambda row: row.total_profit,
    )


def by_weekday(positions: Sequence[Position]) -> list[WeekdayRow]:
    """Day of the week the position was opened, split 0DTE from everything longer."""
    buckets: dict[str, list[Position]] = defaultdict(list)
    for position in positions:
        if not position.is_option:
            continue
        split = "0DTE" if position.dte_at_entry == 0 else "1+ DTE"
        buckets[f"{position.weekday} {split}"].append(position)

    order = [f"{day} {split}" for day in WEEKDAYS for split in ("0DTE", "1+ DTE")]
    rows = []
    for name in order:
        items = buckets.get(name)
        if not items:
            continue
        credits = [p.entry_price for p in items if p.is_credit and p.entry_price is not None]
        rows.append(
            WeekdayRow(
                breakdown=_breakdown(name, [_stat(p) for p in items]),
                avg_credit=statistics.fmean(credits) if credits else None,
            )
        )
    return rows


def streaks(outcomes: Sequence[float]) -> Streaks:
    """Runs of wins and losses in order. A flat result breaks a run without starting one."""
    longest_win = longest_loss = run = 0
    for value in outcomes:
        if value > 0:
            run = run + 1 if run > 0 else 1
        elif value < 0:
            run = run - 1 if run < 0 else -1
        else:
            run = 0
        longest_win = max(longest_win, run)
        longest_loss = max(longest_loss, -run)
    return Streaks(current=run, longest_win=longest_win, longest_loss=longest_loss)


def stop_report(positions: Sequence[Position]) -> StopReport:
    """How close stop-outs came to the rule, and how exits compared with the plan."""
    stopped = [p for p in positions if p.close_multiple is not None and (p.realized or 0.0) < 0]
    multiples = [p.close_multiple for p in stopped if p.close_multiple is not None]
    limit = STOP_MULTIPLE * (1.0 + STOP_TOLERANCE)
    slips = [p.exit_slippage for p in positions if p.exit_slippage is not None]
    return StopReport(
        planned_multiple=STOP_MULTIPLE,
        stopped=len(stopped),
        mean_multiple=statistics.fmean(multiples) if multiples else None,
        worst_multiple=max(multiples) if multiples else None,
        beyond_stop=sum(1 for value in multiples if value > limit),
        planned_exits=len(slips),
        mean_slippage=statistics.fmean(slips) if slips else None,
    )


def sizing(
    positions: Sequence[Position],
    everything: Sequence[Position],
    cash_flows: Sequence[tuple[date, float]],
    starting_balance: float | None,
) -> Sizing:
    """Risk per trade, measured as 1R, and whether it creeps after wins or losses.

    1R rather than risk at entry. 1R is what the trader's stop puts at risk, bounded by
    the credit (or the debit paid), so it cannot be inflated by how the legs of a
    scaled-into position happened to overlap. Risk at entry could, and did: three
    positions at $70,000 to $76,000 dragged "after a losing day" to $14,499 while the
    median trade risked $151.

    The account is the starting balance, plus deposits, interest and fees in the
    imported statements up to that day, plus everything realized before it.

    **Without a starting balance there is no honest denominator.** The statements begin
    part way through the account's life, so "the account" is only what they show, and
    early on that is almost nothing: measured on the real ledger (2026-09-12) the first
    July trades came out at 1,560 percent of an account one deposit old, and twelve
    trades were flagged for size that was an artefact of the division. So until the
    balance is set, sizing is compared in dollars of risk, which answers the same
    question -- does size creep? -- without inventing an account.

    Only option positions are compared and flagged. A share position risks its whole
    cost, so beside a spread's defined risk it always looks enormous and a flag on it
    would say nothing. Stock is still plotted.

    "After a win" means the previous session with a closed result was a winning one.
    """
    use_share = starting_balance is not None
    closed = sorted(
        (p for p in everything if not p.is_open and p.closed_at is not None),
        key=lambda p: p.closed_at or date.min,
    )
    by_day: dict[date, float] = defaultdict(float)
    for p in closed:
        by_day[p.closed_at or p.opened_at] += p.realized or 0.0
    days = sorted(by_day)

    points = []
    measured: dict[str, float] = {}
    after_win: list[float] = []
    after_loss: list[float] = []
    for p in positions:
        risk = p.risk_unit
        if risk is None:
            continue
        account = share = None
        if use_share:
            account = starting_balance or 0.0
            account += sum(amount for day, amount in cash_flows if day <= p.opened_at)
            account += sum(value for day, value in by_day.items() if day < p.opened_at)
            share = risk / account if account > 0 else None
        p.account = account
        points.append(SizingPoint(p.key, p.opened_at, risk, account, share))

        value = share if use_share else risk
        if value is None or not p.is_option:
            continue
        measured[p.key] = value
        before = [day for day in days if day < p.opened_at]
        if before:
            (after_win if by_day[before[-1]] > 0 else after_loss).append(value)

    median = statistics.median(measured.values()) if measured else None
    if median:
        for p in positions:
            value = measured.get(p.key)
            if value is not None and value > SIZE_FLAG_MULTIPLE * median:
                p.flags.append("sized too big")

    return Sizing(
        points=points,
        basis="share" if use_share else "dollars",
        median_risk=median,
        after_win_risk=statistics.fmean(after_win) if after_win else None,
        after_win_n=len(after_win),
        after_loss_risk=statistics.fmean(after_loss) if after_loss else None,
        after_loss_n=len(after_loss),
        starting_balance=starting_balance,
        net_deposits=round(sum(amount for _, amount in cash_flows), 2),
    )


def build_book(
    positions: Sequence[Position],
    everything: Sequence[Position],
    *,
    cash_flows: Sequence[tuple[date, float]] = (),
    starting_balance: float | None = None,
    day_profits: Sequence[float] = (),
) -> Book:
    """Every position-level report. `positions` is the filtered set; `everything` is
    the whole account, because the account balance does not care about a filter."""
    for p in positions:
        p.flags = []
        multiple = p.close_multiple
        if multiple is not None and multiple > STOP_MULTIPLE * (1.0 + STOP_TOLERANCE):
            p.flags.append("held past stop")

    closed = [p for p in positions if not p.is_open]
    size = sizing(positions, everything, cash_flows, starting_balance)

    r_rows = [
        StatRow(p.symbol, p.closed_at or p.opened_at, p.r_multiple)
        for p in closed
        if p.r_multiple is not None
    ]
    earliest = datetime.min.replace(tzinfo=MARKET_TZ)
    ordered = sorted(
        closed,
        key=lambda p: (p.closed_at or date.min, p.exit_at or earliest, p.key),
    )
    timed = sum(1 for p in positions if p.entry_at is not None)

    notes = []
    if timed == 0:
        notes.append(
            "No position has an entry time yet, so the time-of-day breakdowns are empty. "
            "The Robinhood CSV has dates only; times come from the order history import "
            "or from typing them into a position."
        )
    elif timed < len(positions):
        notes.append(
            f"{timed} of {len(positions)} positions have entry times. Time-of-day "
            "breakdowns cover only those."
        )
    if starting_balance is None:
        notes.append(
            "Account size is built from deposits in the imported statements plus realized "
            "results. Set the balance before the first statement for exact sizing."
        )
    uncertain = sum(1 for p in positions if not p.confident and p.annotation.strategy is None)
    if uncertain:
        notes.append(
            f"{uncertain} positions have a shape the journal cannot name with confidence "
            "and are labelled multi-leg until tagged."
        )

    return Book(
        positions=list(positions),
        by_strategy=group(closed, lambda p: p.strategy),
        by_dte_class=group(closed, lambda p: p.dte_class, [c[0] for c in DTE_CLASSES] + ["stock"]),
        by_weekday=by_weekday(closed),
        by_tag=by_tag(closed),
        by_vix=group(closed, lambda p: p.vix_band, [band[0] for band in VIX_BANDS]),
        by_trend=group(closed, lambda p: p.trend, ["SPY uptrend", "SPY flat", "SPY downtrend"]),
        by_entry_time=group(
            closed,
            lambda p: p.entry_bucket,
            [bucket[0] for bucket in ENTRY_BUCKETS] + ["outside regular hours"],
        ),
        by_exit_time=group(
            closed,
            lambda p: p.exit_bucket,
            [EXIT_BY, EXIT_AFTER, "closed before expiry day", "held to expiry"],
        ),
        r_expectancy=expectancy(r_rows),
        day_streaks=streaks(day_profits),
        position_streaks=streaks([p.outcome for p in ordered]),
        stops=stop_report(closed),
        sizing=size,
        timed=timed,
        notes=notes,
    )


def cap_to_median_risk(
    positions: Sequence[Position], everything: Sequence[Position]
) -> tuple[float | None, int]:
    """Scale every trade that risked more than the account's median 1R down to it.

    The question is "what did oversizing cost", and it has to be answered without
    inventing trades that were never taken. The first version scaled every trade to the
    median, up as well as down, and on 2026-09-12 that turned +$154 into +$517. Split
    apart, +$184 of the jump was oversized losers shrinking to normal size, which is the
    answer, and +$218 was trades that risked $13 to $25 being blown up three to six
    times, which is fiction. Capping keeps the first and drops the second: a trade at or
    under the median keeps its real result, and a larger one keeps its R at the median's
    size. The 8/24 loss, -0.97R on a $248 risk, becomes -0.97R on $75.

    Trades too small to carry an R are under the median by construction and keep their
    real result, so nothing is left out. Returns the median 1R and how many trades were
    scaled down.
    """
    units = [p.risk_unit for p in everything if not p.is_open and p.risk_unit]
    if not units:
        return None, 0
    cap = statistics.median(units)
    capped = 0
    for p in positions:
        risk = p.risk_unit
        r = p.r_multiple
        if not p.is_open and risk is not None and r is not None and risk > cap:
            p.scaled = round(r * cap, 2)
            capped += 1
        else:
            p.scaled = None
    return cap, capped


def entries_from_positions(positions: Sequence[Position]) -> list[JournalEntry]:
    """Closed positions folded into (symbol, closing day) entries, counting `outcome`.

    The same grain as `ledger.journal_entries`, which builds from fills and stays the
    path for real dollars because it is the audited one. This is the path for a view in
    which the dollars are scaled, where fills no longer carry the number being counted.
    """
    buckets: dict[tuple[str, date], list[Position]] = defaultdict(list)
    for p in positions:
        if not p.is_open and p.closed_at is not None:
            buckets[(p.symbol, p.closed_at)].append(p)

    entries = []
    for (symbol, day), group in buckets.items():
        options = sum(1 for p in group if p.is_option)
        instrument = "options" if options == len(group) else "equities" if options == 0 else "mixed"
        entries.append(
            JournalEntry(
                symbol=symbol,
                expiry=day,
                profit=round(sum(p.outcome for p in group), 2),
                strategy=instrument,
                dte=max((p.held_days or 0) for p in group),
                contracts=sum(len(p.legs) for p in group),
                fees=round(sum(p.fees for p in group), 2),
            )
        )
    entries.sort(key=lambda e: (e.expiry, e.symbol))
    return entries


def filter_positions(
    positions: Sequence[Position],
    *,
    symbol: str | None = None,
    strategy: str | None = None,
    tag: str | None = None,
    dte_class: str | None = None,
) -> list[Position]:
    out = list(positions)
    if symbol:
        out = [p for p in out if p.symbol == symbol.strip().upper()]
    if strategy:
        out = [p for p in out if p.strategy == strategy]
    if tag:
        out = [
            p
            for p in out
            if tag in p.annotation.tags or (tag == "untagged" and not p.annotation.tags)
        ]
    if dte_class:
        out = [p for p in out if p.dte_class == dte_class]
    return out


def cash_flows(txns: Iterable[BrokerTxn]) -> list[tuple[date, float]]:
    """Deposits, withdrawals, interest and fees: money in and out that is not trading."""
    return [
        (txn.activity_date, txn.amount) for txn in txns if txn.kind is TxnKind.CASH and txn.amount
    ]


def to_trader(moment: datetime) -> datetime:
    """A moment on the trader's clock, which is how every time here is shown."""
    return moment.astimezone(TRADER_TZ)
