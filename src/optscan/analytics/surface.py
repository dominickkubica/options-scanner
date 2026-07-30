"""The volatility surface: term structure across expiries and skew across strikes.

Both exist to answer the same question from different angles: is the vol I am being
offered high because the whole surface is elevated, or because this particular point
on it is? Selling the first is a volatility trade. Selling the second is usually
selling insurance against something specific that is about to happen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from itertools import pairwise

from optscan.analytics.greeks import DAYS_PER_YEAR, bsm_greeks
from optscan.models import Right

#: Front vol has to exceed back vol by more than this to count as backwardation
#: rather than noise in two separately solved numbers.
DEFAULT_BACKWARDATION_THRESHOLD = 0.02

#: The conventional wing for skew measurement.
DEFAULT_SKEW_DELTA = 0.25

#: A slope needs two points.
MIN_POINTS_FOR_SLOPE = 2

#: Expiries inside this many days are excluded from term structure comparisons.
#:
#: Very short dated ATM implied vol is mechanically elevated. As time to expiry goes
#: to zero the diffusive part of the move shrinks with sqrt(t) while the jump part
#: does not, so the vol needed to explain the option's price rises. On a real SPY
#: chain the 0DTE ATM vol was 26.9 percent against 11.4 percent four days out, with
#: nothing whatsoever happening.
#:
#: Comparing that against a back month makes every index look permanently
#: backwardated, which is a fact about the front of the curve rather than a signal.
#: A week is enough distance for the effect to fade.
SHORT_DATED_DTE = 7


@dataclass(frozen=True, slots=True)
class TermPoint:
    """ATM implied vol at one expiry."""

    expiry: date
    dte: int
    iv: float

    @property
    def years(self) -> float:
        return self.dte / DAYS_PER_YEAR


@dataclass(frozen=True, slots=True)
class TermStructure:
    """ATM vol by expiry, front to back."""

    points: tuple[TermPoint, ...]

    @property
    def front(self) -> TermPoint | None:
        return self.points[0] if self.points else None

    @property
    def back(self) -> TermPoint | None:
        return self.points[-1] if self.points else None

    def comparable_points(self, min_dte: int = SHORT_DATED_DTE) -> tuple[TermPoint, ...]:
        """Points far enough out to be compared against each other.

        See SHORT_DATED_DTE for why the front of the curve is excluded.
        """
        return tuple(point for point in self.points if point.dte >= min_dte)

    def slope(self, min_dte: int = SHORT_DATED_DTE) -> float | None:
        """Back vol minus front vol, ignoring the very short dated end.

        Negative is backwardation. Pass min_dte=0 for the raw slope across everything,
        which is the right thing when the question really is about the front week.
        """
        points = self.comparable_points(min_dte)
        if len(points) < MIN_POINTS_FOR_SLOPE:
            return None
        return points[-1].iv - points[0].iv

    def is_backwardated(
        self,
        threshold: float = DEFAULT_BACKWARDATION_THRESHOLD,
        min_dte: int = SHORT_DATED_DTE,
    ) -> bool:
        """Front vol meaningfully above back vol, measured past the short dated end.

        Normal term structure is upward sloping: more time, more uncertainty, more
        vol. An inversion means the market expects something to happen soon and then
        for things to calm down, which is almost always a scheduled event. Selling the
        rich front month without knowing what the event is means selling insurance
        against a specific thing you have not looked up.

        Measured from a week out, because otherwise the structural elevation of very
        short dated vol reports every index as permanently inverted. On a real six
        symbol watchlist that change took the flag from firing on five of six to
        firing on the two with earnings that week, which is what it is for.
        """
        slope = self.slope(min_dte)
        return slope is not None and slope < -threshold

    def iv_at_dte(self, dte: int) -> float | None:
        """ATM vol interpolated to an arbitrary horizon.

        Linear in variance rather than in vol, because variance is what adds across
        time. Interpolating vol directly is the common shortcut and it sags in the
        middle of a steep curve.
        """
        if not self.points:
            return None
        if len(self.points) == 1:
            return self.points[0].iv

        ordered = sorted(self.points, key=lambda p: p.dte)
        if dte <= ordered[0].dte:
            return ordered[0].iv
        if dte >= ordered[-1].dte:
            return ordered[-1].iv

        for near, far in pairwise(ordered):
            if near.dte <= dte <= far.dte:
                if far.dte == near.dte:
                    return near.iv
                near_var = near.iv**2 * near.dte
                far_var = far.iv**2 * far.dte
                weight = (dte - near.dte) / (far.dte - near.dte)
                variance = near_var * (1 - weight) + far_var * weight
                return (variance / dte) ** 0.5
        return None


def build_term_structure(atm_vols: dict[date, float], asof: date) -> TermStructure:
    """Term structure from a mapping of expiry to ATM vol, dropping unusable points."""
    points = [
        TermPoint(expiry=expiry, dte=(expiry - asof).days, iv=iv)
        for expiry, iv in sorted(atm_vols.items())
        if iv is not None and iv > 0 and (expiry - asof).days >= 0
    ]
    return TermStructure(tuple(points))


@dataclass(frozen=True, slots=True)
class SkewPoint:
    """One strike's implied vol and the delta that goes with it."""

    strike: float
    iv: float
    delta: float

    @property
    def abs_delta(self) -> float:
        return abs(self.delta)


@dataclass(frozen=True, slots=True)
class Skew:
    """Implied vol across strikes for one expiry and one right."""

    right: Right
    points: tuple[SkewPoint, ...]

    def iv_at_delta(self, target: float = DEFAULT_SKEW_DELTA) -> float | None:
        """IV at a given absolute delta, linearly interpolated between listed strikes.

        Interpolated because the 25 delta strike is almost never listed exactly, and
        snapping to the nearest one can be 10 delta away on a wide strike ladder.
        """
        usable = sorted(self.points, key=lambda p: p.abs_delta)
        if not usable:
            return None
        if len(usable) == 1:
            return usable[0].iv

        target = abs(target)
        if target <= usable[0].abs_delta:
            return usable[0].iv
        if target >= usable[-1].abs_delta:
            return usable[-1].iv

        for low, high in pairwise(usable):
            if low.abs_delta <= target <= high.abs_delta:
                span = high.abs_delta - low.abs_delta
                if span == 0:
                    return low.iv
                weight = (target - low.abs_delta) / span
                return low.iv * (1 - weight) + high.iv * weight
        return None


def build_skew(
    right: Right | str,
    strike_vols: dict[float, float],
    spot: float,
    time: float,
    rate: float,
    dividend_yield: float = 0.0,
) -> Skew:
    """Attach a delta to each strike, computed at that strike's own implied vol.

    At its own vol, not at the ATM vol. Using one vol for the whole chain is what
    produces the wrong delta on the wings, which is where skew lives and therefore
    where the error matters most.
    """
    option = Right.parse(right)
    points = []
    for strike, iv in sorted(strike_vols.items()):
        if iv is None or iv <= 0 or strike <= 0:
            continue
        greeks = bsm_greeks(option, spot, strike, time, rate, iv, dividend_yield)
        points.append(SkewPoint(strike=strike, iv=iv, delta=greeks.delta))
    return Skew(right=option, points=tuple(points))


def risk_reversal(
    put_wing_iv: float | None,
    call_wing_iv: float | None,
) -> float | None:
    """Put wing vol minus call wing vol at the same delta.

    Positive is normal for equity indices: puts are bid because crashes happen faster
    than rallies. An unusually large value means downside protection is expensive,
    which is good for selling puts and also a signal that the market is nervous, and
    those two readings point in opposite directions.
    """
    if put_wing_iv is None or call_wing_iv is None:
        return None
    return put_wing_iv - call_wing_iv


def butterfly(
    put_wing_iv: float | None,
    call_wing_iv: float | None,
    atm_iv: float | None,
) -> float | None:
    """Average wing vol minus ATM vol. How much the smile curves.

    A high value means the tails are priced richer than the body, so the wings are
    where the premium is, and also where a gap does the most damage.
    """
    if put_wing_iv is None or call_wing_iv is None or atm_iv is None:
        return None
    return (put_wing_iv + call_wing_iv) / 2.0 - atm_iv


@dataclass(frozen=True, slots=True)
class SkewSummary:
    """The three numbers that describe a smile."""

    atm_iv: float | None
    put_wing_iv: float | None
    call_wing_iv: float | None
    wing_delta: float

    @property
    def risk_reversal(self) -> float | None:
        return risk_reversal(self.put_wing_iv, self.call_wing_iv)

    @property
    def butterfly(self) -> float | None:
        return butterfly(self.put_wing_iv, self.call_wing_iv, self.atm_iv)


def summarize_skew(
    put_skew: Skew,
    call_skew: Skew,
    atm_iv: float | None,
    wing_delta: float = DEFAULT_SKEW_DELTA,
) -> SkewSummary:
    """Risk reversal and butterfly at the conventional wing delta."""
    return SkewSummary(
        atm_iv=atm_iv,
        put_wing_iv=put_skew.iv_at_delta(wing_delta),
        call_wing_iv=call_skew.iv_at_delta(wing_delta),
        wing_delta=wing_delta,
    )
