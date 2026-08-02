"""Recording candidates, settling them at expiry, and reporting the result.

Three jobs that together answer the only question Phase 8 asks. They are separate on
purpose: recording runs daily and must be cheap, settling runs whenever an expiry has
passed and needs the network, and reporting is pure arithmetic over what is already
stored and needs neither.

**Start recording on day one.** The same lesson as the IV history, for the same reason:
a candidate that was never logged when it was scored cannot be settled later, and there
is no way to reconstruct what the screen would have surfaced last Tuesday. Every day
this does not run is a permanent hole in the study.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from optscan.analytics.calibration import ValidationReport, validate
from optscan.analytics.outcomes import settle
from optscan.config import Settings
from optscan.jobs.load import latest_snapshot
from optscan.logging import get_logger
from optscan.market_calendar import is_trading_day, market_local_date, previous_trading_day
from optscan.models import PriceBar
from optscan.providers import MarketDataProvider, ProviderError
from optscan.screener.config import ScreenConfig
from optscan.screener.context import analyze_snapshot
from optscan.screener.scan import ScanResult, scan_analysis
from optscan.storage import db
from optscan.storage import validation as store

log = get_logger("optscan.jobs.validate")

#: Calendar days of history fetched when settling. Generous, because an expiry may be
#: weeks old by the time anyone runs this and the close on that exact day is needed.
SETTLEMENT_HISTORY_DAYS = 400


@dataclass(frozen=True, slots=True)
class RecordResult:
    scan_id: int | None
    recorded: int = 0
    symbols: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ResolveResult:
    resolved: int = 0
    pending: int = 0
    unavailable: int = 0
    notes: list[str] = field(default_factory=list)


def _config_digest(config: ScreenConfig) -> str:
    """A short hash of the effective screen config.

    Stored with each scan so that a later reader can tell whether two runs were even
    comparable. Changing a filter changes which candidates exist, and pooling outcomes
    across a config change compares two different screens.
    """
    return hashlib.sha256(config.to_yaml().encode("utf-8")).hexdigest()[:12]


def run_record(
    settings: Settings,
    config: ScreenConfig,
    *,
    symbols: list[str] | None = None,
    asof: date | None = None,
    limit: int | None = None,
) -> RecordResult:
    """Score the watchlist from stored snapshots and log every candidate.

    `limit` exists for a person trying it out and defaults to everything, because
    logging only the top few would bias the study toward whatever the score already
    likes. The point is to record the ones it ranked badly too.
    """
    session = asof or market_local_date(datetime.now(UTC), settings.market_timezone)
    notes: list[str] = []

    with db.session(settings.sqlite_path) as conn:
        db.seed_watchlist(conn, settings.default_watchlist)
        targets = [s.strip().upper() for s in symbols] if symbols else db.list_watchlist(conn)

        combined = ScanResult()
        scanned: list[str] = []
        for symbol in targets:
            snapshot = latest_snapshot(settings.snapshot_path, symbol)
            if snapshot is None:
                notes.append(f"No stored snapshot for {symbol}, so it was not scored.")
                continue
            try:
                analysis = analyze_snapshot(snapshot, rate=settings.risk_free_rate)
                combined.merge(scan_analysis(analysis, config))
                scanned.append(symbol)
            except (ValueError, KeyError) as error:
                notes.append(f"{symbol} could not be scored: {type(error).__name__}: {error}")

        candidates = list(combined.opportunities)
        if limit is not None:
            combined.rank(limit)
            candidates = list(combined.top(limit))
            notes.append(
                f"Only the top {limit} candidates were logged. A study that records "
                "only what the score already likes cannot test the score."
            )

        if not candidates:
            return RecordResult(
                scan_id=None,
                symbols=scanned,
                notes=[*notes, "No candidates passed the screen, so nothing was logged."],
            )

        scan_id = store.start_scan(
            conn,
            session_date=session,
            symbols=scanned,
            config_digest=_config_digest(config),
        )
        added = store.record_opportunities(conn, scan_id, session, candidates)

    log.info("recorded candidates", scan_id=scan_id, count=added, symbols=len(scanned))
    return RecordResult(scan_id=scan_id, recorded=added, symbols=scanned, notes=notes)


def run_resolve(
    settings: Settings,
    *,
    provider: MarketDataProvider | None = None,
    asof: date | None = None,
) -> ResolveResult:
    """Settle every logged candidate whose expiry has passed.

    Needs only the underlying's closing price on expiry day, which is why this works
    without a historical option chain. A candidate whose settlement price cannot be
    found is left unresolved rather than settled at an approximation: an outcome
    recorded against the wrong price is worse than a missing one, because the missing
    one is visible.
    """
    today = asof or datetime.now(UTC).date()
    notes: list[str] = []

    if provider is None:
        return ResolveResult(
            notes=["No provider available, so nothing could be settled."],
        )

    with db.session(settings.sqlite_path) as conn:
        pending = store.unresolved(conn, today)
        if not pending:
            return ResolveResult(notes=["Nothing is waiting to be settled."])

        closes: dict[str, dict[date, float]] = {}
        resolved = 0
        unavailable = 0

        for item in pending:
            if item.symbol not in closes:
                closes[item.symbol] = _closes(provider, item.symbol, notes)

            price, used = _settlement_price(closes[item.symbol], item.expiry, settings)
            if price is None:
                unavailable += 1
                continue

            if item.short_strike is None or item.short_right is None:
                unavailable += 1
                notes.append(f"Candidate {item.id} has no short strike recorded.")
                continue

            result = settle(
                short_right=item.short_right,
                short_strike=item.short_strike,
                credit=item.credit,
                settlement_price=price,
                width=item.width,
                max_loss=item.max_loss,
            )
            store.record_outcome(
                conn,
                item.id,
                settlement_date=used,
                settlement_price=price,
                settlement_source=provider.name,
                outcome=str(result.outcome),
                finished_beyond=result.finished_beyond,
                profit=result.profit,
                profit_fraction=result.profit_fraction,
                note=None if used == item.expiry else f"settled on {used}, expiry was a holiday",
            )
            resolved += 1

        remaining = len(store.unresolved(conn, today))

    if unavailable:
        notes.append(
            f"{unavailable} candidates could not be settled because no close was found "
            "for their expiry date. They stay unresolved rather than being settled at "
            "an approximation. The usual cause is running on expiry day itself, before "
            "the vendor has published that session's bar, and the next run settles them."
        )
    log.info("resolved outcomes", resolved=resolved, unavailable=unavailable)
    return ResolveResult(resolved=resolved, pending=remaining, unavailable=unavailable, notes=notes)


def _closes(
    provider: MarketDataProvider,
    symbol: str,
    notes: list[str],
) -> dict[date, float]:
    try:
        bars: list[PriceBar] = provider.get_history(symbol, SETTLEMENT_HISTORY_DAYS)
    except (ProviderError, OSError, ValueError) as error:
        notes.append(f"No price history for {symbol}: {type(error).__name__}: {error}")
        return {}
    return {bar.ts.date(): bar.close for bar in bars}


def _settlement_price(
    closes: dict[date, float],
    expiry: date,
    settings: Settings,
) -> tuple[float | None, date]:
    """The close on expiry day, or on the prior session when the market was shut.

    Options expire on the third Friday and settle against that session, but a holiday
    moves it to the Thursday. Falling back one trading day handles that; falling back
    further would silently settle against a price days away from the real one, so it
    stops after one step.

    The fallback is gated on the expiry not having been a trading day, and that gate is
    the whole point. `unresolved` selects `expiry <= today`, so a run on expiry day
    itself asks for a close the vendor has not published yet, and an ungated fallback
    settled it against yesterday while labelling it a holiday. Both the price and the
    label were wrong, and `record_outcome` refuses to overwrite, so the error was
    permanent. A trading day with no close yet is missing, not shut: return None, leave
    the candidate pending, and settle it tomorrow when the bar exists.
    """
    if expiry in closes:
        return closes[expiry], expiry
    if is_trading_day(expiry, settings.market_calendar):
        return None, expiry
    try:
        previous = previous_trading_day(expiry, settings.market_calendar)
    except ValueError:
        return None, expiry
    if previous in closes:
        return closes[previous], previous
    return None, expiry


def run_report(settings: Settings) -> ValidationReport:
    """The whole study, over everything settled so far."""
    with db.session(settings.sqlite_path) as conn:
        return validate(store.resolved(conn))


def status_counts(settings: Settings) -> dict[str, int]:
    with db.session(settings.sqlite_path) as conn:
        return store.counts(conn)


def next_settlement(settings: Settings) -> date | None:
    """The earliest logged expiry that has not settled yet.

    Printed by the report so an empty study says when it will stop being empty, rather
    than looking like something is broken.
    """
    with db.session(settings.sqlite_path) as conn:
        row = conn.execute(
            """
            SELECT MIN(o.expiry) FROM opportunity_log o
            LEFT JOIN opportunity_outcome r ON r.opportunity_id = o.id
            WHERE r.opportunity_id IS NULL
            """
        ).fetchone()
    return date.fromisoformat(row[0]) if row and row[0] else None


def suggested_schedule(settings: Settings) -> timedelta:
    """How often recording should run. Daily, matching the snapshot job."""
    del settings
    return timedelta(days=1)
