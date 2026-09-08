"""Bulk daily price history, for many symbols at once.

Fills `vendor_daily` from a provider that can serve a lot of symbols cheaply. That
table was built for a Market Chameleon download and turns out to be the right shape for
any vendor's daily series: identity is `(source, symbol, session_date)` and the source
is on it, so two vendors' histories coexist without ever being pooled.

## Why this is batched and paged rather than a loop over symbols

Alpaca's bars endpoint takes a comma separated list. Measured 2026-09-07: 58 symbols in
one request in 0.7 seconds, against 58 requests and roughly 35 seconds the naive way.
At a 200 request per minute limit the difference is the whole feature, because a few
hundred symbols of multi year history is otherwise an hour of waiting and most of a
rate limit budget that the daily option capture also needs.

## What is stored beyond the close

Three volume fields, not one, because they answer different questions:

  - `volume`, shares traded.
  - `trade_count`, the number of prints. Alpaca's `n`.
  - `vwap`, the volume weighted average price. Alpaca's `vw`.

Volume alone cannot distinguish 30 million shares in 500,000 prints, an ordinary
session, from the same 30 million in 5,000, which is a handful of blocks.
`VendorDailyBar.average_trade_size` is that ratio, and it is only computable because
the count is kept.

## The rule about symbols that do not come back

A vendor returns bars for the symbols it knows and simply omits the rest, with no error
and no mention. Measured: a 58 symbol request returned 57. Delistings, ticker changes
and typos all look identical from here, and all three are things a human needs to see.
So every requested symbol that produced no rows is **collected and reported**, never
silently dropped: a hole in a price history is invisible in every chart drawn over it.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from optscan.config import Settings
from optscan.logging import get_logger
from optscan.models.vendor import VendorDailyBar
from optscan.providers import MarketDataProvider, ProviderError, get_provider
from optscan.storage import db
from optscan.storage.vendor import import_daily_bars

log = get_logger("optscan.jobs.prices")

#: Symbols per request. Alpaca accepted 58 in one call; this leaves room under
#: whatever the real cap is and keeps a single failure from costing a large batch.
BATCH_SIZE = 50

#: Bars per page. The endpoint's own maximum.
PAGE_LIMIT = 10_000

#: Stop following page tokens after this many. A runaway loop against a rate limited
#: endpoint is the failure that costs a capture window.
MAX_PAGES = 200

#: How many missing symbols to name before summarising the rest. Enough to spot a
#: pattern, few enough that the sentence stays readable.
MAX_NAMED_MISSING = 12

#: Default history depth. Ten years covers every window the analytics use, including a
#: 200 session moving average and a realized vol series long enough to mean something.
DEFAULT_DAYS = 3650


@dataclass
class SyncReport:
    """What one sync actually did, per group."""

    requested: list[str] = field(default_factory=list)
    stored: dict[str, int] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    inserted: int = 0
    duplicate: int = 0

    @property
    def resolved(self) -> int:
        return len(self.stored)

    def summary(self) -> str:
        return (
            f"{self.resolved} of {len(self.requested)} symbols, "
            f"{self.inserted} new sessions, {self.duplicate} already held"
        )

    def warning(self) -> str | None:
        """The sentence a human needs, or None when everything resolved."""
        if not self.missing and not self.failed:
            return None
        parts = []
        if self.missing:
            shown = ", ".join(sorted(self.missing)[:MAX_NAMED_MISSING])
            extra = len(self.missing) - MAX_NAMED_MISSING
            more = f" and {extra} more" if extra > 0 else ""
            parts.append(
                f"{len(self.missing)} symbols returned no bars at all ({shown}{more}). "
                "The vendor omits what it does not know rather than saying so, so these "
                "are delistings, ticker changes or typos and cannot be told apart here."
            )
        if self.failed:
            parts.append(
                f"{len(self.failed)} batches failed: "
                + "; ".join(f"{k}: {v}" for k, v in list(self.failed.items())[:3])
            )
        return " ".join(parts)


def batched(symbols: Sequence[str], size: int = BATCH_SIZE) -> Iterator[list[str]]:
    for start in range(0, len(symbols), size):
        yield list(symbols[start : start + size])


def _bars_for_batch(
    provider: MarketDataProvider,
    symbols: list[str],
    start: date,
    end: datetime,
) -> dict[str, list[dict]]:
    """One multi symbol request, paged. Returns raw vendor rows keyed by symbol.

    Reaches past the MarketDataProvider interface deliberately. That interface is one
    symbol at a time by design, which is right for a screener and wrong for a bulk
    backfill; forcing this through it would turn one request into fifty.
    """
    fetch = getattr(provider, "get_daily_bars_bulk", None)
    if fetch is None:
        raise ProviderError(
            f"{type(provider).__name__} cannot fetch several symbols at once. "
            "Bulk price history needs a provider that can, such as alpaca."
        )
    return fetch(symbols, start, end, limit=PAGE_LIMIT, max_pages=MAX_PAGES)


def sync_daily_history(
    settings: Settings,
    symbols: Sequence[str],
    *,
    days: int = DEFAULT_DAYS,
    provider: MarketDataProvider | None = None,
    source: str | None = None,
) -> SyncReport:
    """Fetch and store daily history for many symbols. Safe to re-run.

    Re-running is the normal case: tomorrow's sync covers the same decade plus a day,
    and `import_daily_bars` counts what it already holds rather than rewriting it.
    """
    wanted = [s.strip().upper() for s in symbols if s and s.strip()]
    report = SyncReport(requested=wanted)
    if not wanted:
        return report

    provider = provider or get_provider(settings)
    source = source or provider.name
    # Stops short of now: the free plan refuses consolidated data inside the last
    # quarter hour, and the adapter's own clamp does not apply to a raw bulk call.
    end = datetime.now(UTC) - timedelta(minutes=20)
    start = end.date() - timedelta(days=days)

    with db.session(settings.sqlite_path) as conn:
        for batch in batched(wanted):
            try:
                rows = _bars_for_batch(provider, batch, start, end)
            except ProviderError as error:
                # One bad batch must not cost the other nine hundred symbols.
                report.failed[batch[0] + f"+{len(batch) - 1}"] = str(error)
                log.error("price batch failed", first=batch[0], size=len(batch), error=str(error))
                continue

            for symbol in batch:
                raw = rows.get(symbol) or []
                if not raw:
                    report.missing.append(symbol)
                    continue
                bars = [_to_bar(symbol, row, source) for row in raw]
                bars = [bar for bar in bars if bar is not None]
                if not bars:
                    report.missing.append(symbol)
                    continue
                result = import_daily_bars(conn, bars, file_name=f"{source}:bulk")
                report.stored[symbol] = len(bars)
                report.inserted += result.rows_inserted
                report.duplicate += result.rows_duplicate

            log.info(
                "price batch stored",
                first=batch[0],
                size=len(batch),
                running_total=report.inserted,
            )

    return report


def _to_bar(symbol: str, row: dict, source: str) -> VendorDailyBar | None:
    """One vendor bar row into the stored shape, or None if it cannot be trusted.

    A bar that fails its own coherence check is dropped rather than repaired. The
    model already refuses a close outside its high and low, and a repaired bar is a
    number nobody can trace.
    """
    try:
        session = datetime.fromisoformat(str(row["t"]).replace("Z", "+00:00")).date()
        return VendorDailyBar(
            source=source,
            symbol=symbol,
            session_date=session,
            open=float(row["o"]),
            high=float(row["h"]),
            low=float(row["l"]),
            close=float(row["c"]),
            # Alpaca's bars are requested with adjustment=all, so the close is already
            # split and dividend adjusted. There is no second unadjusted series to
            # store, unlike a Market Chameleon file which carries both.
            adj_close=float(row["c"]),
            volume=_int(row.get("v")),
            vwap=_float(row.get("vw")),
            trade_count=_int(row.get("n")),
        )
    except (KeyError, TypeError, ValueError) as error:
        log.debug("skipping unusable bar", symbol=symbol, error=str(error))
        return None


def _int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float(value) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
