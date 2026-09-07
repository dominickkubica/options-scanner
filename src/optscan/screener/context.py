"""Everything the strategies need about one underlying, computed once.

A chain snapshot goes in, a solved and summarized view comes out: implied vol per
contract, ATM vol per expiry, greeks, skew, term structure, and whatever volatility
history and event data the caller could supply.

Built once per symbol per scan. Solving a 5000 contract chain is the expensive part of
a scan, and every strategy would otherwise redo it.

Pure. The caller does the I/O and hands in the snapshot, the history, and the events.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from optscan.analytics.events import EventWindow
from optscan.analytics.greeks import Greeks, bsm_greeks, time_to_expiry
from optscan.analytics.iv import (
    DEFAULT_MAX_SPREAD_PCT,
    DEFAULT_MIN_PRICE,
    VolResult,
    contract_implied_vol,
    interpolate_atm_vol,
)
from optscan.analytics.ivrank import IVRank, iv_rank_from_series
from optscan.analytics.liquidity import LiquidityScore, score_liquidity
from optscan.analytics.moves import ExpectedMove, expected_move
from optscan.analytics.surface import (
    Skew,
    SkewSummary,
    TermStructure,
    build_skew,
    build_term_structure,
    summarize_skew,
)
from optscan.models import ChainSnapshot, OptionChain, OptionContract, Right
from optscan.screener.history import IvHistory


@dataclass(frozen=True, slots=True)
class ExpiryAnalysis:
    """One expiry, fully solved."""

    expiry: date
    dte: int
    time: float
    chain: OptionChain
    vols: dict[tuple[float, Right], VolResult]
    atm_iv: float | None
    put_skew: Skew
    call_skew: Skew
    skew: SkewSummary
    move: ExpectedMove
    solved: int
    total: int

    @property
    def solve_rate(self) -> float:
        """Fraction of contracts with a usable implied vol.

        A liquidity signal in its own right: a chain where two thirds of the strikes
        cannot support a vol is a chain to be careful in.
        """
        return self.solved / self.total if self.total else 0.0

    def vol(self, strike: float, right: Right | str) -> float | None:
        result = self.vols.get((strike, Right.parse(right)))
        return result.sigma if result and result.ok else None

    def contract(self, strike: float, right: Right | str) -> OptionContract | None:
        return self.chain.get(strike, right)

    def greeks(
        self,
        strike: float,
        right: Right | str,
        spot: float,
        rate: float,
        dividend_yield: float = 0.0,
    ) -> Greeks | None:
        """Greeks at this strike's own implied vol, or None when it has none."""
        sigma = self.vol(strike, right)
        if sigma is None:
            return None
        return bsm_greeks(right, spot, strike, self.time, rate, sigma, dividend_yield)

    def strikes(self, right: Right | str) -> tuple[float, ...]:
        """Listed strikes with a usable vol, ascending."""
        wanted = Right.parse(right)
        return tuple(
            sorted(
                strike
                for (strike, side), result in self.vols.items()
                if side is wanted and result.ok
            )
        )

    def nearest_strike(self, target: float, right: Right | str) -> float | None:
        """The listed strike closest to a target price. Ties go lower, for determinism."""
        available = self.strikes(right)
        if not available:
            return None
        return min(available, key=lambda strike: (abs(strike - target), strike))


#: The tenor an IV history is measured at, and therefore the tenor today's vol has to
#: be read at before it can be ranked against one. Both series this project can build
#: are 30 day constant maturity: `screener.history.TARGET_DTE` interpolates stored
#: chains to 30 days, and Market Chameleon's exported column is literally named IV30.
DEFAULT_IV_HISTORY_DTE = 30

#: How far from the target tenor an expiry may be and still be called comparable.
#: Beyond about a factor of two the comparison stops being like for like and starts
#: being a reading of the term structure.
_TENOR_BAND = (0.5, 2.0)


def atm_iv_near_dte(
    expiries: Sequence[ExpiryAnalysis],
    target_dte: int = DEFAULT_IV_HISTORY_DTE,
) -> float | None:
    """The ATM implied vol at roughly `target_dte`, or None when nothing is close.

    ## Why this is not just `expiries[0]`

    It used to be, and that was the tenth instance of this project's recurring bug: a
    quantity with a large structural component read as though it were a level. Very
    short dated ATM vol is mechanically elevated relative to a thirty day vol, so
    ranking the front expiry against a thirty day history compares two different
    quantities and reads high essentially always. On a real AAPL capture the front
    expiry was a zero day contract at 83 vol points while the thirty day point was
    25.5, which would have ranked in the hundredth percentile on the calmest day of
    the year.

    It never fired in production only because the history was always too short to
    produce a rank at all. Importing twelve years of vendor IV30 is what would have
    made it live, so it is fixed in the same change.

    None rather than the nearest available: a rank computed from a mismatched tenor is
    worse than no rank, because nothing downstream can tell it was mismatched.
    """
    usable = [item for item in expiries if item.atm_iv is not None and item.dte > 0]
    if not usable:
        return None
    nearest = min(usable, key=lambda item: abs(item.dte - target_dte))
    low, high = _TENOR_BAND
    if not low * target_dte <= nearest.dte <= high * target_dte:
        return None
    return nearest.atm_iv


@dataclass(frozen=True, slots=True)
class SymbolAnalysis:
    """One underlying across every captured expiry."""

    symbol: str
    spot: float
    asof: datetime
    session_date: date
    rate: float
    dividend_yield: float
    source: str
    expiries: tuple[ExpiryAnalysis, ...]
    term: TermStructure
    events: EventWindow
    iv_rank: IVRank | None = None
    #: Why there is no rank, when there is history but no comparable tenor.
    iv_rank_note: str | None = None
    quote_age_seconds: float | None = None
    liquidity: dict[tuple[date, float, Right], LiquidityScore] = field(default_factory=dict)

    def expiry(self, expiry: date) -> ExpiryAnalysis | None:
        for analysis in self.expiries:
            if analysis.expiry == expiry:
                return analysis
        return None

    def liquidity_for(self, expiry: date, strike: float, right: Right | str) -> float | None:
        score = self.liquidity.get((expiry, strike, Right.parse(right)))
        return score.score if score else None


def analyze_chain(
    chain: OptionChain,
    spot: float,
    asof: datetime,
    *,
    rate: float,
    dividend_yield: float = 0.0,
    max_spread_pct: float = DEFAULT_MAX_SPREAD_PCT,
    min_price: float = DEFAULT_MIN_PRICE,
) -> ExpiryAnalysis:
    """Solve one expiry and summarize its surface."""
    time = time_to_expiry(chain.expiry, asof)
    dte = (chain.expiry - asof.date()).days

    vols: dict[tuple[float, Right], VolResult] = {}
    for contract in chain.contracts:
        vols[(contract.strike, contract.right)] = contract_implied_vol(
            contract,
            spot,
            asof,
            rate=rate,
            dividend_yield=dividend_yield,
            max_spread_pct=max_spread_pct,
            min_price=min_price,
        )

    put_vols = {
        strike: result.sigma
        for (strike, right), result in vols.items()
        if right is Right.PUT and result.ok and result.sigma
    }
    call_vols = {
        strike: result.sigma
        for (strike, right), result in vols.items()
        if right is Right.CALL and result.ok and result.sigma
    }

    atm_iv = interpolate_atm_vol({**put_vols, **call_vols}, spot)
    put_skew = build_skew(Right.PUT, put_vols, spot, time, rate, dividend_yield)
    call_skew = build_skew(Right.CALL, call_vols, spot, time, rate, dividend_yield)

    atm_strike = min(chain.strikes, key=lambda k: (abs(k - spot), k)) if chain.strikes else None
    atm_call = chain.get(atm_strike, Right.CALL) if atm_strike else None
    atm_put = chain.get(atm_strike, Right.PUT) if atm_strike else None

    return ExpiryAnalysis(
        expiry=chain.expiry,
        dte=dte,
        time=time,
        chain=chain,
        vols=vols,
        atm_iv=atm_iv,
        put_skew=put_skew,
        call_skew=call_skew,
        skew=summarize_skew(put_skew, call_skew, atm_iv),
        move=expected_move(
            spot,
            time,
            atm_iv=atm_iv,
            call_mid=atm_call.mid if atm_call else None,
            put_mid=atm_put.mid if atm_put else None,
        ),
        solved=sum(1 for result in vols.values() if result.ok),
        total=len(vols),
    )


def analyze_snapshot(
    snapshot: ChainSnapshot,
    *,
    rate: float,
    dividend_yield: float = 0.0,
    iv_history: IvHistory | None = None,
    iv_history_dte: int = DEFAULT_IV_HISTORY_DTE,
    events: EventWindow | None = None,
    max_spread_pct: float = DEFAULT_MAX_SPREAD_PCT,
    min_price: float = DEFAULT_MIN_PRICE,
    now: datetime | None = None,
) -> SymbolAnalysis:
    """Solve a whole captured snapshot.

    Raises when the snapshot has no usable underlying price, because every greek and
    every probability downstream is a function of spot and a scan built on a guess
    would be worse than no scan.
    """
    spot = snapshot.quote.price
    if spot is None or spot <= 0:
        raise ValueError(f"{snapshot.symbol} snapshot has no usable underlying price")

    asof = snapshot.fetched_at
    expiries = tuple(
        analyze_chain(
            chain,
            spot,
            asof,
            rate=rate,
            dividend_yield=dividend_yield,
            max_spread_pct=max_spread_pct,
            min_price=min_price,
        )
        for chain in sorted(snapshot.chains, key=lambda chain: chain.expiry)
    )

    atm_by_expiry = {
        analysis.expiry: analysis.atm_iv for analysis in expiries if analysis.atm_iv is not None
    }
    term = build_term_structure(atm_by_expiry, snapshot.session_date)

    rank = None
    rank_note = None
    if iv_history:
        # A downloaded series brings its own latest reading and that is the one to
        # rank, because a value and the range it sits inside have to come from the
        # same vendor. A series solved from this project's own captures has no such
        # reading, so today's vol is read off the chain at the history's own tenor.
        current = iv_history.current or atm_iv_near_dte(expiries, iv_history_dte)
        if current:
            rank = iv_rank_from_series(current, iv_history.points, asof=snapshot.session_date)
        else:
            # There is history but nothing comparable to rank against it. Saying so
            # matters: a gauge that renders nothing and explains nothing reads as a
            # broken feature, and the previous behaviour here was worse than either,
            # ranking whatever the front expiry happened to be.
            tenors = sorted({item.dte for item in expiries if item.atm_iv is not None})
            rank_note = (
                f"No IV rank: the history is measured at {iv_history_dte} days and this "
                f"capture has no expiry near it (found {tenors or 'none'}). Ranking a "
                "short dated vol against a thirty day range would read high on almost "
                "any day."
            )

    liquidity: dict[tuple[date, float, Right], LiquidityScore] = {}
    for analysis in expiries:
        for contract in analysis.chain.contracts:
            liquidity[(contract.expiry, contract.strike, contract.right)] = score_liquidity(
                contract, asof=now or asof
            )

    return SymbolAnalysis(
        symbol=snapshot.symbol,
        spot=spot,
        asof=asof,
        session_date=snapshot.session_date,
        rate=rate,
        dividend_yield=dividend_yield,
        source=snapshot.source,
        expiries=expiries,
        term=term,
        events=events or EventWindow(),
        iv_rank=rank,
        iv_rank_note=rank_note,
        quote_age_seconds=snapshot.age_seconds(now),
        liquidity=liquidity,
    )
