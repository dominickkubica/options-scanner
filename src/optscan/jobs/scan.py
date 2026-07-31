"""Wiring the scan pipeline to real data.

The screener is pure and takes snapshots, histories, and events as arguments. This is
where those arguments get loaded: from stored parquet by default, or from the provider
when the caller asks for live quotes.

Keeping the I/O here means the whole ranking pipeline stays testable against a frozen
fixture with no network and no filesystem anywhere near it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from optscan.analytics.events import EventWindow
from optscan.config import Settings
from optscan.jobs.load import latest_snapshot
from optscan.jobs.snapshot import capture_symbol
from optscan.logging import get_logger
from optscan.market_calendar import market_local_date
from optscan.models import ChainSnapshot, SymbolEvents
from optscan.providers import MarketDataProvider, ProviderError, get_provider
from optscan.screener.config import ScreenConfig
from optscan.screener.history import atm_iv_history
from optscan.screener.scan import ScanResult, scan_snapshots
from optscan.storage import db, read_snapshots

log = get_logger("optscan.jobs.scan")

#: A snapshot older than this is reported as stale in the scan output. Not refused:
#: a scan of yesterday's close is a legitimate thing to want, as long as nobody
#: mistakes it for live.
STALE_AFTER_HOURS = 6.0


@dataclass
class ScanInputs:
    """Everything gathered before the pure pipeline runs."""

    snapshots: list[ChainSnapshot] = field(default_factory=list)
    iv_histories: dict[str, list] = field(default_factory=dict)
    events: dict[str, EventWindow] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)


def gather(
    settings: Settings,
    symbols: list[str],
    *,
    live: bool = False,
    provider: MarketDataProvider | None = None,
    with_events: bool = True,
) -> ScanInputs:
    """Load snapshots, IV histories, and event calendars for a list of symbols.

    Stored snapshots by default. `live` fetches fresh chains, which is slower and is
    the only way to scan the current market rather than the last capture.
    """
    inputs = ScanInputs()
    provider = provider or (get_provider(settings) if (live or with_events) else None)

    for symbol in symbols:
        snapshot = _load_one(settings, symbol, live=live, provider=provider)
        if snapshot is None:
            inputs.missing.append(symbol)
            continue
        inputs.snapshots.append(snapshot)

        frame = read_snapshots(settings.snapshot_path, symbol=symbol)
        if not frame.empty:
            # The snapshot's own source, not the configured provider. A stored capture
            # is ranked against the history of the vendor that produced it, which stays
            # right even when the config has moved on since.
            history = atm_iv_history(frame, rate=settings.risk_free_rate, source=snapshot.source)
            inputs.iv_histories[symbol] = list(history.points)
            note = history.note()
            if note:
                log.info("iv history excludes other vendors", symbol=symbol, note=note)

        if with_events and provider is not None:
            window = _load_events(provider, symbol)
            if window is not None:
                inputs.events[symbol] = window

    return inputs


def _load_one(
    settings: Settings,
    symbol: str,
    *,
    live: bool,
    provider: MarketDataProvider | None,
) -> ChainSnapshot | None:
    if not live:
        return latest_snapshot(settings.snapshot_path, symbol)

    if provider is None:
        raise ValueError("a live scan needs a provider")
    session = market_local_date(datetime.now(UTC), settings.market_timezone)
    try:
        return capture_symbol(provider, symbol, settings, session)
    except ProviderError as error:
        log.error("live capture failed", symbol=symbol, error=str(error))
        return None


def _load_events(provider: MarketDataProvider, symbol: str) -> EventWindow | None:
    """Fetch the corporate calendar, tolerating a provider that has none."""
    try:
        events: SymbolEvents = provider.get_events(symbol)
    except NotImplementedError:
        return None
    except ProviderError as error:
        log.warning("event calendar unavailable", symbol=symbol, error=str(error))
        return None

    return EventWindow(
        earnings=events.earnings_date,
        ex_dividend=events.ex_dividend_date,
        dividend_amount=events.dividend_amount,
    )


def run_scan(
    settings: Settings,
    config: ScreenConfig,
    *,
    symbols: list[str] | None = None,
    live: bool = False,
    provider: MarketDataProvider | None = None,
    with_events: bool = True,
    now: datetime | None = None,
) -> tuple[ScanResult, ScanInputs]:
    """Load, scan, and rank. Returns the result and what went into it."""
    targets = symbols
    if not targets:
        with db.session(settings.sqlite_path) as conn:
            db.seed_watchlist(conn, settings.default_watchlist)
            targets = db.list_watchlist(conn)

    targets = [symbol.strip().upper() for symbol in targets]
    log.info("scan starting", symbols=len(targets), live=live)

    inputs = gather(settings, targets, live=live, provider=provider, with_events=with_events)
    if not inputs.snapshots:
        return ScanResult(), inputs

    result = scan_snapshots(
        inputs.snapshots,
        config,
        rate=settings.risk_free_rate,
        iv_histories=inputs.iv_histories,
        events=inputs.events,
        now=now or datetime.now(UTC),
    )
    return result, inputs
