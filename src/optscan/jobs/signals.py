"""The signal scan: evaluate every symbol with enough history, and alert once each.

Reads only what is already stored. No provider is contacted, which is deliberate: this
runs after the price sync rather than fetching its own bars, so a scan is cheap enough
to run several times a day and cannot be the thing that burns the rate limit before a
capture window.

## The window, and why it is bounded

Levels are built from a trailing two years rather than the full stored history. The
whole history would be worse rather than better: a swing level from 2016 is not a price
anyone is currently defending, and including it inflates the pool of levels price can
be "near" until the approach signal fires on everything. Two years is long enough for
the touch test to have something to count and short enough that the levels are current.

## Stale symbols are skipped, and this is not a detail

A symbol whose price history stopped updating still has a most recent bar, and every
signal here is defined on the most recent bar. So a delisted ticker reports its final
session forever, and reports it as though it happened today.

That is not hypothetical. On the first live run, five of fifteen signals came from
eight symbols that stopped trading between 2024 and early 2026: LTHM at 5.8x volume,
TRUE at 15.6x, PLL at 5.5x. Those are not surges, they are the last day of trading
before an acquisition closed, which is the highest volume day a ticker ever has. A
third of the output was archaeology presented as news.

So a symbol is only evaluated if its last stored session is close to the newest session
anywhere in the database. The newest stored session rather than today's date, because
the calendar does not know which days were holidays and the database does.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from datetime import time as clock

from optscan.alerts import Alert, AlertSink, default_sinks, raise_signals
from optscan.analytics.levels import build_levels
from optscan.analytics.signals import MIN_BARS, Signal, evaluate
from optscan.config import Settings
from optscan.logging import get_logger
from optscan.models import PriceBar
from optscan.storage import db
from optscan.storage.vendor import daily_bars, preferred_source

log = get_logger("optscan.jobs.signals")

#: Sessions of history each evaluation sees. See the module docstring: longer is not
#: better, because a level nobody has defended in five years still counts as a level.
DEFAULT_WINDOW = 504

#: How far behind the newest stored session a symbol may be and still be evaluated.
#: Generous enough for a halt or a vendor's missing day, far short of a delisting. See
#: the module docstring: this is what stops the scan reporting 2024 as this morning.
MAX_STALE_DAYS = 7

#: Severity at or above which a signal chases somebody rather than waiting in the
#: dashboard. At 3 that is level breaks and the two composites; the squeeze and the
#: volume surge sit below it on purpose, because they are context rather than events.
DEFAULT_MIN_SEVERITY = 3


@dataclass
class ScanReport:
    """What one scan actually did."""

    scanned: int = 0
    skipped_short: list[str] = field(default_factory=list)
    skipped_no_source: list[str] = field(default_factory=list)
    #: (symbol, last session) for symbols whose history stopped updating.
    skipped_stale: list[tuple[str, str]] = field(default_factory=list)
    found: list[Signal] = field(default_factory=list)
    delivered: list[Alert] = field(default_factory=list)

    @property
    def by_kind(self) -> Counter[str]:
        return Counter(signal.kind.value for signal in self.found)

    def summary(self) -> str:
        if not self.found:
            return f"{self.scanned} symbols scanned, nothing fired"
        kinds = ", ".join(f"{kind} {count}" for kind, count in self.by_kind.most_common())
        return (
            f"{self.scanned} symbols scanned, {len(self.found)} signals "
            f"({kinds}), {len(self.delivered)} delivered"
        )

    def warning(self) -> str | None:
        """The sentence a human needs, or None when nothing needs saying.

        The check this project keeps asking of its detectors: a scan where most symbols
        fired is not a good day, it is a broken detector, and it should say so on the
        way past rather than waiting for somebody to notice the volume of mail.
        """
        parts = []
        if self.scanned and len(self.found) > self.scanned:
            parts.append(
                f"{len(self.found)} signals across {self.scanned} symbols is more than "
                "one apiece, which is a detector describing the market rather than "
                "finding anything in it. Check the thresholds before trusting these."
            )
        if self.skipped_short:
            parts.append(
                f"{len(self.skipped_short)} symbols have fewer than {MIN_BARS} stored "
                "sessions and were not evaluated. Run `optscan prices sync`."
            )
        if self.skipped_no_source:
            parts.append(f"{len(self.skipped_no_source)} symbols have no stored price source.")
        if self.skipped_stale:
            named = ", ".join(f"{symbol} ({last})" for symbol, last in self.skipped_stale[:6])
            extra = len(self.skipped_stale) - 6
            more = f" and {extra} more" if extra > 0 else ""
            parts.append(
                f"{len(self.skipped_stale)} symbols stopped updating and were skipped "
                f"({named}{more}). Their last session would otherwise be reported as "
                "though it were today, and a delisting's final day is the highest "
                "volume day a ticker ever has."
            )
        return " ".join(parts) or None


def _to_bars(rows: Sequence, source: str) -> list[PriceBar]:
    """Stored vendor rows into the analytics shape.

    Midnight UTC for the timestamp, matching what the history endpoint already does for
    a stored daily bar. Only the date is ever read back off these.
    """
    return [
        PriceBar(
            symbol=row.symbol,
            ts=datetime.combine(row.session_date, clock(), tzinfo=UTC),
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=row.volume,
            fetched_at=datetime.now(UTC),
            source=source,
        )
        for row in rows
    ]


#: Why a symbol was not evaluated, when it was not.
NO_SOURCE = "no_source"
TOO_SHORT = "too_short"
STALE = "stale"


def newest_session(conn) -> date | None:
    """The most recent session held for any symbol: the market's last known day.

    Used as the reference for staleness rather than today's date, because the calendar
    does not know which weekdays were holidays and this does. It also means a scan run
    before the day's price sync measures every symbol against the same reference and
    does not declare the whole universe stale at once.
    """
    row = conn.execute("SELECT MAX(session_date) FROM vendor_daily").fetchone()
    return date.fromisoformat(row[0]) if row and row[0] else None


def scan_symbol(
    conn,
    symbol: str,
    *,
    window: int = DEFAULT_WINDOW,
    asof: date | None = None,
    max_stale_days: int = MAX_STALE_DAYS,
    **thresholds,
) -> tuple[list[Signal] | None, str | None]:
    """Every condition holding on `symbol` right now, and why not when there is a why.

    Returns (signals, None) or (None, reason). An empty list and None are different
    answers and the caller needs both: nothing fired, versus nobody looked.

    `asof` is the reference for staleness, defaulting to the newest session held. Pass
    it explicitly when scanning many symbols so it is queried once rather than per name.
    """
    source = preferred_source(conn, symbol)
    if source is None:
        return None, NO_SOURCE

    bars = _to_bars(daily_bars(conn, symbol, source=source), source)
    if len(bars) < MIN_BARS:
        return None, TOO_SHORT

    reference = asof if asof is not None else newest_session(conn)
    last = bars[-1].ts.date()
    if reference is not None and (reference - last).days > max_stale_days:
        return None, STALE

    recent = bars[-window:]
    levels = build_levels(recent)
    return evaluate(symbol, recent, levels.all(), **thresholds), None


def run_scan(
    settings: Settings,
    symbols: Sequence[str],
    *,
    window: int = DEFAULT_WINDOW,
    min_severity: int = DEFAULT_MIN_SEVERITY,
    send_alerts: bool = True,
    sinks: Sequence[AlertSink] | None = None,
    now: datetime | None = None,
    **thresholds,
) -> ScanReport:
    """Evaluate every symbol and deliver what is new.

    `send_alerts=False` evaluates and reports without delivering or recording, which is
    what the CLI's dry run uses. It must not record either: a preview that consumed the
    suppression would mean the real run stayed silent.
    """
    report = ScanReport()
    destinations = list(sinks) if sinks is not None else default_sinks(settings.data_path)

    with db.session(settings.sqlite_path) as conn:
        # Queried once. Per symbol it would be the same answer three hundred times.
        asof = newest_session(conn)

        for symbol in symbols:
            found, reason = scan_symbol(conn, symbol, window=window, asof=asof, **thresholds)
            if reason == NO_SOURCE:
                report.skipped_no_source.append(symbol)
                continue
            if reason == STALE:
                stale_source = preferred_source(conn, symbol)
                stale_last = daily_bars(conn, symbol, source=stale_source)[-1]
                report.skipped_stale.append((symbol, stale_last.session_date.isoformat()))
                continue
            if found is None:
                # Narrows the type as well as recording the skip, so the remaining
                # branch does not need an assert that `python -O` would strip.
                report.skipped_short.append(symbol)
                continue

            report.scanned += 1
            report.found.extend(found)

            if found and send_alerts:
                report.delivered.extend(
                    raise_signals(
                        conn,
                        found,
                        destinations,
                        min_severity=min_severity,
                        now=now,
                    )
                )

    log.info(
        "signal scan",
        scanned=report.scanned,
        found=len(report.found),
        delivered=len(report.delivered),
    )
    return report
