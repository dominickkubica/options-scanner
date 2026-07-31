"""Ranked candidates, and the mispricing hunt.

Both run against the cached solved snapshots rather than through `jobs.scan.run_scan`.
That function loads and solves for itself, which is right for a CLI invocation that
exits afterwards and wrong for a server that has the same chains solved already.

The rejection tally comes back on every response. An empty results table with no
explanation is the fastest way for a screener to lose its user, and that is as true in
a browser as it was in a terminal: "no candidates passed" next to "20236 considered,
7151 rejected for dte_too_long" is a diagnosis, on its own it is a shrug.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from optscan.api.deps import (
    ProviderFactory,
    ProviderFactoryDep,
    ScreenConfigDep,
    SettingsDep,
    SolvedSymbol,
    solved_symbol,
)
from optscan.api.schemas import GapsOut, RejectionOut, ScanOut
from optscan.api.views import gap_view, opportunity_view
from optscan.config import Settings
from optscan.logging import get_logger
from optscan.screener.config import ScreenConfig
from optscan.screener.gaps import find_gaps
from optscan.screener.scan import ScanResult, scan_analysis
from optscan.storage import db

router = APIRouter(tags=["scan"])
log = get_logger("optscan.api.scan")

#: Rows a browser will actually render before a table stops being useful. The screen
#: config's own max_results still applies first.
MAX_LIMIT = 500

SymbolsQuery = Annotated[
    list[str] | None,
    Query(description="Repeatable. Defaults to the watchlist."),
]


def _targets(symbols: list[str] | None, settings: Settings) -> list[str]:
    if symbols:
        return [symbol.strip().upper() for symbol in symbols if symbol.strip()]
    with db.session(settings.sqlite_path) as conn:
        db.seed_watchlist(conn, settings.default_watchlist)
        return db.list_watchlist(conn)


def _load(
    symbols: list[str],
    settings: Settings,
    config: ScreenConfig,
    provider_factory: ProviderFactory,
) -> tuple[list[SolvedSymbol], list[str], list[str]]:
    """Solve every requested symbol. Returns (solved, missing, notes)."""
    solved: list[SolvedSymbol] = []
    missing: list[str] = []
    notes: list[str] = []

    for symbol in symbols:
        item = solved_symbol(symbol, settings, config, provider_factory=provider_factory)
        if item is None:
            missing.append(symbol)
            continue
        solved.append(item)
        if item.events_note and item.events_note not in notes:
            notes.append(item.events_note)

    if missing:
        notes.append(f"No stored snapshot for {', '.join(missing)}. Run `optscan snapshot` first.")
    return solved, missing, notes


@router.get("/scan", response_model=ScanOut)
def scan(
    settings: SettingsDep,
    config: ScreenConfigDep,
    provider_factory: ProviderFactoryDep,
    symbols: SymbolsQuery = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
) -> ScanOut:
    solved, _missing, notes = _load(_targets(symbols, settings), settings, config, provider_factory)

    combined = ScanResult()
    for item in solved:
        try:
            result = scan_analysis(item.analysis, config)
        except (ValueError, KeyError) as error:
            message = f"{type(error).__name__}: {error}"
            combined.symbols_failed[item.symbol] = message
            log.error("symbol scan failed", symbol=item.symbol, error=message)
            continue
        result.stale_symbols[item.symbol] = item.age_seconds()
        combined.merge(result)

    combined.rank(config.max_results)

    return ScanOut(
        opportunities=[opportunity_view(item) for item in combined.top(limit)],
        considered=combined.tally.considered,
        passed=combined.tally.passed,
        rejections=[
            RejectionOut(reason=reason.value, count=count)
            for reason, count in sorted(
                combined.tally.counts.items(), key=lambda pair: (-pair[1], pair[0].value)
            )
        ],
        symbols_scanned=combined.symbols_scanned,
        symbols_failed=combined.symbols_failed,
        stale=combined.stale_symbols,
        events_checked=bool(solved) and all(item.events_checked for item in solved),
        notes=notes,
    )


@router.get("/gaps", response_model=GapsOut)
def gaps(
    settings: SettingsDep,
    config: ScreenConfigDep,
    provider_factory: ProviderFactoryDep,
    symbols: SymbolsQuery = None,
) -> GapsOut:
    solved, _missing, notes = _load(_targets(symbols, settings), settings, config, provider_factory)

    if not config.gaps.enabled:
        notes.append("The gaps module is disabled in screen.yaml.")
    if not config.gaps.vertical_mispricing_enabled:
        notes.append(
            "Vertical mispricing detection is off. It measures the variance risk premium, "
            "which is present almost everywhere, and separating an anomaly from that needs "
            "a historical baseline this install does not have yet."
        )

    found = []
    if config.gaps.enabled:
        for item in solved:
            found.extend(find_gaps(item.analysis, config.gaps))

    return GapsOut(
        gaps=[gap_view(gap) for gap in found],
        symbols_scanned=[item.symbol for item in solved],
        notes=notes,
    )
