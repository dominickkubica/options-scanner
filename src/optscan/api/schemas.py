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


class SymbolSummaryOut(ApiModel):
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

    time: str = Field(description="ISO date, which is what the chart library expects.")
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
