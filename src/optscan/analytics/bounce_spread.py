"""What to buy when the oversold bounce triggers: the call debit spread plan.

## Status: a lead under forward test, not a validated trade

`oversold_bounce` is validated **on the stock**. Its call debit spread expression is not.
In the 2026-10-21 overnight study it showed an edge of +14.0% over random timing on the
development period and +16.5% on the holdout, survived every knob of a fragility sweep,
and held with an exact hold-to-expiry payoff -- but it missed the gate fixed before the
run (dev q 0.146 against 0.10). The full record is the DECISIONS entry of that date.
Everything this module produces carries that status, and the caller is expected to show
it: a plan rendered without it would be the tool asserting something it has not earned.

## The plan is the studied rule, translated to listed contracts

The study priced continuous strikes at exactly 15 days out. A broker lists neither, so:

- **Entry** is the next trading session after the signal, because the study fills at
  the next open. A signal on today's close is a trade at tomorrow's open.
- **Expiry** is the listed one nearest 15 calendar days from entry, among those that
  land after the exit. A spread that expires before it is due to be closed is a
  different trade.
- **Long leg** is the listed call nearest spot. **Short leg** is the listed call nearest
  `spot * (1 + 0.84 * iv * sqrt(dte / 365))`, with `iv` the chain's own at-the-money
  vol. 0.84 sigmas is the width the study used; it is about the 20 delta strike.
- **Exit** is the close of the fifth session after entry, win or lose. No target and no
  stop: the study had neither, and the sweep found five sessions close to the best hold.

## Why the fill guidance is the most important number on the plan

The study's edge over random timing barely moved with slippage, but the money did: the
mean trade went +11.7%, +1.0% and -8.8% at 5%, 10% and 15% of the debit given up each
way. So the plan gives a limit at mid + 5% (the study's base assumption) and a walk-away
price at mid + 10% (about breakeven), and flags a quoted market too wide to fill inside
them. Paying the ask on a wide market is how this trade loses money while being right.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

from optscan.analytics.greeks import bsm_price
from optscan.models import OptionChain, OptionContract, Right

#: Calendar days from entry to the expiry the study priced.
TARGET_DTE = 15
#: The window a listed expiry may fall in. Fifteen days is what was studied; the
#: fragility sweep also measured 21 and 30 and found the edge smaller but present
#: (+10.2% and +8.3% over the null against +12.3% at 15), so a name that lists only
#: monthlies still gets a plan, with the tenor named on it, rather than being refused on
#: a cutoff the data does not support.
MIN_DTE = 10
MAX_DTE = 31
#: Above this the plan says the tenor is longer than the one the edge was measured at.
STUDIED_DTE_CEILING = 21
#: Sessions held, entry open to exit close.
HOLD_SESSIONS = 5
#: Distance from the long to the short strike, in standard deviations of the move to
#: expiry. About the 20 delta call.
WIDTH_SIGMAS = 0.84
#: The study's base slippage assumption, and the level where its mean trade was about
#: flat, each as a fraction of the debit.
LIMIT_SLIPPAGE = 0.05
WALK_AWAY_SLIPPAGE = 0.10
#: A spread needs a strike to buy and a different one to sell.
MIN_CALLS = 2
#: How far the nearest listed short strike may sit from the studied one, as a fraction
#: of spot, before the plan says so. A coarse strike grid moves the width, and the
#: width is part of what was tested.
STRIKE_DRIFT_WORTH_SAYING = 0.01

STATUS = (
    "Lead under forward test, not a validated trade. The bounce is validated on the "
    "stock; this options expression missed its pre-registered gate (2026-10-21)."
)


@dataclass(frozen=True, slots=True)
class Leg:
    """One listed contract in the plan, with whatever quote was available for it."""

    strike: float
    contract: str | None
    bid: float | None
    ask: float | None
    iv: float | None

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None or self.bid <= 0 or self.ask <= 0:
            return None
        if self.bid > self.ask:
            return None
        return (self.bid + self.ask) / 2


@dataclass(frozen=True, slots=True)
class SpreadPlan:
    """A call debit spread to open at the next session and close five sessions later."""

    symbol: str
    signal_session: date
    entry_session: date
    exit_session: date
    expiry: date
    dte: int
    spot: float
    iv: float
    long: Leg
    short: Leg
    #: Mid of the spread, from quotes when both legs have a two sided market, else from
    #: the model at `iv`. `quoted` says which.
    debit: float
    quoted: bool
    #: Long ask less short bid: what paying the market costs. None without quotes.
    natural: float | None
    limit: float
    walk_away: float
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def width(self) -> float:
        return self.short.strike - self.long.strike

    @property
    def max_loss(self) -> float:
        """Dollars per spread if it goes wrong, at the limit price. The whole debit."""
        return self.limit * 100.0

    @property
    def max_gain(self) -> float:
        """Dollars per spread at expiry above the short strike, at the limit price.

        Not the expected outcome: the plan closes after five sessions, long before
        expiry, so this is the ceiling the structure has rather than a target.
        """
        return max(self.width - self.limit, 0.0) * 100.0

    @property
    def market_too_wide(self) -> bool:
        """Whether paying the quoted market would already cost more than breakeven."""
        return self.natural is not None and self.natural > self.walk_away

    def as_dict(self) -> dict[str, object]:
        return {
            "status": STATUS,
            "symbol": self.symbol,
            "signal_session": self.signal_session.isoformat(),
            "entry_session": self.entry_session.isoformat(),
            "exit_session": self.exit_session.isoformat(),
            "expiry": self.expiry.isoformat(),
            "dte": self.dte,
            "spot": self.spot,
            "iv": self.iv,
            "long_strike": self.long.strike,
            "long_contract": self.long.contract,
            "short_strike": self.short.strike,
            "short_contract": self.short.contract,
            "width": self.width,
            "debit": self.debit,
            "quoted": self.quoted,
            "natural": self.natural,
            "limit": self.limit,
            "walk_away": self.walk_away,
            "max_loss": self.max_loss,
            "max_gain": self.max_gain,
            "market_too_wide": self.market_too_wide,
            "notes": list(self.notes),
        }


def next_session(day: date, is_trading_day: Callable[[date], bool]) -> date:
    """The first trading day strictly after `day`."""
    probe = day + timedelta(days=1)
    for _ in range(15):
        if is_trading_day(probe):
            return probe
        probe += timedelta(days=1)
    raise ValueError(f"no trading day within fifteen days after {day}")


def session_after(start: date, sessions: int, is_trading_day: Callable[[date], bool]) -> date:
    """The trading day `sessions` sessions after `start`, counting `start` as zero."""
    day = start
    for _ in range(sessions):
        day = next_session(day, is_trading_day)
    return day


def choose_expiry(listed: Sequence[date], entry: date, exit_: date) -> date | None:
    """The listed expiry nearest fifteen days from entry that lands after the exit."""
    usable = [item for item in listed if item > exit_ and MIN_DTE <= (item - entry).days <= MAX_DTE]
    if not usable:
        return None
    return min(usable, key=lambda item: (abs((item - entry).days - TARGET_DTE), item))


def _calls(chain: OptionChain) -> list[OptionContract]:
    return sorted(
        (c for c in chain.contracts if c.right is Right.CALL and c.strike > 0),
        key=lambda c: c.strike,
    )


def _leg(contract: OptionContract) -> Leg:
    return Leg(
        strike=contract.strike,
        contract=contract.contract_symbol,
        bid=contract.bid,
        ask=contract.ask,
        iv=contract.vendor_iv,
    )


def _atm_iv(calls: Sequence[OptionContract], spot: float) -> float | None:
    """The chain's own at-the-money vol: the nearest strikes to spot that carry one."""
    priced = [c for c in calls if c.vendor_iv]
    if not priced:
        return None
    nearest = sorted(priced, key=lambda c: abs(c.strike - spot))[:2]
    return sum(c.vendor_iv for c in nearest) / len(nearest)  # type: ignore[misc]


def plan_spread(
    symbol: str,
    chain: OptionChain,
    spot: float,
    signal_session: date,
    entry_session: date,
    exit_session: date,
    *,
    iv_fallback: float | None = None,
    rate: float = 0.043,
    quoted_outside_session: bool = False,
) -> SpreadPlan | str:
    """The plan for one trigger on one chain, or a sentence saying why there is none."""
    if spot <= 0:
        return f"{symbol}: no usable spot price."
    calls = _calls(chain)
    if len(calls) < MIN_CALLS:
        return f"{symbol}: the {chain.expiry} chain lists too few calls to build a spread."

    iv = _atm_iv(calls, spot) or iv_fallback
    if not iv or iv <= 0:
        return f"{symbol}: no implied vol on the chain or in the history, so no width."

    dte = (chain.expiry - entry_session).days
    years = max(dte, 1) / 365.0
    long_contract = min(calls, key=lambda c: (abs(c.strike - spot), c.strike))
    target = spot * (1 + WIDTH_SIGMAS * iv * math.sqrt(years))
    above = [c for c in calls if c.strike > long_contract.strike]
    if not above:
        return f"{symbol}: no listed call above {long_contract.strike:g} to sell."
    short_contract = min(above, key=lambda c: (abs(c.strike - target), c.strike))

    long_leg, short_leg = _leg(long_contract), _leg(short_contract)
    notes: list[str] = []

    if long_leg.mid is not None and short_leg.mid is not None:
        debit = long_leg.mid - short_leg.mid
        natural = (long_leg.ask or 0.0) - (short_leg.bid or 0.0)
        quoted = True
    else:
        debit = bsm_price(Right.CALL, spot, long_leg.strike, years, rate, iv) - bsm_price(
            Right.CALL, spot, short_leg.strike, years, rate, iv
        )
        natural = None
        quoted = False
        notes.append(
            "No two sided quote on one leg, so the debit is a model estimate at the "
            "chain's IV. Check the live market before placing anything."
        )

    width = short_leg.strike - long_leg.strike
    if debit <= 0 or debit >= width:
        return (
            f"{symbol}: the {long_leg.strike:g}/{short_leg.strike:g} spread prices at "
            f"{debit:.2f} against a {width:g} width, which is not a tradeable debit."
        )

    limit = round(debit * (1 + LIMIT_SLIPPAGE), 2)
    walk_away = round(debit * (1 + WALK_AWAY_SLIPPAGE), 2)

    if natural is not None and natural > walk_away:
        notes.append(
            f"The quoted market is wide: paying it costs {natural:.2f} against a "
            f"{walk_away:.2f} walk-away. At that fill the backtested edge is gone; work "
            "the limit or skip this one."
        )
    if dte > STUDIED_DTE_CEILING:
        notes.append(
            f"Nearest usable expiry is {dte} days out, longer than studied. The sweep found "
            "the edge smaller at longer tenors: +10% over the null at 21 days, +8% at 30, "
            "against +12% at 15."
        )
    elif dte != TARGET_DTE:
        notes.append(f"Nearest listed expiry is {dte} days out rather than {TARGET_DTE}.")
    if quoted_outside_session and quoted:
        notes.append(
            "Quotes were taken outside regular hours, when option markets are wider than "
            "at the open. Re-check the debit and the width after 09:30 before judging it."
        )
    if abs(target - short_leg.strike) / spot > STRIKE_DRIFT_WORTH_SAYING:
        notes.append(
            f"The study's short strike would be {target:.2f}; the nearest listed is "
            f"{short_leg.strike:g}."
        )

    return SpreadPlan(
        symbol=symbol,
        signal_session=signal_session,
        entry_session=entry_session,
        exit_session=exit_session,
        expiry=chain.expiry,
        dte=dte,
        spot=spot,
        iv=iv,
        long=long_leg,
        short=short_leg,
        debit=debit,
        quoted=quoted,
        natural=natural,
        limit=limit,
        walk_away=walk_away,
        notes=tuple(notes),
    )
