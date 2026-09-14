"""Fetch and cache intraday option trades for the short premium study.

Fills `storage/option_tape.py` one session at a time, for the underlyings asked for.
Safe to re-run and safe to interrupt: a held session is skipped, and a session is only
held once its manifest is written.

## Which strikes and expiries

Whole dollar strikes from `width` below the session's low to `width` above its high,
puts and calls, for the session's own expiry and the next `horizon` sessions'. Measured
on 2026-09-13 over four sessions from calm to crashing: strikes within five dollars of
QQQ or SPY print nearly every minute of a 0DTE session, ten dollars out prints most
minutes, and twenty out barely prints on a calm day. Three percent past the day's range
covers every strike a 0 to 2 DTE credit spread uses and stops where the prints do.

**Daily expiries are assumed.** QQQ and SPY list one every session, and have since late
2022, before Alpaca's option history begins in February 2024, so "the next session" is
always an expiry for them. A single name lists weeklies; for it most ladders come back
empty, and the report says how many did.

## Raw prices

The underlying is fetched unadjusted, because the ladder is centred on it. See
`get_session_minutes`: an adjusted 2024 minute sits about six dollars under what QQQ
traded at, which moves the ladder off the strikes that actually printed.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import partial
from time import sleep

from optscan.config import ALPACA_OPTIONS_START, Settings
from optscan.logging import get_logger
from optscan.market_calendar import is_trading_day, previous_trading_day, session_bounds
from optscan.providers import MarketDataProvider, ProviderError, get_provider
from optscan.providers.errors import RateLimited
from optscan.storage.option_tape import has_session, tape_root, write_session

log = get_logger("optscan.jobs.option_tape")

#: The two underlyings with an expiry every session.
DEFAULT_SYMBOLS = ("QQQ", "SPY")

#: How far past the day's range the strike ladder reaches. See the module docstring.
DEFAULT_WIDTH = 0.03

#: Expiries beyond the session's own. One covers 0 and 1 DTE entries.
DEFAULT_HORIZON = 1

#: How many failures to name before summarising the rest.
MAX_NAMED_FAILURES = 5

#: Requests a minute for a sync, under the vendor's 200 so bursts do not reach it.
SYNC_REQUESTS_PER_MINUTE = 150

#: A 429 is waited out and the session asked again, this many times, this long apart.
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_PAUSE_SECONDS = 20.0


def _patient[T](call: Callable[[], T], pause: float = RATE_LIMIT_PAUSE_SECONDS) -> T:
    """Run `call`, waiting out a rate limit rather than failing the session on it."""
    for attempt in range(RATE_LIMIT_RETRIES):
        try:
            return call()
        except RateLimited:
            if attempt == RATE_LIMIT_RETRIES - 1:
                raise
            log.warning("rate limited; waiting before asking again", seconds=pause)
            sleep(pause)
    raise AssertionError("unreachable")


@dataclass
class TapeReport:
    """What one sync did."""

    considered: int = 0
    held: int = 0
    stored: int = 0
    empty: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    contracts_asked: int = 0
    contracts_traded: int = 0
    bars: int = 0

    def summary(self) -> str:
        return (
            f"{self.stored} sessions fetched, {self.held} already held, {len(self.failed)} "
            f"failed. {self.contracts_traded} of {self.contracts_asked} contracts asked for "
            f"traded, {self.bars:,} minute bars stored"
        )

    def warning(self) -> str | None:
        parts = []
        if self.empty:
            parts.append(
                f"{len(self.empty)} sessions came back with no option trades at all "
                f"(first: {self.empty[0]}). Stored as empty so they are not asked again; "
                "for a symbol without daily expiries that is expected."
            )
        if self.failed:
            named = "; ".join(
                f"{k}: {v}" for k, v in list(self.failed.items())[:MAX_NAMED_FAILURES]
            )
            parts.append(
                f"{len(self.failed)} sessions failed and will be retried next run: {named}"
            )
        return " ".join(parts) or None


def trading_sessions(start: date, end: date, calendar: str = "NYSE") -> list[date]:
    days = []
    day = start
    while day <= end:
        if is_trading_day(day, calendar):
            days.append(day)
        day += timedelta(days=1)
    return days


def following_sessions(session: date, count: int, calendar: str = "NYSE") -> list[date]:
    """The next `count` trading days after `session`, which may lie past the range."""
    found: list[date] = []
    day = session
    while len(found) < count:
        day += timedelta(days=1)
        if is_trading_day(day, calendar):
            found.append(day)
    return found


def strike_ladder(low: float, high: float, width: float = DEFAULT_WIDTH) -> list[float]:
    """Whole dollar strikes from `width` under `low` to `width` over `high`."""
    if low <= 0 or high < low:
        raise ValueError(f"not a price range: {low} to {high}")
    return [
        float(strike)
        for strike in range(math.floor(low * (1 - width)), math.ceil(high * (1 + width)) + 1)
    ]


def sync_tape(
    settings: Settings,
    symbols: Sequence[str] = DEFAULT_SYMBOLS,
    *,
    start: date | None = None,
    end: date | None = None,
    width: float = DEFAULT_WIDTH,
    horizon: int = DEFAULT_HORIZON,
    provider: MarketDataProvider | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> TapeReport:
    """Fetch every session in the range that is not already held."""
    # Headroom under the vendor's 200 a minute. At the full rate the client's own limiter
    # let bursts through and one session in forty came back 429 (measured 2026-09-14).
    provider = provider or get_provider(
        settings.model_copy(
            update={
                "provider": "alpaca",
                "alpaca_requests_per_minute": min(
                    settings.alpaca_requests_per_minute, SYNC_REQUESTS_PER_MINUTE
                ),
            }
        )
    )
    minutes_of = getattr(provider, "get_session_minutes", None)
    ladder_of = getattr(provider, "get_option_ladder_tape", None)
    if minutes_of is None or ladder_of is None:
        raise ProviderError(
            f"{type(provider).__name__} has no historical intraday option trades. "
            "The premium study needs alpaca."
        )

    calendar = settings.market_calendar
    first = max(start or ALPACA_OPTIONS_START, ALPACA_OPTIONS_START)
    last = end or previous_trading_day(datetime.now(UTC).date(), calendar)
    root = tape_root(settings.data_path)
    wanted = [symbol.strip().upper() for symbol in symbols if symbol and symbol.strip()]
    sessions = trading_sessions(first, last, calendar)
    total = len(sessions) * len(wanted)

    report = TapeReport()
    for session in sessions:
        bounds = session_bounds(session, calendar)
        if bounds is None:
            continue
        opened, closed = bounds
        for symbol in wanted:
            report.considered += 1
            label = f"{symbol} {session}"
            if has_session(root, symbol, session):
                report.held += 1
                continue
            try:
                minutes = _patient(partial(minutes_of, symbol, opened, closed))
                strikes = strike_ladder(
                    min(bar.low for bar in minutes), max(bar.high for bar in minutes), width
                )
                expiries = [session, *following_sessions(session, horizon, calendar)]
                asked, tape = _patient(
                    partial(ladder_of, symbol, expiries, strikes, opened, closed)
                )
                write_session(
                    root,
                    symbol,
                    session,
                    minutes,
                    tape,
                    manifest={
                        "fetched_at": datetime.now(UTC).isoformat(),
                        "source": provider.name,
                        "expiries": [expiry.isoformat() for expiry in expiries],
                        "strikes": [strikes[0], strikes[-1]],
                        "width": width,
                        "contracts_asked": asked,
                    },
                )
            except (ProviderError, ValueError) as error:
                # One bad session must not cost the other thousand. Not written, so the
                # next run asks again.
                report.failed[label] = str(error)
                log.error("option tape session failed", session=label, error=str(error))
                continue

            report.stored += 1
            report.contracts_asked += asked
            traded = sum(1 for series in tape.values() if series)
            report.contracts_traded += traded
            report.bars += sum(len(series) for series in tape.values())
            if traded == 0:
                report.empty.append(label)
            if progress is not None:
                progress(report.considered, total, label)

    return report
