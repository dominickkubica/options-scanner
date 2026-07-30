"""Multi leg positions: vertical credit spreads, iron condors, short strangles.

The defined risk shapes are where credit-to-width matters, and where a candidate is
only as real as its long wing: a beautiful short strike paired with an unquotable
protective leg is not a trade, and the liquidity score takes the worse of the two for
exactly that reason.
"""

from __future__ import annotations

from optscan.analytics.probability import probability_of_profit
from optscan.analytics.returns import (
    ReturnProfile,
    credit_spread_profile,
    iron_condor_profile,
)
from optscan.models import Action, Leg, Right, Strategy
from optscan.screener.config import ScreenConfig
from optscan.screener.context import ExpiryAnalysis, SymbolAnalysis
from optscan.screener.strategies.base import (
    CONTRACT_SIZE_NOTE,
    Candidate,
    StrategyGenerator,
    build_leg,
    commissions_from,
    net_credit,
    pick_wing,
    short_leg_metrics,
    worst_liquidity,
)

#: A two sided position needs both shorts before the combined maths applies.
MIN_TWO_SIDED_LEGS = 2


class VerticalCreditSpread(StrategyGenerator):
    """Sell a strike, buy a further one for protection.

    Generated for every configured width. A width that is not listed produces nothing
    rather than the nearest available, because substituting a 4 wide for a 5 wide
    silently changes max loss by a quarter.
    """

    def __init__(self, right: Right) -> None:
        self.right = right
        self.strategy = (
            Strategy.PUT_CREDIT_SPREAD if right is Right.PUT else Strategy.CALL_CREDIT_SPREAD
        )

    def generate(
        self,
        analysis: SymbolAnalysis,
        expiry: ExpiryAnalysis,
        config: ScreenConfig,
    ) -> list[Candidate]:
        candidates: list[Candidate] = []
        for short_strike in expiry.strikes(self.right):
            if self.right is Right.PUT and short_strike > analysis.spot:
                continue
            if self.right is Right.CALL and short_strike < analysis.spot:
                continue

            short_contract = expiry.contract(short_strike, self.right)
            if short_contract is None:
                continue
            short_leg = build_leg(short_contract, Action.SELL, expiry, analysis)

            for width in config.strategies.spread_widths:
                long_contract = pick_wing(expiry, short_strike, width, self.right)
                if long_contract is None:
                    continue
                long_leg = build_leg(long_contract, Action.BUY, expiry, analysis)

                legs = (short_leg, long_leg)
                credit = net_credit(legs)
                if credit is None or credit >= width:
                    # Credit at or above width is arbitrage on paper and a stale quote
                    # in practice. The capital model rejects it outright.
                    continue

                pop, touch = short_leg_metrics(short_leg, credit, analysis, expiry)
                candidates.append(
                    Candidate(
                        strategy=self.strategy,
                        legs=legs,
                        credit=credit,
                        profile=credit_spread_profile(
                            short_strike,
                            long_contract.strike,
                            credit,
                            expiry.dte,
                            commissions_from(config),
                        ),
                        short_delta=short_leg.delta,
                        short_iv=short_leg.iv,
                        probability_of_profit=pop,
                        probability_of_touch=touch,
                        liquidity_score=worst_liquidity(legs, analysis),
                        width=width,
                    )
                )
        return candidates


class IronCondor(StrategyGenerator):
    """A put credit spread and a call credit spread at the same expiry.

    Built by pairing short strikes at roughly matching absolute delta, so the position
    starts near delta neutral rather than leaning one way by accident. Only one side
    can finish in the money, which is why capital is the wider wing and not the sum.
    """

    strategy = Strategy.IRON_CONDOR

    def generate(
        self,
        analysis: SymbolAnalysis,
        expiry: ExpiryAnalysis,
        config: ScreenConfig,
    ) -> list[Candidate]:
        band = config.filters.delta
        put_shorts = _shorts_in_delta_band(
            expiry, analysis, Right.PUT, band.min_abs_delta, band.max_abs_delta
        )
        call_shorts = _shorts_in_delta_band(
            expiry, analysis, Right.CALL, band.min_abs_delta, band.max_abs_delta
        )
        if not put_shorts or not call_shorts:
            return []

        candidates: list[Candidate] = []
        for put_leg in put_shorts:
            call_leg = _closest_by_delta(call_shorts, abs(put_leg.delta or 0.0))
            if call_leg is None:
                continue

            for width in config.strategies.spread_widths:
                put_wing = pick_wing(expiry, put_leg.strike, width, Right.PUT)
                call_wing = pick_wing(expiry, call_leg.strike, width, Right.CALL)
                if put_wing is None or call_wing is None:
                    continue

                legs = (
                    put_leg,
                    build_leg(put_wing, Action.BUY, expiry, analysis),
                    call_leg,
                    build_leg(call_wing, Action.BUY, expiry, analysis),
                )
                credit = net_credit(legs)
                if credit is None or credit >= width:
                    continue

                candidates.append(
                    Candidate(
                        strategy=self.strategy,
                        legs=legs,
                        credit=credit,
                        profile=iron_condor_profile(
                            put_leg.strike,
                            put_wing.strike,
                            call_leg.strike,
                            call_wing.strike,
                            credit,
                            expiry.dte,
                            commissions_from(config),
                        ),
                        short_delta=_net_delta(legs),
                        short_iv=_average_short_iv(legs),
                        probability_of_profit=_two_sided_pop(legs, credit, analysis, expiry),
                        probability_of_touch=_worse_touch(legs, credit, analysis, expiry),
                        liquidity_score=worst_liquidity(legs, analysis),
                        width=width,
                    )
                )
        return candidates


class ShortStrangle(StrategyGenerator):
    """Sell a put and a call, both out of the money, with no protection.

    Undefined risk on both sides. Max loss is reported as None rather than as a large
    number, because there is not one, and the return metrics that need a denominator
    come back as None too. That makes strangles rank below everything with a real
    return figure, which is the correct default for a tool that cannot see your account.
    """

    strategy = Strategy.SHORT_STRANGLE

    def generate(
        self,
        analysis: SymbolAnalysis,
        expiry: ExpiryAnalysis,
        config: ScreenConfig,
    ) -> list[Candidate]:
        band = config.filters.delta
        put_shorts = _shorts_in_delta_band(
            expiry, analysis, Right.PUT, band.min_abs_delta, band.max_abs_delta
        )
        call_shorts = _shorts_in_delta_band(
            expiry, analysis, Right.CALL, band.min_abs_delta, band.max_abs_delta
        )
        if not put_shorts or not call_shorts:
            return []

        candidates: list[Candidate] = []
        for put_leg in put_shorts:
            call_leg = _closest_by_delta(call_shorts, abs(put_leg.delta or 0.0))
            if call_leg is None:
                continue

            legs = (put_leg, call_leg)
            credit = net_credit(legs)
            if credit is None:
                continue

            candidates.append(
                Candidate(
                    strategy=self.strategy,
                    legs=legs,
                    credit=credit,
                    profile=ReturnProfile(
                        credit=credit,
                        max_profit=credit * 100 - commissions_from(config).cost(legs=2),
                        max_loss=None,
                        capital=None,
                        dte=expiry.dte,
                        commission=commissions_from(config).cost(legs=2),
                    ),
                    short_delta=_net_delta(legs),
                    short_iv=_average_short_iv(legs),
                    probability_of_profit=_two_sided_pop(legs, credit, analysis, expiry),
                    probability_of_touch=_worse_touch(legs, credit, analysis, expiry),
                    liquidity_score=worst_liquidity(legs, analysis),
                    notes=(
                        "undefined risk on both sides, so there is no capital figure "
                        "and no return on it",
                        CONTRACT_SIZE_NOTE,
                    ),
                )
            )
        return candidates


def _shorts_in_delta_band(
    expiry: ExpiryAnalysis,
    analysis: SymbolAnalysis,
    right: Right,
    min_abs: float,
    max_abs: float,
) -> list[Leg]:
    """Short legs whose delta sits inside the configured band, out of the money only."""
    legs: list[Leg] = []
    for strike in expiry.strikes(right):
        if right is Right.PUT and strike > analysis.spot:
            continue
        if right is Right.CALL and strike < analysis.spot:
            continue
        contract = expiry.contract(strike, right)
        if contract is None:
            continue
        leg = build_leg(contract, Action.SELL, expiry, analysis)
        if leg.delta is None or not (min_abs <= abs(leg.delta) <= max_abs):
            continue
        legs.append(leg)
    return legs


def _closest_by_delta(legs: list[Leg], target_abs_delta: float) -> Leg | None:
    """The leg whose absolute delta is nearest the target, for a balanced position."""
    usable = [leg for leg in legs if leg.delta is not None]
    if not usable:
        return None
    return min(usable, key=lambda leg: abs(abs(leg.delta) - target_abs_delta))


def _net_delta(legs: tuple[Leg, ...]) -> float | None:
    """Position delta, short legs counted against you."""
    deltas = [leg.delta for leg in legs if leg.delta is not None]
    if not deltas:
        return None
    return sum(-leg.delta if leg.is_short else leg.delta for leg in legs if leg.delta is not None)


def _average_short_iv(legs: tuple[Leg, ...]) -> float | None:
    vols = [leg.iv for leg in legs if leg.is_short and leg.iv is not None]
    return sum(vols) / len(vols) if vols else None


def _two_sided_pop(
    legs: tuple[Leg, ...],
    credit: float,
    analysis: SymbolAnalysis,
    expiry: ExpiryAnalysis,
) -> float | None:
    """Probability both short strikes finish inside their breakevens.

    Computed as one minus the sum of the two tail probabilities. That is exact here,
    because the two breach events are mutually exclusive: the underlying cannot finish
    both below the put breakeven and above the call breakeven.

    The credit is applied to both sides, which is how a real condor works: the whole
    credit widens both breakevens, since only one side can be breached at expiry.
    """
    shorts = [leg for leg in legs if leg.is_short and leg.iv is not None]
    if len(shorts) < MIN_TWO_SIDED_LEGS:
        return None

    inside = 1.0
    for leg in shorts:
        pop = probability_of_profit(
            leg.right,
            leg.strike,
            credit,
            analysis.spot,
            expiry.time,
            leg.iv,
            analysis.rate,
            analysis.dividend_yield,
        )
        inside -= 1.0 - pop
    return max(min(inside, 1.0), 0.0)


def _worse_touch(
    legs: tuple[Leg, ...],
    credit: float,
    analysis: SymbolAnalysis,
    expiry: ExpiryAnalysis,
) -> float | None:
    """The higher of the two sides' touch probabilities.

    Not the combined probability of touching either side. The number a trader wants is
    how likely the position is to be tested, and the nearer strike dominates that.
    """
    touches = []
    for leg in legs:
        if not leg.is_short or leg.iv is None:
            continue
        _, touch = short_leg_metrics(leg, credit, analysis, expiry)
        if touch is not None:
            touches.append(touch)
    return max(touches) if touches else None
