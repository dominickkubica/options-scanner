"""Open interest structure and dealer gamma exposure, per expiry.

Pure math: no I/O, no config, no globals. Every input is an argument and every
threshold is a documented default, so the scanner can call this with its own numbers
later without inheriting the dashboard's.

## What this computes, and what it assumes

Four of the five outputs are counting exercises on open interest and assume nothing:
the call wall, the put wall, the pin candidate, and max pain. They are facts about
where contracts are held.

The fifth, gamma exposure, is a model built on an assumption that cannot be verified
from public data: **that dealers are long every call and short every put.** That is the
conventional sign rule and it is what the requirement asks for, but it is a convention
rather than a measurement. Real dealer positioning is not published; a single large
call overwrite programme can flip the true sign of a strike without changing anything
this module can see. So GEX here says "if the usual convention holds, this is the
shape", and the regime label inherits exactly that much confidence and no more.

## Units

`OI x gamma x 100 x spot^2 x 0.01` is dollars of delta that dealers must buy or sell
per **1% move** in the underlying. The 100 is the contract multiplier, the spot^2 turns
per-share gamma into dollars per unit move, and the 0.01 turns a unit move into a
percent move. Positive means hedging leans against the move and dampens it; negative
means hedging follows the move and feeds it.

## Why gamma is recomputed rather than read

yfinance publishes open interest and an implied vol and no greeks at all, so gamma is
solved here from the chain's own implied vol with `bsm_greeks`. That is also what makes
the zero-gamma search possible: the level is found by asking what total GEX *would* be
at a hypothetical spot, which requires recomputing every strike's gamma at that spot.
Reading a vendor's gamma would fix it at today's spot and the search would be measuring
nothing but the OI distribution.

## None is not zero

A strike whose implied vol is missing has **no** gamma and reports `None`, not 0.0. It
still reports its open interest, because the walls are a count and do not need a model.
Collapsing the two would draw a dead strike as a flat bar and let it pull the zero-gamma
level toward itself.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Literal

from optscan.analytics.greeks import MIN_TIME_TO_EXPIRY, bsm_greeks
from optscan.models import Right

#: Contracts per option. Every US listed equity and index option, barring an
#: adjustment after a corporate action, which this does not attempt to detect.
CONTRACT_SIZE = 100

#: The band the zero-gamma search sweeps, either side of spot. Wide enough to hold the
#: flip in a normal session, narrow enough that the constant-IV assumption underneath
#: it is not doing something absurd: this reprices gamma at a hypothetical spot without
#: moving the volatility surface, which is defensible over a few percent and not over
#: twenty.
DEFAULT_GAMMA_BAND = 0.03

#: Grid points across that band. 121 puts them about 0.05% apart, which is finer than
#: any strike spacing this is drawn against.
DEFAULT_GAMMA_POINTS = 121

#: Fewer points than this cannot bracket anything: a sign change needs two prices to
#: sit between.
MIN_GAMMA_POINTS = 2

#: Half-width of the pin search, as a fraction of spot.
DEFAULT_PIN_BAND = 0.01

#: Below this many strikes carrying open interest, the shape is not a distribution, it
#: is a handful of prints. Everything is still computed and the caller is told.
MIN_STRIKES_WITH_OI = 6

#: Below this share of open interest priced, the GEX curve is describing a minority of
#: the contracts that exist and the zero-gamma level with it.
MIN_PRICED_OI_SHARE = 0.5

#: Past this distance from spot, a wall is reported with a caveat. The wall is defined
#: as the heaviest strike on its side with no distance limit, which is the honest
#: definition and is regularly a deep wing: on the first live run the SPX call wall came
#: back 70% above spot and SPY's put wall 32% below, both of them tail hedges that price
#: will not meet this expiry. The number is still reported, because a chain whose
#: heaviest strike is 70% away is telling you something real about the chain.
FAR_WALL_PCT = 0.05

Regime = Literal["dampened", "volatile", "unknown"]


@dataclass(frozen=True, slots=True)
class OiRow:
    """One side of one strike: what is held there and what it is worth pricing with.

    `years` is carried per row rather than per profile so that a single expiry and
    "everything inside 30 days" are the same code path. Two expiries at one strike stay
    two rows, because they have different gammas and summing their open interest before
    pricing would use one expiry's clock for both.
    """

    strike: float
    right: Right
    open_interest: int
    iv: float | None
    years: float

    @property
    def priceable(self) -> bool:
        """Whether a gamma can be solved for this row at all."""
        return (
            self.iv is not None
            and math.isfinite(self.iv)
            and self.iv > 0.0
            and self.years > MIN_TIME_TO_EXPIRY
        )


@dataclass(frozen=True, slots=True)
class StrikeProfile:
    """One strike's open interest and gamma exposure, both sides folded together."""

    strike: float
    call_oi: int
    put_oi: int
    #: None when no call at this strike could be priced. Never 0.0 for "unknown".
    call_gex: float | None
    put_gex: float | None
    net_gex: float | None

    @property
    def total_oi(self) -> int:
        return self.call_oi + self.put_oi


@dataclass(frozen=True, slots=True)
class Wall:
    """A strike singled out by how much open interest sits on it."""

    strike: float
    open_interest: int
    #: Signed distance from spot, as a fraction. Negative is below spot.
    distance_pct: float


@dataclass(frozen=True, slots=True)
class GexProfile:
    """Everything one expiry selection says about where price is pinned.

    `notes` carries every reason a number here is weaker than it looks: a thin chain,
    unpriced open interest, a zero-gamma search that never crossed. The caller is
    expected to show them. A regime label with no caveat attached is the one output of
    this module that could be believed more than it deserves.
    """

    spot: float
    strikes: tuple[StrikeProfile, ...]
    total_gex: float | None
    call_wall: Wall | None
    put_wall: Wall | None
    pin: Wall | None
    max_pain: float | None
    zero_gamma: float | None
    regime: Regime
    #: Open interest that could be priced, and all of it, so the caller can say how
    #: much of the chain the GEX curve actually describes.
    priced_oi: int
    total_oi: int
    notes: tuple[str, ...] = ()


def strike_gamma(spot: float, strike: float, years: float, rate: float, iv: float) -> float | None:
    """Per-share BSM gamma, or None when the inputs cannot support one.

    Gamma does not depend on the right, so this prices a call and uses it for both
    sides. Below `MIN_TIME_TO_EXPIRY` the model is numerically useless rather than
    merely imprecise, and `bsm_greeks` returns a degenerate zero there; that zero is
    turned back into None, because "expired" and "no gamma" are different facts.
    """
    if not (math.isfinite(spot) and math.isfinite(strike) and spot > 0.0 and strike > 0.0):
        return None
    if not math.isfinite(iv) or iv <= 0.0 or years <= MIN_TIME_TO_EXPIRY:
        return None
    gamma = bsm_greeks(Right.CALL, spot, strike, years, rate, iv).gamma
    return gamma if math.isfinite(gamma) else None


def row_gex(
    row: OiRow,
    spot: float,
    rate: float,
    *,
    contract_size: int = CONTRACT_SIZE,
) -> float | None:
    """One row's dollar gamma exposure per 1% move, signed by the dealer convention.

    Calls positive, puts negative: the assumption is dealers are long calls and short
    puts. See the module docstring on how much that is worth.
    """
    if row.open_interest <= 0:
        return 0.0
    if not row.priceable:
        return None
    gamma = strike_gamma(spot, row.strike, row.years, rate, row.iv)  # type: ignore[arg-type]
    if gamma is None:
        return None
    exposure = row.open_interest * gamma * contract_size * spot * spot * 0.01
    return exposure if row.right is Right.CALL else -exposure


def net_gex_by_strike(
    rows: Iterable[OiRow],
    spot: float,
    rate: float,
    *,
    contract_size: int = CONTRACT_SIZE,
) -> list[StrikeProfile]:
    """Per-strike open interest and net GEX, ascending by strike.

    A strike where one side prices and the other does not keeps the side that priced
    and reports the net, because dropping the strike entirely would lose open interest
    the walls are counted from.
    """
    calls: dict[float, int] = {}
    puts: dict[float, int] = {}
    call_gex: dict[float, float] = {}
    put_gex: dict[float, float] = {}

    for row in rows:
        oi = max(row.open_interest, 0)
        side_oi, side_gex = (calls, call_gex) if row.right is Right.CALL else (puts, put_gex)
        side_oi[row.strike] = side_oi.get(row.strike, 0) + oi
        exposure = row_gex(row, spot, rate, contract_size=contract_size)
        if exposure is not None:
            side_gex[row.strike] = side_gex.get(row.strike, 0.0) + exposure

    profiles: list[StrikeProfile] = []
    for strike in sorted(set(calls) | set(puts)):
        call_value = call_gex.get(strike)
        put_value = put_gex.get(strike)
        if call_value is None and put_value is None:
            net: float | None = None
        else:
            net = (call_value or 0.0) + (put_value or 0.0)
        profiles.append(
            StrikeProfile(
                strike=strike,
                call_oi=calls.get(strike, 0),
                put_oi=puts.get(strike, 0),
                call_gex=call_value,
                put_gex=put_value,
                net_gex=net,
            )
        )
    return profiles


def total_gex_at(
    rows: Iterable[OiRow],
    spot: float,
    rate: float,
    *,
    contract_size: int = CONTRACT_SIZE,
) -> float | None:
    """Net GEX across every priceable row at a hypothetical spot.

    Returns None when nothing could be priced, which is the difference between a
    market with no gamma and a chain with no volatilities.
    """
    total = 0.0
    priced = False
    for row in rows:
        if row.open_interest <= 0:
            # Contributes nothing, and must not count as "priced". A chain of listed
            # strikes that nobody holds would otherwise total a confident 0.0 and hand
            # the zero-gamma search a curve that is flat at zero everywhere, which it
            # would faithfully report as a flip at the first price it tried. Found on
            # the first live run against ^SPX, where Yahoo publishes no open interest
            # for most expiries.
            continue
        exposure = row_gex(row, spot, rate, contract_size=contract_size)
        if exposure is None:
            continue
        priced = True
        total += exposure
    return total if priced else None


def zero_gamma(
    rows: Sequence[OiRow],
    spot: float,
    rate: float,
    *,
    band: float = DEFAULT_GAMMA_BAND,
    points: int = DEFAULT_GAMMA_POINTS,
    contract_size: int = CONTRACT_SIZE,
) -> float | None:
    """The spot at which total GEX changes sign, or None when it does not change.

    Found by sweeping hypothetical spots across the band and repricing every strike's
    gamma at each one, then interpolating linearly between the two grid points that
    straddle the flip. Nothing else about the chain moves: the implied vols, the open
    interest and the clock are all held fixed, so this answers "where would the sign
    flip if only spot moved" and not "where will the sign flip".

    None is returned when the whole band has one sign, which is the common case on a
    strongly positioned day and is information rather than a failure. The caller is
    expected to say so rather than drawing a line at an edge of the band.
    """
    if points < MIN_GAMMA_POINTS or band <= 0.0 or not math.isfinite(spot) or spot <= 0.0:
        return None

    low, high = spot * (1.0 - band), spot * (1.0 + band)
    step = (high - low) / (points - 1)

    samples: list[tuple[float, float]] = []
    for index in range(points):
        price = low + step * index
        total = total_gex_at(rows, price, rate, contract_size=contract_size)
        if total is not None:
            samples.append((price, total))

    # A curve that is zero at every sample is not a flip at the first price tried, it is
    # a chain with nothing in it. Checked before the scan rather than inside it, because
    # an exact zero is a crossing when its neighbours have signs and noise when they do
    # not, and only the whole sample can tell the two apart.
    if not any(total != 0.0 for _, total in samples):
        return None

    for (previous_price, previous_total), (price, total) in pairwise(samples):
        if total == 0.0:
            return price
        if previous_total != 0.0 and (total > 0.0) != (previous_total > 0.0):
            # Linear between the bracketing points. The curve is smooth at this
            # spacing, so a root finder would move the answer by less than a cent.
            span = total - previous_total
            weight = -previous_total / span if span else 0.0
            return previous_price + weight * (price - previous_price)
    return None


def max_pain(rows: Iterable[OiRow]) -> float | None:
    """The listed strike where the most option value expires worthless.

    Total intrinsic value paid out to holders if the underlying settled at each listed
    strike; the minimum is max pain. Open interest is the only input, so this needs no
    volatility and is unaffected by a chain with no implied vols at all.

    The usual caution applies: this is a description of where contracts sit, not a
    forecast, and it moves whenever the open interest behind it does.
    """
    holdings = [(row.strike, row.right, max(row.open_interest, 0)) for row in rows]
    strikes = sorted({strike for strike, _, oi in holdings if oi > 0})
    if not strikes:
        return None

    best_strike: float | None = None
    best_payout = math.inf
    for settle in strikes:
        payout = 0.0
        for strike, right, oi in holdings:
            if oi <= 0:
                continue
            if right is Right.CALL:
                payout += max(settle - strike, 0.0) * oi
            else:
                payout += max(strike - settle, 0.0) * oi
        if payout < best_payout:
            best_payout, best_strike = payout, settle
    return best_strike


def _wall(strike: float | None, open_interest: int, spot: float) -> Wall | None:
    if strike is None or open_interest <= 0 or spot <= 0.0:
        return None
    return Wall(strike=strike, open_interest=open_interest, distance_pct=(strike - spot) / spot)


def walls(rows: Iterable[OiRow], spot: float) -> tuple[Wall | None, Wall | None]:
    """The heaviest call strike above spot and the heaviest put strike below it.

    Strictly above and strictly below. A wall at spot is not a wall, it is the money,
    and it is already reported as the pin candidate.
    """
    call_oi: dict[float, int] = {}
    put_oi: dict[float, int] = {}
    for row in rows:
        oi = max(row.open_interest, 0)
        if oi <= 0:
            continue
        side = call_oi if row.right is Right.CALL else put_oi
        side[row.strike] = side.get(row.strike, 0) + oi

    above = {strike: oi for strike, oi in call_oi.items() if strike > spot}
    below = {strike: oi for strike, oi in put_oi.items() if strike < spot}

    # max() on the open interest, ties broken by the strike nearer spot: the closer of
    # two equally held strikes is the one price reaches first.
    call_strike = min(above, key=lambda k: (-above[k], k - spot)) if above else None
    put_strike = min(below, key=lambda k: (-below[k], spot - k)) if below else None

    return (
        _wall(call_strike, above.get(call_strike, 0) if call_strike else 0, spot),
        _wall(put_strike, below.get(put_strike, 0) if put_strike else 0, spot),
    )


def pin_candidate(
    rows: Iterable[OiRow],
    spot: float,
    *,
    band: float = DEFAULT_PIN_BAND,
) -> Wall | None:
    """The strike inside the band holding the most call plus put open interest."""
    if spot <= 0.0:
        return None
    low, high = spot * (1.0 - band), spot * (1.0 + band)

    totals: dict[float, int] = {}
    for row in rows:
        oi = max(row.open_interest, 0)
        if oi <= 0 or not (low <= row.strike <= high):
            continue
        totals[row.strike] = totals.get(row.strike, 0) + oi

    if not totals:
        return None
    strike = min(totals, key=lambda k: (-totals[k], abs(k - spot)))
    return _wall(strike, totals[strike], spot)


def build_profile(
    rows: Sequence[OiRow],
    spot: float,
    rate: float,
    *,
    band: float = DEFAULT_GAMMA_BAND,
    points: int = DEFAULT_GAMMA_POINTS,
    pin_band: float = DEFAULT_PIN_BAND,
    contract_size: int = CONTRACT_SIZE,
    min_strikes_with_oi: int = MIN_STRIKES_WITH_OI,
    min_priced_oi_share: float = MIN_PRICED_OI_SHARE,
    far_wall_pct: float = FAR_WALL_PCT,
) -> GexProfile:
    """Every level for one expiry selection, with the caveats that belong to them."""
    if not math.isfinite(spot) or spot <= 0.0:
        raise ValueError(f"spot must be a positive number, got {spot!r}")

    usable = [row for row in rows if math.isfinite(row.strike) and row.strike > 0.0]
    strikes = net_gex_by_strike(usable, spot, rate, contract_size=contract_size)
    call_wall, put_wall = walls(usable, spot)

    total_oi = sum(max(row.open_interest, 0) for row in usable)
    priced_oi = sum(max(row.open_interest, 0) for row in usable if row.priceable)
    total = total_gex_at(usable, spot, rate, contract_size=contract_size)
    flip = zero_gamma(usable, spot, rate, band=band, points=points, contract_size=contract_size)

    notes: list[str] = []
    strikes_with_oi = sum(1 for item in strikes if item.total_oi > 0)
    if strikes_with_oi == 0:
        notes.append("No open interest in this chain, so every level here is empty.")
    elif strikes_with_oi < min_strikes_with_oi:
        notes.append(
            f"Only {strikes_with_oi} strikes carry open interest. That is a handful of "
            "prints rather than a distribution, and a wall drawn from it is one trade."
        )

    if total_oi > 0 and priced_oi < total_oi * min_priced_oi_share:
        share = priced_oi / total_oi
        notes.append(
            f"Implied vol was missing for {(1 - share) * 100:.0f}% of the open interest, "
            "so the gamma curve and the zero-gamma level describe a minority of the "
            "contracts. The walls and max pain are counts and are unaffected."
        )

    far = [
        (name, wall)
        for name, wall in (("call wall", call_wall), ("put wall", put_wall))
        if wall is not None and abs(wall.distance_pct) > far_wall_pct
    ]
    for name, wall in far:
        notes.append(
            f"The {name} is at {wall.strike:g}, {abs(wall.distance_pct) * 100:.0f}% from "
            "spot. The heaviest strike on a side is often a deep wing held as a tail "
            "hedge rather than a level price will trade into, and at that distance it is "
            "off the chart below."
        )

    if total is None:
        notes.append(
            "No strike could be priced, so there is no gamma exposure to report. "
            "Open interest levels below still stand."
        )
    elif flip is None:
        direction = "positive" if total > 0 else "negative"
        notes.append(
            f"Total gamma exposure stays {direction} across the whole "
            f"{band * 100:.0f}% band, so there is no zero-gamma level to mark. The flip, "
            "if there is one, is further from spot than this chart looks."
        )

    return GexProfile(
        spot=spot,
        strikes=tuple(strikes),
        total_gex=total,
        call_wall=call_wall,
        put_wall=put_wall,
        pin=pin_candidate(usable, spot, band=pin_band),
        max_pain=max_pain(usable),
        zero_gamma=flip,
        regime=_regime(total, flip, spot),
        priced_oi=priced_oi,
        total_oi=total_oi,
        notes=tuple(notes),
    )


def _regime(total: float | None, flip: float | None, spot: float) -> Regime:
    """Which side of the flip spot sits on, read from the sign that is actually known.

    When a zero-gamma level was found, the comparison is spot against it. When it was
    not, the sign of total GEX at spot says the same thing without the level, and is
    the more direct measurement of the two: the level is an inference from the sign,
    not the other way round.
    """
    if total is None:
        return "unknown"
    if flip is not None:
        return "dampened" if spot > flip else "volatile"
    return "dampened" if total > 0 else "volatile"
