"""Market signals: what is holding right now, and what has already fired.

Two routes because they answer two different questions, and conflating them would make
the panel lie in one direction or the other.

`/signals` **evaluates**. It is the current state of the market as this tool reads it,
computed on request from stored bars, and it says nothing about whether anyone was told.
Nothing is delivered and nothing is recorded, so opening the dashboard cannot consume
the suppression that the scheduled scan relies on.

`/signals/recent` **reads the record**. It is the list of what was actually delivered
and when, which is the only one of the two that can answer "did you tell me about this".

The scan is bounded to the watchlist by default rather than the whole universe. Three
hundred symbols is about two seconds of work, which is too long for a page load and
would be repeated on every refresh; the scheduled job is what covers the universe.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Query

from optscan.api.deps import SettingsDep
from optscan.api.schemas import SignalOut, SignalsOut
from optscan.jobs.signals import run_scan
from optscan.storage import db
from optscan.storage import signals as store
from optscan.storage.vendor import preferred_source

router = APIRouter(tags=["signals"])

#: Most symbols one request may evaluate. The bound exists because this runs on a page
#: load: a scan is roughly seven milliseconds a symbol, so this is about half a second.
MAX_SCAN_SYMBOLS = 80

#: Days of history the recent panel shows by default.
DEFAULT_RECENT_DAYS = 14


def _watchlist_symbols(settings) -> list[str]:
    with db.session(settings.sqlite_path) as conn:
        rows = conn.execute("SELECT symbol FROM watchlist ORDER BY symbol").fetchall()
        symbols = [row[0] for row in rows]
        # A symbol with no stored bars would be reported as skipped on every request,
        # which is true but not news. The catalogue is where that gap belongs.
        return [s for s in symbols if preferred_source(conn, s) is not None]


@router.get("/signals", response_model=SignalsOut)
def current_signals(
    settings: SettingsDep,
    symbol: str | None = Query(None, description="Evaluate one symbol instead."),
) -> SignalsOut:
    """Every condition holding right now. Evaluates; does not deliver or record."""
    symbols = [symbol.upper()] if symbol else _watchlist_symbols(settings)
    notes: list[str] = []

    if len(symbols) > MAX_SCAN_SYMBOLS:
        notes.append(
            f"Evaluating the first {MAX_SCAN_SYMBOLS} of {len(symbols)} symbols. The "
            "scheduled scan covers the whole universe; this route runs on a page load."
        )
        symbols = symbols[:MAX_SCAN_SYMBOLS]

    if not symbols:
        return SignalsOut(
            notes=[
                "No watchlist symbol has stored price history yet. "
                "Run `optscan prices sync` and the panel will fill in."
            ]
        )

    # send_alerts=False is the whole contract of this route. A dashboard refresh that
    # consumed the once-per-session suppression would leave the scheduled run silent.
    report = run_scan(settings, symbols, send_alerts=False)

    warning = report.warning()
    if warning:
        notes.append(warning)

    return SignalsOut(
        signals=[
            SignalOut(**signal.as_dict())
            for signal in sorted(report.found, key=lambda s: (-s.severity, s.symbol, s.kind.value))
        ],
        counts=dict(report.by_kind),
        scanned=report.scanned,
        notes=notes,
    )


@router.get("/signals/recent", response_model=SignalsOut)
def recent_signals(
    settings: SettingsDep,
    days: int = Query(DEFAULT_RECENT_DAYS, ge=1, le=365),
    symbol: str | None = Query(None),
    min_severity: int = Query(0, ge=0, le=5),
) -> SignalsOut:
    """What has actually been delivered, newest first. Reads the record; evaluates nothing."""
    since = (datetime.now(UTC) - timedelta(days=days)).date()
    with db.session(settings.sqlite_path) as conn:
        rows = store.recent_signals(
            conn,
            since=since,
            symbol=symbol.upper() if symbol else None,
            min_severity=min_severity,
        )
        counts = store.signal_counts(conn, since=since)

    notes = []
    if not rows:
        notes.append(
            f"Nothing has been delivered since {since}. Signals are recorded by the "
            "scheduled scan, so this stays empty until `optscan signals` has run."
        )

    return SignalsOut(
        signals=[SignalOut(**row) for row in rows],
        counts=counts,
        notes=notes,
    )
