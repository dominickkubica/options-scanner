"""Single leg short premium: cash secured puts and covered calls.

The two simplest positions and the two where the capital model does the most work.
A CSP ties up the full assignment cost; a covered call ties up shares you already own.
Reporting a return on either without saying which is how a 2 percent trade gets sold
as a 20 percent one.
"""

from __future__ import annotations

from optscan.analytics.returns import covered_call_profile, short_put_profile
from optscan.models import Action, Right, Strategy
from optscan.screener.config import ScreenConfig
from optscan.screener.context import ExpiryAnalysis, SymbolAnalysis
from optscan.screener.strategies.base import (
    Candidate,
    StrategyGenerator,
    build_leg,
    commissions_from,
    net_credit,
    short_leg_metrics,
    worst_liquidity,
)


class CashSecuredPut(StrategyGenerator):
    """Sell an out of the money put, hold the cash to buy the shares.

    Only strikes at or below spot are generated. An in the money short put is a
    different trade with a different intent, and mixing it into a list of premium
    selling candidates makes the list dishonest.
    """

    strategy = Strategy.CASH_SECURED_PUT

    def generate(
        self,
        analysis: SymbolAnalysis,
        expiry: ExpiryAnalysis,
        config: ScreenConfig,
    ) -> list[Candidate]:
        candidates: list[Candidate] = []
        for strike in expiry.strikes(Right.PUT):
            if strike > analysis.spot:
                continue
            contract = expiry.contract(strike, Right.PUT)
            if contract is None:
                continue

            leg = build_leg(contract, Action.SELL, expiry, analysis)
            credit = net_credit((leg,))
            if credit is None:
                continue

            pop, touch = short_leg_metrics(leg, credit, analysis, expiry)
            candidates.append(
                Candidate(
                    strategy=self.strategy,
                    legs=(leg,),
                    credit=credit,
                    profile=short_put_profile(strike, credit, expiry.dte, commissions_from(config)),
                    short_delta=leg.delta,
                    short_iv=leg.iv,
                    probability_of_profit=pop,
                    probability_of_touch=touch,
                    liquidity_score=worst_liquidity((leg,), analysis),
                )
            )
        return candidates


class CoveredCall(StrategyGenerator):
    """Sell an out of the money call against shares.

    Cost basis is assumed to be the current spot, which is the honest default when the
    tool does not know your basis. It makes max profit the premium plus the move to the
    strike from here, and it means the reported return is what a new position would
    make, not what an existing one would. Phase 7 knows real positions and can do better.
    """

    strategy = Strategy.COVERED_CALL

    def generate(
        self,
        analysis: SymbolAnalysis,
        expiry: ExpiryAnalysis,
        config: ScreenConfig,
    ) -> list[Candidate]:
        candidates: list[Candidate] = []
        for strike in expiry.strikes(Right.CALL):
            if strike < analysis.spot:
                continue
            contract = expiry.contract(strike, Right.CALL)
            if contract is None:
                continue

            leg = build_leg(contract, Action.SELL, expiry, analysis)
            credit = net_credit((leg,))
            if credit is None:
                continue

            pop, touch = short_leg_metrics(leg, credit, analysis, expiry)
            candidates.append(
                Candidate(
                    strategy=self.strategy,
                    legs=(leg,),
                    credit=credit,
                    profile=covered_call_profile(
                        strike, credit, analysis.spot, expiry.dte, commissions_from(config)
                    ),
                    short_delta=leg.delta,
                    short_iv=leg.iv,
                    probability_of_profit=pop,
                    probability_of_touch=touch,
                    liquidity_score=worst_liquidity((leg,), analysis),
                    notes=("cost basis assumed to be current spot",),
                )
            )
        return candidates
