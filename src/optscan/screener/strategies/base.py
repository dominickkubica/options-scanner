"""Strategy interface and the shared leg building helpers.

A strategy turns a solved expiry into candidate positions. It does not filter, score,
or rank: it enumerates what could be sold, and the rest of the pipeline decides which
of those are worth showing. Keeping generation separate from filtering means a filter
change never silently changes what was considered.

Candidates come out as `Candidate`, a plain shape with the economics attached but no
score. Scoring turns them into `Opportunity`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from optscan.analytics.probability import probability_of_profit, touch_and_finish
from optscan.analytics.returns import Commissions, ReturnProfile
from optscan.models import Action, Leg, OptionContract, Right, Strategy
from optscan.screener.config import ScreenConfig
from optscan.screener.context import ExpiryAnalysis, SymbolAnalysis

#: Attached to candidates whose numbers are easy to misread at a glance.
CONTRACT_SIZE_NOTE = "figures are per contract, which is 100 shares"


def commissions_from(config: ScreenConfig) -> Commissions:
    """The configured broker cost model."""
    return Commissions(
        per_contract=config.costs.per_contract,
        per_trade=config.costs.per_trade,
        assume_closing_trade=config.costs.assume_closing_trade,
    )


@dataclass(frozen=True, slots=True)
class Candidate:
    """A position that could be opened, priced but not yet judged."""

    strategy: Strategy
    legs: tuple[Leg, ...]
    credit: float
    profile: ReturnProfile
    #: Delta of the short leg nearest to being tested, as an absolute value. On a two
    #: sided structure this is deliberately not the net: see `_tested_delta`.
    short_delta: float | None
    short_iv: float | None
    probability_of_profit: float | None
    probability_of_touch: float | None
    liquidity_score: float | None
    width: float | None = None
    #: Which way the whole position leans. None for single sided structures, where it
    #: would only repeat `short_delta` with a sign.
    net_delta: float | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def expiry(self):
        return self.legs[0].expiry

    @property
    def short_legs(self) -> tuple[Leg, ...]:
        return tuple(leg for leg in self.legs if leg.is_short)


class StrategyGenerator(ABC):
    """Enumerates candidate positions for one expiry."""

    strategy: Strategy

    @abstractmethod
    def generate(
        self,
        analysis: SymbolAnalysis,
        expiry: ExpiryAnalysis,
        config: ScreenConfig,
    ) -> list[Candidate]:
        """Every candidate this strategy can build at this expiry.

        Returning a lot is fine. The filter layer is what narrows it, and a candidate
        that was never generated cannot be explained away later.
        """


def build_leg(
    contract: OptionContract,
    action: Action,
    expiry_analysis: ExpiryAnalysis,
    analysis: SymbolAnalysis,
) -> Leg:
    """A leg carrying the quote, vol, and greek it was evaluated against.

    Snapshotting these onto the leg rather than recomputing later means a candidate
    stays reproducible: the numbers that justified it are the numbers stored with it.
    """
    greeks = expiry_analysis.greeks(
        contract.strike,
        contract.right,
        analysis.spot,
        analysis.rate,
        analysis.dividend_yield,
    )
    return Leg(
        action=action,
        right=contract.right,
        strike=contract.strike,
        expiry=contract.expiry,
        contract_symbol=contract.contract_symbol,
        bid=contract.bid,
        ask=contract.ask,
        mid=contract.mid,
        iv=expiry_analysis.vol(contract.strike, contract.right),
        delta=greeks.delta if greeks else None,
        open_interest=contract.open_interest,
        volume=contract.volume,
        fetched_at=contract.fetched_at,
        source=contract.source,
    )


def net_credit(legs: tuple[Leg, ...]) -> float | None:
    """Credit taken in across every leg, or None if any leg cannot be priced.

    Uses mids. A real fill lands between mid and the far side, and a candidate priced
    at mid is therefore optimistic by roughly half the spread on each leg. That is why
    the spread filter is tight and why liquidity carries a quarter of the score.
    """
    total = 0.0
    for leg in legs:
        signed = leg.signed_mid
        if signed is None:
            return None
        total += signed
    return total if total > 0 else None


def short_leg_metrics(
    short: Leg,
    credit: float,
    analysis: SymbolAnalysis,
    expiry: ExpiryAnalysis,
) -> tuple[float | None, float | None]:
    """Probability of profit and probability of touch for a single short strike."""
    if short.iv is None:
        return None, None
    pop = probability_of_profit(
        short.right,
        short.strike,
        credit,
        analysis.spot,
        expiry.time,
        short.iv,
        analysis.rate,
        analysis.dividend_yield,
    )
    touch = touch_and_finish(
        short.right,
        short.strike,
        analysis.spot,
        expiry.time,
        short.iv,
        analysis.rate,
        analysis.dividend_yield,
    ).touch
    return pop, touch


def worst_liquidity(legs: tuple[Leg, ...], analysis: SymbolAnalysis) -> float | None:
    """The weakest leg's liquidity score.

    The weakest rather than the average, because a position is only as fillable as its
    hardest leg. Averaging lets a liquid short strike hide an untradeable long wing.
    """
    scores = [analysis.liquidity_for(leg.expiry, leg.strike, leg.right) for leg in legs]
    present = [score for score in scores if score is not None]
    return min(present) if present else None


def pick_wing(
    expiry: ExpiryAnalysis,
    short_strike: float,
    width: float,
    right: Right,
) -> OptionContract | None:
    """The long leg `width` points away from the short strike, on the correct side.

    Returns None rather than the nearest available strike when the exact width is not
    listed. Silently substituting a 4 wide for a 5 wide would change max loss by 25
    percent without changing what the row says it is.
    """
    target = short_strike - width if right is Right.PUT else short_strike + width
    contract = expiry.contract(target, right)
    if contract is None:
        return None
    return contract
