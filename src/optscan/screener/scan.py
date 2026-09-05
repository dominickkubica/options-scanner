"""Scan orchestration: snapshot in, ranked opportunities out.

Pure. The caller loads the snapshot, the IV history, and the events, and hands them in.
That keeps the pipeline testable against a frozen fixture with no I/O anywhere near it,
which is what makes the snapshot tests in Phase 3 meaningful.

Pipeline, in order:

    snapshot -> solve the surface -> generate candidates -> filter -> score -> rank

Generation is deliberately generous and filtering is deliberately strict. A candidate
that was never generated cannot be explained, and the rejection tally is what turns an
empty result into a diagnosis.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from optscan.analytics.events import EventWindow
from optscan.logging import get_logger
from optscan.models import ChainSnapshot, Opportunity
from optscan.screener.config import ScreenConfig
from optscan.screener.context import SymbolAnalysis, analyze_snapshot
from optscan.screener.filters import (
    Rejection,
    RejectionTally,
    check_volatility,
    evaluate,
    evaluate_all,
)
from optscan.screener.gaps import Gap, find_gaps
from optscan.screener.scoring import score_candidate
from optscan.screener.strategies import generators_for

log = get_logger("optscan.screener.scan")

#: Near misses kept per blocking gate. Ranking them in one pool and cutting at a
#: global limit does not work: one gate dominates the top of the list by score and
#: starves every other, so the gate that clears by waiting was never reaching a caller
#: that asked for exactly that. Enough per gate to fill a panel, not a table.
NEAR_MISSES_PER_BLOCKER = 12


@dataclass(frozen=True, slots=True)
class NearMiss:
    """A candidate that failed exactly one check group, and the group that blocked it.

    Deliberately a separate list rather than an entry in `opportunities` carrying a
    flag. Everything in that list has passed the screen, and a caller that had to know
    about a flag to exclude these would eventually not know, and would be sizing
    positions the screen rejected.
    """

    opportunity: Opportunity
    blocker: Rejection


@dataclass
class ScanResult:
    """Everything one scan produced, including what it rejected and why."""

    opportunities: list[Opportunity] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    tally: RejectionTally = field(default_factory=RejectionTally.empty)
    symbols_scanned: list[str] = field(default_factory=list)
    symbols_failed: dict[str, str] = field(default_factory=dict)
    stale_symbols: dict[str, float] = field(default_factory=dict)
    near_misses: list[NearMiss] = field(default_factory=list)

    def top(self, limit: int) -> list[Opportunity]:
        return self.opportunities[:limit]

    def merge(self, other: ScanResult) -> None:
        self.opportunities.extend(other.opportunities)
        self.gaps.extend(other.gaps)
        self.tally.merge(other.tally)
        self.symbols_scanned.extend(other.symbols_scanned)
        self.symbols_failed.update(other.symbols_failed)
        self.stale_symbols.update(other.stale_symbols)
        self.near_misses.extend(other.near_misses)

    def rank(self, limit: int | None = None) -> None:
        """Sort by score, then by annualized return as a stable tiebreak."""
        self.opportunities.sort(
            key=lambda opportunity: (
                -opportunity.score,
                -(opportunity.annualized_return or 0.0),
                opportunity.symbol,
                opportunity.expiry,
            )
        )
        self.near_misses.sort(
            key=lambda item: (
                -item.opportunity.score,
                -(item.opportunity.annualized_return or 0.0),
                item.opportunity.symbol,
                item.opportunity.expiry,
            )
        )
        kept: list[NearMiss] = []
        per_blocker: Counter[Rejection] = Counter()
        for item in self.near_misses:
            if per_blocker[item.blocker] >= NEAR_MISSES_PER_BLOCKER:
                continue
            per_blocker[item.blocker] += 1
            kept.append(item)
        self.near_misses = kept

        if limit is not None:
            del self.opportunities[limit:]


def scan_analysis(
    analysis: SymbolAnalysis,
    config: ScreenConfig,
    *,
    collect_near_misses: bool = False,
) -> ScanResult:
    """Run the whole pipeline against one already solved symbol.

    `collect_near_misses` re-checks every rejected candidate against all the gates
    instead of stopping at the first, and scores the ones that failed only one. It
    is off by default because it is pure cost to a caller that only wants the
    ranked list, and the scan runs the filter tens of thousands of times.
    """
    result = ScanResult(symbols_scanned=[analysis.symbol])

    volatility = check_volatility(analysis, config)
    if not volatility:
        result.tally.record(volatility)
        log.info(
            "symbol rejected on volatility",
            symbol=analysis.symbol,
            reason=volatility.reason.value if volatility.reason else None,
        )
        return result

    generators = generators_for(config.strategies.enabled)

    for expiry in analysis.expiries:
        produced = 0
        for generator in generators:
            for candidate in generator.generate(analysis, expiry, config):
                if produced >= config.strategies.max_candidates_per_expiry:
                    break
                verdict = evaluate(candidate, analysis, expiry, config)
                result.tally.record(verdict)
                if not verdict:
                    if collect_near_misses:
                        _record_near_miss(result, candidate, analysis, expiry, config)
                    continue

                result.opportunities.append(score_candidate(candidate, analysis, expiry, config))
                produced += 1

    result.gaps = find_gaps(analysis, config.gaps)
    result.rank()
    return result


def _record_near_miss(
    result: ScanResult,
    candidate,
    analysis: SymbolAnalysis,
    expiry,
    config: ScreenConfig,
) -> None:
    """Keep a rejected candidate if exactly one check group stood in its way.

    Scored with the same function as a passing candidate, so the two lists rank on the
    same number and a watch item can be compared against a live one. Scoring a
    candidate the filters dropped can raise on the missing data that got it dropped,
    and a watch list is not worth failing a scan over, so it is guarded.
    """
    blockers = evaluate_all(candidate, analysis, expiry, config)
    if len(blockers) != 1:
        return
    # A contract under the DTE floor is not on its way to qualifying, it is on its way
    # out: tomorrow it is a day shorter. Listing it beside candidates that qualify by
    # waiting would put two opposite trajectories under one heading.
    if blockers[0] is Rejection.DTE_TOO_SHORT:
        return
    try:
        scored = score_candidate(candidate, analysis, expiry, config)
    except (ValueError, KeyError, ZeroDivisionError, TypeError):
        return
    result.near_misses.append(NearMiss(opportunity=scored, blocker=blockers[0]))


def scan_snapshot(
    snapshot: ChainSnapshot,
    config: ScreenConfig,
    *,
    rate: float,
    dividend_yield: float = 0.0,
    iv_history: list | None = None,
    events: EventWindow | None = None,
    now: datetime | None = None,
) -> ScanResult:
    """Solve a snapshot and scan it.

    A snapshot older than the market's current state produces stale opportunities, so
    the age comes back on the result rather than being silently accepted. The CLI is
    what decides whether to warn or refuse.
    """
    analysis = analyze_snapshot(
        snapshot,
        rate=rate,
        dividend_yield=dividend_yield,
        iv_history=iv_history,
        events=events,
        max_spread_pct=config.filters.liquidity.max_spread_pct,
        now=now,
    )

    result = scan_analysis(analysis, config)
    if analysis.quote_age_seconds is not None:
        result.stale_symbols[analysis.symbol] = analysis.quote_age_seconds
    return result


def scan_snapshots(
    snapshots: list[ChainSnapshot],
    config: ScreenConfig,
    *,
    rate: float,
    dividend_yield: float = 0.0,
    iv_histories: dict[str, list] | None = None,
    events: dict[str, EventWindow] | None = None,
    now: datetime | None = None,
) -> ScanResult:
    """Scan several symbols and rank them against each other.

    A symbol that fails is recorded and skipped. One bad snapshot must not cost the
    whole scan, for the same reason one bad symbol does not stop the capture job.
    """
    combined = ScanResult()
    histories = iv_histories or {}
    event_map = events or {}

    for snapshot in snapshots:
        try:
            combined.merge(
                scan_snapshot(
                    snapshot,
                    config,
                    rate=rate,
                    dividend_yield=dividend_yield,
                    iv_history=histories.get(snapshot.symbol),
                    events=event_map.get(snapshot.symbol),
                    now=now,
                )
            )
        except (ValueError, KeyError) as error:
            message = f"{type(error).__name__}: {error}"
            combined.symbols_failed[snapshot.symbol] = message
            log.error("symbol scan failed", symbol=snapshot.symbol, error=message)

    combined.rank(config.max_results)
    log.info(
        "scan finished",
        symbols=len(combined.symbols_scanned),
        failed=len(combined.symbols_failed),
        opportunities=len(combined.opportunities),
        gaps=len(combined.gaps),
        considered=combined.tally.considered,
    )
    return combined
