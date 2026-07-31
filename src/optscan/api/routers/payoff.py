"""Payoff diagrams, computed server side.

The expiry curve is piecewise linear and a browser could draw it from the strikes and
the credit alone. The T+0 curve cannot: it needs Black-Scholes repricing at every
point, and putting a second pricing implementation in JavaScript is how the two
quietly disagree. One implementation, tested, in Python.

The request carries no prices. The browser says which strikes, which rights, and which
direction; the mid and the implied vol are looked up from the same solved snapshot the
rest of the page came from. A client that could post its own prices could post a
payoff for a position nobody could enter, and it would look exactly as convincing.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from optscan.analytics.payoff import DEFAULT_RANGE, build_payoff
from optscan.api.deps import (
    ProviderFactoryDep,
    ScreenConfigDep,
    SettingsDep,
    SolvedSymbol,
)
from optscan.api.routers.symbols import require_symbol
from optscan.api.schemas import PayoffOut, PayoffRequest
from optscan.api.views import payoff_view
from optscan.models import Action, Leg, Right
from optscan.screener.context import ExpiryAnalysis

router = APIRouter(tags=["payoff"])


def _build_leg(solved: SolvedSymbol, expiry: ExpiryAnalysis, raw) -> Leg:
    """Price one requested leg from the stored chain, or refuse with a reason."""
    try:
        right = Right.parse(raw.right)
        action = Action(raw.action.strip().lower())
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    contract = expiry.contract(raw.strike, right)
    if contract is None:
        raise HTTPException(
            status_code=404,
            detail=(f"{solved.symbol} {expiry.expiry} has no listed {right} at {raw.strike:g}."),
        )
    if contract.mid is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{solved.symbol} {expiry.expiry} {raw.strike:g}{right} has no two sided "
                "market in this snapshot, so it has no price and no payoff."
            ),
        )

    greeks = expiry.greeks(
        raw.strike,
        right,
        solved.analysis.spot,
        solved.analysis.rate,
        solved.analysis.dividend_yield,
    )
    return Leg(
        action=action,
        right=right,
        strike=raw.strike,
        expiry=expiry.expiry,
        quantity=raw.quantity,
        contract_symbol=contract.contract_symbol,
        bid=contract.bid,
        ask=contract.ask,
        mid=contract.mid,
        iv=expiry.vol(raw.strike, right),
        delta=greeks.delta if greeks else None,
        open_interest=contract.open_interest,
        volume=contract.volume,
        fetched_at=contract.fetched_at,
        source=contract.source,
    )


@router.post("/payoff", response_model=PayoffOut)
def payoff(
    request: PayoffRequest,
    settings: SettingsDep,
    config: ScreenConfigDep,
    provider_factory: ProviderFactoryDep,
) -> PayoffOut:
    solved = require_symbol(request.symbol, settings, config, provider_factory)

    expiry = solved.analysis.expiry(request.expiry)
    if expiry is None:
        listed = ", ".join(item.expiry.isoformat() for item in solved.analysis.expiries)
        raise HTTPException(
            status_code=404,
            detail=(
                f"{solved.symbol} has no captured chain for {request.expiry}. "
                f"Captured expiries: {listed or 'none'}."
            ),
        )

    legs = [_build_leg(solved, expiry, raw) for raw in request.legs]

    # A position with no short leg is not what this tool builds, but refusing to draw
    # its diagram would be pointless: the payoff math is direction agnostic and a user
    # exploring a long wing on its own deserves an answer.
    curve = build_payoff(
        legs,
        solved.analysis.spot,
        time=expiry.time,
        rate=solved.analysis.rate,
        dividend_yield=solved.analysis.dividend_yield,
        price_range=request.price_range or DEFAULT_RANGE,
    )

    # pnl_at_time returns None for the whole curve if any leg lacks a vol, so the flag
    # is read off the curve rather than guessed at from the legs.
    note = None
    if all(point.at_now is None for point in curve.points):
        unpriced = [leg.describe() for leg in legs if leg.iv is None]
        note = "No T+0 curve: " + (
            f"no implied vol for {', '.join(unpriced)}."
            if unpriced
            else "this expiry has no time left on the clock."
        )

    return payoff_view(curve, legs, expiry.dte, solved.provenance(), note)
