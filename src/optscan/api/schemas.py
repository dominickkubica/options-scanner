"""Wire formats.

Separate from the domain models on purpose. The domain models are strict, frozen, and
carry things the browser has no use for; these are shaped for a table or a chart and
are allowed to flatten and rename. Keeping them apart means a UI change never pulls on
the models the analytics depend on.

Two rules that carry over from the domain side and matter more here, not less:

- Every payload that contains a number derived from market data also carries when that
  data was fetched. A dashboard is exactly where a stale number gets believed.
- None stays None. A missing greek serializes as null, never as zero, because a chart
  that draws zero for "unknown" is lying in a way nobody can see.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    """Base for everything on the wire."""

    model_config = ConfigDict(from_attributes=True)


class Provenance(ApiModel):
    """Where a payload's numbers came from and how old they are."""

    source: str
    fetched_at: datetime
    age_seconds: float
    stale: bool = Field(description="True once the data is older than the staleness limit.")


class HealthOut(ApiModel):
    status: str
    version: str
    provider: str
    realtime: bool = Field(description="Whether the configured provider quotes in real time.")
    delay_minutes: int | None = Field(
        default=None,
        description=(
            "Documented delay on this provider's quotes, in minutes. Null means the "
            "delay is unknown rather than zero: a vendor that does not publish one is "
            "not thereby real time."
        ),
    )
    live_enabled: bool = Field(
        default=False,
        description="Whether the live feed is configured to run at all.",
    )


class LiveStatusOut(ApiModel):
    """The live feed's own condition.

    Reported whether or not data is flowing, because the case the UI most needs to get
    right is the one where nothing is arriving and the panels are quietly ageing.
    """

    state: str = Field(description="disabled, starting, live, idle, degraded, or stopped.")
    session: str = Field(description="Market session right now: pre, open, post, or closed.")
    source: str | None = None
    realtime: bool = False
    delay_minutes: int | None = None
    symbols: list[str] = Field(default_factory=list)
    last_cycle_at: datetime | None = None
    consecutive_failures: int = 0
    detail: str | None = Field(
        default=None,
        description="A sentence explaining the state, shown verbatim in the UI.",
    )
    poll_seconds: float | None = Field(
        default=None,
        description=(
            "Seconds to expect between cycles, or null when nothing is being polled. "
            "The UI marks a panel overdue against this rather than against a threshold "
            "of its own, so that a feed which died without closing its socket cannot "
            "keep looking live."
        ),
    )


class CatalogueEntryOut(ApiModel):
    """One symbol in the search results.

    `screenable` is the field that matters and it is not the same as `has_prices`.
    After a bulk price sync there are hundreds of symbols with a decade of bars and a
    handful with option chains, and only the second can produce a candidate.
    """

    symbol: str
    groups: list[str] = Field(default_factory=list)
    on_watchlist: bool = False
    has_prices: bool = False
    price_sessions: int = 0
    price_first: date | None = None
    price_last: date | None = None
    price_sources: list[str] = Field(default_factory=list)
    has_iv_history: bool = Field(
        default=False,
        description=(
            "Whether any vendor published an implied vol series for this symbol. Only "
            "imported downloads carry one, and it is what makes an IV rank possible."
        ),
    )
    last_capture: date | None = None
    screenable: bool = False
    status: str = ""
    #: Last stored close and the move into it, from daily bars rather than a quote.
    #: Dated on purpose: this is history, not a live price.
    last_close: float | None = None
    change_pct: float | None = None


class CatalogueOut(ApiModel):
    """Search results, plus the groups and totals the browser needs to render tabs."""

    entries: list[CatalogueEntryOut] = Field(default_factory=list)
    groups: dict[str, int] = Field(default_factory=dict)
    total_symbols: int = 0
    matched: int = 0
    universe_checked: date | None = None
    universe_note: str | None = Field(
        default=None,
        description=(
            "Set when the curated universe is old enough to have missed index changes. "
            "These groups are hand typed and notice nothing on their own."
        ),
    )


class HomeOut(ApiModel):
    """The landing page payload."""

    watchlist: list[CatalogueEntryOut] = Field(default_factory=list)
    groups: dict[str, int] = Field(default_factory=dict)
    total_symbols: int = 0
    with_prices: int = 0
    screenable: int = 0
    universe_note: str | None = None
    notes: list[str] = Field(default_factory=list)


class WatchlistChangeOut(ApiModel):
    """What adding or removing a symbol actually did."""

    symbol: str
    on_watchlist: bool
    changed: bool
    note: str | None = Field(
        default=None,
        description=(
            "What the user needs to know next. Adding a symbol does not capture a "
            "chain: the earliest it can be screened is after the next snapshot run."
        ),
    )


class WatchlistOut(ApiModel):
    symbols: list[str]
    captured: dict[str, date | None] = Field(
        default_factory=dict,
        description="Most recent stored session per symbol, or null if never captured.",
    )


class TermPointOut(ApiModel):
    expiry: date
    dte: int
    iv: float


class SkewPointOut(ApiModel):
    strike: float
    iv: float
    delta: float


class ExpirySummaryOut(ApiModel):
    expiry: date
    dte: int
    atm_iv: float | None
    contracts: int
    solved: int
    solve_rate: float
    expected_move: float | None = Field(
        default=None, description="One sigma move in price terms, market implied where possible."
    )
    straddle: float | None = None


class IvRankOut(ApiModel):
    """IV rank with the qualification attached, never on its own."""

    iv: float
    rank: float | None
    percentile: float | None
    confidence: str
    observations: int
    span_days: int
    caveat: str | None = None
    source_note: str | None = Field(
        default=None,
        description=(
            "Set when stored sessions from another vendor were excluded from the "
            "history. Two vendors' implied vols are not one series, so they are never "
            "pooled, and the exclusion is stated because it lowers the confidence."
        ),
    )


class SymbolSummaryOut(ApiModel):
    #: Why there is no iv_rank, when the reason is the capture rather than the history.
    #: Set only when iv_rank is null; the gauge renders this sentence in its place.
    iv_rank_note: str | None = None
    symbol: str
    spot: float
    session_date: date
    provenance: Provenance
    iv_rank: IvRankOut | None = None
    term_structure: list[TermPointOut] = Field(default_factory=list)
    backwardated: bool = False
    term_slope: float | None = None
    expiries: list[ExpirySummaryOut] = Field(default_factory=list)
    earnings_date: date | None = None
    ex_dividend_date: date | None = None
    events_checked: bool = Field(
        default=False,
        description=(
            "Whether the corporate calendar was reachable. False means the absence of "
            "an earnings date below says nothing, and the UI must not read it as clear."
        ),
    )
    events_note: str | None = None
    partial: bool = Field(
        default=False,
        description="True when at least one expiry failed to capture in this snapshot.",
    )


class ContractOut(ApiModel):
    """One row of the chain grid."""

    strike: float
    right: str
    contract_symbol: str | None = None
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    last: float | None = None
    volume: int | None = None
    open_interest: int | None = None
    iv: float | None = None
    vendor_iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    liquidity: float | None = None
    in_the_money: bool | None = None
    reject_reason: str | None = Field(
        default=None,
        description="Why this contract has no implied vol. Null when it solved.",
    )


class ChainOut(ApiModel):
    symbol: str
    expiry: date
    dte: int
    spot: float
    atm_iv: float | None
    provenance: Provenance
    calls: list[ContractOut] = Field(default_factory=list)
    puts: list[ContractOut] = Field(default_factory=list)
    put_skew: list[SkewPointOut] = Field(default_factory=list)
    call_skew: list[SkewPointOut] = Field(default_factory=list)


class BarOut(ApiModel):
    """One candle, in the shape lightweight-charts wants."""

    time: str | int = Field(
        description=(
            "An ISO date for a daily bar, or epoch seconds for an intraday one. Both "
            "are shapes lightweight-charts accepts, and the distinction is not "
            "cosmetic: a date string identifies a session, so every intraday bar in "
            "one day would carry the same value and the library would collapse 78 "
            "five minute candles onto a single point. A series is one or the other "
            "throughout and never mixes them."
        )
    )
    open: float
    high: float
    low: float
    close: float
    volume: int | None = None


class HistoryOut(ApiModel):
    symbol: str
    bars: list[BarOut] = Field(default_factory=list)
    provenance: Provenance | None = None
    note: str | None = Field(
        default=None,
        description=(
            "Why there are no candles. An empty series without a stated reason would "
            "render as a blank panel that looks like a styling bug."
        ),
    )


class PositionLegOut(ApiModel):
    action: str
    right: str
    strike: float
    expiry: date
    quantity: int
    fill_price: float
    mark: float | None = None
    iv: float | None = None
    delta: float | None = None
    theta: float | None = None
    vega: float | None = None


class TriggerOut(ApiModel):
    """A condition that has become true. Never a recommendation.

    Phase 8 has not run, so nothing here has been validated against outcomes. The
    message says what changed and why it might matter, and stops.
    """

    kind: str
    message: str
    severity: int
    value: float | None = None
    threshold: float | None = None


class PositionOut(ApiModel):
    id: int
    symbol: str
    strategy: str | None = None
    expiry: date
    dte: int
    opened_at: datetime
    legs: list[PositionLegOut] = Field(default_factory=list)
    entry_credit: float
    net_credit: float
    unrealized: float | None = Field(
        default=None,
        description="Null when any leg could not be marked. A partial mark is not a P/L.",
    )
    profit_fraction: float | None = Field(
        default=None, description="Share of maximum profit. Null for a debit position."
    )
    delta: float | None = None
    theta: float | None = None
    vega: float | None = None
    beta_weighted_delta: float | None = None
    tested: bool = False
    marks_complete: bool = True
    triggers: list[TriggerOut] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class PortfolioOut(ApiModel):
    """Every open position and what they add up to.

    The greeks are null unless every position could be marked, while the profit total
    is a sum over whatever priced. That asymmetry is deliberate: an incomplete profit
    is still the profit of the positions in it, but a delta that silently omits a
    position gets used to size a hedge.
    """

    positions: list[PositionOut] = Field(default_factory=list)
    asof: date
    reference: str = "SPY"
    unrealized: float = 0.0
    unmarked: int = 0
    delta: float | None = None
    theta: float | None = Field(
        default=None, description="Dollars of decay per calendar day if nothing moves."
    )
    vega: float | None = None
    beta_weighted_delta: float | None = Field(
        default=None,
        description=(
            "Dollars of reference symbol exposure. Null when any position lacks a "
            "usable beta, which needs enough overlapping history to estimate a slope."
        ),
    )
    notes: list[str] = Field(default_factory=list)
    disclaimer: str = (
        "Triggers say a condition became true. They are not advice, and none of them "
        "has been validated against outcomes."
    )


class LevelOut(ApiModel):
    """One horizontal line on the price chart.

    `strength` is deliberately only comparable against other levels of the same kind.
    A swing level's strength restates a p-value, a volume node's is a share of the
    busiest bin, and a round number's is a constant standing in for the fact that
    nothing was measured. The UI groups by kind rather than sorting them together.
    """

    price: float
    kind: str
    strength: float
    touches: int = 0
    first_touch: date | None = None
    last_touch: date | None = None
    p_value: float | None = Field(
        default=None,
        description=(
            "Probability of seeing at least this many touches by chance, given how "
            "much time price actually spent in this band. Null for kinds where there "
            "is no count to test."
        ),
    )
    expected_touches: float | None = Field(
        default=None,
        description=(
            "Touches chance alone would have produced here. Sent so the UI can show "
            "5 against 1.6 expected rather than a bare 5, which reads as far more "
            "evidence than it is."
        ),
    )
    distance: float | None = Field(
        default=None, description="Signed distance from spot as a fraction. Positive is above."
    )


class BollingerOut(ApiModel):
    middle: float
    upper: float
    lower: float
    width: float


class ConeBandOut(ApiModel):
    deviations: float
    low: float
    high: float


class ConePointOut(ApiModel):
    """One expiry's projected band, from that expiry's own implied volatility."""

    expiry: date
    dte: int
    sigma: float
    bands: list[ConeBandOut] = Field(default_factory=list)


class HistogramBinOut(ApiModel):
    low: float
    high: float
    probability: float


class DistributionOut(ApiModel):
    """Simulated terminal prices for one expiry."""

    expiry: date
    dte: int
    sigma: float
    paths: int
    median: float
    mode: float | None = None
    quantiles: dict[str, float] = Field(default_factory=dict)
    bins: list[HistogramBinOut] = Field(default_factory=list)


class CandidateStrikeOut(ApiModel):
    """A short strike the screener surfaced, for drawing against the levels."""

    strike: float
    right: str
    strategy: str
    expiry: date
    dte: int
    probability_of_profit: float | None = None
    short_delta: float | None = None
    credit: float | None = None
    score: float | None = None


class LevelsOut(ApiModel):
    """Everything the one chart needs: price, levels, cone, and candidate strikes.

    Two provenances on purpose, because this payload mixes two ages and the phase's own
    rule is that a screen must never do that silently. The bars are fetched at request
    time; the implied volatilities behind the cone and the strikes come from the last
    stored capture, which may be hours or days old.
    """

    symbol: str
    spot: float
    session_date: date
    bars_provenance: Provenance | None = None
    chain_provenance: Provenance
    bars: list[BarOut] = Field(default_factory=list)
    levels: list[LevelOut] = Field(default_factory=list)
    moving_averages: dict[str, float] = Field(default_factory=dict)
    bollinger: BollingerOut | None = None
    atr: float | None = None
    realized_vol: float | None = Field(
        default=None,
        description="Annualized close to close realized volatility over the recent window.",
    )
    implied_vol: float | None = Field(
        default=None,
        description="At the money implied vol nearest the realized vol window, for comparison.",
    )
    variance_risk_premium: float | None = Field(
        default=None,
        description=(
            "Implied minus realized, in volatility points. What a premium seller is "
            "being paid for. Null when either side could not be computed."
        ),
    )
    sessions: int = 0
    swing_candidates: int = Field(
        default=0,
        description=(
            "Swing levels clustered and tested. Reported alongside how many survived, "
            "because no levels and no candidates are different facts."
        ),
    )
    cone: list[ConePointOut] = Field(default_factory=list)
    distribution: DistributionOut | None = None
    candidates: list[CandidateStrikeOut] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class LegOut(ApiModel):
    action: str
    right: str
    strike: float
    expiry: date
    quantity: int
    mid: float | None = None
    iv: float | None = None
    delta: float | None = None


class ScoreComponentsOut(ApiModel):
    premium: float
    iv_rank: float | None
    liquidity: float
    probability: float
    event_risk: float


class OpportunityOut(ApiModel):
    """A ranked candidate, flattened for a table."""

    id: str = Field(description="Stable within one scan, for expanding a row.")
    symbol: str
    strategy: str
    expiry: date
    dte: int
    legs: list[LegOut]
    underlying_price: float

    credit: float
    max_profit: float
    max_loss: float | None
    capital: float | None
    commission: float
    return_on_capital: float | None
    annualized_return: float | None

    probability_of_profit: float | None
    probability_of_touch: float | None
    short_delta: float | None

    iv: float | None
    iv_rank: float | None
    iv_confidence: str | None
    liquidity_score: float | None

    has_earnings: bool
    early_assignment_risk: bool
    warnings: list[str] = Field(default_factory=list)

    score: float
    components: ScoreComponentsOut


class NearMissOut(ApiModel):
    """A candidate one check group away from passing the screen.

    `blocker` is the single gate standing in the way. `enters_screen_in_days` is only
    populated when that gate is the DTE ceiling, because that is the one blocker that
    clears on its own: a 74 day contract under a 60 day ceiling is tradeable in 14
    days and nothing has to change for it to be. Every other blocker needs the market
    to move, so a countdown there would be a promise the screen cannot make.
    """

    opportunity: OpportunityOut
    blocker: str
    enters_screen_in_days: int | None = None


class RejectionOut(ApiModel):
    reason: str
    count: int


class ScanOut(ApiModel):
    opportunities: list[OpportunityOut] = Field(default_factory=list)
    considered: int = 0
    passed: int = 0
    rejections: list[RejectionOut] = Field(default_factory=list)
    symbols_scanned: list[str] = Field(default_factory=list)
    symbols_failed: dict[str, str] = Field(default_factory=dict)
    stale: dict[str, float] = Field(
        default_factory=dict, description="Quote age in seconds, per symbol."
    )
    events_checked: bool = Field(
        default=False,
        description=(
            "Whether the earnings exclusion actually ran. When false the screen is "
            "weaker than the same config run from the CLI, and saying so is the only "
            "way that difference is visible."
        ),
    )
    max_dte: int | None = Field(
        default=None,
        description=(
            "The screen's DTE ceiling, echoed so the UI can name it instead of "
            "hardcoding it. It was hardcoded as 60 in the near miss heading and went "
            "stale the day the band moved to 0-45, which is the whole argument for "
            "sending it."
        ),
    )
    near_misses: list[NearMissOut] = Field(
        default_factory=list,
        description=(
            "Candidates blocked by exactly one gate. Empty unless near_miss=true was "
            "requested, which is not the same as there being none."
        ),
    )
    notes: list[str] = Field(default_factory=list)
    disclaimer: str = (
        "Scores rank candidates for review. They have not been validated against outcomes."
    )


class GapOut(ApiModel):
    kind: str
    symbol: str
    expiry: date
    strike: float | None
    right: str | None
    description: str
    magnitude: float
    executability: str
    actionable: bool
    caveats: list[str] = Field(default_factory=list)


class GapsOut(ApiModel):
    gaps: list[GapOut] = Field(default_factory=list)
    symbols_scanned: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    disclaimer: str = (
        "Candidates for human review, not edge. Every flag here is more likely to be a "
        "stale quote, an uncrossable spread, or a dividend than a mispricing."
    )


class PayoffLegIn(ApiModel):
    """One leg of a position the browser wants a payoff diagram for.

    Deliberately carries no prices. The strike, right, expiry, and direction are the
    user's choice; the mid and the implied vol are looked up server side from the same
    solved snapshot everything else on the page came from. A browser that could post
    its own prices could post a payoff for a position nobody could enter.
    """

    action: str = Field(description="buy or sell")
    right: str = Field(description="C or P")
    strike: float = Field(gt=0.0)
    quantity: int = Field(default=1, gt=0)


class PayoffRequest(ApiModel):
    symbol: str
    expiry: date
    legs: list[PayoffLegIn] = Field(min_length=1)
    price_range: float | None = Field(
        default=None, gt=0.0, le=1.0, description="Fraction of spot to draw either side."
    )


class PayoffPointOut(ApiModel):
    price: float
    at_expiry: float
    at_now: float | None = None


class PayoffOut(ApiModel):
    points: list[PayoffPointOut]
    breakevens: list[float]
    max_profit: float | None
    max_loss: float | None
    spot: float
    net_credit: float
    legs: list[LegOut] = Field(
        default_factory=list,
        description="The legs as priced, so the diagram can be checked against its inputs.",
    )
    dte: int = 0
    provenance: Provenance | None = None
    note: str | None = Field(
        default=None,
        description="Set when the T+0 curve could not be drawn, with the reason.",
    )


class IntervalOut(ApiModel):
    """A proportion and its interval, carrying both sample sizes.

    `observations` and `clusters` are both present on purpose. The proportion is
    computed per row and the interval is widened to the cluster count, so a reader
    given only one of the two numbers cannot tell which one the interval belongs to.
    """

    value: float
    low: float
    high: float
    observations: int
    clusters: int


class EstimateOut(ApiModel):
    """A mean in dollars, with an interval built from the spread between clusters."""

    value: float
    low: float | None
    high: float | None
    observations: int
    clusters: int
    indistinguishable_from_zero: bool = Field(
        description=(
            "Whether zero sits inside the interval. When true, a positive mean has not "
            "been shown to be positive and must not be presented as an edge."
        )
    )


class DayPointOut(ApiModel):
    day: date
    profit: float
    trades: int
    clusters: int
    cumulative: float


class BreakdownOut(ApiModel):
    key: str
    trades: int
    clusters: int
    win_rate: IntervalOut | None = None
    total_profit: float
    mean_profit: float
    reportable: bool = Field(
        description="False when the group has too few independent clusters to mean anything."
    )


class JournalOut(ApiModel):
    """Journal style reporting over settled candidates.

    Not a record of trades taken. See `analytics/journal.py`: every row here is a
    candidate the screen surfaced and `optscan resolve` settled at expiry, with no
    fill, no slippage and no early management.
    """

    trades: int
    clusters: int
    settlement_dates: int
    wins: int
    losses: int
    scratches: int
    win_rate: IntervalOut | None = None
    expectancy: EstimateOut | None = None
    avg_win: float | None = None
    avg_loss: float | None = None
    profit_factor: float | None = Field(
        default=None,
        description="None when nothing lost, which is undefined rather than infinite.",
    )
    total_profit: float
    max_drawdown: float
    best_day: DayPointOut | None = None
    worst_day: DayPointOut | None = None
    days: list[DayPointOut] = Field(default_factory=list)
    by_strategy: list[BreakdownOut] = Field(default_factory=list)
    by_symbol: list[BreakdownOut] = Field(default_factory=list)
    by_dte: list[BreakdownOut] = Field(default_factory=list)
    by_score: list[BreakdownOut] = Field(default_factory=list)
    reportable: bool = Field(
        description="False when the whole sample is under the cluster minimum for a claim."
    )
    notes: list[str] = Field(default_factory=list)
