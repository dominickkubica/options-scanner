"""The short premium study's engine: marks, entries, management, and what a trade made.

Pure. It takes one session of stored option trades as plain frames and returns
positions; the fetching lives in `jobs/option_tape.py`, the orchestration in
`jobs/premium.py`, and the statistics in `analytics/premium_stats.py`.

## Marks come from a smile, not from last prints

The only history is trades (see `providers/alpaca.py`): a contract that did not trade
in a minute has no row, and a row's price is whichever side lifted. Marking each leg
at its own last print would mark the wings, which print least, hours stale. So every
fresh print becomes an implied volatility, each minute's out of the money prints make
that minute's smile across strikes, and every leg is priced off the smile at the
minute's spot. A thin strike is marked by its neighbours; a one lot on the wrong side
moves one point of a curve rather than the whole position. Where a minute has too few
prints the last smile carries forward for at most `STALE_MINUTES`, and past that the
minute has no mark and no rule is checked in it.

## Fills are a modelled spread around the mark

There is no quote to fill against. An entry receives `mid - k x half spread` per leg
and an exit pays `mid + k x half spread`, the half spread coming from a `SpreadModel`
measured on this project's own captured quotes. Every result is produced at several
k, and one that only survives at k=0 does not survive.

## What an exit fills at

Every exit, target or stop, fills at the **next** minute's mark, never the minute that
triggered it. For a stop that is the slippage the study exists to measure: a stop that
always fills exactly at its trigger is the fiction that makes short premium look safe.
For a target it is a correction measured on the data. A mark moves a few cents on
noise, so the minute a target triggers is disproportionately a minute the mark dipped,
and filling there added $2.50 to $6.60 to every condor, the whole of every target's
apparent edge (2026-09-14).

## Units

Prices are per share. A trade's `pnl` is dollars for one lot of the structure, after
the per contract fees measured from the account's own fills; `r` is `pnl` over the
credit received in dollars, so a stop at 1x credit loses about 1R.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from optscan.analytics.greeks import EXPIRY_HOUR_LOCAL, EXPIRY_TIMEZONE, SECONDS_PER_YEAR
from optscan.analytics.vector_bsm import bsm_delta, bsm_price, implied_vol

#: A print informs a minute's smile for this many minutes.
FRESH_MINUTES = 5

#: A smile is carried forward at most this long; past it a minute has no mark.
STALE_MINUTES = 10

#: Strikes a smile needs before it is interpolated rather than refused.
MIN_SMILE_POINTS = 3

#: How far past its outermost print a smile is extended, flat, in dollars of strike.
EXTRAPOLATE_DOLLARS = 3.0

#: Within this fraction of spot both rights inform the smile; outside it only the out
#: of the money one, because an in the money print carries the spread in its intrinsic.
ATM_BAND = 0.0025

#: Per contract fees, measured 2026-09-13 as the median over 414 of the account's own
#: option fills: 4 cents a contract to buy and 6 to sell. The regulatory part scales
#: with sells only, which is why they differ.
BUY_FEE = 0.04
SELL_FEE = 0.06

#: Shares per contract.
MULTIPLIER = 100

#: An intraday position is flattened this many minutes before the close unless the
#: rule holds it to expiry.
FLATTEN_BEFORE_CLOSE = 5

_EXPIRY_TZ = ZoneInfo(EXPIRY_TIMEZONE)
_EPOCH = pd.Timestamp("1970-01-01", tz="UTC")


class Structure(StrEnum):
    PUT_SPREAD = "put_spread"
    CALL_SPREAD = "call_spread"
    IRON_CONDOR = "iron_condor"


class ExitReason(StrEnum):
    TARGET = "target"
    STOP = "stop"
    TOUCH = "touch"
    DELTA = "delta"
    FLATTEN = "flatten"
    EXPIRY = "expiry"


# --------------------------------------------------------------------------------
# The spread model
# --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpreadModel:
    """Half the quoted spread per leg, by days to expiry and |delta|.

    `edges` are the upper |delta| bound of each bucket. The defaults are the medians
    measured 2026-09-13 on 13 sessions of this project's own 15:45 QQQ and SPY
    captures: out of the money 0 to 3 DTE contracts are penny markets, a half spread of
    half a cent, widening to a cent from 0.20 delta at 1 DTE or more. The in the money
    buckets were not measurable there and are assumptions, stated as such. Refit from
    captures with `jobs.premium.fit_spread_model`.
    """

    edges: tuple[float, ...] = (0.10, 0.20, 0.30, 0.50, 0.70, 1.01)
    same_day: tuple[float, ...] = (0.005, 0.005, 0.005, 0.005, 0.02, 0.05)
    later: tuple[float, ...] = (0.005, 0.005, 0.01, 0.01, 0.02, 0.05)
    measured: bool = True

    def half(self, dte: np.ndarray, abs_delta: np.ndarray) -> np.ndarray:
        bucket = np.clip(
            np.searchsorted(np.asarray(self.edges), np.nan_to_num(abs_delta), side="right"),
            0,
            len(self.edges) - 1,
        )
        return np.where(
            np.asarray(dte) == 0, np.asarray(self.same_day)[bucket], np.asarray(self.later)[bucket]
        )


# --------------------------------------------------------------------------------
# Marks
# --------------------------------------------------------------------------------


@dataclass(slots=True)
class Smile:
    """One expiry's implied volatility by minute and strike, within one session."""

    expiry: date
    strikes: np.ndarray
    years: np.ndarray
    iv: np.ndarray
    #: Minutes since the smile was last rebuilt from fresh prints; large if never.
    age: np.ndarray

    def column(self, strike: float) -> np.ndarray | None:
        index = int(np.searchsorted(self.strikes, strike))
        if index >= len(self.strikes) or not np.isclose(self.strikes[index], strike):
            return None
        return self.iv[:, index]


@dataclass(slots=True)
class SessionMarks:
    underlying: str
    session: date
    #: Epoch seconds at the start of each regular session minute.
    times: np.ndarray
    spot: np.ndarray
    smiles: dict[date, Smile]


def _seconds(series: pd.Series) -> np.ndarray:
    return ((series - _EPOCH) // pd.Timedelta(seconds=1)).to_numpy(dtype=np.int64)


def expiry_seconds(expiry: date) -> int:
    """Epoch seconds of the 16:00 New York close on the expiry date."""
    moment = datetime.combine(expiry, time(EXPIRY_HOUR_LOCAL), tzinfo=_EXPIRY_TZ)
    return int(moment.timestamp())


def build_marks(
    underlying: str,
    session: date,
    minutes: pd.DataFrame,
    options: pd.DataFrame,
    *,
    close: datetime,
    rate: float,
) -> SessionMarks:
    """Turn one session's prints into a smile per expiry per minute.

    `minutes` needs `ts` and `close`; `options` needs `expiry`, `right`, `strike`,
    `ts`, `close` and `vwap`. Bars at or after `close` are dropped: the bar stamped
    16:00 is the closing print, not a minute anybody can trade in.
    """
    ordered = minutes.sort_values("ts")
    times = _seconds(ordered["ts"])
    spot = ordered["close"].to_numpy(dtype=float)
    keep = times < int(close.timestamp())
    times, spot = times[keep], spot[keep]

    smiles: dict[date, Smile] = {}
    if options.empty or len(times) == 0:
        return SessionMarks(underlying, session, times, spot, smiles)

    stamps = _seconds(options["ts"])
    index = np.searchsorted(times, stamps)
    on_grid = (index < len(times)) & (times[np.minimum(index, len(times) - 1)] == stamps)

    for expiry, group in options[on_grid].groupby("expiry"):
        expiry_date = expiry if isinstance(expiry, date) else pd.Timestamp(expiry).date()
        rows = index[on_grid][options[on_grid]["expiry"].to_numpy() == expiry]
        smiles[expiry_date] = _smile(expiry_date, group, rows, times, spot, rate)
    return SessionMarks(underlying, session, times, spot, smiles)


def _smile(
    expiry: date,
    prints: pd.DataFrame,
    minute_of: np.ndarray,
    times: np.ndarray,
    spot: np.ndarray,
    rate: float,
) -> Smile:
    strikes = np.unique(prints["strike"].to_numpy(dtype=float))
    years = np.maximum((expiry_seconds(expiry) - times) / SECONDS_PER_YEAR, 0.0)

    is_call = prints["right"].to_numpy() == "C"
    strike = prints["strike"].to_numpy(dtype=float)
    vwap = prints["vwap"].to_numpy(dtype=float)
    price = np.where(np.isfinite(vwap) & (vwap > 0), vwap, prints["close"].to_numpy(dtype=float))
    at = spot[minute_of]
    otm = np.where(is_call, strike >= at * (1 - ATM_BAND), strike <= at * (1 + ATM_BAND))
    sigma = implied_vol(is_call, price, at, strike, years[minute_of], rate)
    usable = otm & np.isfinite(sigma)

    order = np.argsort(minute_of[usable], kind="stable")
    print_minute = minute_of[usable][order]
    print_column = np.searchsorted(strikes, strike[usable][order])
    print_sigma = sigma[usable][order]

    minutes = len(times)
    grid = np.full((minutes, len(strikes)), np.nan)
    age = np.full(minutes, 10**6, dtype=np.int64)
    latest_minute = np.full(len(strikes), -(10**6), dtype=np.int64)
    latest_sigma = np.full(len(strikes), np.nan)
    last_built: int | None = None
    cursor = 0
    for minute in range(minutes):
        while cursor < len(print_minute) and print_minute[cursor] == minute:
            latest_minute[print_column[cursor]] = minute
            latest_sigma[print_column[cursor]] = print_sigma[cursor]
            cursor += 1
        fresh = latest_minute > minute - FRESH_MINUTES
        if int(fresh.sum()) >= MIN_SMILE_POINTS:
            known = strikes[fresh]
            row = np.interp(strikes, known, latest_sigma[fresh])
            row[
                (strikes < known[0] - EXTRAPOLATE_DOLLARS)
                | (strikes > known[-1] + EXTRAPOLATE_DOLLARS)
            ] = np.nan
            grid[minute] = row
            last_built = minute
        elif last_built is not None and minute - last_built <= STALE_MINUTES:
            grid[minute] = grid[last_built]
        if last_built is not None:
            age[minute] = minute - last_built
    return Smile(expiry, strikes, years, grid, age)


# --------------------------------------------------------------------------------
# Entries
# --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Leg:
    is_call: bool
    strike: float
    short: bool

    @property
    def sign(self) -> float:
        """+1 for a short leg, -1 for a long: the leg's share of the debit to close."""
        return 1.0 if self.short else -1.0


@dataclass(frozen=True, slots=True)
class EntryRule:
    #: Sessions from entry to expiry. 0 is same day.
    dte: int = 0
    #: Minutes after the open. 30 is 10:00 New York, past the opening range.
    minute: int = 30
    short_delta: float = 0.20
    width: float = 2.0
    #: Refuse an entry whose credit after fills is below this, per share.
    min_credit: float = 0.05
    #: Refuse an entry when the modelled spread takes more than this share of the
    #: credit at the mid. A liquidity filter that never refuses anything is not working.
    max_cost_share: float = 0.5


@dataclass(frozen=True, slots=True)
class Entry:
    underlying: str
    session: date
    expiry: date
    dte: int
    structure: Structure
    legs: tuple[Leg, ...]
    credit_mid: float
    #: Half spread per leg at the entry minute.
    entry_half: tuple[float, ...]
    short_deltas: tuple[float, ...]
    spot: float
    width: float


def select_entry(
    marks: SessionMarks,
    expiry: date,
    structure: Structure,
    rule: EntryRule,
    spreads: SpreadModel,
    rate: float,
) -> Entry | str:
    """The position a rule opens this session, or the reason it opens nothing."""
    if rule.minute >= len(marks.times):
        return "the session ends before the entry minute"
    smile = marks.smiles.get(expiry)
    if smile is None:
        return f"no trades on the {expiry} expiry"
    minute = rule.minute
    row = smile.iv[minute]
    if not np.isfinite(row).any():
        return "no smile at the entry minute"
    spot = float(marks.spot[minute])
    years = float(smile.years[minute])

    sides = {
        Structure.PUT_SPREAD: (False,),
        Structure.CALL_SPREAD: (True,),
        Structure.IRON_CONDOR: (False, True),
    }[structure]
    legs: list[Leg] = []
    short_deltas: list[float] = []
    for is_call in sides:
        candidates = np.isfinite(row) & (smile.strikes > spot if is_call else smile.strikes < spot)
        if not candidates.any():
            return "no out of the money strikes at the entry minute"
        deltas = np.abs(
            bsm_delta(is_call, spot, smile.strikes[candidates], years, rate, row[candidates])
        )
        pick = int(np.argmin(np.abs(deltas - rule.short_delta)))
        short_strike = float(smile.strikes[candidates][pick])
        long_strike = short_strike + rule.width if is_call else short_strike - rule.width
        long_column = smile.column(long_strike)
        if long_column is None or not np.isfinite(long_column[minute]):
            return f"no mark for the {long_strike:g} wing"
        legs += [Leg(is_call, short_strike, True), Leg(is_call, long_strike, False)]
        short_deltas.append(float(deltas[pick]))

    sigmas = np.array([smile.column(leg.strike)[minute] for leg in legs])
    calls = np.array([leg.is_call for leg in legs])
    strikes = np.array([leg.strike for leg in legs])
    prices = bsm_price(calls, spot, strikes, years, rate, sigmas)
    credit_mid = float(sum(leg.sign * price for leg, price in zip(legs, prices, strict=True)))
    if credit_mid <= 0:
        return "no credit at the mid"
    abs_delta = np.abs(bsm_delta(calls, spot, strikes, years, rate, sigmas))
    half = spreads.half(np.full(len(legs), rule.dte), abs_delta)
    return Entry(
        underlying=marks.underlying,
        session=marks.session,
        expiry=expiry,
        dte=rule.dte,
        structure=structure,
        legs=tuple(legs),
        credit_mid=credit_mid,
        entry_half=tuple(float(value) for value in half),
        short_deltas=tuple(short_deltas),
        spot=spot,
        width=rule.width,
    )


# --------------------------------------------------------------------------------
# The path
# --------------------------------------------------------------------------------


@dataclass(slots=True)
class Path:
    """A position's legs marked minute by minute, from its entry minute to expiry."""

    spot: np.ndarray
    price: np.ndarray
    delta: np.ndarray
    half: np.ndarray
    #: Index of the minute an intraday rule flattens in, on the expiry session.
    flatten_at: int
    settle_spot: float

    @property
    def debit(self) -> np.ndarray:
        """What closing the whole position costs at the mid, per share, each minute."""
        return self.price @ self._signs

    _signs: np.ndarray


def build_path(
    entry: Entry,
    sessions: Sequence[SessionMarks],
    entry_minute: int,
    spreads: SpreadModel,
    rate: float,
) -> Path | None:
    """Mark the entry's legs across `sessions`, the entry session first, expiry last.

    None when the expiry session or its smile is missing: a position whose path cannot
    be marked is left out and counted, never guessed at.
    """
    if not sessions or sessions[-1].session != entry.expiry:
        return None
    calls = np.array([leg.is_call for leg in entry.legs])
    strikes = np.array([leg.strike for leg in entry.legs])
    spots, prices, deltas, halves = [], [], [], []
    for position, marks in enumerate(sessions):
        smile = marks.smiles.get(entry.expiry)
        if smile is None:
            return None
        start = entry_minute if position == 0 else 0
        columns = [smile.column(strike) for strike in strikes]
        sigma = np.column_stack(
            [
                column[start:] if column is not None else np.full(len(marks.times) - start, np.nan)
                for column in columns
            ]
        )
        spot = marks.spot[start:]
        years = smile.years[start:, None]
        price = bsm_price(calls, spot[:, None], strikes, years, rate, sigma)
        delta = bsm_delta(calls, spot[:, None], strikes, years, rate, sigma)
        price[~np.isfinite(sigma)] = np.nan
        dte = len(sessions) - 1 - position
        spots.append(spot)
        prices.append(price)
        deltas.append(delta)
        halves.append(spreads.half(np.full(price.shape, dte), np.abs(delta)))
    last = len(sessions[-1].times)
    total = sum(len(spot) for spot in spots)
    return Path(
        spot=np.concatenate(spots),
        price=np.vstack(prices),
        delta=np.vstack(deltas),
        half=np.vstack(halves),
        flatten_at=max(total - FLATTEN_BEFORE_CLOSE, 1)
        if last > FLATTEN_BEFORE_CLOSE
        else total - 1,
        settle_spot=float(sessions[-1].spot[-1]),
        _signs=np.array([leg.sign for leg in entry.legs]),
    )


# --------------------------------------------------------------------------------
# Exits
# --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExitRule:
    #: Close once this share of the credit is captured. None holds for the full credit.
    target: float | None = None
    #: Close once the loss reaches this multiple of the credit.
    stop: float | None = None
    #: Close once the underlying reaches a short strike.
    touch: bool = False
    #: Close once a short leg's |delta| reaches this.
    delta_stop: float | None = None
    #: Hold to expiry and settle, rather than flatten before the close.
    settle: bool = False

    @property
    def label(self) -> str:
        parts = []
        if self.stop is not None:
            parts.append(f"stop {self.stop:g}x")
        if self.touch:
            parts.append("stop at touch")
        if self.delta_stop is not None:
            parts.append(f"stop at {self.delta_stop:.2f} delta")
        if not parts:
            parts.append("no stop")
        parts.append(f"target {self.target:.0%}" if self.target is not None else "no target")
        parts.append("hold to expiry" if self.settle else "flatten")
        return ", ".join(parts)


@dataclass(frozen=True, slots=True)
class Trade:
    underlying: str
    session: date
    dte: int
    structure: Structure
    rule: str
    k: float
    credit: float
    exit_debit: float
    reason: ExitReason
    minutes_held: int
    #: Dollars for one lot, after fees.
    pnl: float
    #: `pnl` over the credit in dollars.
    r: float
    #: For a stop-like exit, how far past the trigger level the next minute filled, per
    #: share. The price of a stop that cannot fill where it was set.
    slip: float | None
    width: float


def _first(mask: np.ndarray, start: int = 1) -> int | None:
    hits = np.flatnonzero(mask[start:])
    return int(hits[0]) + start if hits.size else None


def _fees(entry: Entry, closing: bool) -> float:
    opening = sum(SELL_FEE if leg.short else BUY_FEE for leg in entry.legs)
    if not closing:
        return opening
    return opening + sum(BUY_FEE if leg.short else SELL_FEE for leg in entry.legs)


def run_exit(entry: Entry, path: Path, rule: ExitRule, k: float) -> Trade | None:
    """What one exit rule makes of one position at one fill assumption.

    None when the fill assumption leaves too little credit to trade, which the caller
    counts as a refusal rather than a zero.
    """
    credit = entry.credit_mid - k * sum(entry.entry_half)
    if credit <= 0:
        return None
    debit = path.debit
    valid = np.isfinite(debit)
    horizon = len(debit) if rule.settle else path.flatten_at
    window = np.zeros(len(debit), dtype=bool)
    window[1:horizon] = True

    found = _triggers(entry, path, rule, credit, window, valid & window)
    if found:
        trigger, _, reason, level = min(found, key=lambda item: item[:2])
        # Every exit fills at the next minute with a mark, targets included. A mark is
        # a smile fitted to trade prints and moves a few cents on noise, so the minute a
        # target triggers is disproportionately a minute the mark dipped: filling there
        # took the favourable side of the noise. Measured 2026-09-14 on 0 and 1 DTE
        # condors, it added $2.50 to $6.60 a position, which was the whole of every
        # target's apparent edge. Past the last marked minute an exit runs to the end.
        fill = _first(valid, trigger + 1)
        if fill is not None:
            exit_debit = float(debit[fill] + k * path.half[fill].sum())
            slip = None if level is None else float(debit[fill] - level)
            return _trade(entry, rule, k, credit, exit_debit, reason, fill, slip, closing=True)

    if not rule.settle:
        fill = path.flatten_at if valid[path.flatten_at] else _last(valid, path.flatten_at)
        if fill is not None and fill >= 1:
            exit_debit = float(debit[fill] + k * path.half[fill].sum())
            return _trade(
                entry, rule, k, credit, exit_debit, ExitReason.FLATTEN, fill, None, closing=True
            )
    return _settle(entry, path, rule, k, credit)


def _triggers(
    entry: Entry,
    path: Path,
    rule: ExitRule,
    credit: float,
    window: np.ndarray,
    live: np.ndarray,
) -> list[tuple[int, int, ExitReason, float | None]]:
    """(minute, priority, reason, trigger level) for each condition the rule watches.

    Priority settles a tie inside one minute: a resting target is already in the book
    when that minute's stop condition is seen, so the target fills first.
    """
    debit = path.debit
    found: list[tuple[int, int, ExitReason, float | None]] = []
    if rule.target is not None:
        hit = _first(live & (debit <= credit * (1 - rule.target)))
        if hit is not None:
            found.append((hit, 0, ExitReason.TARGET, None))
    if rule.stop is not None:
        level = credit * (1 + rule.stop)
        hit = _first(live & (debit >= level))
        if hit is not None:
            found.append((hit, 1, ExitReason.STOP, level))
    if rule.touch:
        touched = np.zeros(len(debit), dtype=bool)
        for leg in entry.legs:
            if leg.short:
                touched |= path.spot >= leg.strike if leg.is_call else path.spot <= leg.strike
        hit = _first(window & touched)
        if hit is not None:
            found.append((hit, 1, ExitReason.TOUCH, None))
    if rule.delta_stop is not None:
        shorts = [index for index, leg in enumerate(entry.legs) if leg.short]
        breached = (np.abs(path.delta[:, shorts]) >= rule.delta_stop).any(axis=1)
        hit = _first(live & breached)
        if hit is not None:
            found.append((hit, 1, ExitReason.DELTA, None))
    return found


def _settle(entry: Entry, path: Path, rule: ExitRule, k: float, credit: float) -> Trade:
    """Held to the end: settled at intrinsic on the expiry close, no spread, no closing
    fees. Assignment fees and after hours moves past the close are not modelled."""
    payoff = np.array(
        [
            max(path.settle_spot - leg.strike, 0.0)
            if leg.is_call
            else max(leg.strike - path.settle_spot, 0.0)
            for leg in entry.legs
        ]
    )
    exit_debit = float(payoff @ np.array([leg.sign for leg in entry.legs]))
    return _trade(
        entry,
        rule,
        k,
        credit,
        exit_debit,
        ExitReason.EXPIRY,
        len(path.spot) - 1,
        None,
        closing=False,
    )


def _last(mask: np.ndarray, before: int) -> int | None:
    hits = np.flatnonzero(mask[: before + 1])
    return int(hits[-1]) if hits.size else None


def _trade(
    entry: Entry,
    rule: ExitRule,
    k: float,
    credit: float,
    exit_debit: float,
    reason: ExitReason,
    minutes: int,
    slip: float | None,
    *,
    closing: bool,
) -> Trade:
    pnl = (credit - exit_debit) * MULTIPLIER - _fees(entry, closing)
    return Trade(
        underlying=entry.underlying,
        session=entry.session,
        dte=entry.dte,
        structure=entry.structure,
        rule=rule.label,
        k=k,
        credit=credit,
        exit_debit=exit_debit,
        reason=reason,
        minutes_held=minutes,
        pnl=pnl,
        r=pnl / (credit * MULTIPLIER),
        slip=slip,
        width=entry.width,
    )
