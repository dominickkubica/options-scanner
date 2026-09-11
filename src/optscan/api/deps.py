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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import Depends

from optscan.analytics.events import EventWindow
from optscan.api.schemas import Provenance
from optscan.config import REPO_ROOT, Settings, get_settings
from optscan.jobs.load import latest_snapshot
from optscan.jobs.scan import STALE_AFTER_HOURS, load_iv_history
from optscan.jobs.snapshot import capture_symbol
from optscan.live import LiveHub
from optscan.logging import get_logger
from optscan.market_calendar import SessionState, session_state
from optscan.models import ChainSnapshot, LiveQuote, PriceBar, SymbolEvents
from optscan.providers import (
    MarketDataProvider,
    NoDataAvailable,
    ProviderError,
    get_intraday_providers,
    get_provider,
    get_quote_providers,
)
from optscan.screener.config import DEFAULT_CONFIG_FILENAME, ScreenConfig
from optscan.screener.context import SymbolAnalysis, analyze_snapshot
from optscan.storage import db
from optscan.storage.vendor import recent_daily_bars

log = get_logger("optscan.api.deps")

#: Solved symbols held in memory. Six watchlist symbols is the normal case and a
#: solved SPY capture is a few megabytes, so this is generous rather than tight.
MAX_CACHED_SYMBOLS = 16

#: How long a fetched candle series is reused. Daily bars only move once a day, but
#: the current session's bar keeps updating, so this is short enough that the age
#: shown next to the chart stays small and long enough that clicking between panels
#: does not refetch.
HISTORY_TTL_SECONDS = 900.0

#: Intraday candles get their own, far shorter, window. Fifteen minutes is right for a
#: daily series that changes once a day and absurd for a one minute chart: the last
#: candle would sit frozen for fifteen bars while the price beside it moved every ten
#: seconds. The whole point of opening a minute chart is to watch it.
#:
#: Costs one vendor request per symbol per window, against a published 200 a minute, so
#: even several charts open at once is a rounding error of the budget.
INTRADAY_TTL_SECONDS = 15.0

#: How long a live quote is reused. Short, because this is the number the whole
#: feature exists to keep current, and a batch of the entire watchlist is one request:
#: at this TTL a browser polling every 5 seconds costs 12 requests a minute against a
#: budget of 200. Not zero, because three panels on one screen asking at the same
#: instant should share one fetch rather than race for three.
QUOTE_TTL_SECONDS = 5.0

#: Live quotes are never fetched for more symbols than this in one call. The watchlist
#: is seven; the catalogue is hundreds, and it deliberately stays on stored closes.
#: Without a ceiling, one Browse page would turn a 1 request poll into a 3 request one
#: and put a vendor round trip in front of a screen that does not need it.
MAX_LIVE_QUOTE_SYMBOLS = 50

#: How long a live chain is reused before it is fetched again. The chain is the input
#: to every judgement on the page -- greeks, probability, credit, DTE -- so this is the
#: freshness that actually decides whether a card is about today. Short enough that a
#: number on screen is never a minute old, long enough that clicking between the chain,
#: the payoff and the best plays tabs is one fetch rather than three.
LIVE_CHAIN_TTL_SECONDS = 20.0

#: Expiries fetched for a live view, against the 40 the nightly capture takes. The
#: capture is building a permanent history and a chain not captured today cannot be
#: captured later; this is answering "what can I trade now", and the screen's own DTE
#: band tops out at 45 days. Six expiries covers that with room, at roughly a third of
#: the requests.
LIVE_CHAIN_EXPIRIES = 6

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
    #: Set when stored sessions from another vendor were left out of the IV history.
    #: Surfaced next to the rank, because a confidence level that drops after a
    #: provider switch otherwise looks like the snapshot job has been failing.
    iv_history_note: str | None = None

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

#: Live quotes, keyed by the exact set asked for.
_QUOTES = _TimedCache(32)

#: Live chains, per symbol. Holds False for a symbol whose vendor just failed, so an
#: outage costs one request per window rather than one per panel per render.
_LIVE_CHAINS = _TimedCache(8)

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


def clear_live_caches() -> None:
    """Drop only the short-lived caches, so the next request refetches from the vendor.

    Deliberately not `clear_caches`, which also tears down the live hub and the screen
    config. This is what a refresh button needs: the quote, the chain and the candles
    are the three things that age, and everything else on the page is derived from them.

    The solved-symbol cache is keyed by the snapshot's own `fetched_at`, so a newly
    fetched chain misses it naturally and re-solves. Clearing it here would only throw
    away work that is still correct.
    """
    _QUOTES.clear()
    _LIVE_CHAINS.clear()
    _HISTORY.clear()


def clear_caches() -> None:
    """Drop everything cached. Called between tests, and by the app on startup.

    The live hub is torn down here too. A test that leaves one running keeps a polling
    thread and a provider connection alive into the next test, which is how an offline
    suite quietly acquires a network call.
    """
    _SOLVED.clear()
    _EVENTS.clear()
    _HISTORY.clear()
    _QUOTES.clear()
    _LIVE_CHAINS.clear()
    with _SOLVE_LOCKS_GUARD:
        _SOLVE_LOCKS.clear()
    screen_config.cache_clear()
    reset_live_hub()


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


#: The live hub, built once per process. A singleton rather than a dependency because
#: it owns a polling thread and a provider connection: one per request would be one
#: poller per request, which is exactly the request budget catastrophe Phase 5 is
#: supposed to prevent.
#: A one slot dict rather than a rebound module global, so the lock is what guards it
#: rather than the import machinery.
_HUB: dict[str, LiveHub] = {}
_HUB_GUARD = threading.Lock()


def live_hub(settings: Settings) -> LiveHub:
    """The process wide live hub, created on first use.

    Created even when the feed is disabled, because the status endpoint has to be able
    to say "disabled" and a None here would make every caller handle that separately.
    A disabled hub never starts its thread and never builds a provider.
    """
    with _HUB_GUARD:
        hub = _HUB.get("hub")
        if hub is None:
            hub = LiveHub(
                settings,
                lambda: get_provider(settings),
                # The same accessor the chain endpoint uses, so both resolve an
                # unnamed expiry to the same one. If they diverged, a live panel
                # would follow an expiry the grid is not showing and the table
                # would never move while the feed insisted it was live.
                screen_config=screen_config,
            )
            _HUB["hub"] = hub
        return hub


def reset_live_hub() -> None:
    """Stop and forget the hub. Called on shutdown, and between tests."""
    with _HUB_GUARD:
        hub = _HUB.pop("hub", None)
    if hub is not None:
        hub.stop()


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


def live_chain(settings: Settings, symbol: str) -> ChainSnapshot | None:
    """Today's chain, fetched on demand. Never stored.

    ## Why this exists

    Everything else on the page was made current and the analysis was not. Best plays,
    the greeks, the probability and the credit were all computed from the 15:45 capture,
    so mid-session they described yesterday. It showed most clearly in the DTE: a card
    for a 2026-09-11 expiry read "2d" on 2026-09-10, because DTE is measured from the
    capture date and the capture was the previous afternoon.

    ## The rule that must not be broken

    **Nothing fetched here is ever written to storage.** The IV history has exactly one
    writer, the 15:45 snapshot job, and that single sampling time is what makes the
    series comparable across days. Appending marks taken at whatever moment a browser
    happened to be open would pool two different sampling regimes into one series and
    move every rank without the market having moved. This returns a snapshot for
    display and scoring; the stored capture remains the only history.

    ## When it declines

    Outside a session. A live fetch then is not fresher than the stored capture, it is
    simply a different and worse sample: the 15:45 capture was taken deliberately at a
    known time, while an after-hours chain is thin quotes and wide spreads. Returning
    None hands the caller back to the stored snapshot, which is the better artifact.
    """
    state = session_state(datetime.now(UTC), settings.market_calendar, settings.market_timezone)
    if state is not SessionState.OPEN:
        return None

    key = ("chain", symbol)
    now = datetime.now(UTC).timestamp()
    cached = _LIVE_CHAINS.get(key, now)
    if isinstance(cached, ChainSnapshot):
        return cached
    if cached is not None:  # a cached failure, so the vendor is not retried per request
        return None

    providers = _QUOTE_PROVIDER["factory"](settings)
    shallow = settings.model_copy(update={"snapshot_max_expiries": LIVE_CHAIN_EXPIRIES})
    for provider in providers:
        try:
            try:
                snapshot = capture_symbol(provider, symbol, shallow, date.today())
            finally:
                closer = getattr(provider, "close", None)
                if callable(closer):
                    closer()
        except (ProviderError, OSError, NotImplementedError) as error:
            log.warning(
                "live chain unavailable",
                symbol=symbol,
                vendor=provider.name,
                error=str(error),
            )
            continue
        _LIVE_CHAINS.put(key, snapshot, expires_at=now + LIVE_CHAIN_TTL_SECONDS)
        return snapshot

    # Remember the failure for the same window. Without this every panel on the page
    # retries a dead vendor on every request, which is how an outage becomes a stall.
    _LIVE_CHAINS.put(key, False, expires_at=now + LIVE_CHAIN_TTL_SECONDS)
    return None


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
    # A live chain when the market is open and a vendor answers, the stored capture
    # otherwise. The stored one is never skipped on failure: a day old chain that says
    # it is a day old beats an empty page.
    snapshot = live_chain(settings, normalized) or latest_snapshot(
        settings.snapshot_path, normalized
    )
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
    # The same loader the CLI scan uses, so the dashboard and the terminal can never
    # rank a symbol against different history. It picks one vendor's series and says
    # which; it never pools two.
    iv_history = load_iv_history(settings, normalized, snapshot)
    history = list(iv_history.points)

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
        iv_history=iv_history,
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
        iv_history_note=iv_history.note(),
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


#: Bar intervals the chart may ask for. Anything not "1Day" is intraday and needs a
#: vendor that serves it, which is a different question from the configured provider:
#: see `price_history`.
DAILY_INTERVAL = "1Day"
INTRADAY_INTERVALS = ("1Min", "2Min", "5Min", "15Min", "30Min", "1Hour")


def price_history(
    symbol: str,
    days: int = DEFAULT_HISTORY_DAYS,
    *,
    interval: str = DAILY_INTERVAL,
    session: date | None = None,
    settings: Settings | None = None,
    provider_factory: ProviderFactory | None = None,
) -> tuple[list[PriceBar], str | None]:
    """Candles for the underlying, cached briefly.

    Returns (bars, note). An empty list with a note is a stated failure; an empty list
    with no note cannot happen.

    ## Where the bars come from, and why it is not simply "the provider"

    **Daily candles are read from storage first.** `optscan prices sync` stores a decade
    of daily bars for every symbol in the universe, which is a few hundred of them
    against the six the screener captures chains for. Reading those means a chart opens
    for any symbol the catalogue lists rather than only for the pinned ones, it is a
    local query instead of a vendor round trip, and it works with no provider
    configured at all. The provider is the fallback for a symbol nothing has synced.

    **Intraday candles come from a vendor that has them, which may not be the configured
    one.** `OPTSCAN_PROVIDER` selects what captures option chains, and that choice is
    load bearing for reasons that have nothing to do with charting: an IV history is per
    vendor, so switching it restarts the rank from zero. A minute candle carries none of
    that history, so it is fetched from whichever vendor can serve one.

    That is a deliberate exception to "all market data flows through the configured
    provider" and it is narrow: prices only, never volatility, never anything stored.
    """
    key = (symbol, days, interval, session)
    now = datetime.now(UTC).timestamp()
    cached = _HISTORY.get(key, now)
    if isinstance(cached, tuple):
        return _with_forming_bar(cached, symbol, interval, settings)

    if interval != DAILY_INTERVAL:
        result = _intraday_history(symbol, interval, days, settings, session)
    else:
        result = _daily_history(symbol, days, settings, provider_factory)

    ttl = HISTORY_TTL_SECONDS if interval == DAILY_INTERVAL else INTRADAY_TTL_SECONDS
    _HISTORY.put(key, result, expires_at=now + ttl)
    return _with_forming_bar(result, symbol, interval, settings)


def _with_forming_bar(
    result: tuple[list[PriceBar], str | None],
    symbol: str,
    interval: str,
    settings: Settings | None,
) -> tuple[list[PriceBar], str | None]:
    """Append today's in-progress bar, outside the history cache.

    Deliberately not inside it. Stored daily bars change once a day, so caching them for
    fifteen minutes costs nothing -- but the current session's bar changes every tick,
    and caching that alongside them would have frozen the chart's last candle for
    fifteen minutes while the header price beside it moved every ten seconds. The quote
    underneath has its own short cache, so this stays cheap: a redraw shares one fetch
    with the pills rather than making its own.
    """
    bars, note = result
    if interval != DAILY_INTERVAL or settings is None or not bars:
        return result

    newest = bars[-1].ts.date()
    forming = _forming_bar(settings, symbol, newest)
    if forming is None:
        return result
    return [*bars, forming], note


#: How the quote provider is obtained. A module level seam rather than a direct call,
#: so a test that has carefully overridden the provider is not bypassed by a second code
#: path reaching for the network on its own. That is exactly what happened: appending a
#: live bar to the daily chart put a real Alpaca request inside the offline suite,
#: because it called `get_quote_provider` instead of going through the injection the
#: rest of the history path already used.
#: A one entry dict rather than a bare module global, so swapping it is a mutation
#: rather than a rebinding and no `global` statement is needed.
_QUOTE_PROVIDER: dict[str, Callable[[Settings], list[MarketDataProvider]]] = {
    "factory": get_quote_providers
}


def set_quote_provider(factory: Callable[[Settings], list[MarketDataProvider]]) -> None:
    """Swap the quote providers. For tests, and for anything that must stay offline."""
    _QUOTE_PROVIDER["factory"] = factory


def live_quotes(
    settings: Settings, symbols: Sequence[str]
) -> tuple[dict[str, LiveQuote], str | None]:
    """Current headline prices, falling back to the last stored close.

    Returns (quotes, note). The note is a sentence for the UI when the numbers are not
    what was asked for -- no vendor configured, or the vendor failed -- and is None when
    they are live.

    ## Why a failure degrades rather than empties

    The stored close is a worse answer than a live quote and a far better one than a
    blank pill. A watchlist that loses its prices because a vendor timed out is a
    watchlist that looks broken; one showing Friday's close, dated Friday, is merely
    late, and the date is on screen either way. That is the same principle the chart
    already follows: the price panel is the least load bearing thing here and must
    never take the page down with it.

    The fallback is deliberately *not* silent. It carries `as_of` from the session it
    came from, so the age that the caller renders is the real age of the number, not
    the moment this function ran.
    """
    wanted = [s.strip().upper() for s in symbols if s and s.strip()]
    if not wanted:
        return {}, None
    if len(wanted) > MAX_LIVE_QUOTE_SYMBOLS:
        return _stored_quotes(settings, wanted), (
            f"{len(wanted)} symbols is more than live quoting covers, so these are "
            "the last stored closes."
        )

    key = ("quotes", tuple(sorted(wanted)))
    now = datetime.now(UTC).timestamp()
    cached = _QUOTES.get(key, now)
    if isinstance(cached, tuple):
        return cached

    result = _fetch_live_quotes(settings, wanted)
    _QUOTES.put(key, result, expires_at=now + QUOTE_TTL_SECONDS)
    return result


def _fetch_live_quotes(
    settings: Settings, wanted: list[str]
) -> tuple[dict[str, LiveQuote], str | None]:
    providers = _QUOTE_PROVIDER["factory"](settings)
    if not providers:
        return _stored_quotes(settings, wanted), (
            "No vendor here can serve a live price, so these are the last stored "
            "closes. Set Alpaca credentials to quote them."
        )

    # Each vendor in turn until one answers. A single vendor's outage used to take every
    # price in the application down to the previous session's close at once.
    quotes: dict[str, LiveQuote] = {}
    failures: list[str] = []
    for index, provider in enumerate(providers):
        try:
            try:
                quotes = provider.get_live_quotes(wanted)
            finally:
                # Not every provider holds a connection. yfinance has no close(), and
                # assuming one here is a mistake this project has already made once.
                closer = getattr(provider, "close", None)
                if callable(closer):
                    closer()
        except (ProviderError, OSError, NotImplementedError) as error:
            log.warning("quote vendor failed", vendor=provider.name, error=str(error))
            failures.append(f"{provider.name} ({type(error).__name__})")
            continue
        if quotes:
            # A symbol the vendor did not return still needs a price. A delisted ticker
            # falls back on its own rather than taking the rest of the batch with it.
            missing = [symbol for symbol in wanted if symbol not in quotes]
            if missing:
                quotes = {**_stored_quotes(settings, missing), **quotes}
            if index:
                # Say which vendor answered when it was not the preferred one. A price
                # from the second choice is still a price, and the reader deserves to
                # know the first one is down rather than wondering why numbers moved.
                return quotes, (
                    f"{', '.join(failures)} unavailable, so these are quoted by {provider.name}."
                )
            return quotes, None

    if failures:
        return _stored_quotes(settings, wanted), (
            f"Live quotes are unavailable ({'; '.join(failures)}), so these are the "
            "last stored closes."
        )

    return _stored_quotes(settings, wanted), (
        "No vendor returned a quote, so these are the last stored closes."
    )


def _forming_bar(settings: Settings, symbol: str, newest_stored: date) -> PriceBar | None:
    """Today's in-progress daily bar, or None when storage is already current.

    Built from the same live quote the watchlist pills use, so the chart's last candle
    and the header price cannot disagree. Returns None outside a session, when the quote
    is a stored fallback, or when the quote's session is not newer than what is already
    held -- appending a bar for a session storage already has would double it.
    """
    quotes, _note = live_quotes(settings, [symbol])
    quote = quotes.get(symbol)
    if quote is None or quote.feed == "stored" or quote.last is None:
        return None
    session = quote.session_date
    if session is None or session <= newest_stored:
        return None

    return PriceBar(
        symbol=symbol,
        ts=datetime.combine(session, time(), tzinfo=UTC),
        # A session that has only just opened can be missing a high or a low; the last
        # price is a floor for both and never invents a range the market did not print.
        open=quote.day_open if quote.day_open is not None else quote.last,
        high=max(quote.day_high or quote.last, quote.last),
        low=min(quote.day_low or quote.last, quote.last),
        close=quote.last,
        volume=quote.volume or 0,
        fetched_at=quote.as_of or datetime.now(UTC),
        source=quote.feed or "live",
    )


def _stored_quotes(settings: Settings, symbols: Sequence[str]) -> dict[str, LiveQuote]:
    """The last stored close per symbol, shaped as a quote.

    Built on `recent_daily_bars` rather than on a query of its own, because that helper
    already picks a single source per symbol. Taking the two most recent rows across all
    sources would compare AAPL's Alpaca close against its Market Chameleon close and
    report the disagreement between two vendors as a daily move -- which is exactly the
    bug `db_moves` was written to prevent, and it would be reintroduced here.

    `as_of` is midnight UTC on the session the bar belongs to, not now. The caller
    renders an age from it, and the honest age of a stored close is its session's.
    """
    out: dict[str, LiveQuote] = {}
    now = datetime.now(UTC)
    with db.session(settings.sqlite_path) as conn:
        for symbol in symbols:
            bars, source = recent_daily_bars(conn, symbol, 2)
            if not bars:
                continue
            out[symbol] = LiveQuote(
                symbol=symbol,
                last=bars[-1].close,
                previous_close=bars[-2].close if len(bars) > 1 else None,
                volume=bars[-1].volume,
                as_of=datetime.combine(bars[-1].session_date, time(), tzinfo=UTC),
                realtime=False,
                feed="stored",
                delay_minutes=None,
                fetched_at=now,
                source=source or "stored",
            )
    return out


def _daily_history(
    symbol: str,
    days: int,
    settings: Settings | None,
    provider_factory: ProviderFactory | None,
) -> tuple[list[PriceBar], str | None]:
    """Stored bars if there are any, otherwise ask the provider.

    Today's bar is appended live. The price sync runs after the close, so storage does
    not contain the current session until the evening: a chart drawn from storage alone
    is a full day behind from the opening bell until then, and looks precisely like a
    chart that is up to date. That is the same failure the live quotes were added to fix,
    one panel over.
    """
    if settings is not None:
        with db.session(settings.sqlite_path) as conn:
            stored, source = recent_daily_bars(conn, symbol, days)
        if stored:
            bars = [
                PriceBar(
                    symbol=bar.symbol,
                    ts=datetime.combine(bar.session_date, time(), tzinfo=UTC),
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    # The session this bar covers, not the moment it was read out of
                    # sqlite. Stamping `now` here is what made a day old chart report
                    # itself as fetched seconds ago.
                    fetched_at=datetime.combine(bar.session_date, time(), tzinfo=UTC),
                    source=source or "stored",
                )
                for bar in stored
            ]
            return bars, None

    if provider_factory is None:
        return [], (
            f"No stored daily bars for {symbol} and no provider configured. "
            "Run `optscan prices sync` to store them."
        )

    try:
        bars = list(provider_factory().get_history(symbol, days))
    except NotImplementedError:
        return [], "The configured provider has no price history, so there are no candles."
    except (ProviderError, OSError) as error:
        log.warning("price history unavailable", symbol=symbol, error=str(error))
        return [], f"Candles unavailable: {type(error).__name__}: {error}"

    if not bars:
        return [], f"The provider returned no bars for {symbol}."
    return bars, None


def _intraday_refusal(interval: str, settings: Settings | None) -> str | None:
    """Why intraday cannot be served, or None when it can.

    Split out so each refusal keeps its own sentence. They send the reader somewhere
    different: one is a typo, the other is a missing credential.
    """
    if interval not in INTRADAY_INTERVALS:
        return f"{interval} is not an interval this serves."
    if settings is None or not settings.alpaca_credentials_set:
        return (
            "Intraday candles need Alpaca credentials. Set OPTSCAN_ALPACA_KEY_ID and "
            "OPTSCAN_ALPACA_SECRET_KEY in .env; daily candles work without them."
        )
    return None


def _intraday_from_any(
    providers: Sequence[MarketDataProvider],
    symbol: str,
    interval: str,
    days: int,
    session: date | None,
) -> tuple[list[PriceBar], list[str]]:
    """Try each vendor in turn, returning the first non-empty answer and any failures.

    A single vendor's outage used to leave the chart blank while the market traded,
    which is a worse failure than being fifteen minutes late. NoDataAvailable is left to
    propagate: it means the market was shut, which every vendor will agree about and
    which the caller turns into a sentence rather than an error.
    """
    errors: list[str] = []
    for provider in providers:
        try:
            try:
                bars = provider.get_intraday_bars(symbol, interval, days, session=session)
            finally:
                closer = getattr(provider, "close", None)
                if callable(closer):
                    closer()
        except (ProviderError, OSError, NotImplementedError) as error:
            log.warning(
                "intraday vendor failed", symbol=symbol, vendor=provider.name, error=str(error)
            )
            errors.append(f"{provider.name} ({type(error).__name__})")
            continue
        if bars:
            return bars, errors
    return [], errors


def _intraday_history(
    symbol: str,
    interval: str,
    days: int,
    settings: Settings | None,
    session: date | None = None,
) -> tuple[list[PriceBar], str | None]:
    """Minute and hourly candles, from a vendor that serves them.

    Nothing intraday is stored. A few hundred symbols at a decade of minute bars is
    hundreds of millions of rows for a chart somebody looks at for ten seconds, so this
    is fetched on demand and cached for the same short window as everything else.
    """
    refusal = _intraday_refusal(interval, settings)
    if refusal is not None:
        return [], refusal

    providers = get_intraday_providers(settings)
    if not providers:
        return [], "No provider here can serve intraday candles."

    try:
        bars, errors = _intraday_from_any(providers, symbol, interval, days, session)
        if not bars and errors:
            return [], f"{interval} candles unavailable: {'; '.join(errors)}"
    except NoDataAvailable:
        # A shut market is not a failure and must not read like one. The vendor says
        # "no bars" for a holiday, a weekend and a broken request identically, so the
        # sentence has to come from what was asked rather than from what came back.
        bars = []
    except (ProviderError, OSError) as error:
        log.warning("intraday history unavailable", symbol=symbol, error=str(error))
        return [], f"{interval} candles unavailable: {error}"

    if bars:
        return bars, None
    # Over a weekend a short window contains no sessions at all, which is not a failure
    # and should not read as one.
    empty = (
        f"No {interval} bars on {session}. The market was shut, or it is too recent."
        if session is not None
        else f"No {interval} bars in the last {days} days. Try a longer window."
    )
    return [], empty
