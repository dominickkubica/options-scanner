"""The filter layer.

Every rejection carries a reason. That matters more than it sounds: the common failure
mode of a screener is showing an empty table and leaving the user to guess whether the
market is quiet, the filters are too tight, or something upstream broke. A rejection
tally answers that in one line.

No thresholds live here. Everything comes from ScreenConfig.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from optscan.analytics.events import assess_events
from optscan.analytics.ivrank import Confidence
from optscan.analytics.returns import credit_to_width
from optscan.models import Right
from optscan.screener.config import ScreenConfig
from optscan.screener.context import ExpiryAnalysis, SymbolAnalysis
from optscan.screener.strategies.base import Candidate


class Rejection(StrEnum):
    """Why a candidate did not make the list."""

    DTE_TOO_SHORT = "dte_too_short"
    DTE_TOO_LONG = "dte_too_long"
    DELTA_TOO_LOW = "delta_too_low"
    DELTA_TOO_HIGH = "delta_too_high"
    NO_DELTA = "no_delta"
    CREDIT_TOO_SMALL = "credit_too_small"
    MAX_PROFIT_TOO_SMALL = "max_profit_too_small"
    CREDIT_TO_WIDTH_TOO_LOW = "credit_to_width_too_low"
    RETURN_TOO_LOW = "return_too_low"
    OPEN_INTEREST_TOO_LOW = "open_interest_too_low"
    VOLUME_TOO_LOW = "volume_too_low"
    SPREAD_TOO_WIDE = "spread_too_wide"
    LIQUIDITY_TOO_LOW = "liquidity_too_low"
    IV_RANK_TOO_LOW = "iv_rank_too_low"
    IV_RANK_UNAVAILABLE = "iv_rank_unavailable"
    IV_TOO_LOW = "iv_too_low"
    EARNINGS_BEFORE_EXPIRY = "earnings_before_expiry"
    EARLY_ASSIGNMENT_RISK = "early_assignment_risk"


@dataclass(frozen=True, slots=True)
class FilterResult:
    """Whether a candidate passed, and if not, why."""

    passed: bool
    reason: Rejection | None = None

    def __bool__(self) -> bool:
        return self.passed


PASSED = FilterResult(True)


def _fail(reason: Rejection) -> FilterResult:
    return FilterResult(False, reason)


def check_dte(candidate: Candidate, config: ScreenConfig) -> FilterResult:
    dte = candidate.profile.dte
    if dte < config.filters.dte.min_dte:
        return _fail(Rejection.DTE_TOO_SHORT)
    if dte > config.filters.dte.max_dte:
        return _fail(Rejection.DTE_TOO_LONG)
    return PASSED


def check_delta(candidate: Candidate, config: ScreenConfig) -> FilterResult:
    """Delta band on the short leg.

    Multi leg positions are checked on their nearest short strike rather than on net
    position delta, because the band is about how far the tested strike is, not about
    directional exposure. A condor is near delta neutral and its shorts are still 20
    delta each.
    """
    deltas = [abs(leg.delta) for leg in candidate.short_legs if leg.delta is not None]
    if not deltas:
        return _fail(Rejection.NO_DELTA)
    worst = max(deltas)
    if worst < config.filters.delta.min_abs_delta:
        return _fail(Rejection.DELTA_TOO_LOW)
    if worst > config.filters.delta.max_abs_delta:
        return _fail(Rejection.DELTA_TOO_HIGH)
    return PASSED


def check_premium(candidate: Candidate, config: ScreenConfig) -> FilterResult:
    rules = config.filters.premium
    if candidate.credit < rules.min_credit:
        return _fail(Rejection.CREDIT_TOO_SMALL)

    if candidate.profile.max_profit < rules.min_max_profit:
        return _fail(Rejection.MAX_PROFIT_TOO_SMALL)

    if candidate.width and (
        credit_to_width(candidate.credit, candidate.width) < rules.min_credit_to_width
    ):
        return _fail(Rejection.CREDIT_TO_WIDTH_TOO_LOW)

    annualized = candidate.profile.annualized_return
    if annualized is not None and annualized < rules.min_annualized_return:
        return _fail(Rejection.RETURN_TOO_LOW)
    return PASSED


def check_liquidity(candidate: Candidate, config: ScreenConfig) -> FilterResult:
    """Every leg has to be tradeable, not just the short one."""
    rules = config.filters.liquidity
    for leg in candidate.legs:
        if leg.open_interest is not None and leg.open_interest < rules.min_open_interest:
            return _fail(Rejection.OPEN_INTEREST_TOO_LOW)
        if leg.volume is not None and leg.volume < rules.min_volume:
            return _fail(Rejection.VOLUME_TOO_LOW)
        if leg.mid and leg.bid is not None and leg.ask is not None:
            spread_pct = (leg.ask - leg.bid) / leg.mid
            if spread_pct > rules.max_spread_pct:
                return _fail(Rejection.SPREAD_TOO_WIDE)

    if candidate.liquidity_score is not None and (
        candidate.liquidity_score < rules.min_liquidity_score
    ):
        return _fail(Rejection.LIQUIDITY_TOO_LOW)
    return PASSED


def check_volatility(analysis: SymbolAnalysis, config: ScreenConfig) -> FilterResult:
    """IV rank and absolute IV gates, applied per symbol rather than per candidate.

    require_iv_rank exists because an IV rank filter is worse than useless before the
    history is deep enough to support one: it would either pass everything or reject
    everything, depending on which side of the threshold three weeks of data happened
    to land. Off by default, and when it is on, an unusable rank rejects rather than
    silently passing.
    """
    rules = config.filters.volatility

    if rules.min_iv is not None:
        front = analysis.expiries[0].atm_iv if analysis.expiries else None
        if front is None or front < rules.min_iv:
            return _fail(Rejection.IV_TOO_LOW)

    if rules.min_iv_rank is None and not rules.require_iv_rank:
        return PASSED

    rank = analysis.iv_rank
    if rank is None or rank.rank is None or rank.confidence is Confidence.INSUFFICIENT:
        if rules.require_iv_rank:
            return _fail(Rejection.IV_RANK_UNAVAILABLE)
        return PASSED

    if rules.min_iv_rank is not None and rank.rank < rules.min_iv_rank:
        return _fail(Rejection.IV_RANK_TOO_LOW)
    return PASSED


def check_events(
    candidate: Candidate,
    analysis: SymbolAnalysis,
    expiry: ExpiryAnalysis,
    config: ScreenConfig,
) -> FilterResult:
    """Earnings inside the position's life, and dividend driven assignment risk."""
    rules = config.filters.events
    risk = _event_risk(candidate, analysis, expiry)

    if rules.exclude_earnings and risk.has_earnings:
        return _fail(Rejection.EARNINGS_BEFORE_EXPIRY)
    if rules.exclude_early_assignment_risk and risk.early_assignment_risk:
        return _fail(Rejection.EARLY_ASSIGNMENT_RISK)
    return PASSED


def _event_risk(candidate: Candidate, analysis: SymbolAnalysis, expiry: ExpiryAnalysis):
    """Event assessment for the position's worst short call, if it has one."""
    short_call = next(
        (leg for leg in candidate.short_legs if leg.right is Right.CALL),
        None,
    )
    extrinsic = None
    if short_call is not None and short_call.mid is not None:
        intrinsic = max(analysis.spot - short_call.strike, 0.0)
        extrinsic = short_call.mid - intrinsic

    return assess_events(
        analysis.events,
        expiry.expiry,
        analysis.session_date,
        right=Right.CALL if short_call else None,
        extrinsic_value=extrinsic,
        assignment_window_days=5,
    )


def evaluate(
    candidate: Candidate,
    analysis: SymbolAnalysis,
    expiry: ExpiryAnalysis,
    config: ScreenConfig,
) -> FilterResult:
    """Run every per candidate filter, stopping at the first failure.

    Order is cheapest and most decisive first, so the rejection tally reports the
    most informative reason rather than whichever check happened to run last.
    """
    for check in (check_dte, check_delta, check_premium, check_liquidity):
        result = check(candidate, config)
        if not result:
            return result
    return check_events(candidate, analysis, expiry, config)


def evaluate_all(
    candidate: Candidate,
    analysis: SymbolAnalysis,
    expiry: ExpiryAnalysis,
    config: ScreenConfig,
) -> list[Rejection]:
    """Every check group a candidate failed, rather than only the first.

    `evaluate` stops at the first failure because the tally wants the most decisive
    reason and the scan calls it tens of thousands of times per run. That is the wrong
    shape for a different question: whether a candidate is one adjustment away from
    passing. A position blocked by a single gate is worth watching, one blocked by
    four is not, and a short circuiting check cannot tell them apart.

    Granularity is the check group, not the individual threshold. `check_premium`
    still returns at its own first failing sub-gate, so a candidate under both the
    credit floor and the return floor reports one reason here, not two. Read the
    result as written: "failed exactly one group" means one group, and that group may
    be hiding a second problem behind the reason it names.
    """
    reasons: list[Rejection] = []
    for check in (check_dte, check_delta, check_premium, check_liquidity):
        result = check(candidate, config)
        if not result and result.reason is not None:
            reasons.append(result.reason)

    events = check_events(candidate, analysis, expiry, config)
    if not events and events.reason is not None:
        reasons.append(events.reason)
    return reasons


@dataclass
class RejectionTally:
    """Counts of why candidates were dropped, for reporting.

    An empty result table with no explanation is the most common way a screener
    loses a user's trust.
    """

    counts: Counter[Rejection]
    considered: int = 0
    passed: int = 0

    @classmethod
    def empty(cls) -> RejectionTally:
        return cls(counts=Counter())

    def record(self, result: FilterResult) -> None:
        self.considered += 1
        if result.passed:
            self.passed += 1
        elif result.reason is not None:
            self.counts[result.reason] += 1

    def summary(self, limit: int = 5) -> str:
        """One line: what happened to everything that did not make the list."""
        if not self.counts:
            return f"{self.considered} candidates considered, {self.passed} passed"
        top = ", ".join(
            f"{reason.value} {count}" for reason, count in self.counts.most_common(limit)
        )
        return (
            f"{self.considered} candidates considered, {self.passed} passed. Top rejections: {top}"
        )

    def merge(self, other: RejectionTally) -> None:
        self.counts.update(other.counts)
        self.considered += other.considered
        self.passed += other.passed


def earnings_in_window(analysis: SymbolAnalysis, expiry: date, config: ScreenConfig) -> bool:
    """Whether earnings fall before an expiry, honoring the configured buffer."""
    earnings = analysis.events.earnings
    if earnings is None:
        return False
    buffer = timedelta(days=config.filters.events.earnings_buffer_days)
    return analysis.session_date <= earnings <= expiry + buffer
