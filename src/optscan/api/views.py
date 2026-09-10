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

from optscan.analytics.levels import DEFAULT_REALIZED_VOL_WINDOW
from optscan.analytics.payoff import Payoff
from optscan.analytics.projection import build_cone, terminal_histogram
from optscan.api.deps import make_provenance
from optscan.api.schemas import (
    BarOut,
    BollingerOut,
    BreakdownOut,
    CandidateStrikeOut,
    ChainOut,
    ConeBandOut,
    ConePointOut,
    ContractOut,
    DayPointOut,
    DistributionOut,
    EstimateOut,
    ExpirySummaryOut,
    GapOut,
    HistogramBinOut,
    HistoryOut,
    IntervalOut,
    IvRankOut,
    JournalOut,
    LegOut,
    LevelOut,
    LevelsOut,
    NearMissOut,
    OpportunityOut,
    PayoffOut,
    PayoffPointOut,
    PortfolioOut,
    PositionLegOut,
    PositionOut,
    Provenance,
    ScoreComponentsOut,
    SkewPointOut,
    SymbolSummaryOut,
    TermPointOut,
    TriggerOut,
)
from optscan.models import Leg, Opportunity, PriceBar, Right
from optscan.screener.context import ExpiryAnalysis
from optscan.screener.filters import Rejection
from optscan.screener.gaps import Gap
from optscan.screener.scan import scan_analysis


def iv_rank_view(rank, source_note: str | None = None) -> IvRankOut | None:
    """IV rank with its caveat attached, or None when there is no history at all.

    The caveat is not decoration. With one session captured every rank in this UI
    reads `insufficient`, and a gauge with no number and no explanation looks broken
    rather than honest.

    source_note is the second reason a rank can be weaker than the stored history
    suggests: sessions captured from a different vendor are excluded rather than
    pooled, and the count that was left out belongs next to the number it shrank.
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
        source_note=source_note,
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
        iv_rank=iv_rank_view(analysis.iv_rank, solved.iv_history_note),
        iv_rank_note=analysis.iv_rank_note,
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


def bar_view(bar: PriceBar, *, intraday: bool = False) -> BarOut:
    """One candle in the shape the chart library wants.

    Extracted so the levels panel and the history panel cannot drift into rendering
    the same bar two different ways.

    `intraday` switches the time field from an ISO date to epoch seconds. A date
    identifies a session, so without the switch every bar within one day carries an
    identical value and the chart draws one candle where there should be seventy eight.
    """
    if intraday:
        return BarOut(
            time=int(bar.ts.timestamp()),
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
        )
    return BarOut(
        time=bar.ts.date().isoformat(),
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
    )


def history_view(
    symbol: str,
    bars: list[PriceBar],
    note: str | None,
    now: datetime | None = None,
    *,
    intraday: bool = False,
) -> HistoryOut:
    """Candles for the chart, daily or intraday.

    `intraday` is passed rather than inferred, and the first draft did infer it: any
    bar carrying a time of day was treated as intraday. That is wrong on real data.
    **Alpaca stamps daily bars at 04:00Z**, not midnight, so every symbol served from
    the provider rather than from storage would have been mislabelled and its whole
    series keyed by epoch seconds against a chart expecting dates.

    The caller knows which interval it asked for. Nothing else does.
    """
    provenance = None
    if bars:
        newest = max(bar.fetched_at for bar in bars)
        provenance = make_provenance(bars[0].source, newest, now)

    return HistoryOut(
        symbol=symbol,
        bars=[bar_view(bar, intraday=intraday) for bar in sorted(bars, key=lambda bar: bar.ts)],
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


def near_miss_view(near_miss, max_dte: int) -> NearMissOut:
    """Flatten a near miss, and say when a DTE blocked one becomes eligible.

    The countdown is arithmetic on the calendar, not a forecast: only the DTE ceiling
    clears purely by waiting. A candidate held back by credit or liquidity needs the
    market to change, and dating that would be inventing a number.
    """
    blocker = near_miss.blocker
    enters = None
    if blocker is Rejection.DTE_TOO_LONG:
        enters = max(near_miss.opportunity.dte - max_dte, 0)
    return NearMissOut(
        opportunity=opportunity_view(near_miss.opportunity),
        blocker=blocker.value,
        enters_screen_in_days=enters,
    )


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
        net_delta=opportunity.net_delta,
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


def portfolio_view(result) -> PortfolioOut:
    """A management run's output, flattened for the browser.

    Takes the whole ManageResult rather than the portfolio alone, because the triggers
    are keyed by position id and pairing them back up is exactly the kind of join that
    should happen once here rather than in the browser.
    """
    book = result.portfolio
    return PortfolioOut(
        positions=[
            _position_view(risk, result.triggers.get(risk.position.id or -1, []))
            for risk in book.positions
        ],
        asof=book.asof,
        reference=book.reference,
        unrealized=book.unrealized,
        unmarked=book.unmarked,
        delta=book.delta,
        theta=book.theta,
        vega=book.vega,
        beta_weighted_delta=book.beta_weighted_delta,
        notes=list(book.notes),
    )


def _position_view(risk, triggers) -> PositionOut:
    position = risk.position
    return PositionOut(
        id=position.id or 0,
        symbol=position.symbol,
        strategy=str(position.strategy) if position.strategy else None,
        expiry=position.expiry,
        dte=risk.dte,
        opened_at=position.opened_at,
        legs=[
            PositionLegOut(
                action=str(item.leg.action),
                right=str(item.leg.right),
                strike=item.leg.strike,
                expiry=item.leg.expiry,
                quantity=item.leg.quantity,
                fill_price=item.leg.fill_price,
                mark=item.mark,
                iv=item.iv,
                delta=item.delta,
                theta=item.theta,
                vega=item.vega,
            )
            for item in risk.legs
        ],
        entry_credit=position.entry_credit,
        net_credit=position.net_credit,
        unrealized=risk.unrealized,
        profit_fraction=risk.profit_fraction,
        delta=risk.delta,
        theta=risk.theta,
        vega=risk.vega,
        beta_weighted_delta=risk.beta_weighted_delta,
        tested=risk.tested,
        marks_complete=risk.marks_complete,
        triggers=[
            TriggerOut(
                kind=str(item.kind),
                message=item.message,
                severity=item.severity,
                value=item.value,
                threshold=item.threshold,
            )
            for item in triggers
        ],
        notes=list(risk.notes),
    )


def levels_view(
    *,
    solved,
    bars: list[PriceBar],
    bars_note: str | None,
    levels,
    expiry: date | None,
    rate: float,
    config,
) -> LevelsOut:
    """Assemble the one chart's payload from parts that were computed separately.

    The assembly is here rather than in the router for the usual reason, but there is a
    second one specific to this panel: it is the only place in the API where a number
    from the stored capture and a number fetched seconds ago are drawn on the same
    axes. Keeping the joining in one function keeps the two provenances together with
    the values they belong to.
    """
    analysis = solved.analysis
    notes: list[str] = []
    if bars_note:
        notes.append(bars_note)
    if levels is not None:
        notes.extend(levels.notes)

    chosen = analysis.expiry(expiry) if expiry is not None else None
    if expiry is not None and chosen is None:
        notes.append(f"{expiry} is listed but produced no solved expiry to project.")

    implied = _comparable_implied_vol(analysis, levels)
    realized = levels.realized_vol if levels is not None else None
    premium = None
    if implied is not None and realized is not None:
        premium = implied - realized
        if premium < 0:
            notes.append(
                "Implied volatility is below realized. Selling premium here is being "
                "paid less than the recent move has actually cost, which is unusual "
                "and worth understanding before treating it as an opportunity."
            )

    return LevelsOut(
        symbol=analysis.symbol,
        spot=analysis.spot,
        session_date=analysis.session_date,
        bars_provenance=(make_provenance(bars[-1].source, bars[-1].fetched_at) if bars else None),
        chain_provenance=solved.provenance(),
        bars=[bar_view(bar) for bar in bars],
        levels=[_level_view(level, analysis.spot) for level in (levels.all() if levels else ())],
        moving_averages=(
            {str(period): value for period, value in levels.moving_averages.items()}
            if levels
            else {}
        ),
        bollinger=(
            BollingerOut(
                middle=levels.bollinger.middle,
                upper=levels.bollinger.upper,
                lower=levels.bollinger.lower,
                width=levels.bollinger.width,
            )
            if levels is not None and levels.bollinger is not None
            else None
        ),
        atr=levels.atr if levels else None,
        realized_vol=realized,
        implied_vol=implied,
        variance_risk_premium=premium,
        sessions=levels.sessions if levels else 0,
        swing_candidates=levels.swing_candidates if levels else 0,
        cone=_cone_view(analysis),
        distribution=_distribution_view(analysis, chosen, rate),
        candidates=_candidate_view(analysis, chosen, config),
        notes=notes,
    )


def _level_view(level, spot: float) -> LevelOut:
    return LevelOut(
        price=level.price,
        kind=str(level.kind),
        strength=level.strength,
        touches=level.touches,
        first_touch=level.first_touch,
        last_touch=level.last_touch,
        p_value=level.p_value,
        expected_touches=level.expected_touches,
        distance=level.distance_from(spot),
    )


def _comparable_implied_vol(analysis, levels) -> float | None:
    """ATM implied vol at the tenor the realized vol was measured over.

    Comparing a 30 day realized vol against a 7 day implied is the term structure
    talking, not the variance risk premium. The expiry nearest the realized window is
    picked so the two sides describe the same horizon, and None comes back rather than
    a mismatched pair when nothing is close.
    """
    if levels is None or levels.realized_vol is None:
        return None
    target = DEFAULT_REALIZED_VOL_WINDOW
    usable = [item for item in analysis.expiries if item.atm_iv is not None and item.dte > 0]
    if not usable:
        return None
    nearest = min(usable, key=lambda item: abs(item.dte - target))
    # Beyond about a factor of two away in tenor the comparison stops being like for
    # like, and a silently mismatched premium is worse than no premium.
    if not 0.5 * target <= nearest.dte <= 2.0 * target:
        return None
    return nearest.atm_iv


def _cone_view(analysis) -> list[ConePointOut]:
    cone = build_cone(
        analysis.spot,
        analysis.session_date,
        [(item.expiry, item.atm_iv) for item in analysis.expiries if item.atm_iv],
    )
    return [
        ConePointOut(
            expiry=point.expiry,
            dte=point.dte,
            sigma=point.sigma,
            bands=[
                ConeBandOut(deviations=deviations, low=band[0], high=band[1])
                for deviations, band in sorted(point.bands.items())
            ],
        )
        for point in cone.points
    ]


def _distribution_view(analysis, chosen, rate: float) -> DistributionOut | None:
    if chosen is None or chosen.atm_iv is None:
        return None
    result = terminal_histogram(analysis.spot, chosen.time, chosen.atm_iv, rate=rate)
    if result is None:
        return None
    return DistributionOut(
        expiry=chosen.expiry,
        dte=chosen.dte,
        sigma=chosen.atm_iv,
        paths=result.paths,
        median=result.median,
        mode=result.mode,
        quantiles={f"{key:g}": value for key, value in sorted(result.quantiles.items())},
        bins=[
            HistogramBinOut(low=item.low, high=item.high, probability=item.probability)
            for item in result.bins
        ],
    )


def _candidate_view(analysis, chosen, config) -> list[CandidateStrikeOut]:
    """The screener's own short strikes for the projected expiry.

    The screener rather than a fresh delta band, because the phase's exit criterion is
    "your candidate strikes" and the answer to which strikes are yours is whatever
    screen.yaml says. Running it here also means a strike drawn on this chart is the
    same strike, with the same POP, as the row in the opportunities table.
    """
    if chosen is None:
        return []

    try:
        result = scan_analysis(analysis, config)
    except (ValueError, KeyError):
        # A screen that cannot run is not a reason to lose the chart. The levels and
        # the cone are the substance of this panel; the strikes are an overlay.
        return []

    seen: set[tuple[float, str]] = set()
    candidates: list[CandidateStrikeOut] = []
    for opportunity in result.opportunities:
        if opportunity.expiry != chosen.expiry:
            continue
        for leg in opportunity.legs:
            if leg.quantity >= 0 and str(leg.action).lower() != "sell":
                continue
            key = (leg.strike, str(leg.right))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                CandidateStrikeOut(
                    strike=leg.strike,
                    right=str(leg.right),
                    strategy=str(opportunity.strategy),
                    expiry=opportunity.expiry,
                    dte=opportunity.dte,
                    probability_of_profit=opportunity.probability_of_profit,
                    short_delta=opportunity.short_delta,
                    net_delta=opportunity.net_delta,
                    credit=opportunity.credit,
                    score=opportunity.score,
                )
            )
    return sorted(candidates, key=lambda item: item.strike)


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


def interval_view(interval) -> IntervalOut | None:
    if interval is None:
        return None
    return IntervalOut(
        value=interval.value,
        low=interval.low,
        high=interval.high,
        observations=interval.observations,
        clusters=interval.clusters,
    )


def estimate_view(estimate) -> EstimateOut | None:
    if estimate is None:
        return None
    return EstimateOut(
        value=estimate.value,
        low=estimate.low,
        high=estimate.high,
        observations=estimate.observations,
        clusters=estimate.clusters,
        indistinguishable_from_zero=estimate.indistinguishable_from_zero,
    )


def day_point_view(point) -> DayPointOut | None:
    if point is None:
        return None
    return DayPointOut(
        day=point.day,
        profit=point.profit,
        trades=point.trades,
        clusters=point.clusters,
        cumulative=point.cumulative,
    )


def breakdown_view(breakdown) -> BreakdownOut:
    return BreakdownOut(
        key=breakdown.key,
        trades=breakdown.trades,
        clusters=breakdown.clusters,
        win_rate=interval_view(breakdown.win_rate),
        total_profit=breakdown.total_profit,
        mean_profit=breakdown.mean_profit,
        reportable=breakdown.reportable,
    )


def journal_view(report) -> JournalOut:
    return JournalOut(
        trades=report.trades,
        clusters=report.clusters,
        settlement_dates=report.settlement_dates,
        wins=report.wins,
        losses=report.losses,
        scratches=report.scratches,
        win_rate=interval_view(report.win_rate),
        expectancy=estimate_view(report.expectancy),
        avg_win=report.avg_win,
        avg_loss=report.avg_loss,
        profit_factor=report.profit_factor,
        total_profit=report.total_profit,
        max_drawdown=report.max_drawdown,
        best_day=day_point_view(report.best_day),
        worst_day=day_point_view(report.worst_day),
        days=[day_point_view(point) for point in report.days],
        by_strategy=[breakdown_view(item) for item in report.by_strategy],
        by_symbol=[breakdown_view(item) for item in report.by_symbol],
        by_dte=[breakdown_view(item) for item in report.by_dte],
        by_score=[breakdown_view(item) for item in report.by_score],
        reportable=report.reportable,
        notes=report.notes,
    )
