"""Marking held positions against a solved chain.

Lives in screener/ rather than analytics/ because it needs a `SymbolAnalysis`, and
analytics must not import the screener. The layering is worth keeping: everything in
analytics/ is callable with plain numbers, and the moment one of those modules needs a
solved snapshot it stops being unit testable without building one.

## Marking a short position uses the ask, not the mid

The mid is what the position is theoretically worth. The ask is what it costs to close,
and closing is the only way an open profit becomes a real one. Marking a short book at
the mid overstates every position by half the spread, and on the wide contracts premium
sellers live in that is most of the last 20 percent of the credit, which is exactly the
range where a profit target fires.

So the default is the conservative side of the market for whichever direction closes
the position, and `mark_position` says which convention it used. The mid is available
for anyone who wants it and it is not the default.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from enum import StrEnum

from optscan.analytics.portfolio import Beta, LegRisk, PositionRisk
from optscan.models import OptionContract, Right
from optscan.models.opportunity import Action
from optscan.models.position import Position, PositionLeg
from optscan.screener.context import SymbolAnalysis


class MarkConvention(StrEnum):
    """Which side of the market a held position is valued at."""

    #: What it would cost to close. A short leg marks at the ask, a long one at the bid.
    CLOSING = "closing"
    #: Midpoint. Optimistic for a short book by half the spread on every leg.
    MID = "mid"


def leg_mark(
    contract: OptionContract | None,
    action: Action,
    convention: MarkConvention = MarkConvention.CLOSING,
) -> float | None:
    """Price one leg, or None when the market cannot support a mark.

    None rather than falling back to the last trade. A last print from three days ago
    on a contract with no current two sided market is not a mark, and a position valued
    on one would show a profit that cannot be taken.
    """
    if contract is None:
        return None
    if convention is MarkConvention.MID:
        return contract.mid

    if not contract.has_two_sided_market or contract.is_crossed:
        return None
    # Closing a short means buying it back at the ask; closing a long means selling at
    # the bid. Either way the position is marked at the price that is actually available
    # to the person holding it.
    return contract.ask if action is Action.SELL else contract.bid


def mark_position(
    position: Position,
    analysis: SymbolAnalysis | None,
    *,
    asof: date,
    beta: Beta | None = None,
    convention: MarkConvention = MarkConvention.CLOSING,
) -> PositionRisk:
    """Value one position and compute its greeks from a solved snapshot.

    A position with no snapshot at all comes back fully unmarked rather than raising:
    holding something that is not on the watchlist is a normal state, and the fix is a
    sentence telling the user to capture it, not an exception.
    """
    notes: list[str] = []

    if analysis is None:
        notes.append(
            f"No stored snapshot for {position.symbol}, so this position cannot be "
            "marked. Add it to the watchlist and run `optscan snapshot`."
        )
        return PositionRisk(
            position=position,
            spot=None,
            legs=tuple(LegRisk(leg=leg) for leg in position.legs),
            asof=asof,
            marks_complete=False,
            beta=beta,
            notes=tuple(notes),
        )

    legs: list[LegRisk] = []
    missing: list[str] = []

    for leg in position.legs:
        expiry = analysis.expiry(leg.expiry)
        if expiry is None:
            missing.append(f"{leg.expiry} was not captured")
            legs.append(LegRisk(leg=leg))
            continue

        contract = expiry.contract(leg.strike, leg.right)
        mark = leg_mark(contract, leg.action, convention)
        if mark is None:
            missing.append(_why_unmarked(leg, contract))

        greeks = expiry.greeks(
            leg.strike, leg.right, analysis.spot, analysis.rate, analysis.dividend_yield
        )
        legs.append(
            LegRisk(
                leg=leg,
                mark=mark,
                delta=greeks.delta if greeks else None,
                gamma=greeks.gamma if greeks else None,
                theta=greeks.theta if greeks else None,
                vega=greeks.vega if greeks else None,
                iv=expiry.vol(leg.strike, leg.right),
            )
        )

    if missing:
        notes.append(
            "This position has no profit and loss because "
            + "; ".join(missing)
            + ". A partial mark would read as a real number."
        )

    return PositionRisk(
        position=position,
        spot=analysis.spot,
        legs=tuple(legs),
        asof=asof,
        marks_complete=not missing,
        beta=beta,
        notes=tuple(notes),
    )


def _why_unmarked(leg: PositionLeg, contract: OptionContract | None) -> str:
    """A specific reason, because "unmarked" alone sends nobody anywhere useful."""
    label = f"the {leg.strike:g} {'call' if leg.right is Right.CALL else 'put'}"
    if contract is None:
        return f"{label} is not in the captured chain"
    if contract.is_crossed:
        return f"{label} is quoted crossed, bid above ask"
    if not contract.has_two_sided_market:
        return f"{label} has no two sided market"
    return f"{label} could not be priced"


def build_portfolio(
    positions: list[Position],
    analyses: dict[str, SymbolAnalysis | None],
    *,
    asof: date,
    betas: dict[str, Beta] | None = None,
    convention: MarkConvention = MarkConvention.CLOSING,
    reference: str = "SPY",
) -> tuple[list[PositionRisk], list[str]]:
    """Mark every position. Returns the risks and any portfolio level notes."""
    lookup: Callable[[str], Beta | None] = (betas or {}).get
    risks = [
        mark_position(
            position,
            analyses.get(position.symbol),
            asof=asof,
            beta=lookup(position.symbol),
            convention=convention,
        )
        for position in positions
    ]

    notes: list[str] = []
    unmarked = sum(1 for risk in risks if not risk.marks_complete)
    if unmarked:
        notes.append(
            f"{unmarked} of {len(risks)} positions could not be marked, so the portfolio "
            "greeks are withheld. The profit total covers only the positions that priced."
        )
    without_beta = [
        risk.position.symbol for risk in risks if risk.beta is None or not risk.beta.usable
    ]
    if without_beta:
        notes.append(
            f"No usable beta for {', '.join(sorted(set(without_beta)))}, so the beta "
            f"weighted delta against {reference} is not published. It needs enough "
            "overlapping history to estimate a slope."
        )
    return risks, notes
