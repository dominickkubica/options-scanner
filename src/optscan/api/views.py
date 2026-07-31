"""Domain objects to wire formats.

One place for every conversion, so the routers stay about HTTP and the rule that a
missing number stays missing is enforced in one readable file rather than sprinkled
across eight endpoints.

The rule, restated because this is where it is actually applied: `None` survives. A
contract with no solvable implied vol serializes `iv: null` and a `reject_reason`
saying which of the solver's named refusals it hit. It never serializes `iv: 0`. A
chart that draws zero for "unknown" is lying in a way nobody can see, and a heatmap
is the easiest place in this whole project to hide that lie.
"""

from __future__ import annotations

from datetime import date, datetime

from optscan.analytics.payoff import Payoff
from optscan.api.deps import make_provenance
from optscan.api.schemas import (
    BarOut,
    ChainOut,
    ContractOut,
    ExpirySummaryOut,
    GapOut,
    HistoryOut,
    IvRankOut,
    LegOut,
    OpportunityOut,
    PayoffOut,
    PayoffPointOut,
    Provenance,
    ScoreComponentsOut,
    SkewPointOut,
    SymbolSummaryOut,
    TermPointOut,
)
from optscan.models import Leg, Opportunity, PriceBar, Right
from optscan.screener.context import ExpiryAnalysis
from optscan.screener.gaps import Gap


def iv_rank_view(rank) -> IvRankOut | None:
    """IV rank with its caveat attached, or None when there is no history at all.

    The caveat is not decoration. With one session captured every rank in this UI
    reads `insufficient`, and a gauge with no number and no explanation looks broken
    rather than honest.
    """
    if rank is None:
        return None
    return IvRankOut(
        iv=rank.iv,
        rank=rank.rank,
        percentile=rank.percentile,
        confidence=str(rank.confidence),
        observations=rank.observations,
        span_days=rank.span_days,
        caveat=rank.caveat(),
    )


def expiry_summary_view(expiry: ExpiryAnalysis) -> ExpirySummaryOut:
    return ExpirySummaryOut(
        expiry=expiry.expiry,
        dte=expiry.dte,
        atm_iv=expiry.atm_iv,
        contracts=expiry.total,
        solved=expiry.solved,
        solve_rate=expiry.solve_rate,
        expected_move=expiry.move.market_sigma_move or expiry.move.model_sigma_move,
        straddle=expiry.move.straddle_price,
    )


def symbol_summary_view(solved, provenance: Provenance) -> SymbolSummaryOut:
    analysis = solved.analysis
    events = solved.events
    return SymbolSummaryOut(
        symbol=analysis.symbol,
        spot=analysis.spot,
        session_date=analysis.session_date,
        provenance=provenance,
        iv_rank=iv_rank_view(analysis.iv_rank),
        term_structure=[
            TermPointOut(expiry=point.expiry, dte=point.dte, iv=point.iv)
            for point in analysis.term.points
        ],
        backwardated=analysis.term.is_backwardated(),
        term_slope=analysis.term.slope(),
        expiries=[expiry_summary_view(expiry) for expiry in analysis.expiries],
        earnings_date=events.earnings if events else None,
        ex_dividend_date=events.ex_dividend if events else None,
        events_checked=solved.events_checked,
        events_note=solved.events_note,
        partial=solved.snapshot.partial,
    )


def contract_view(solved, expiry: ExpiryAnalysis, strike: float, right: Right) -> ContractOut:
    analysis = solved.analysis
    contract = expiry.contract(strike, right)
    result = expiry.vols.get((strike, right))
    greeks = expiry.greeks(strike, right, analysis.spot, analysis.rate, analysis.dividend_yield)

    return ContractOut(
        strike=strike,
        right=str(right),
        contract_symbol=contract.contract_symbol if contract else None,
        bid=contract.bid if contract else None,
        ask=contract.ask if contract else None,
        mid=contract.mid if contract else None,
        last=contract.last if contract else None,
        volume=contract.volume if contract else None,
        open_interest=contract.open_interest if contract else None,
        iv=result.sigma if result and result.ok else None,
        vendor_iv=contract.vendor_iv if contract else None,
        delta=greeks.delta if greeks else None,
        gamma=greeks.gamma if greeks else None,
        theta=greeks.theta if greeks else None,
        vega=greeks.vega if greeks else None,
        liquidity=analysis.liquidity_for(expiry.expiry, strike, right),
        in_the_money=contract.in_the_money if contract else None,
        reject_reason=(None if result is None or result.ok else str(result.reason)),
    )


def chain_view(solved, expiry: ExpiryAnalysis, provenance: Provenance) -> ChainOut:
    """The full grid for one expiry, both sides, every listed strike.

    Every listed strike, not just the solved ones. A strike that could not be solved
    is a row with nulls and a reason, because dropping it would leave a hole in the
    ladder that reads as a strike that is not listed.
    """
    analysis = solved.analysis
    strikes = expiry.chain.strikes
    return ChainOut(
        symbol=analysis.symbol,
        expiry=expiry.expiry,
        dte=expiry.dte,
        spot=analysis.spot,
        atm_iv=expiry.atm_iv,
        provenance=provenance,
        calls=[
            contract_view(solved, expiry, strike, Right.CALL)
            for strike in strikes
            if expiry.contract(strike, Right.CALL) is not None
        ],
        puts=[
            contract_view(solved, expiry, strike, Right.PUT)
            for strike in strikes
            if expiry.contract(strike, Right.PUT) is not None
        ],
        put_skew=[
            SkewPointOut(strike=point.strike, iv=point.iv, delta=point.delta)
            for point in expiry.put_skew.points
        ],
        call_skew=[
            SkewPointOut(strike=point.strike, iv=point.iv, delta=point.delta)
            for point in expiry.call_skew.points
        ],
    )


def history_view(
    symbol: str,
    bars: list[PriceBar],
    note: str | None,
    now: datetime | None = None,
) -> HistoryOut:
    provenance = None
    if bars:
        newest = max(bar.fetched_at for bar in bars)
        provenance = make_provenance(bars[0].source, newest, now)

    return HistoryOut(
        symbol=symbol,
        bars=[
            BarOut(
                time=bar.ts.date().isoformat(),
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )
            for bar in sorted(bars, key=lambda bar: bar.ts)
        ],
        provenance=provenance,
        note=note,
    )


def leg_view(leg: Leg) -> LegOut:
    return LegOut(
        action=str(leg.action),
        right=str(leg.right),
        strike=leg.strike,
        expiry=leg.expiry,
        quantity=leg.quantity,
        mid=leg.mid,
        iv=leg.iv,
        delta=leg.delta,
    )


def opportunity_id(opportunity: Opportunity) -> str:
    """A stable handle for one candidate.

    Built from what the position is rather than from where it landed in a list, so
    expanding a row survives a re-sort and a re-scan of the same snapshot. Two
    identical positions on the same expiry are the same opportunity, which is the
    behaviour a UI wants.
    """
    legs = "_".join(
        f"{leg.action}{leg.quantity}x{leg.strike:g}{leg.right}"
        for leg in sorted(opportunity.legs, key=lambda leg: (leg.right, leg.strike))
    )
    return f"{opportunity.symbol}:{opportunity.expiry.isoformat()}:{opportunity.strategy}:{legs}"


def opportunity_view(opportunity: Opportunity) -> OpportunityOut:
    return OpportunityOut(
        id=opportunity_id(opportunity),
        symbol=opportunity.symbol,
        strategy=str(opportunity.strategy),
        expiry=opportunity.expiry,
        dte=opportunity.dte,
        legs=[leg_view(leg) for leg in opportunity.legs],
        underlying_price=opportunity.underlying_price,
        credit=opportunity.credit,
        max_profit=opportunity.max_profit,
        max_loss=opportunity.max_loss,
        capital=opportunity.capital,
        commission=opportunity.commission,
        return_on_capital=opportunity.return_on_capital,
        annualized_return=opportunity.annualized_return,
        probability_of_profit=opportunity.probability_of_profit,
        probability_of_touch=opportunity.probability_of_touch,
        short_delta=opportunity.short_delta,
        iv=opportunity.iv,
        iv_rank=opportunity.iv_rank,
        iv_confidence=opportunity.iv_confidence,
        liquidity_score=opportunity.liquidity_score,
        has_earnings=opportunity.has_earnings,
        early_assignment_risk=opportunity.early_assignment_risk,
        warnings=list(opportunity.warnings),
        score=opportunity.score,
        components=ScoreComponentsOut(
            premium=opportunity.components.premium,
            iv_rank=opportunity.components.iv_rank,
            liquidity=opportunity.components.liquidity,
            probability=opportunity.components.probability,
            event_risk=opportunity.components.event_risk,
        ),
    )


def gap_view(gap: Gap) -> GapOut:
    return GapOut(
        kind=str(gap.kind),
        symbol=gap.symbol,
        expiry=gap.expiry,
        strike=gap.strike,
        right=str(gap.right) if gap.right else None,
        description=gap.description,
        magnitude=gap.magnitude,
        executability=str(gap.executability),
        actionable=gap.actionable,
        caveats=list(gap.caveats),
    )


def payoff_view(
    payoff: Payoff,
    legs: list[Leg],
    dte: int,
    provenance: Provenance,
    note: str | None = None,
) -> PayoffOut:
    return PayoffOut(
        points=[
            PayoffPointOut(price=point.price, at_expiry=point.at_expiry, at_now=point.at_now)
            for point in payoff.points
        ],
        breakevens=list(payoff.breakevens),
        max_profit=payoff.max_profit,
        max_loss=payoff.max_loss,
        spot=payoff.spot,
        net_credit=payoff.net_credit,
        legs=[leg_view(leg) for leg in legs],
        dte=dte,
        provenance=provenance,
        note=note,
    )


def expiry_or_none(solved, expiry: date) -> ExpiryAnalysis | None:
    return solved.analysis.expiry(expiry)
