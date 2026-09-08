"""Every symbol this installation knows anything about, and how much it knows.

Three separate facts get confused constantly once there are hundreds of tickers, and
this module exists to keep them apart:

  - **In a universe group.** A name somebody typed into `universe.yaml`. Means nothing
    on its own; it is a label, not data.
  - **Has price history.** Daily bars are stored. Enough to chart it, compute realized
    volatility, and draw levels. **Not enough to screen it.**
  - **Has captured option chains.** A snapshot exists on disk. This is the only one of
    the three that lets the screener produce a candidate.

The gap between the second and third is the one that will bite. After a bulk price
sync there are hundreds of symbols with a decade of bars each and six with option
chains, and a search result that showed only "known" would invite somebody to expect a
scan of a symbol that has never been captured. So `screenable` is computed and shown,
and it is false for the overwhelming majority.

## Watchlist membership is what changes that, and it is not instant

Adding a symbol to the watchlist is what makes the daily capture job start fetching its
chains. It does not conjure a chain that was never captured: the earliest a newly added
symbol can be screened is after the next `optscan snapshot` run. That is a real wait
with no way around it, and the API says so rather than letting an empty result look
like a bug.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from optscan.config import Settings
from optscan.logging import get_logger
from optscan.storage import db, snapshot_files
from optscan.universe import Universe, UniverseError, load_universe

log = get_logger("optscan.catalogue")


@dataclass(frozen=True, slots=True)
class SymbolEntry:
    """One symbol, and every source that has something to say about it."""

    symbol: str
    groups: tuple[str, ...] = ()
    on_watchlist: bool = False

    #: Daily bar coverage, per vendor that has any.
    price_sessions: int = 0
    price_first: date | None = None
    price_last: date | None = None
    price_sources: tuple[str, ...] = ()

    #: Whether any vendor published an implied vol series for it. Only Market
    #: Chameleon downloads do, so this is the flag that says an IV rank is possible.
    has_iv_history: bool = False

    #: Most recent stored option chain, which is the only thing that makes it
    #: screenable.
    last_capture: date | None = None

    @property
    def has_prices(self) -> bool:
        return self.price_sessions > 0

    @property
    def screenable(self) -> bool:
        """Whether the screener could produce a candidate for this symbol today.

        Price history is not enough and this is the distinction the whole module is
        built around: a chain has to have been captured.
        """
        return self.last_capture is not None

    def status(self) -> str:
        """A short phrase for the UI. Never just "ok"; it says what is actually held."""
        if self.screenable:
            return f"chains through {self.last_capture}"
        if self.on_watchlist:
            return "on watchlist, awaiting first capture"
        if self.has_prices:
            return f"{self.price_sessions:,} sessions of prices, no chains"
        return "no data held"


@dataclass(frozen=True, slots=True)
class Catalogue:
    """The full set, with the group definitions that labelled it."""

    entries: tuple[SymbolEntry, ...] = ()
    groups: dict[str, int] = field(default_factory=dict)
    universe_checked: date | None = None
    universe_note: str | None = None

    def by_symbol(self, symbol: str) -> SymbolEntry | None:
        target = symbol.strip().upper()
        return next((e for e in self.entries if e.symbol == target), None)

    @property
    def watchlist(self) -> tuple[SymbolEntry, ...]:
        return tuple(e for e in self.entries if e.on_watchlist)

    @property
    def screenable(self) -> tuple[SymbolEntry, ...]:
        return tuple(e for e in self.entries if e.screenable)


def build_catalogue(settings: Settings, universe: Universe | None = None) -> Catalogue:
    """Assemble everything known about every symbol.

    One pass over three sources rather than a query per symbol. With a few hundred
    tickers the per-symbol version is a few hundred round trips to answer a question
    the sidebar asks on every page load.
    """
    if universe is None:
        try:
            universe = load_universe()
        except UniverseError as error:
            # A missing universe file makes the groups empty, not the catalogue. Price
            # history and the watchlist are real data and do not depend on a label file.
            log.warning("no universe file; groups will be empty", error=str(error))
            universe = Universe(groups={})

    group_of: dict[str, list[str]] = {}
    for name, members in universe.groups.items():
        for symbol in members:
            group_of.setdefault(symbol, []).append(name)

    with db.session(settings.sqlite_path) as conn:
        db.seed_watchlist(conn, settings.default_watchlist)
        watchlist = set(db.list_watchlist(conn))
        coverage = {
            row["symbol"]: row
            for row in conn.execute(
                """
                SELECT symbol,
                       -- DISTINCT, not a sum over sources. AAPL holds 2,511 Alpaca
                       -- sessions and 3,188 Market Chameleon ones covering mostly the
                       -- same days; summing them reported 5,699 sessions of history
                       -- for a symbol that has about 3,188. The two richest histories
                       -- in the database were the two the number lied about.
                       COUNT(DISTINCT session_date) AS sessions,
                       MIN(session_date)            AS first,
                       MAX(session_date)            AS last,
                       MAX(iv30 IS NOT NULL)        AS has_iv,
                       GROUP_CONCAT(DISTINCT source) AS sources
                FROM vendor_daily
                GROUP BY symbol
                """
            )
        }

    symbols = set(group_of) | set(coverage) | watchlist
    entries: list[SymbolEntry] = []
    for symbol in sorted(symbols):
        row = coverage.get(symbol)
        entries.append(
            SymbolEntry(
                symbol=symbol,
                groups=tuple(sorted(group_of.get(symbol, ()))),
                on_watchlist=symbol in watchlist,
                price_sessions=int(row["sessions"]) if row else 0,
                price_first=_as_date(row["first"]) if row else None,
                price_last=_as_date(row["last"]) if row else None,
                price_sources=tuple(sorted((row["sources"] or "").split(","))) if row else (),
                has_iv_history=bool(row["has_iv"]) if row else False,
                # Only watchlist symbols are ever captured, so only they can have a
                # chain on disk. Scanning the snapshot tree for all of them would be a
                # directory walk per symbol to prove a negative.
                last_capture=last_capture(settings, symbol) if symbol in watchlist else None,
            )
        )

    log.info(
        "catalogue built",
        symbols=len(entries),
        watchlist=len(watchlist),
        with_prices=sum(1 for e in entries if e.has_prices),
        screenable=sum(1 for e in entries if e.screenable),
    )
    return Catalogue(
        entries=tuple(entries),
        groups={name: len(members) for name, members in sorted(universe.groups.items())},
        universe_checked=universe.date_checked,
        universe_note=universe.staleness_note(),
    )


def search(
    catalogue: Catalogue,
    query: str = "",
    *,
    group: str | None = None,
    only_watchlist: bool = False,
    only_screenable: bool = False,
    limit: int = 50,
) -> list[SymbolEntry]:
    """Filter and rank the catalogue for a search box.

    Ranking puts an exact ticker match first, then prefixes, then anything containing
    the query. Typing "MP" should find MP before AMPL and MPWR, and a plain substring
    match sorted alphabetically would bury it.
    """
    text = query.strip().upper()
    results = list(catalogue.entries)

    if group:
        results = [e for e in results if group in e.groups]
    if only_watchlist:
        results = [e for e in results if e.on_watchlist]
    if only_screenable:
        results = [e for e in results if e.screenable]
    if text:
        results = [e for e in results if text in e.symbol]

    def rank(entry: SymbolEntry) -> tuple:
        if not text:
            exactness = 0
        elif entry.symbol == text:
            exactness = -2
        elif entry.symbol.startswith(text):
            exactness = -1
        else:
            exactness = 0
        # Watchlist and screenable symbols float up: they are the ones somebody is
        # most likely to be reaching for, and the ones an action can be taken on.
        return (exactness, not entry.on_watchlist, not entry.screenable, entry.symbol)

    results.sort(key=rank)
    return results[:limit]


def last_capture(settings: Settings, symbol: str) -> date | None:
    """Latest stored session for a symbol, read from the partition path.

    Public because the watchlist router needs the same answer, and it had its own copy
    of this until the catalogue was written. Two implementations of "when was this last
    captured" is exactly the kind of thing that drifts silently.

    From the directory name rather than by opening the parquet: the layout is
    `symbol=SPY/session_date=2026-07-30/...`, so the answer is already in the path.
    """
    latest: date | None = None
    for path in snapshot_files(settings.snapshot_path, symbol):
        part = path.parent.name
        if not part.startswith("session_date="):
            continue
        try:
            captured = date.fromisoformat(part.removeprefix("session_date="))
        except ValueError:
            continue
        if latest is None or captured > latest:
            latest = captured
    return latest


def _as_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None
