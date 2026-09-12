"""Cboe volatility indices as a free, always current IV history for four ETFs.

## Why

IV rank needs a year of one series. The only long series held were Market Chameleon
exports, which stop on the day they were downloaded: on 2026-09-12 AAPL and QQQ were
ranking against their own reading from 2026-09-04, and the trial allows two downloads a
day. Cboe publishes a thirty day implied volatility index for several of the ETFs this
account trades, every session, for decades, and Yahoo serves them for free:

    SPY  VIX    S&P 500
    QQQ  VXN    Nasdaq-100
    GLD  GVZ    gold, computed on GLD options themselves

IWM would take RVX, the Russell 2000 index, but Yahoo returns no data for ^RVX
(checked 2026-09-12, "possibly delisted"), so it is left out rather than failing the
daily sync every morning. IWM keeps the history this project captures for itself.

## What makes it honest to rank against

A rank places today inside a range, so today and the range have to be one series. They
are here: today's index close is ranked against the same index's past year, from the
same publisher, updated daily. The index is not the ETF's own ATM vol -- VIX is a
variance swap on the index and carries the skew, so it sits above SPY's at-the-money
number -- and it is never mixed with either the vendor export or this project's own
solved vols. It is a third series, chosen or not as a whole. See `choose_iv_history`.

Single stocks have no current Cboe index, so they keep whatever they had.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from optscan.config import Settings
from optscan.logging import get_logger
from optscan.screener.history import IvHistory, vendor_iv_history
from optscan.storage import db, vol_index

log = get_logger("optscan.jobs.vol_indices")

#: Which index measures which ETF's thirty day implied volatility.
VOL_INDICES: dict[str, str] = {"SPY": "^VIX", "QQQ": "^VXN", "GLD": "^GVZ"}

#: How each is named where a rank says what it was measured against.
INDEX_NAMES: dict[str, str] = {
    "^VIX": "Cboe VIX",
    "^VXN": "Cboe VXN",
    "^GVZ": "Cboe GVZ",
}

#: First fetch: ten years, which is more than any rank here reads and cheap to hold.
BACKFILL_DAYS = 3650

#: Every fetch after the first: enough to cover a long weekend and a missed day or two.
TOP_UP_DAYS = 30

#: Published in vol points; ranks and every other vol in this project are decimals.
VOL_POINTS = 100.0

Fetch = Callable[[str, int], list[tuple[date, float]]]


@dataclass
class SyncReport:
    stored: dict[str, int] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        parts = [f"{INDEX_NAMES.get(index, index)} {count}" for index, count in self.stored.items()]
        text = "Volatility indices: " + (", ".join(parts) if parts else "nothing stored")
        if self.failed:
            text += ". Failed: " + ", ".join(f"{i} ({why})" for i, why in self.failed.items())
        return text


def _yahoo(index: str, days: int) -> list[tuple[date, float]]:
    """Daily closes from Yahoo. A seam: tests replace it, because it reaches the network."""
    from optscan.providers.yfinance_provider import YFinanceProvider  # noqa: PLC0415

    return [(bar.ts.date(), bar.close) for bar in YFinanceProvider().get_history(index, days)]


def sync_vol_indices(settings: Settings, *, fetch: Fetch | None = None) -> SyncReport:
    """Fetch every index: a full backfill the first time, a short top-up after that.

    A failure on one index is recorded and the rest carry on. This runs inside the daily
    price sync, and a Yahoo hiccup must not fail that job or the price history it keeps.
    """
    fetch = fetch or _yahoo
    report = SyncReport()
    with db.session(settings.sqlite_path) as conn:
        held = vol_index.latest(conn)

    for index in sorted(set(VOL_INDICES.values())):
        days = TOP_UP_DAYS if index in held else BACKFILL_DAYS
        try:
            rows = fetch(index, days)
        except Exception as error:  # a vendor failure is reported, never raised
            report.failed[index] = str(error)[:120]
            log.warning("volatility index fetch failed", index=index, error=str(error))
            continue
        with db.session(settings.sqlite_path) as conn:
            report.stored[index] = vol_index.save_closes(conn, index, rows)

    log.info("volatility indices", stored=report.stored, failed=list(report.failed))
    return report


def index_history(conn, symbol: str, *, until: date | None = None) -> IvHistory:
    """The symbol's volatility index as an IV history, or an empty one if it has none.

    Built like a downloaded vendor series: the latest close on or before `until` is held
    out as today's reading and the rest is the range, both from the one index.
    """
    index = VOL_INDICES.get(symbol.strip().upper())
    if index is None:
        return IvHistory()
    points = [
        (day, close / VOL_POINTS) for day, close in vol_index.series(conn, index, until=until)
    ]
    return vendor_iv_history(points, INDEX_NAMES[index])
