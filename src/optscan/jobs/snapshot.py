"""The daily chain snapshot job.

Nothing consumes its output yet. That is the point: IV rank and IV percentile need
months of history, and history cannot be backfilled from any free source. Every day
this job does not run is a permanent hole in the Phase 2 signal.

Design choices that follow from that:
- It never writes a snapshot it cannot attribute to a real trading session.
- A symbol that fails does not stop the others.
- A partial capture is written and flagged, not discarded. Ten of twelve expiries is
  worth keeping; pretending it was complete is not.
- Every attempt is recorded in sqlite, failures included, so gaps are explainable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from optscan.config import Settings
from optscan.logging import get_logger
from optscan.market_calendar import (
    SessionState,
    market_local_date,
    session_date_for,
    session_state,
)
from optscan.models import ChainSnapshot, OptionChain
from optscan.providers import MarketDataProvider, ProviderError, get_provider
from optscan.providers.retry import with_retry
from optscan.storage import db, snapshots

log = get_logger("optscan.jobs.snapshot")


@dataclass(slots=True)
class SymbolResult:
    """Outcome of one symbol's capture."""

    symbol: str
    ok: bool
    contracts: int = 0
    expiries: int = 0
    partial: bool = False
    path: str | None = None
    error: str | None = None


@dataclass(slots=True)
class SnapshotReport:
    """Outcome of one run of the job."""

    session_date: date | None
    started_at: datetime
    finished_at: datetime | None = None
    skipped_reason: str | None = None
    results: list[SymbolResult] = field(default_factory=list)

    @property
    def succeeded(self) -> list[SymbolResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed(self) -> list[SymbolResult]:
        return [r for r in self.results if not r.ok]

    @property
    def total_contracts(self) -> int:
        return sum(r.contracts for r in self.results)


def select_expiries(
    expirations: list[date],
    asof: date,
    *,
    max_dte: int,
    max_expiries: int,
) -> list[date]:
    """Which expiries to capture: the nearest ones inside the DTE window.

    Nearest rather than a spread across the curve, because premium selling lives in
    the front of the curve and the far dated tail is where the storage cost is.
    Already expired dates are dropped: vendors keep listing them for a day or so.
    """
    eligible = [d for d in sorted(expirations) if 0 <= (d - asof).days <= max_dte]
    return eligible[:max_expiries]


def capture_symbol(
    provider: MarketDataProvider,
    symbol: str,
    settings: Settings,
    session_date: date,
) -> ChainSnapshot:
    """Fetch the quote and every selected expiry for one symbol.

    Raises ProviderError only when the symbol is a total loss, meaning no quote or no
    chain at all. Anything less is returned as a partial snapshot.
    """
    symbol = symbol.strip().upper()

    def _quote():
        return provider.get_quote(symbol)

    def _expirations():
        return provider.get_expirations(symbol)

    quote = with_retry(
        _quote,
        attempts=settings.max_retries,
        base_delay=settings.retry_backoff_seconds,
        description=f"get_quote {symbol}",
    )
    expirations = with_retry(
        _expirations,
        attempts=settings.max_retries,
        base_delay=settings.retry_backoff_seconds,
        description=f"get_expirations {symbol}",
    )

    wanted = select_expiries(
        expirations,
        session_date,
        max_dte=settings.snapshot_max_dte,
        max_expiries=settings.snapshot_max_expiries,
    )
    if not wanted:
        raise ProviderError(
            f"{symbol} has no listed expiries inside {settings.snapshot_max_dte} DTE"
        )

    chains: list[OptionChain] = []
    notes: list[str] = []
    for expiry in wanted:
        try:
            chain = with_retry(
                lambda expiry=expiry: provider.get_chain(symbol, expiry),
                attempts=settings.max_retries,
                base_delay=settings.retry_backoff_seconds,
                description=f"get_chain {symbol} {expiry}",
            )
            chains.append(chain)
        except ProviderError as error:
            notes.append(f"{expiry.isoformat()}: {type(error).__name__}: {error}")
            log.warning(
                "expiry capture failed",
                symbol=symbol,
                expiry=expiry.isoformat(),
                error=str(error),
                error_type=type(error).__name__,
            )

    if not chains:
        raise ProviderError(f"{symbol}: every expiry failed ({len(notes)} attempts)")

    return ChainSnapshot(
        symbol=symbol,
        session_date=session_date,
        quote=quote,
        chains=tuple(chains),
        partial=len(chains) < len(wanted),
        notes=tuple(notes),
        fetched_at=datetime.now(UTC),
        source=provider.name,
    )


def run_snapshot(
    settings: Settings,
    *,
    symbols: list[str] | None = None,
    force: bool = False,
    skip_existing: bool = True,
    provider: MarketDataProvider | None = None,
    now: datetime | None = None,
) -> SnapshotReport:
    """Capture every watchlist symbol for the current session.

    force ignores the market calendar. Useful on a weekend for testing, and dangerous
    in a scheduled task: an out of session capture records a stale mark under a
    session date it does not belong to. The session date then falls back to the market
    local date so the row is at least honestly labelled.
    """
    started = now or datetime.now(UTC)
    report = SnapshotReport(session_date=None, started_at=started)

    state = session_state(started, settings.market_calendar, settings.market_timezone)
    session_date = session_date_for(started, settings.market_calendar, settings.market_timezone)

    if session_date is None:
        if not force:
            report.skipped_reason = (
                f"not a capturable session ({state}). "
                "Use force to capture anyway, knowing the mark will be stale."
            )
            report.finished_at = datetime.now(UTC)
            log.info("snapshot skipped", reason=report.skipped_reason, state=str(state))
            return report
        session_date = market_local_date(started, settings.market_timezone)
        log.warning(
            "capturing outside a session because force was set",
            state=str(state),
            session_date=session_date.isoformat(),
        )

    report.session_date = session_date
    provider = provider or get_provider(settings)
    settings.ensure_dirs()

    with db.session(settings.sqlite_path) as conn:
        db.seed_watchlist(conn, settings.default_watchlist)
        targets = [s.strip().upper() for s in symbols] if symbols else db.list_watchlist(conn)

        if not targets:
            report.skipped_reason = "watchlist is empty"
            report.finished_at = datetime.now(UTC)
            log.warning("snapshot skipped", reason=report.skipped_reason)
            return report

        log.info(
            "snapshot starting",
            session_date=session_date.isoformat(),
            state=str(state),
            provider=provider.name,
            realtime=provider.realtime,
            symbols=len(targets),
        )

        for symbol in targets:
            if skip_existing and db.has_run(conn, symbol, session_date):
                log.info("already captured today, skipping", symbol=symbol)
                continue
            report.results.append(
                _capture_and_store(provider, symbol, settings, session_date, conn)
            )

    report.finished_at = datetime.now(UTC)
    log.info(
        "snapshot finished",
        session_date=session_date.isoformat(),
        ok=len(report.succeeded),
        failed=len(report.failed),
        contracts=report.total_contracts,
        seconds=round((report.finished_at - report.started_at).total_seconds(), 1),
    )
    return report


def _capture_and_store(
    provider: MarketDataProvider,
    symbol: str,
    settings: Settings,
    session_date: date,
    conn,
) -> SymbolResult:
    """One symbol, end to end, never raising. Failures become rows, not crashes."""
    try:
        snapshot = capture_symbol(provider, symbol, settings, session_date)
        path = snapshots.write_snapshot(snapshot, settings.snapshot_path)
        db.record_run(
            conn,
            symbol=symbol,
            session_date=session_date,
            captured_at=snapshot.fetched_at,
            provider=provider.name,
            expiries=len(snapshot.chains),
            contracts=snapshot.contract_count,
            partial=snapshot.partial,
            path=path,
        )
        return SymbolResult(
            symbol=symbol,
            ok=True,
            contracts=snapshot.contract_count,
            expiries=len(snapshot.chains),
            partial=snapshot.partial,
            path=str(path),
        )
    except (ProviderError, ValueError) as error:
        message = f"{type(error).__name__}: {error}"
        log.error("symbol capture failed", symbol=symbol, error=message)
        db.record_run(
            conn,
            symbol=symbol,
            session_date=session_date,
            captured_at=datetime.now(UTC),
            provider=provider.name,
            expiries=0,
            contracts=0,
            error=message,
        )
        return SymbolResult(symbol=symbol, ok=False, error=message)


def describe_state(settings: Settings, now: datetime | None = None) -> str:
    """One line summary of where the clock is, for the CLI and logs."""
    moment = now or datetime.now(UTC)
    state = session_state(moment, settings.market_calendar, settings.market_timezone)
    if state is SessionState.CLOSED:
        return f"{settings.market_calendar} is closed today"
    return f"{settings.market_calendar} session is {state}"
