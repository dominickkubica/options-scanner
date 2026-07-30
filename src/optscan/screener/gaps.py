"""The mispricing hunt.

Looks for places where the surface disagrees with itself: a strike whose vol is far
off the fitted smile, a term structure inversion with no event to explain it, a put
call parity breach, a vertical whose credit does not match its own delta.

## Read this before trusting anything in here

Almost all apparent free money in an options chain is one of four things, and none of
them is free money:

1. A stale quote. The last update was an hour ago and the market has moved.
2. A spread you cannot fill inside. The mid is fiction; the fill is at the far side.
3. A hard to borrow underlying, where put call parity legitimately breaks because the
   arbitrage requires shorting stock nobody will lend you.
4. A dividend or an early exercise feature the parity calculation ignored.

So every flag produced here carries an executability assessment and a confidence, and
the module is built to talk you out of things. A gap that survives all four checks is
worth a look. A gap that does not is a data quality report.

This module finds candidates for human review. It does not find edge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from math import exp, log

from optscan.models import Right
from optscan.screener.config import GapsConfig
from optscan.screener.context import ExpiryAnalysis, SymbolAnalysis

#: Minimum strikes needed before a smile can be fitted at all.
MIN_SMILE_POINTS = 5

#: A term structure needs two points to be inverted.
MIN_TERM_POINTS = 2

#: Below this pivot the normal equations are singular and the fit is meaningless.
SINGULAR_PIVOT = 1e-12

#: Executability thresholds. Deliberately stricter than the screen's own liquidity
#: filter, because a gap is a claim about a price and the claim has to survive a fill.
GAP_MAX_SPREAD_PCT = 0.10
GAP_MIN_OPEN_INTEREST = 50

#: Verticals needed in an expiry before its own distribution can be a baseline.
MIN_VERTICALS_FOR_BASELINE = 20

#: Scales a median absolute deviation to a standard deviation for a normal.
MAD_TO_SIGMA = 1.4826

#: Log moneyness band the smile is fitted over. A quadratic describes a smile near the
#: money and not the wings, where vol turns up faster than any parabola.
SMILE_FIT_BAND = 0.20

#: Log moneyness band parity is checked over, measured against the implied forward
#: rather than spot. Tight, because parity only says anything where both legs are
#: close to at the money. A strike 25 points in the money puts real early exercise
#: value into the American put, which breaks European parity legitimately and in a
#: consistent direction, and no threshold can tell that apart from a dislocation.
PARITY_BAND = 0.02

#: Neither leg may be quoted wider than this fraction of its mid. A parity edge
#: smaller than the spread you cross to capture it is not an edge.
PARITY_MAX_LEG_SPREAD_PCT = 0.05

#: Strikes nearest spot used to solve for the implied forward, and the minimum that
#: must produce a value before the forward is trusted.
FORWARD_STRIKES = 8
MIN_FORWARD_STRIKES = 3


class GapKind(StrEnum):
    SKEW_ANOMALY = "skew_anomaly"
    TERM_INVERSION = "term_inversion"
    PARITY_VIOLATION = "parity_violation"
    CALENDAR_MISPRICING = "calendar_mispricing"
    VERTICAL_MISPRICING = "vertical_mispricing"


class Executability(StrEnum):
    """Whether a flagged gap could be acted on at all."""

    LOOKS_TRADEABLE = "looks_tradeable"
    WIDE_SPREAD = "wide_spread"
    STALE_QUOTE = "stale_quote"
    NO_TWO_SIDED_MARKET = "no_two_sided_market"
    THIN_INTEREST = "thin_interest"


@dataclass(frozen=True, slots=True)
class Gap:
    """Something that looks wrong, with the reasons it probably is not."""

    kind: GapKind
    symbol: str
    expiry: date
    description: str
    magnitude: float
    strike: float | None = None
    right: Right | None = None
    executability: Executability = Executability.LOOKS_TRADEABLE
    caveats: tuple[str, ...] = ()

    @property
    def actionable(self) -> bool:
        """Survived the executability checks. Still only means worth a look."""
        return self.executability is Executability.LOOKS_TRADEABLE


def fit_smile(strikes: list[float], vols: list[float]) -> tuple[float, float, float] | None:
    """Least squares quadratic in log moneyness. Returns coefficients, or None.

    A quadratic because a smile is curved and a line cannot represent skew and
    convexity at once, and because anything more flexible starts fitting the outliers
    this is meant to find.

    Solved with the normal equations rather than a library call, so the whole thing is
    inspectable and has no numpy shaped surprises on a five point chain.
    """
    if len(strikes) < MIN_SMILE_POINTS or len(strikes) != len(vols):
        return None

    n = len(strikes)
    sums = [0.0] * 5
    rhs = [0.0] * 3
    for x, y in zip(strikes, vols, strict=True):
        powers = [1.0, x, x * x, x**3, x**4]
        for index in range(5):
            sums[index] += powers[index]
        rhs[0] += y
        rhs[1] += y * x
        rhs[2] += y * x * x

    matrix = [
        [float(n), sums[1], sums[2]],
        [sums[1], sums[2], sums[3]],
        [sums[2], sums[3], sums[4]],
    ]
    return _solve3(matrix, rhs)


def _solve3(matrix: list[list[float]], rhs: list[float]) -> tuple[float, float, float] | None:
    """Gaussian elimination with partial pivoting on a 3x3. None if singular."""
    augmented = [[*row, rhs[index]] for index, row in enumerate(matrix)]

    for column in range(3):
        pivot_row = max(range(column, 3), key=lambda r: abs(augmented[r][column]))
        if abs(augmented[pivot_row][column]) < SINGULAR_PIVOT:
            return None
        augmented[column], augmented[pivot_row] = augmented[pivot_row], augmented[column]

        for row in range(column + 1, 3):
            factor = augmented[row][column] / augmented[column][column]
            for col in range(column, 4):
                augmented[row][col] -= factor * augmented[column][col]

    solution = [0.0, 0.0, 0.0]
    for row in reversed(range(3)):
        total = augmented[row][3]
        for col in range(row + 1, 3):
            total -= augmented[row][col] * solution[col]
        solution[row] = total / augmented[row][row]
    return solution[0], solution[1], solution[2]


def find_skew_anomalies(
    analysis: SymbolAnalysis,
    expiry: ExpiryAnalysis,
    config: GapsConfig,
) -> list[Gap]:
    """Strikes whose implied vol sits far off the fitted smile.

    Two things make this work at all, and both were learned by running it against a
    real chain and getting 963 hits on one symbol:

    The fit is restricted to a moneyness band around spot. A quadratic is a decent
    description of a smile near the money and a poor one across the whole listed range,
    where the wings turn up far faster than any parabola. Fitting the full range makes
    every wing strike look anomalous, which is a statement about the model rather than
    about the market.

    The threshold is a robust z score against the fit's own residual scale, not an
    absolute number of vol points. Three vol points off the smile is remarkable on a
    12 vol index chain and unremarkable on a 90 vol single name, and one fixed number
    cannot be right for both.
    """
    gaps: list[Gap] = []
    for right, skew in ((Right.PUT, expiry.put_skew), (Right.CALL, expiry.call_skew)):
        points = [
            point
            for point in skew.points
            if point.iv > 0 and abs(_log_moneyness(point.strike, analysis.spot)) <= SMILE_FIT_BAND
        ]
        if len(points) < MIN_SMILE_POINTS:
            continue

        moneyness = [_log_moneyness(p.strike, analysis.spot) for p in points]
        vols = [p.iv for p in points]
        fit = fit_smile(moneyness, vols)
        if fit is None:
            continue

        a, b, c = fit
        residuals = [
            point.iv - (a + b * x + c * x * x) for point, x in zip(points, moneyness, strict=True)
        ]
        scale = _median([abs(value) for value in residuals]) * MAD_TO_SIGMA
        if scale <= 0:
            continue

        for point, x, residual in zip(points, moneyness, residuals, strict=True):
            fitted = a + b * x + c * x * x
            z_score = abs(residual) / scale
            if z_score < config.min_skew_zscore or abs(residual) < config.min_skew_residual:
                continue

            contract = expiry.contract(point.strike, right)
            executability, caveats = assess_executability(contract, analysis, config)
            direction = "rich" if residual > 0 else "cheap"
            gaps.append(
                Gap(
                    kind=GapKind.SKEW_ANOMALY,
                    symbol=analysis.symbol,
                    expiry=expiry.expiry,
                    strike=point.strike,
                    right=right,
                    description=(
                        f"{point.strike:g}{right} implied vol {point.iv:.1%} is "
                        f"{abs(residual):.1%} {direction} against a fitted smile value of "
                        f"{fitted:.1%}, which is {z_score:.1f} deviations off this "
                        "expiry's own residual scale"
                    ),
                    magnitude=z_score,
                    executability=executability,
                    caveats=(
                        *caveats,
                        "the smile is fitted near the money only, so this says nothing "
                        "about the wings.",
                    ),
                )
            )
    return gaps


def find_term_inversions(analysis: SymbolAnalysis, config: GapsConfig) -> list[Gap]:
    """Front month vol above back month vol with no earnings date to explain it.

    An inversion with a known earnings date inside the front expiry is not a gap, it
    is the market working correctly, so it is not reported.
    """
    term = analysis.term
    if len(term.points) < MIN_TERM_POINTS:
        return []

    front, back = term.points[0], term.points[-1]
    inversion = front.iv - back.iv
    if inversion < config.min_term_inversion:
        return []

    if analysis.events.earnings_before(front.expiry, analysis.session_date):
        return []

    return [
        Gap(
            kind=GapKind.TERM_INVERSION,
            symbol=analysis.symbol,
            expiry=front.expiry,
            description=(
                f"front month {front.expiry} at {front.iv:.1%} is {inversion:.1%} above "
                f"{back.expiry} at {back.iv:.1%}, with no earnings date on file to "
                "explain it"
            ),
            magnitude=inversion,
            caveats=(
                "no earnings date on file is not the same as no event. "
                "Check for guidance, an FDA date, a lockup expiry, or an index change.",
            ),
        )
    ]


def find_parity_violations(
    analysis: SymbolAnalysis,
    expiry: ExpiryAnalysis,
    config: GapsConfig,
) -> list[Gap]:
    """Put call parity breaches measured at the price you would actually pay.

    Parity is C - P = S - K*exp(-rt) - D for European options, where D is the present
    value of dividends before expiry. The synthetic is built at the far side of both
    spreads, not at mids, because a violation that disappears once you cross the
    spread was never there.

    Parity is measured against the forward the chain itself implies, not against a
    forward computed from spot and an assumed carry. That distinction is the whole
    thing working or not working.

    The first version measured against spot * exp(rt) with a zero dividend yield and
    flagged 1043 strikes on one SPY chain. Every one of them was the same offset: the
    implied forward was 742.0 across every strike while the model forward was 742.45,
    and that 0.45 was SPY's dividend yield over 22 days. Parity was holding perfectly;
    the assumption about carry was wrong.

    Solving for the forward instead of assuming it removes the need to know the
    dividend, the borrow rate, or the exact discount rate. Whatever they are, the
    market has already priced them into every strike, and a genuine dislocation is a
    strike that disagrees with the others rather than one that disagrees with a guess.

    Only strikes near the money are checked. Deep in the money options have wide
    spreads and carry real early exercise value, and both look like breaches.
    """
    gaps: list[Gap] = []
    discount = exp(-analysis.rate * expiry.time)
    forward = implied_forward(analysis, expiry)
    if forward is None:
        return []

    for strike in expiry.strikes(Right.CALL):
        if abs(_log_moneyness(strike, forward)) > PARITY_BAND:
            continue

        call = expiry.contract(strike, Right.CALL)
        put = expiry.contract(strike, Right.PUT)
        if call is None or put is None:
            continue
        if not (call.has_two_sided_market and put.has_two_sided_market):
            continue
        if not _both_legs_tight(call, put):
            continue

        theoretical = (forward - strike) * discount

        # Buy the synthetic long: pay the call ask, receive the put bid.
        buy_synthetic = call.ask - put.bid
        # Sell the synthetic long: receive the call bid, pay the put ask.
        sell_synthetic = call.bid - put.ask

        if buy_synthetic < theoretical - config.min_parity_violation:
            edge = theoretical - buy_synthetic
            direction = "synthetic long is cheap against stock"
        elif sell_synthetic > theoretical + config.min_parity_violation:
            edge = sell_synthetic - theoretical
            direction = "synthetic long is rich against stock"
        else:
            continue

        executability, caveats = assess_executability(call, analysis, config)
        extra = [
            "parity assumes European exercise. These are American, and an early "
            "exercise feature is worth money that shows up here as a violation.",
            "a hard to borrow underlying breaks parity legitimately, because the "
            "arbitrage needs stock nobody will lend.",
            f"measured against an implied forward of {forward:.2f}, solved from the "
            "at the money strikes of this same expiry.",
        ]

        gaps.append(
            Gap(
                kind=GapKind.PARITY_VIOLATION,
                symbol=analysis.symbol,
                expiry=expiry.expiry,
                strike=strike,
                description=(
                    f"{strike:g} strike: {direction} by {edge:.2f} after crossing both spreads"
                ),
                magnitude=edge,
                executability=executability,
                caveats=(*caveats, *extra),
            )
        )
    return gaps


def find_vertical_mispricings(
    analysis: SymbolAnalysis,
    expiry: ExpiryAnalysis,
    config: GapsConfig,
) -> list[Gap]:
    """Verticals whose credit is out of line with the rest of their own chain.

    A spread's short delta implies roughly how often it finishes in the money, and
    credit over width is what the market charges for that. The gap between the two is
    almost always positive, because that gap is the variance risk premium: the market
    charges more for insurance than the model's probabilities say it should. That is
    the whole reason premium selling exists.

    So the absolute size of the gap is not a signal. Flagging it flagged 63 percent of
    a liquid SPY chain on the first real run, which is a description of the market
    rather than an anomaly in it.

    Measuring each vertical against the others in its own expiry and width was the
    next attempt. It does not work either, and the reason is worth recording rather
    than patching around. On a real SPY chain the excess runs smoothly from +0.002 far
    out of the money to +0.268 at the money, monotonically, with no scatter to speak
    of. It is not a distribution with outliers in it, it is a curve. Every near the
    money spread then reads as 20 or 40 deviations out, which is a statement about
    gamma, not about mispricing.

    Separating a real anomaly from the normal shape of that curve needs a baseline for
    what the curve usually looks like on this underlying, which means history this
    tool has only just started collecting. So the check is off by default, and turning
    it on gets you a ranked list of at the money spreads.

    Left in place rather than deleted because the measurement is right and only the
    baseline is missing, and Phase 8 is where the baseline comes from.
    """
    if not config.vertical_mispricing_enabled:
        return []

    gaps: list[Gap] = []
    for right in (Right.PUT, Right.CALL):
        by_width: dict[float, list[tuple[float, float, float, float, float]]] = {}
        for item in _measure_verticals(analysis, expiry, right):
            by_width.setdefault(item[5], []).append(item)

        for measured in by_width.values():
            gaps.extend(_flag_outliers(measured, analysis, expiry, right, config))
    return gaps


def _flag_outliers(
    measured: list[tuple[float, float, float, float, float, float]],
    analysis: SymbolAnalysis,
    expiry: ExpiryAnalysis,
    right: Right,
    config: GapsConfig,
) -> list[Gap]:
    """Flag verticals whose excess is an outlier among spreads of the same width.

    Stratified by width because a 1 wide spread near the money always pays a larger
    fraction of its width than a 10 wide does, for reasons of gamma rather than
    mispricing. Pooling them makes every narrow spread look anomalous.
    """
    gaps: list[Gap] = []
    if len(measured) >= MIN_VERTICALS_FOR_BASELINE:
        excesses = sorted(item[3] for item in measured)
        centre = _median(excesses)
        scale = _median([abs(value - centre) for value in excesses]) * MAD_TO_SIGMA
        if scale <= 0:
            return gaps

        for short_strike, target, credit_ratio, excess, delta_spread, _width in measured:
            z_score = (excess - centre) / scale
            if z_score < config.min_vertical_zscore:
                continue

            short = expiry.contract(short_strike, right)
            executability, caveats = assess_executability(short, analysis, config)
            gaps.append(
                Gap(
                    kind=GapKind.VERTICAL_MISPRICING,
                    symbol=analysis.symbol,
                    expiry=expiry.expiry,
                    strike=short_strike,
                    right=right,
                    description=(
                        f"{short_strike:g}/{target:g}{right} pays {credit_ratio:.0%} of width "
                        f"against a delta spread of {delta_spread:.0%}, which is "
                        f"{z_score:.1f} deviations above this chain's own median"
                    ),
                    magnitude=z_score,
                    executability=executability,
                    caveats=(
                        *caveats,
                        "measured against this chain only. A whole surface priced "
                        "unusually will not show up here.",
                        "delta implied probability is a model output, not a market "
                        "price. Disagreement can mean the model is wrong.",
                    ),
                )
            )
    return gaps


def _measure_verticals(
    analysis: SymbolAnalysis,
    expiry: ExpiryAnalysis,
    right: Right,
) -> list[tuple[float, float, float, float, float, float]]:
    """Every out of the money vertical, as
    (short, long, credit ratio, excess, delta spread, width)."""
    measured: list[tuple[float, float, float, float, float, float]] = []
    width_candidates = (1.0, 2.5, 5.0, 10.0)

    for short_strike in expiry.strikes(right):
        if right is Right.PUT and short_strike > analysis.spot:
            continue
        if right is Right.CALL and short_strike < analysis.spot:
            continue

        short = expiry.contract(short_strike, right)
        short_greeks = expiry.greeks(
            short_strike, right, analysis.spot, analysis.rate, analysis.dividend_yield
        )
        if short is None or short_greeks is None or short.mid is None:
            continue

        for width in width_candidates:
            target = short_strike - width if right is Right.PUT else short_strike + width
            wing = expiry.contract(target, right)
            wing_greeks = expiry.greeks(
                target, right, analysis.spot, analysis.rate, analysis.dividend_yield
            )
            if wing is None or wing_greeks is None or wing.mid is None:
                continue

            credit = short.mid - wing.mid
            if credit <= 0 or credit >= width:
                continue

            credit_ratio = credit / width
            delta_spread = abs(short_greeks.delta) - abs(wing_greeks.delta)
            if delta_spread <= 0:
                continue

            measured.append(
                (
                    short_strike,
                    target,
                    credit_ratio,
                    credit_ratio - delta_spread,
                    delta_spread,
                    width,
                )
            )
    return measured


def _both_legs_tight(call, put) -> bool:
    """Whether both legs are quoted tightly enough for a parity edge to survive a fill."""
    for contract in (call, put):
        spread_pct = contract.spread_pct_of_mid
        if spread_pct is None or spread_pct > PARITY_MAX_LEG_SPREAD_PCT:
            return False
    return True


def implied_forward(analysis: SymbolAnalysis, expiry: ExpiryAnalysis) -> float | None:
    """The forward price the chain itself implies, from put call parity at the money.

    Rearranging C - P = (F - K) * exp(-rt) gives F = K + (C - P) * exp(rt). Computed at
    the strikes nearest spot, where both legs have real time value and the tightest
    spreads, and taken as a median across them so one bad quote cannot move it.

    This is what makes the parity check independent of any assumption about dividends,
    borrow, or the exact discount rate. Whatever those are, they are already in every
    option price, and the forward is where they show up.
    """
    discount = exp(-analysis.rate * expiry.time)
    if discount <= 0:
        return None

    candidates: list[float] = []
    for strike in sorted(expiry.strikes(Right.CALL), key=lambda k: abs(k - analysis.spot))[
        :FORWARD_STRIKES
    ]:
        call = expiry.contract(strike, Right.CALL)
        put = expiry.contract(strike, Right.PUT)
        if call is None or put is None:
            continue
        if not (call.has_two_sided_market and put.has_two_sided_market):
            continue
        if call.mid is None or put.mid is None:
            continue
        candidates.append(strike + (call.mid - put.mid) / discount)

    if len(candidates) < MIN_FORWARD_STRIKES:
        return None
    return _median(candidates)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def assess_executability(
    contract,
    analysis: SymbolAnalysis,
    config: GapsConfig,
) -> tuple[Executability, tuple[str, ...]]:
    """Could this be traded at anything like the price that made it look interesting.

    Runs in the order that matters: a quote with no other side is not a market at all,
    a stale one is a fossil, and a wide one is a price you will not get.
    """
    if contract is None:
        return Executability.NO_TWO_SIDED_MARKET, ("no contract to trade",)

    if not contract.has_two_sided_market:
        return Executability.NO_TWO_SIDED_MARKET, (
            "no two sided market, so the mid is an average of nothing",
        )

    age_hours = contract.quote_age_seconds(analysis.asof)
    if age_hours is not None:
        age_hours /= 3600.0
        if age_hours > config.max_quote_age_hours:
            return Executability.STALE_QUOTE, (
                f"last trade was {age_hours:.1f} hours ago, so this is probably a "
                "stale mark rather than an opportunity",
            )

    spread_pct = contract.spread_pct_of_mid
    if spread_pct is not None and spread_pct > GAP_MAX_SPREAD_PCT:
        return Executability.WIDE_SPREAD, (
            f"spread is {spread_pct:.0%} of mid, so most of the apparent edge is the "
            "spread you would cross",
        )

    if contract.open_interest is not None and contract.open_interest < GAP_MIN_OPEN_INTEREST:
        return Executability.THIN_INTEREST, (
            f"only {contract.open_interest} open interest, so the quote may not survive "
            "contact with an order",
        )

    return Executability.LOOKS_TRADEABLE, ()


def find_gaps(analysis: SymbolAnalysis, config: GapsConfig) -> list[Gap]:
    """Every gap check, across every expiry. Sorted by magnitude, tradeable first."""
    if not config.enabled:
        return []

    gaps: list[Gap] = list(find_term_inversions(analysis, config))
    for expiry in analysis.expiries:
        gaps.extend(find_skew_anomalies(analysis, expiry, config))
        gaps.extend(find_parity_violations(analysis, expiry, config))
        gaps.extend(find_vertical_mispricings(analysis, expiry, config))

    return sorted(gaps, key=lambda gap: (not gap.actionable, -gap.magnitude))


def _log_moneyness(strike: float, spot: float) -> float:
    return log(strike / spot)
