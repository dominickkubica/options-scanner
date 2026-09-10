"""Composite scoring.

Turns a filtered candidate into a ranked Opportunity carrying the components that
produced its score.

## What this score is and is not

It is a sorting device for a list a human then reads. It is not a prediction, an
expected value, or a recommendation, and nothing here has been validated against
outcomes yet. Phase 8 exists to find out whether a high score does better than a low
one, and the honest possible answer is no.

The weights and the normalization ramps are the least defensible numbers in the
project. They encode opinions like "25 percent annualized is full marks" that nothing
outside the config file justifies. They live in config precisely so they can be
replaced by values that earned their place.

## Missing components

A component that cannot be computed is dropped and the remaining weights renormalized,
rather than scored zero. IV rank is the case that matters: before the snapshot history
is deep enough, every symbol would score zero on it, which would not change the
ranking but would compress every score toward the bottom and make the numbers look
like failures rather than like an unavailable input.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from optscan.analytics.ivrank import Confidence
from optscan.models import Opportunity, ScoreComponents
from optscan.screener.config import ScreenConfig
from optscan.screener.context import ExpiryAnalysis, SymbolAnalysis
from optscan.screener.filters import _event_risk
from optscan.screener.strategies.base import Candidate

#: Below this fraction of an expiry's contracts solving for a vol, the surface is thin
#: enough that every number derived from it deserves a warning.
THIN_SURFACE_SOLVE_RATE = 0.5


def ramp(value: float | None, floor: float, ceiling: float) -> float | None:
    """Linear 0 to 1 between floor and ceiling, clamped. None passes through."""
    if value is None:
        return None
    if ceiling == floor:
        raise ValueError("floor and ceiling must differ")
    return min(max((value - floor) / (ceiling - floor), 0.0), 1.0)


def log_ramp(value: float | None, floor: float, ceiling: float) -> float | None:
    """`ramp`, but across orders of magnitude.

    A linear ramp assumes the interesting variation is evenly spread across the range.
    For annualized return it is not: the population runs from 13% to 13,572%, so any
    linear ceiling low enough to distinguish the bottom decile pins everything above it
    at 1.0, and any ceiling high enough to reach the top squashes the entire middle into
    the first few percent of the scale. Neither ranks.

    On a log scale a tenfold difference is the same distance wherever it sits, which is
    the right shape for a return: 20% versus 200% is the same kind of gap as 200% versus
    2000%, and a linear scale says the second is ten times more important.

    Values at or below zero return 0.0 rather than raising. A negative annualized return
    is a real thing a candidate can have and it is simply the bottom of the scale.
    """
    if value is None:
        return None
    if ceiling <= floor:
        raise ValueError("floor must be below ceiling")
    if floor <= 0:
        raise ValueError("a log ramp needs a positive floor")
    if value <= 0:
        return 0.0
    span = math.log(ceiling) - math.log(floor)
    return min(max((math.log(value) - math.log(floor)) / span, 0.0), 1.0)


def score_premium(candidate: Candidate, config: ScreenConfig) -> float:
    """How well the position pays for the capital it uses, over the time it uses it.

    Annualized return on capital when there is a capital figure. Strangles have none,
    by design, so they fall back to credit against the underlying's price, which is a
    weaker measure and deliberately so: an undefined risk position should not outrank
    a defined one on a metric that ignores the undefined part.
    """
    norm = config.normalization
    annualized = candidate.profile.annualized_return
    if annualized is not None:
        return (
            log_ramp(annualized, norm.annualized_return_floor, norm.annualized_return_ceiling)
            or 0.0
        )

    strike = candidate.short_legs[0].strike if candidate.short_legs else None
    if strike:
        crude = (candidate.credit / strike) * (365 / max(candidate.profile.dte, 1))
        scaled = (
            log_ramp(crude, norm.annualized_return_floor, norm.annualized_return_ceiling) or 0.0
        )
        return scaled * 0.75  # discounted for having no honest capital denominator
    return 0.0


def score_probability(candidate: Candidate, config: ScreenConfig) -> float:
    """Probability of profit, ramped across the range worth distinguishing.

    Floor at 50 percent because below a coin flip a short premium position is not
    doing what it exists to do, and ceiling at 90 because past that the credit is
    usually too small for the tail risk and the extra probability stops being
    informative.
    """
    norm = config.normalization
    return (
        ramp(
            candidate.probability_of_profit,
            norm.probability_floor,
            norm.probability_ceiling,
        )
        or 0.0
    )


def score_iv_rank(analysis: SymbolAnalysis) -> float | None:
    """IV rank, discounted by how much history stands behind it.

    A rank from three months of observations is worth less than the same rank from
    two years, and multiplying by a confidence factor says so without throwing the
    signal away. Insufficient history returns None, which drops the component.
    """
    rank = analysis.iv_rank
    if rank is None or rank.rank is None:
        return None
    if rank.confidence is Confidence.INSUFFICIENT:
        return None

    discount = {
        Confidence.LOW: 0.5,
        Confidence.MEDIUM: 0.8,
        Confidence.HIGH: 1.0,
    }[rank.confidence]
    return rank.rank * discount


def score_event_risk(
    candidate: Candidate,
    analysis: SymbolAnalysis,
    expiry: ExpiryAnalysis,
) -> float:
    """1.0 for a clean window, less for anything scheduled inside it.

    Earnings is the heavy penalty: elevated IV before a report is priced, not
    mispriced, and selling it is a different trade from the one this tool screens for.
    Reaching here at all means the earnings filter was turned off, so the position is
    scored down rather than excluded.
    """
    risk = _event_risk(candidate, analysis, expiry)
    score = 1.0
    if risk.has_earnings:
        score -= 0.6
    if risk.early_assignment_risk:
        score -= 0.3
    if risk.has_ex_dividend:
        score -= 0.1
    return max(score, 0.0)


def composite(components: ScoreComponents, config: ScreenConfig) -> float:
    """Weighted average over the components that exist.

    Weights are renormalized across present components, so a missing IV rank shifts
    its weight onto the others instead of dragging every score down equally.
    """
    weights = config.weights.as_dict()
    values = components.as_dict()

    total_weight = 0.0
    total = 0.0
    for name, value in values.items():
        if value is None:
            continue
        weight = weights.get(name, 0.0)
        total += value * weight
        total_weight += weight

    if total_weight <= 0:
        return 0.0
    return min(max(total / total_weight, 0.0), 1.0)


def score_candidate(
    candidate: Candidate,
    analysis: SymbolAnalysis,
    expiry: ExpiryAnalysis,
    config: ScreenConfig,
) -> Opportunity:
    """Score a candidate and package it as an Opportunity."""
    risk = _event_risk(candidate, analysis, expiry)

    components = ScoreComponents(
        premium=score_premium(candidate, config),
        iv_rank=score_iv_rank(analysis),
        liquidity=candidate.liquidity_score if candidate.liquidity_score is not None else 0.0,
        probability=score_probability(candidate, config),
        event_risk=score_event_risk(candidate, analysis, expiry),
        fetched_at=analysis.asof,
        source=analysis.source,
    )

    warnings = list(candidate.notes)
    if analysis.iv_rank is not None and analysis.iv_rank.caveat():
        warnings.append(analysis.iv_rank.caveat())
    if risk.reasons:
        warnings.extend(risk.reasons)
    if analysis.term.is_backwardated():
        warnings.append("term structure is backwardated, which usually means a pending event")
    if expiry.solve_rate < THIN_SURFACE_SOLVE_RATE:
        warnings.append(
            f"only {expiry.solve_rate:.0%} of this expiry's contracts had a usable "
            "implied vol, so the surface here is thin"
        )

    rank = analysis.iv_rank
    return Opportunity(
        symbol=analysis.symbol,
        strategy=candidate.strategy,
        expiry=expiry.expiry,
        dte=expiry.dte,
        legs=candidate.legs,
        underlying_price=analysis.spot,
        credit=candidate.credit,
        max_profit=candidate.profile.max_profit,
        max_loss=candidate.profile.max_loss,
        capital=candidate.profile.capital,
        commission=candidate.profile.commission,
        return_on_capital=candidate.profile.return_on_capital,
        annualized_return=candidate.profile.annualized_return,
        probability_of_profit=candidate.probability_of_profit,
        probability_of_touch=candidate.probability_of_touch,
        short_delta=candidate.short_delta,
        net_delta=candidate.net_delta,
        iv=candidate.short_iv,
        iv_rank=rank.rank if rank else None,
        iv_percentile=rank.percentile if rank else None,
        iv_confidence=str(rank.confidence) if rank else None,
        liquidity_score=candidate.liquidity_score,
        has_earnings=risk.has_earnings,
        early_assignment_risk=risk.early_assignment_risk,
        warnings=tuple(warnings),
        score=composite(components, config),
        components=components,
        fetched_at=analysis.asof,
        source=analysis.source,
    )


def comparable_components(opportunities: Sequence[Opportunity]) -> set[str]:
    """The component names every one of these opportunities could compute.

    The intersection, not the union. See `harmonize_scores` for why that direction.
    """
    available: set[str] | None = None
    for opportunity in opportunities:
        present = {
            name for name, value in opportunity.components.as_dict().items() if value is not None
        }
        available = present if available is None else (available & present)
    return available or set()


def harmonize_scores(
    opportunities: Sequence[Opportunity], config: ScreenConfig
) -> list[Opportunity]:
    """Rescore a set that will be ranked against each other, on components they share.

    ## The bug this exists to prevent

    `composite` drops a component it cannot compute and renormalizes the remaining
    weights. Within one symbol that is right: it stops an unavailable input from
    dragging every score toward zero and reordering nothing.

    Across symbols it is badly wrong, and it became wrong the moment a downloaded vol
    history existed for some tickers and not others. Renormalizing away a missing
    component silently replaces it with the *average of that candidate's other
    components*, which for a candidate the screen already likes is a high number. So a
    symbol with no history is scored as though its IV rank were excellent, and a
    symbol with a real, honest, low IV rank is scored on the truth.

    Measured on the live config against an otherwise identical candidate: the break
    even IV rank is 0.939. Below that, having imported a vol history *lowers* a
    symbol's score. QQQ's real rank of 0.18 cost it 0.152 of composite, enough to drop
    it out of the Best plays list entirely, for the sole reason that its data exists.

    That is this project's recurring bug in a new place, the eleventh instance: a
    structural difference, here which components happen to be computable, read as a
    difference in quality.

    ## The rule

    A ranking may only use what every candidate in it has. The intersection is
    therefore the right direction, even though it throws away real information about
    the symbols that have more: a comparison is only as good as its weakest common
    ground, and ranking on a component half the field is missing is not a comparison
    at all. Importing history for the rest of the watchlist is what widens it back,
    and that is a data problem with an obvious fix rather than a scoring one.

    Scores computed for a single symbol are untouched. This is only for a list that
    will be sorted against itself.
    """
    if not opportunities:
        return list(opportunities)

    shared = comparable_components(opportunities)
    weights = config.weights.as_dict()
    dropped = sorted(name for name in weights if name not in shared)
    if not dropped:
        return list(opportunities)

    note = (
        "Ranked without "
        + ", ".join(dropped)
        + ": not every symbol compared here could compute it, and a ranking may only "
        "use what all of its candidates have."
    )

    rescored = []
    for opportunity in opportunities:
        values = opportunity.components.as_dict()
        total = sum(values[name] * weights[name] for name in shared)
        weight = sum(weights[name] for name in shared)
        score = min(max(total / weight, 0.0), 1.0) if weight > 0 else 0.0
        rescored.append(
            opportunity.model_copy(
                update={
                    "score": score,
                    "warnings": (*opportunity.warnings, note),
                }
            )
        )
    return rescored
