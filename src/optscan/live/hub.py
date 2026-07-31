"""The live refresh loop.

## Why this polls

Phase 4 left the transport open: push over websockets, or let the client poll a cheap
endpoint. Tradier settled the upstream half of it. Their sandbox, which is the tier
this project is actually built against, has no streaming endpoint at all: "Presently,
we do not offer a delayed streaming endpoint for paper trading." There is a websocket,
it needs a production session, and its data is fifteen minutes delayed here regardless.

So there is no vendor push to forward. What exists is a server side refresh loop, and
the only question left is how the browser learns that a cycle landed. That is server
sent events: one direction, which is all this needs; automatic reconnection with
backoff built into EventSource, which is the load bearing part of "degrades honestly
when the feed drops" and which a websocket would have us hand roll; and no new
dependency. Subscription changes go the other way over ordinary HTTP, because they
happen when somebody clicks a symbol and not sixty times a minute.

## Why deltas are safe here, when they usually are not

The worry recorded in DECISIONS.md was a partially updated screen: a chain that is live
while the scan beside it is four minutes old is worse than a screen that is uniformly
four minutes old and says so.

The unit of update here is therefore a whole cycle, never a field. One cycle is one
quote plus one chain plus one solve, and it carries a single fetched_at for all of it.
A delta then lists only the contracts whose numbers moved since the previous cycle,
and that is not a mixture of ages: a contract that did not move has the same value at
this cycle as it did at the last one, so the client's table is entirely as of the
latest version. Two rules keep that true, and both are enforced below:

- **A cycle is complete or it does not exist.** A failed or partial fetch emits a
  status event, never a cycle. There is no such thing as a half applied version.
- **Disappearances are explicit.** A contract that leaves the chain is named in
  `removed`, because a row nobody mentions again is a row the browser would keep
  showing forever.

A subscriber that cannot keep up gets its queue cleared and its next cycle sent in
full, rather than being fed a delta against a version it never received.

## What this never does

It never writes to storage. The IV history has exactly one writer, the daily snapshot
job, and a live feed appending its own marks would be the vendor mixing problem in a
new place: intraday polls at whatever moment the browser happened to be open, pooled
with a history sampled deliberately at 15:45. See optscan.screener.history.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from itertools import count
from typing import Any

from optscan.analytics.greeks import Greeks
from optscan.config import REPO_ROOT, Settings
from optscan.logging import get_logger
from optscan.market_calendar import SessionState, session_state
from optscan.models import OptionChain, Quote, Right
from optscan.providers import MarketDataProvider, ProviderError, provider_is_realtime
from optscan.screener.config import DEFAULT_CONFIG_FILENAME, ScreenConfig
from optscan.screener.context import ExpiryAnalysis, analyze_chain

log = get_logger("optscan.live.hub")

#: Cycles a subscriber may fall behind before it is resynchronized with a full cycle
#: instead of a delta. Small on purpose: a browser that is this far behind has been
#: backgrounded or throttled, and catching it up matters more than the queued history.
MAX_PENDING_CYCLES = 8

#: Consecutive provider failures before the feed calls itself degraded rather than
#: just reporting the one error. One failure is a hiccup, three is a condition.
DEGRADED_AFTER_FAILURES = 3


class LiveState(StrEnum):
    """What the feed is doing, as the UI needs to say it."""

    DISABLED = "disabled"  # live_enabled is false: this is a stored snapshot tool
    STARTING = "starting"  # subscribed, nothing fetched yet
    LIVE = "live"  # a cycle landed and the market is open
    IDLE = "idle"  # market is closed or outside the session, polling slowed or stopped
    DEGRADED = "degraded"  # the provider keeps failing, the last cycle is ageing
    STOPPED = "stopped"


def _default_screen_config() -> ScreenConfig:
    """screen.yaml from the repo root, for a hub built outside the API."""
    return ScreenConfig.load(REPO_ROOT / DEFAULT_CONFIG_FILENAME)


def contract_key(strike: float, right: Right | str) -> str:
    """Stable identity for one contract inside a cycle.

    Strike and right rather than the vendor's contract symbol, because the symbol is
    the one field a vendor is free to reformat and a delta keyed on it would resync the
    entire chain the day they do.
    """
    return f"{strike:g}:{Right.parse(right)}"


@dataclass(frozen=True, slots=True)
class LiveQuote:
    """The underlying, as of one cycle."""

    last: float | None
    bid: float | None
    ask: float | None
    spot: float

    def as_dict(self) -> dict[str, Any]:
        return {"last": self.last, "bid": self.bid, "ask": self.ask, "spot": self.spot}


@dataclass(frozen=True, slots=True)
class LiveContract:
    """One contract's numbers, all solved from the same fetch."""

    strike: float
    right: str
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    last: float | None = None
    volume: int | None = None
    open_interest: int | None = None
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    reject_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "strike": self.strike,
            "right": self.right,
            "bid": self.bid,
            "ask": self.ask,
            "mid": self.mid,
            "last": self.last,
            "volume": self.volume,
            "open_interest": self.open_interest,
            "iv": self.iv,
            "delta": self.delta,
            "gamma": self.gamma,
            "theta": self.theta,
            "vega": self.vega,
            "reject_reason": self.reject_reason,
        }


@dataclass(frozen=True, slots=True)
class LiveCycle:
    """One complete refresh of one symbol and expiry."""

    symbol: str
    expiry: date
    version: int
    fetched_at: datetime
    source: str
    realtime: bool
    delay_minutes: int | None
    quote: LiveQuote
    atm_iv: float | None
    dte: int
    contracts: dict[str, LiveContract]

    def age_seconds(self, now: datetime) -> float:
        return (now - self.fetched_at).total_seconds()


@dataclass(frozen=True, slots=True)
class LiveDelta:
    """What changed between two cycles, or a whole cycle when `full` is set."""

    symbol: str
    expiry: date
    version: int
    fetched_at: datetime
    source: str
    realtime: bool
    delay_minutes: int | None
    quote: LiveQuote
    atm_iv: float | None
    dte: int
    changed: dict[str, LiveContract]
    removed: list[str]
    full: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "expiry": self.expiry.isoformat(),
            "version": self.version,
            "fetched_at": self.fetched_at.isoformat(),
            "source": self.source,
            "realtime": self.realtime,
            "delay_minutes": self.delay_minutes,
            "quote": self.quote.as_dict(),
            "atm_iv": self.atm_iv,
            "dte": self.dte,
            "changed": {key: value.as_dict() for key, value in self.changed.items()},
            "removed": list(self.removed),
            "full": self.full,
        }


@dataclass(frozen=True, slots=True)
class LiveStatus:
    """The feed's own condition, which the UI shows whether or not data is flowing."""

    state: LiveState
    session: SessionState
    source: str | None = None
    realtime: bool = False
    delay_minutes: int | None = None
    symbols: tuple[str, ...] = ()
    last_cycle_at: datetime | None = None
    consecutive_failures: int = 0
    detail: str | None = None
    #: Seconds the browser should expect between cycles right now, or None when nothing
    #: is being polled. Sent so the UI can call a panel overdue without hardcoding a
    #: threshold, and so that a feed which has died quietly cannot go on looking live:
    #: a socket that never reports an error still leaves the age climbing past this.
    poll_seconds: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": str(self.state),
            "session": str(self.session),
            "source": self.source,
            "realtime": self.realtime,
            "delay_minutes": self.delay_minutes,
            "symbols": list(self.symbols),
            "last_cycle_at": self.last_cycle_at.isoformat() if self.last_cycle_at else None,
            "consecutive_failures": self.consecutive_failures,
            "detail": self.detail,
            "poll_seconds": self.poll_seconds,
        }


@dataclass
class Subscription:
    """One open browser connection's view of one symbol and expiry."""

    id: int
    symbol: str
    expiry: date | None
    events: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=MAX_PENDING_CYCLES))
    #: Set when this subscriber has never been sent a cycle, or fell far enough behind
    #: that a delta would be applied against a version it never saw.
    needs_full: bool = True
    closed: bool = False

    def offer(self, name: str, payload: dict[str, Any]) -> None:
        """Queue an event, resynchronizing rather than dropping if the client is behind."""
        try:
            self.events.put_nowait((name, payload))
        except queue.Full:
            # Deltas cannot be dropped: the next one would be applied against a version
            # this client never received. Clear the backlog and start again from a full
            # cycle, which is the only recovery that leaves the browser correct.
            self.needs_full = True
            _drain(self.events)
            self.events.put_nowait((name, payload))


def _drain(target: queue.Queue) -> None:
    while True:
        try:
            target.get_nowait()
        except queue.Empty:
            return


@dataclass
class _Feed:
    """One (symbol, expiry) being polled, and everyone watching it."""

    symbol: str
    expiry: date | None
    subscribers: dict[int, Subscription] = field(default_factory=dict)
    cycle: LiveCycle | None = None
    version: int = 0
    next_due: float = 0.0
    idle_since: float | None = None
    consecutive_failures: int = 0
    last_error: str | None = None


class LiveHub:
    """Polls the configured provider for subscribed symbols and fans out the changes.

    The loop runs on one thread rather than one per symbol. A local dashboard watches a
    handful of symbols at most, one thread is trivial to reason about, and the request
    budget is shared rather than raced for.
    """

    def __init__(
        self,
        settings: Settings,
        provider_factory,
        *,
        clock=None,
        screen_config=None,
    ) -> None:
        self._settings = settings
        self._provider_factory = provider_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        # Injected rather than imported, because the API's cached accessor lives in
        # optscan.api.deps and deps imports this module. A callable also means an edit
        # to screen.yaml takes hold here the same way it does everywhere else.
        self._screen_config = screen_config or _default_screen_config
        self._feeds: dict[tuple[str, date | None], _Feed] = {}
        self._lock = threading.RLock()
        self._ids = count(1)
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        # Distinct from _running, which only says whether the loop thread is alive.
        # A hub being stepped by hand, which is how the tests drive it and how a single
        # refresh could be triggered on demand, is not a stopped hub. Reporting it as
        # one would have the UI show "stopped" over data that is arriving.
        self._stopped = False
        self._provider: MarketDataProvider | None = None
        self._quote_cache: dict[str, tuple[datetime, Quote]] = {}
        self._last_cycle_at: datetime | None = None
        self._detail: str | None = None

    # ----------------------------------------------------------------------------
    # Lifecycle
    # ----------------------------------------------------------------------------

    def start(self) -> None:
        if not self._settings.live_enabled or self._running:
            return
        self._running = True
        self._stopped = False
        self._thread = threading.Thread(target=self._loop, name="optscan-live", daemon=True)
        self._thread.start()
        log.info(
            "live hub started",
            provider=self._settings.provider,
            poll_seconds=self._settings.live_poll_seconds,
        )

    def stop(self) -> None:
        self._running = False
        self._stopped = True
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
        self._thread = None
        with self._lock:
            for feed in self._feeds.values():
                for sub in feed.subscribers.values():
                    sub.closed = True
                    sub.offer("status", self.status().as_dict())
            self._feeds.clear()
            self._quote_cache.clear()
        self._close_provider()

    def _close_provider(self) -> None:
        provider = self._provider
        self._provider = None
        close = getattr(provider, "close", None)
        if close is not None:
            close()

    # ----------------------------------------------------------------------------
    # Subscriptions
    # ----------------------------------------------------------------------------

    def subscribe(self, symbol: str, expiry: date | None) -> Subscription:
        """Register interest. Raises ValueError past the configured symbol ceiling."""
        symbol = symbol.strip().upper()
        key = (symbol, expiry)
        with self._lock:
            feed = self._feeds.get(key)
            if feed is None:
                distinct = {name for name, _ in self._feeds}
                if symbol not in distinct and len(distinct) >= self._settings.live_max_symbols:
                    raise ValueError(
                        f"The live feed is already following {len(distinct)} symbols, which is "
                        f"the configured maximum. Close another live panel, or raise "
                        f"OPTSCAN_LIVE_MAX_SYMBOLS."
                    )
                feed = _Feed(symbol=symbol, expiry=expiry)
                self._feeds[key] = feed

            feed.idle_since = None
            sub = Subscription(id=next(self._ids), symbol=symbol, expiry=expiry)
            feed.subscribers[sub.id] = sub

            # Whatever is already known goes out immediately, so a browser that opens
            # mid cycle sees the last good numbers with their real age instead of an
            # empty panel until the next poll.
            if feed.cycle is not None:
                sub.offer("cycle", self._as_delta(feed.cycle, None, full=True).as_dict())
                sub.needs_full = False
            sub.offer("status", self.status().as_dict())

        self._wake.set()
        return sub

    def release(self, sub: Subscription) -> None:
        sub.closed = True
        key = (sub.symbol, sub.expiry)
        with self._lock:
            feed = self._feeds.get(key)
            if feed is None:
                return
            feed.subscribers.pop(sub.id, None)
            if not feed.subscribers:
                # Kept briefly rather than dropped, so a page refresh does not throw
                # away a cycle that was about to be reused.
                feed.idle_since = self._monotonic()

    # ----------------------------------------------------------------------------
    # Status
    # ----------------------------------------------------------------------------

    def status(self) -> LiveStatus:
        settings = self._settings
        session = session_state(self._clock(), settings.market_calendar, settings.market_timezone)

        with self._lock:
            symbols = tuple(sorted({name for name, _ in self._feeds}))
            failures = max((feed.consecutive_failures for feed in self._feeds.values()), default=0)
            errors = [feed.last_error for feed in self._feeds.values() if feed.last_error]
            has_cycle = any(feed.cycle is not None for feed in self._feeds.values())

        if not settings.live_enabled:
            state = LiveState.DISABLED
            detail = (
                "The live feed is off. Every number on this screen comes from the last "
                "stored capture. Set OPTSCAN_LIVE_ENABLED=true to turn it on."
            )
        elif self._stopped:
            state = LiveState.STOPPED
            detail = self._detail
        elif failures >= DEGRADED_AFTER_FAILURES:
            state = LiveState.DEGRADED
            detail = errors[0] if errors else "The provider is failing."
        elif session is SessionState.CLOSED:
            state = LiveState.IDLE
            detail = "The market is closed, so nothing is being polled."
        elif not symbols:
            # Ready, but nothing has asked for anything. Reporting this as "starting"
            # made the header read as a connection in progress on every view that does
            # not use the feed, which is an alarm about nothing.
            state = LiveState.IDLE
            detail = "No panel is subscribed, so nothing is being polled."
        elif session in (SessionState.PRE, SessionState.POST) and not has_cycle:
            state = LiveState.IDLE
            detail = f"Outside the regular session ({session}). Polling is slowed."
        elif has_cycle:
            state = LiveState.LIVE
            detail = errors[0] if errors else self._detail
        else:
            state = LiveState.STARTING
            detail = self._detail

        return LiveStatus(
            state=state,
            session=session,
            source=settings.provider,
            realtime=self._realtime(),
            delay_minutes=settings.quote_delay_minutes,
            symbols=symbols,
            last_cycle_at=self._last_cycle_at,
            consecutive_failures=failures,
            detail=detail,
            poll_seconds=self.poll_interval(),
        )

    def _realtime(self) -> bool:
        return provider_is_realtime(self._settings)

    # ----------------------------------------------------------------------------
    # The loop
    # ----------------------------------------------------------------------------

    def _monotonic(self) -> float:
        """Seconds on a monotonic-enough scale, derived from the injected clock so the
        tests can drive scheduling without sleeping."""
        return self._clock().timestamp()

    def poll_interval(self) -> float | None:
        """Seconds between cycles right now, or None when nothing should be polled.

        None during a closed market. Nothing is quoted, every fetch would return the
        same settled numbers, and spending the request budget to confirm that is how a
        rate limit gets hit on the one morning it matters.
        """
        settings = self._settings
        state = session_state(self._clock(), settings.market_calendar, settings.market_timezone)
        if state is SessionState.CLOSED:
            return None
        if state is SessionState.OPEN:
            return settings.live_poll_seconds
        return settings.live_poll_seconds * settings.live_offhours_poll_multiple

    def _loop(self) -> None:
        while self._running:
            try:
                wait = self.tick()
            except Exception:  # a hub that dies silently is worse than a logged bug
                log.exception("live loop iteration failed")
                wait = self._settings.live_poll_seconds
            self._wake.wait(timeout=wait)
            self._wake.clear()

    def tick(self) -> float:
        """Refresh whatever is due and return how long to wait before the next look.

        Public so the tests can drive the loop one step at a time with a frozen clock
        rather than starting a thread and sleeping.
        """
        self._expire_idle_feeds()
        interval = self.poll_interval()
        if interval is None:
            self._broadcast_status()
            return self._settings.live_poll_seconds

        now = self._monotonic()
        with self._lock:
            due = [
                feed for feed in self._feeds.values() if feed.subscribers and feed.next_due <= now
            ]

        for feed in due:
            self._refresh(feed)
            feed.next_due = self._monotonic() + interval

        return interval

    def _expire_idle_feeds(self) -> None:
        cutoff = self._settings.live_idle_timeout_seconds
        now = self._monotonic()
        with self._lock:
            for key, feed in list(self._feeds.items()):
                if feed.subscribers:
                    continue
                if feed.idle_since is not None and now - feed.idle_since >= cutoff:
                    del self._feeds[key]
                    log.info("live feed idle, stopped polling", symbol=feed.symbol)

    def _broadcast_status(self) -> None:
        payload = self.status().as_dict()
        with self._lock:
            for feed in self._feeds.values():
                for sub in feed.subscribers.values():
                    sub.offer("status", payload)

    # ----------------------------------------------------------------------------
    # One refresh
    # ----------------------------------------------------------------------------

    def _refresh(self, feed: _Feed) -> None:
        """Fetch, solve, encode, and fan out one cycle for one feed.

        Every failure path here ends in a status event and leaves the previous cycle
        in place with its age growing. Nothing partial is ever published: a cycle that
        could not be completed does not become a version.
        """
        try:
            cycle = self._build_cycle(feed)
        except ProviderError as error:
            feed.consecutive_failures += 1
            feed.last_error = f"{type(error).__name__}: {error}"
            log.warning(
                "live refresh failed",
                symbol=feed.symbol,
                failures=feed.consecutive_failures,
                error=str(error),
            )
            self._broadcast_status()
            return
        except ValueError as error:
            # A chain that cannot be solved at all, usually no usable underlying price.
            feed.consecutive_failures += 1
            feed.last_error = str(error)
            log.warning("live refresh unusable", symbol=feed.symbol, error=str(error))
            self._broadcast_status()
            return

        previous = feed.cycle
        feed.cycle = cycle
        feed.consecutive_failures = 0
        feed.last_error = None
        self._last_cycle_at = cycle.fetched_at

        status = self.status().as_dict()
        with self._lock:
            subscribers = list(feed.subscribers.values())

        for sub in subscribers:
            full = sub.needs_full or previous is None
            delta = self._as_delta(cycle, None if full else previous, full=full)
            sub.offer("cycle", delta.as_dict())
            sub.needs_full = False
            sub.offer("status", status)

    def _build_cycle(self, feed: _Feed) -> LiveCycle:
        provider = self._get_provider()
        settings = self._settings

        quote = self._quote(provider, feed.symbol)
        spot = quote.price
        if spot is None or spot <= 0:
            raise ValueError(f"{feed.symbol} has no usable price, so nothing can be solved")

        expiry = feed.expiry
        if expiry is None:
            expiry = self._default_expiry(provider, feed.symbol)

        chain: OptionChain = provider.get_chain(feed.symbol, expiry)

        # Solved exactly the way the stored path solves, with our own rate, so a live
        # row and a stored row of the same contract differ only by their data and never
        # by their derivation.
        analysis = analyze_chain(
            chain,
            spot,
            chain.fetched_at,
            rate=settings.risk_free_rate,
        )

        feed.version += 1
        return LiveCycle(
            symbol=feed.symbol,
            expiry=expiry,
            version=feed.version,
            # The chain's own timestamp, not the quote's and not now. It is the newest
            # thing in the cycle and it is what the age on screen must be measured from.
            fetched_at=chain.fetched_at,
            source=chain.source,
            realtime=self._realtime(),
            delay_minutes=settings.quote_delay_minutes,
            quote=LiveQuote(last=quote.last, bid=quote.bid, ask=quote.ask, spot=spot),
            atm_iv=analysis.atm_iv,
            dte=analysis.dte,
            contracts=self._contracts(analysis, spot, settings.risk_free_rate),
        )

    def _default_expiry(self, provider: MarketDataProvider, symbol: str) -> date:
        """Which expiry to follow when the browser did not name one.

        The same rule the chain endpoint applies: the first expiry at or beyond the
        screen's minimum DTE, not the front month. It has to be the same rule, and it
        has to live on this side of the wire. The browser cannot pick the expiry itself
        without hardcoding a screen threshold it has no business knowing, and if these
        two resolved it differently the live panel would quietly never match the grid
        it is supposed to be updating: a stream connected, cycles arriving, and a table
        that never moves.
        """
        expiries = provider.get_expirations(symbol)
        if not expiries:
            raise ValueError(f"{symbol} has no listed expirations")

        minimum = self._screen_config().filters.dte.min_dte
        today = self._clock().date()
        eligible = [item for item in expiries if (item - today).days >= minimum]
        return eligible[0] if eligible else expiries[0]

    def _quote(self, provider: MarketDataProvider, symbol: str) -> Quote:
        """The underlying quote, reused across expiries of the same symbol.

        Two panels on two expiries of one symbol are two chains but one underlying, and
        paying for the quote twice inside the same few seconds is budget spent on a
        number that cannot have moved much. The TTL is configurable and short: this is
        deduplication, not caching.
        """
        ttl = self._settings.live_chain_ttl_seconds
        now = self._clock()
        cached = self._quote_cache.get(symbol)
        if cached is not None and (now - cached[0]).total_seconds() < ttl:
            return cached[1]

        quote = provider.get_quote(symbol)
        self._quote_cache[symbol] = (now, quote)
        return quote

    def _contracts(
        self,
        analysis: ExpiryAnalysis,
        spot: float,
        rate: float,
    ) -> dict[str, LiveContract]:
        contracts: dict[str, LiveContract] = {}
        for contract in analysis.chain.contracts:
            result = analysis.vols.get((contract.strike, contract.right))
            greeks: Greeks | None = analysis.greeks(
                contract.strike, contract.right, spot, rate, 0.0
            )
            contracts[contract_key(contract.strike, contract.right)] = LiveContract(
                strike=contract.strike,
                right=str(contract.right),
                bid=contract.bid,
                ask=contract.ask,
                mid=contract.mid,
                last=contract.last,
                volume=contract.volume,
                open_interest=contract.open_interest,
                iv=result.sigma if result and result.ok else None,
                delta=greeks.delta if greeks else None,
                gamma=greeks.gamma if greeks else None,
                theta=greeks.theta if greeks else None,
                vega=greeks.vega if greeks else None,
                reject_reason=(None if result is None or result.ok else str(result.reason)),
            )
        return contracts

    def _get_provider(self) -> MarketDataProvider:
        if self._provider is None:
            self._provider = self._provider_factory()
        return self._provider

    # ----------------------------------------------------------------------------
    # Delta encoding
    # ----------------------------------------------------------------------------

    def _as_delta(
        self,
        cycle: LiveCycle,
        previous: LiveCycle | None,
        *,
        full: bool,
    ) -> LiveDelta:
        if full or previous is None:
            changed = dict(cycle.contracts)
            removed: list[str] = []
        else:
            changed = {
                key: value
                for key, value in cycle.contracts.items()
                if previous.contracts.get(key) != value
            }
            # Named rather than left out. A contract nobody mentions again is one the
            # browser keeps rendering at its last value forever.
            removed = [key for key in previous.contracts if key not in cycle.contracts]

        return LiveDelta(
            symbol=cycle.symbol,
            expiry=cycle.expiry,
            version=cycle.version,
            fetched_at=cycle.fetched_at,
            source=cycle.source,
            realtime=cycle.realtime,
            delay_minutes=cycle.delay_minutes,
            quote=cycle.quote,
            atm_iv=cycle.atm_iv,
            dte=cycle.dte,
            changed=changed,
            removed=removed,
            full=full,
        )
