"""Shared request dependencies: config, caches, and the frontend path.

Three things live here, and each exists for a reason worth stating.

**The solved symbol cache.** Solving a chain is the expensive part of everything this
API does: several hundred milliseconds for a real SPY capture, and three of the four
dashboard panels ask about the same symbol at once. The cache is keyed on the
snapshot's own `fetched_at`, so a new capture invalidates it without anyone having to
remember to. A cache keyed on the symbol alone would serve yesterday's chain until
somebody restarted the server, which is exactly the failure this project keeps
guarding against.

**What the API is allowed to fetch.** Market data comes from stored snapshots only.
There is no live refresh, because delayed vendor quotes rendered in a dashboard get
read as live and there is no honest way to label them otherwise until Phase 5 brings
a real feed. Two exceptions, both reference data rather than quotes, both cached and
both degrading to a stated reason rather than to a plausible blank:

- the corporate calendar, because without it the API's screen would silently differ
  from the CLI's, which applies the earnings exclusion
- daily candles, because nothing stores them and a price chart is in the phase's
  exit criteria

**Provenance.** Every payload carrying market data gets its age computed here, once,
from the record's own fetched_at rather than from anything cached alongside it.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import Depends

from optscan.analytics.events import EventWindow
from optscan.api.schemas import Provenance
from optscan.config import REPO_ROOT, Settings, get_settings
from optscan.jobs.load import latest_snapshot
from optscan.jobs.scan import STALE_AFTER_HOURS
from optscan.logging import get_logger
from optscan.models import ChainSnapshot, PriceBar, SymbolEvents
from optscan.providers import MarketDataProvider, ProviderError, get_provider
from optscan.screener.config import DEFAULT_CONFIG_FILENAME, ScreenConfig
from optscan.screener.context import SymbolAnalysis, analyze_snapshot
from optscan.screener.history import atm_iv_history
from optscan.storage import read_snapshots

log = get_logger("optscan.api.deps")

#: Solved symbols held in memory. Six watchlist symbols is the normal case and a
#: solved SPY capture is a few megabytes, so this is generous rather than tight.
MAX_CACHED_SYMBOLS = 16

#: How long a fetched candle series is reused. Daily bars only move once a day, but
#: the current session's bar keeps updating, so this is short enough that the age
#: shown next to the chart stays small and long enough that clicking between panels
#: does not refetch.
HISTORY_TTL_SECONDS = 900.0

#: Default candle window for the underlying detail chart.
DEFAULT_HISTORY_DAYS = 180

#: Where a built frontend lands. Absent in development, where Vite serves it instead.
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"

ProviderFactory = Callable[[], MarketDataProvider]


@dataclass(frozen=True, slots=True)
class SolvedSymbol:
    """One symbol's stored snapshot, solved, with everything the panels need.

    The snapshot is kept alongside the analysis because provenance is computed from
    the snapshot's own fetched_at, and the analysis has already folded that into a
    quote age that was correct when it was solved and is not correct now.
    """

    symbol: str
    snapshot: ChainSnapshot
    analysis: SymbolAnalysis
    iv_history: list[tuple[date, float]] = field(default_factory=list)
    events: EventWindow | None = None
    events_note: str | None = None

    @property
    def events_checked(self) -> bool:
        return self.events is not None

    def age_seconds(self, now: datetime | None = None) -> float:
        """How old the underlying capture is, measured now and not at solve time."""
        return self.snapshot.age_seconds(now)

    def provenance(self, now: datetime | None = None) -> Provenance:
        return make_provenance(self.snapshot.source, self.snapshot.fetched_at, now)


def make_provenance(
    source: str,
    fetched_at: datetime,
    now: datetime | None = None,
    *,
    stale_after_hours: float = STALE_AFTER_HOURS,
) -> Provenance:
    """Age and staleness for one payload, from the record's own timestamp."""
    reference = now or datetime.now(UTC)
    age = (reference - fetched_at).total_seconds()
    return Provenance(
        source=source,
        fetched_at=fetched_at,
        age_seconds=age,
        stale=age > stale_after_hours * 3600.0,
    )


class _TimedCache:
    """Smallest thing that works: an LRU with an optional per entry expiry.

    Uvicorn runs sync endpoints in a thread pool, so this is locked. Nothing here is
    hot enough for the lock to matter and a torn cache would be very hard to see.
    """

    def __init__(self, maxsize: int) -> None:
        self._maxsize = maxsize
        self._entries: OrderedDict[object, tuple[float | None, object]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: object, now: float) -> object | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at is not None and now >= expires_at:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return value

    def put(self, key: object, value: object, expires_at: float | None = None) -> None:
        with self._lock:
            self._entries[key] = (expires_at, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._maxsize:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_SOLVED = _TimedCache(MAX_CACHED_SYMBOLS)
_EVENTS = _TimedCache(MAX_CACHED_SYMBOLS)
_HISTORY = _TimedCache(MAX_CACHED_SYMBOLS)

#: One lock per symbol and capture, so concurrent requests for the same chain solve it
#: once between them instead of once each.
#:
#: This is not a refinement, it is the case the cache exists for. Opening a symbol
#: fires the summary, the chain, and the scan at the same instant, uvicorn runs sync
#: endpoints in a thread pool, and all three miss a cache that none of them has
#: finished filling. Checking, solving, and storing under one lock turns three solves
#: into one solve and two waits.
_SOLVE_LOCKS: dict[object, threading.Lock] = {}
_SOLVE_LOCKS_GUARD = threading.Lock()


def _solve_lock(key: object) -> threading.Lock:
    with _SOLVE_LOCKS_GUARD:
        lock = _SOLVE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _SOLVE_LOCKS[key] = lock
        return lock


def _release_solve_lock(key: object) -> None:
    """Forget a key's lock once its result is cached.

    Anyone already waiting holds their own reference and will re-check the cache and
    hit it, so dropping the entry here only stops the dictionary growing by one lock
    per capture for the life of the process.
    """
    with _SOLVE_LOCKS_GUARD:
        _SOLVE_LOCKS.pop(key, None)


def clear_caches() -> None:
    """Drop everything cached. Called between tests, and by the app on startup."""
    _SOLVED.clear()
    _EVENTS.clear()
    _HISTORY.clear()
    with _SOLVE_LOCKS_GUARD:
        _SOLVE_LOCKS.clear()
    screen_config.cache_clear()


# --------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------


def settings_dep() -> Settings:
    """Process settings. A dependency rather than a direct call so tests can override."""
    return get_settings()


@lru_cache(maxsize=4)
def _load_screen_config(path: Path, mtime: float) -> ScreenConfig:
    """Cached on the file's own modification time, so editing screen.yaml takes hold.

    mtime is part of the key rather than something checked inside, which is what makes
    the cache correct: a changed file is a different key and the old entry is simply
    never asked for again.
    """
    del mtime
    return ScreenConfig.load(path)


def screen_config(path: Path | None = None) -> ScreenConfig:
    """The effective screen config, reloaded when the YAML changes on disk."""
    target = path or (REPO_ROOT / DEFAULT_CONFIG_FILENAME)
    mtime = target.stat().st_mtime if target.exists() else 0.0
    return _load_screen_config(target, mtime)


screen_config.cache_clear = _load_screen_config.cache_clear  # type: ignore[attr-defined]


def screen_config_dep() -> ScreenConfig:
    return screen_config()


def provider_factory_dep() -> ProviderFactory:
    """How the API gets a provider when it needs reference data.

    A factory rather than a provider, because most requests never need one and
    constructing a vendor adapter per request would be waste. Overridden in tests,
    which is what keeps the suite offline.
    """
    return lambda: get_provider(get_settings())


# Annotated aliases rather than `= Depends(...)` in the signature. Same wiring, and it
# keeps the call out of a default argument, which is a real trap everywhere except
# here and which the linter is right to refuse in general.
SettingsDep = Annotated[Settings, Depends(settings_dep)]
ScreenConfigDep = Annotated[ScreenConfig, Depends(screen_config_dep)]
ProviderFactoryDep = Annotated[ProviderFactory, Depends(provider_factory_dep)]


def frontend_dist() -> Path | None:
    """The built frontend directory, or None in development.

    None is the normal state while developing: Vite serves the app on 5173 and the
    API only answers /api. A missing dist is not an error and must not be reported
    as one, or every dev session starts with a spurious warning.
    """
    index = FRONTEND_DIST / "index.html"
    return FRONTEND_DIST if index.exists() else None


# --------------------------------------------------------------------------------
# The solved symbol cache
# --------------------------------------------------------------------------------


def solved_symbol(
    symbol: str,
    settings: Settings,
    config: ScreenConfig,
    *,
    provider_factory: ProviderFactory | None = None,
    with_events: bool = True,
) -> SolvedSymbol | None:
    """Load, solve, and cache one symbol's most recent stored snapshot.

    Returns None when nothing has ever been captured for the symbol, which is a normal
    state on a fresh install rather than an error. The caller turns that into a 404
    with a sentence about running the snapshot job.
    """
    normalized = symbol.strip().upper()
    snapshot = latest_snapshot(settings.snapshot_path, normalized)
    if snapshot is None:
        return None

    key = (normalized, snapshot.fetched_at)
    cached = _SOLVED.get(key, datetime.now(UTC).timestamp())
    if isinstance(cached, SolvedSymbol):
        return cached

    with _solve_lock(key):
        # Checked again inside the lock. Whoever was ahead has finished by now, and
        # without this the waiters would each go on to solve the chain a second time,
        # which is the whole thing the lock is here to prevent.
        cached = _SOLVED.get(key, datetime.now(UTC).timestamp())
        if isinstance(cached, SolvedSymbol):
            return cached
        return _solve(
            normalized,
            snapshot,
            key,
            settings=settings,
            config=config,
            provider_factory=provider_factory,
            with_events=with_events,
        )


def _solve(
    normalized: str,
    snapshot: ChainSnapshot,
    key: object,
    *,
    settings: Settings,
    config: ScreenConfig,
    provider_factory: ProviderFactory | None,
    with_events: bool,
) -> SolvedSymbol:
    """Do the expensive part. Called with the key's solve lock held."""
    frame = read_snapshots(settings.snapshot_path, symbol=normalized)
    history = atm_iv_history(frame, rate=settings.risk_free_rate) if not frame.empty else []

    events: EventWindow | None = None
    events_note: str | None = None
    if with_events:
        events, events_note = event_window(
            normalized,
            snapshot.session_date,
            provider_factory=provider_factory,
        )

    # now is deliberately not passed. Liquidity scoring measures trade recency, and
    # for a stored capture the honest reference is the capture itself: a snapshot from
    # Friday should score the same on Monday as it did on Friday. It also makes the
    # solved result deterministic, which is what allows caching it at all.
    analysis = analyze_snapshot(
        snapshot,
        rate=settings.risk_free_rate,
        iv_history=history,
        events=events,
        max_spread_pct=config.filters.liquidity.max_spread_pct,
    )

    solved = SolvedSymbol(
        symbol=normalized,
        snapshot=snapshot,
        analysis=analysis,
        iv_history=history,
        events=events,
        events_note=events_note,
    )
    _SOLVED.put(key, solved)
    _release_solve_lock(key)
    log.info(
        "solved symbol",
        symbol=normalized,
        expiries=len(analysis.expiries),
        captured_at=snapshot.fetched_at.isoformat(),
        iv_history=len(history),
    )
    return solved


# --------------------------------------------------------------------------------
# Reference data, the only two things the API fetches
# --------------------------------------------------------------------------------


def event_window(
    symbol: str,
    session_date: date,
    *,
    provider_factory: ProviderFactory | None = None,
) -> tuple[EventWindow | None, str | None]:
    """The corporate calendar for one symbol, cached until the session rolls over.

    Returns (window, note). A None window means the calendar could not be checked,
    and the note says why. Callers must surface that: a screen that silently skips
    its earnings exclusion returns candidates the CLI would have rejected, and the
    difference is invisible unless it is stated.
    """
    key = (symbol, session_date)
    cached = _EVENTS.get(key, datetime.now(UTC).timestamp())
    if isinstance(cached, tuple):
        return cached

    if provider_factory is None:
        return None, "No market data provider is configured, so earnings were not checked."

    result: tuple[EventWindow | None, str | None]
    try:
        provider = provider_factory()
        events: SymbolEvents = provider.get_events(symbol)
        result = (
            EventWindow(
                earnings=events.earnings_date,
                ex_dividend=events.ex_dividend_date,
                dividend_amount=events.dividend_amount,
            ),
            None,
        )
    except NotImplementedError:
        result = (
            None,
            "The configured provider has no corporate calendar, so earnings were not checked.",
        )
    except (ProviderError, OSError) as error:
        result = (None, f"Earnings could not be checked: {type(error).__name__}: {error}")
        log.warning("event calendar unavailable", symbol=symbol, error=str(error))

    _EVENTS.put(key, result)
    return result


def price_history(
    symbol: str,
    days: int = DEFAULT_HISTORY_DAYS,
    *,
    provider_factory: ProviderFactory | None = None,
) -> tuple[list[PriceBar], str | None]:
    """Daily candles for the underlying, cached briefly.

    The one place this API asks a vendor for a price. Daily bars are not a quote and
    the chart is drawn from settled closes, but the last bar of a live session is
    still forming, which is why the series carries its own provenance and the panel
    shows its age.

    Returns (bars, note). An empty list with a note is a stated failure. An empty list
    with no note cannot happen: a provider that returns nothing gets a note too.
    """
    key = (symbol, days)
    now = datetime.now(UTC).timestamp()
    cached = _HISTORY.get(key, now)
    if isinstance(cached, tuple):
        return cached

    if provider_factory is None:
        return [], "No market data provider is configured, so there are no candles."

    result: tuple[list[PriceBar], str | None]
    try:
        provider = provider_factory()
        bars = provider.get_history(symbol, days)
        empty = f"The provider returned no bars for {symbol}."
        result = (list(bars), None) if bars else ([], empty)
    except NotImplementedError:
        result = ([], "The configured provider has no price history, so there are no candles.")
    except (ProviderError, OSError) as error:
        result = ([], f"Candles unavailable: {type(error).__name__}: {error}")
        log.warning("price history unavailable", symbol=symbol, error=str(error))

    _HISTORY.put(key, result, expires_at=now + HISTORY_TTL_SECONDS)
    return result
