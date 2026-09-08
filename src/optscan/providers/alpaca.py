"""Alpaca adapter.

Everything in this file was measured against the live API on 2026-09-07 with a paper
account on the free Basic plan. Nothing is taken from the documentation, because the
documentation disagrees with the API in three places that matter and the disagreements
all fail silently.

## The three things the docs get wrong or leave out

**Market data is not on the host people paste.** `paper-api.alpaca.markets` is the
trading API. Every quote, bar and chain lives on `data.alpaca.markets`, and a data
request sent to the trading host returns a 404 whose body says "Not Found", which reads
like a delisted symbol rather than a wrong base URL.

**`feed=iex` is a trap and it returns 200.** Measured on QQQ for 2026-08-25: IEX
reported 433,063 shares against SIP's 24,035,386, so IEX is about 1.8 percent of
consolidated volume, and its close was 710.66 against 710.72. Every number is
plausible, none is right, and nothing in the response says which exchange it covers.
The free plan does serve SIP for anything older than fifteen minutes, so SIP is the
default here and IEX is never chosen automatically.

**Greeks and implied volatility ARE served on the free indicative feed.** The docs
imply otherwise and a first probe agreed, because a `limit=2` chain request returns the
two deepest contracts, which have never traded and therefore carry a quote and nothing
else. Filtered to a real expiry, snapshots carry `greeks`, `impliedVolatility`,
`dailyBar`, `latestTrade` and `latestQuote`.

## What this vendor cannot do, which shapes what is built on it

**There is no historical option quote endpoint.** `/v1beta1/options/quotes` is a 404 at
every parameter combination tried; only `/quotes/latest` exists. Historical option
trades are shallow too: a 2024 window returns an empty object while a two day old one
returns trades. Historical option **bars** do go back to at least 2024-01-18, earlier
than the documented "February 2024".

So a historical option chain can be reconstructed at **trade** prices and never at the
mid. That is a weaker mark than this project's live path, which prices at the mid and
refuses a crossed or absent quote, and it matters most exactly where it is used least
safely: a thin contract's last trade can be hours stale and on the wrong side.

**OPRA is refused with `403 OPRA agreement is not signed`**, which is a different
problem from a bad key and is reported as such: the fix is a form on Alpaca's site, not
a new credential.

## Open interest comes from the other host

The screen filters on open interest and the data snapshots do not carry it. It lives on
the trading host at `/v2/options/contracts`, where 99 of 100 sampled QQQ contracts had
one. So a chain is assembled from two hosts, and a contract whose open interest could
not be found gets None rather than zero, because zero is a real and meaningful value
for a listed contract and the liquidity filter treats the two very differently.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, ClassVar

import httpx

from optscan.config import ALPACA_OPTIONS_START, Settings
from optscan.logging import get_logger
from optscan.models import (
    OptionChain,
    OptionContract,
    PriceBar,
    Quote,
    Right,
    SymbolEvents,
)
from optscan.providers.base import MarketDataProvider
from optscan.providers.errors import (
    AuthenticationError,
    MalformedResponse,
    NoDataAvailable,
    ProviderUnavailable,
    RateLimited,
    SymbolNotFound,
)
from optscan.providers.ratelimit import RateLimiter

log = get_logger("optscan.providers.alpaca")

#: Market data. Not the trading host; see the module docstring.
STOCK_BARS_PATH = "/v2/stocks/bars"
STOCK_SNAPSHOT_PATH = "/v2/stocks/snapshots"
OPTION_CHAIN_PATH = "/v1beta1/options/snapshots/{underlying}"
OPTION_BARS_PATH = "/v1beta1/options/bars"
CORPORATE_ACTIONS_PATH = "/v1/corporate-actions"

#: Trading host. Contract metadata, and the only place open interest is published.
TRADING_URL = "https://paper-api.alpaca.markets"
OPTION_CONTRACTS_PATH = "/v2/options/contracts"

#: The feed name differs per endpoint, and each is rejected where the other works.
#: Measured 2026-09-07 on QQQ:
#:
#:     endpoint    iex              sip                    delayed_sip
#:     bars        200, 376k vol    200, 23.6m vol         400 invalid feed
#:     snapshot    200, 553k vol    403 recent SIP         200, 33.2m vol
#:
#: So historical bars take `sip` and snapshots take `delayed_sip`, and neither is a
#: preference: the other value is an error or a wrong number. IEX is available on both
#: and is right on neither, being roughly two percent of consolidated volume while
#: returning 200 and a plausible price.
STOCK_BARS_FEED = "sip"

#: Consolidated and fifteen minutes behind, which is what the free plan permits for a
#: recent quote. Strictly better here than the real time IEX book: this project reads
#: the last stored capture rather than deciding a fill, and it already reports quote
#: age on every screen, so being late is visible while being 2 percent of the market
#: is not.
STOCK_SNAPSHOT_FEED = "delayed_sip"

#: Alpaca's rate limit headers. Lowercase because httpx normalizes them.
HEADER_LIMIT = "x-ratelimit-limit"
HEADER_REMAINING = "x-ratelimit-remaining"
HEADER_RESET = "x-ratelimit-reset"

#: `/v2/options/contracts` pages at this many rows. A full chain for a liquid
#: underlying runs to several thousand contracts across all expiries.
CONTRACTS_PAGE_LIMIT = 10_000

#: The chain snapshot endpoint's own maximum page size.
SNAPSHOT_PAGE_LIMIT = 1_000

#: Shortest possible OCC symbol: a one character root plus six date digits, a side
#: letter and an eight digit strike.
MIN_OCC_LENGTH = 16

#: Everything after the root: YYMMDD + C/P + 8 strike digits.
OCC_TAIL_LENGTH = 15

#: The free plan refuses SIP for anything inside this window, with
#: `403 subscription does not permit querying recent SIP data`. It is a refusal
#: rather than a silent downgrade, which is the good outcome, but it means every
#: historical request has to stop short of now rather than ending at it. One extra
#: minute of margin because the boundary is enforced against Alpaca's clock.
SIP_RECENCY_MINUTES = 16

#: How far out to ask for expiries when the caller did not name one.
#:
#: **Never omit the expiry bounds on /v2/options/contracts.** Measured 2026-09-07
#: on QQQ: with no bounds the endpoint returns 1,606 contracts across exactly 4
#: expiries, 200 OK, no next_page_token, and paging at limit=100 walks 17 pages to
#: the same 1,606. It is an undocumented default window, not pagination, and the
#: result looks complete. With bounds: 90 days gives 20 expiries, two years gives
#: 33. A screener reading the unbounded answer would believe QQQ lists options for
#: the next four days only.
EXPIRY_LOOKAHEAD_DAYS = 730

#: How far ahead to look for a dividend. Longer than any expiry this screen will
#: trade, so the nearest ex date inside a position's life is always found.
EVENT_LOOKAHEAD_DAYS = 400

#: Refuse to walk more pages than this for one call. A runaway page_token loop against
#: a rate limited endpoint is the failure that costs a capture window.
MAX_PAGES = 40


def occ_symbol(symbol: str, expiry: date, right: Right, strike: float) -> str:
    """Build an OCC contract symbol, the identifier Alpaca uses everywhere.

    `QQQ260918C00400000` is QQQ, 2026-09-18, call, strike 400. The strike is in
    thousandths of a dollar, zero padded to eight digits, which is why a half dollar
    strike is 00400500 and not 00400050.
    """
    letter = "C" if Right.parse(right) is Right.CALL else "P"
    thousandths = round(strike * 1000)
    return f"{symbol.upper()}{expiry:%y%m%d}{letter}{thousandths:08d}"


def parse_occ_symbol(contract: str) -> tuple[str, date, Right, float]:
    """Split an OCC symbol back into its parts.

    The underlying is whatever precedes the six digit date, so it is found by position
    from the right rather than by length: root symbols are one to six characters and a
    fixed offset silently mangles the short ones.
    """
    body = contract.strip().upper()
    if len(body) < MIN_OCC_LENGTH:
        raise MalformedResponse(f"not an OCC option symbol: {contract!r}")
    tail = body[-OCC_TAIL_LENGTH:]
    underlying = body[: -len(tail)]
    try:
        expiry = datetime.strptime(tail[:6], "%y%m%d").date()
        right = Right.parse("C" if tail[6] == "C" else "P")
        strike = int(tail[7:]) / 1000.0
    except (ValueError, IndexError) as error:
        raise MalformedResponse(f"not an OCC option symbol: {contract!r}") from error
    if tail[6] not in ("C", "P") or not underlying:
        raise MalformedResponse(f"not an OCC option symbol: {contract!r}")
    return underlying, expiry, right, strike


class AlpacaProvider(MarketDataProvider):
    """Alpaca market data, free Basic plan by default.

    `realtime` is decided by config rather than by anything in a response, exactly as
    with Tradier: the indicative feed is documented as fifteen minutes delayed and a
    delayed quote is indistinguishable from a live one once it is parsed.
    """

    name: ClassVar[str] = "alpaca"
    realtime: ClassVar[bool] = False

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        if not settings.alpaca_credentials_set:
            raise AuthenticationError(
                "Alpaca needs both OPTSCAN_ALPACA_KEY_ID and OPTSCAN_ALPACA_SECRET_KEY. "
                "One without the other cannot authenticate."
            )
        self.settings = settings
        self.feed = settings.alpaca_feed
        self.realtime = settings.alpaca_is_realtime
        self._limiter = RateLimiter(settings.alpaca_requests_per_minute)
        self._client = client or httpx.Client(
            timeout=30.0,
            headers={
                "APCA-API-KEY-ID": settings.alpaca_key_id.get_secret_value(),
                "APCA-API-SECRET-KEY": settings.alpaca_secret_key.get_secret_value(),
                "accept": "application/json",
            },
        )

    def close(self) -> None:
        self._client.close()

    # ----------------------------------------------------------------- transport

    def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """One request, with the vendor's failure modes turned into ours.

        The status codes are separated because their remedies are completely
        different, and a single "request failed" would send someone to regenerate a
        key that is working fine.
        """
        self._limiter.acquire()
        try:
            response = self._client.get(url, params=params)
        except httpx.HTTPError as error:
            raise ProviderUnavailable(f"alpaca request failed: {error}") from error

        self._note_rate_limit(response)

        if response.status_code == httpx.codes.UNAUTHORIZED:
            raise AuthenticationError(
                "Alpaca rejected the key pair. Check OPTSCAN_ALPACA_KEY_ID and "
                "OPTSCAN_ALPACA_SECRET_KEY, and that they belong to the same account."
            )
        if response.status_code == httpx.codes.FORBIDDEN:
            message = self._message(response)
            if "opra" in message.lower():
                raise AuthenticationError(
                    f"Alpaca refused the OPRA feed: {message}. This is an unsigned "
                    "market data agreement rather than a bad key, and it is fixed on "
                    "Alpaca's site, not by regenerating credentials. Set "
                    "OPTSCAN_ALPACA_FEED=indicative to use the free delayed feed."
                )
            raise AuthenticationError(f"Alpaca refused the request: {message}")
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            raise RateLimited(
                f"Alpaca rate limit reached ({self.settings.alpaca_requests_per_minute}/min)"
            )
        if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
            raise ProviderUnavailable(f"alpaca returned {response.status_code}")
        if response.status_code != httpx.codes.OK:
            raise MalformedResponse(
                f"alpaca returned {response.status_code} for {url}: {self._message(response)}"
            )

        try:
            body = response.json()
        except ValueError as error:
            raise MalformedResponse("alpaca returned a body that is not JSON") from error
        if not isinstance(body, dict):
            raise MalformedResponse(f"expected a JSON object, got {type(body).__name__}")
        return body

    @staticmethod
    def _message(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return response.text[:200]
        return str(body.get("message", body))[:300] if isinstance(body, dict) else str(body)[:200]

    def _note_rate_limit(self, response: httpx.Response) -> None:
        remaining = response.headers.get(HEADER_REMAINING)
        if remaining is None:
            return
        try:
            left = int(remaining)
        except ValueError:
            return
        # Logged only when it is nearly gone. A line per request would bury everything
        # else in a run that touches hundreds of symbols.
        if left <= max(self.settings.alpaca_requests_per_minute // 20, 5):
            log.warning(
                "alpaca rate limit nearly spent",
                remaining=left,
                limit=response.headers.get(HEADER_LIMIT),
                reset=response.headers.get(HEADER_RESET),
            )

    def _paged(
        self, url: str, params: dict[str, Any], collect: str
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Follow `next_page_token` until it runs out, up to MAX_PAGES."""
        merged: Any = None
        token: str | None = None
        for page in range(MAX_PAGES):
            body = self._get(url, {**params, **({"page_token": token} if token else {})})
            chunk = body.get(collect)
            if isinstance(chunk, dict):
                merged = {**(merged or {}), **chunk}
            elif isinstance(chunk, list):
                merged = (merged or []) + chunk
            token = body.get("next_page_token")
            if not token:
                return merged if merged is not None else {}
            if page == MAX_PAGES - 1:
                log.warning("alpaca paging stopped at the cap", url=url, pages=MAX_PAGES)
        return merged if merged is not None else {}

    # ------------------------------------------------------------------ the API

    def get_quote(self, symbol: str) -> Quote:
        """Underlying quote, from the snapshot endpoint.

        The snapshot is used rather than `/quotes/latest` because one request carries
        the quote, the day's bar and the previous close, and the previous close is
        what every percent change in the UI is measured against.
        """
        ticker = symbol.strip().upper()
        body = self._get(
            f"{self.settings.alpaca_data_url}{STOCK_SNAPSHOT_PATH}",
            {"symbols": ticker, "feed": STOCK_SNAPSHOT_FEED},
        )
        snapshot = body.get(ticker)
        if not isinstance(snapshot, dict) or not snapshot:
            raise SymbolNotFound(f"alpaca has no snapshot for {ticker}")

        quote = snapshot.get("latestQuote") or {}
        trade = snapshot.get("latestTrade") or {}
        daily = snapshot.get("dailyBar") or {}
        previous = snapshot.get("prevDailyBar") or {}

        return Quote(
            symbol=ticker,
            last=_positive(trade.get("p")) or _positive(daily.get("c")),
            bid=_positive(quote.get("bp")),
            ask=_positive(quote.get("ap")),
            bid_size=_size(quote.get("bs")),
            ask_size=_size(quote.get("as")),
            previous_close=_positive(previous.get("c")),
            volume=_size(daily.get("v")),
            fetched_at=self.now(),
            source=self.name,
        )

    def get_expirations(self, symbol: str) -> list[date]:
        """Listed expiries, ascending, from the contract metadata on the trading host.

        An underlying with no listed options returns an empty list. That is a fact
        about the symbol, not a failure, and the base class documents it as such.
        """
        contracts = self._contract_metadata(symbol)
        return sorted({row["expiry"] for row in contracts.values()})

    def _contract_metadata(self, symbol: str, expiry: date | None = None) -> dict[str, dict]:
        """Contract rows keyed by OCC symbol, from the trading host.

        This exists for one field. The data host's chain snapshots carry quotes,
        greeks and a vendor implied vol but no open interest, and the screen filters
        on open interest, so the two hosts are joined per contract.
        """
        params: dict[str, Any] = {
            "underlying_symbols": symbol.strip().upper(),
            "status": "active",
            "limit": CONTRACTS_PAGE_LIMIT,
        }
        if expiry is not None:
            params["expiration_date"] = expiry.isoformat()
        else:
            # Bounds are mandatory, not an optimization. See EXPIRY_LOOKAHEAD_DAYS:
            # without them this endpoint quietly answers for four expiries.
            today = self.now().date()
            params["expiration_date_gte"] = today.isoformat()
            params["expiration_date_lte"] = (
                today + timedelta(days=EXPIRY_LOOKAHEAD_DAYS)
            ).isoformat()

        rows = self._paged(
            f"{TRADING_URL}{OPTION_CONTRACTS_PATH}", params, collect="option_contracts"
        )
        out: dict[str, dict] = {}
        for row in rows or []:
            contract_symbol = row.get("symbol")
            raw_expiry = row.get("expiration_date")
            if not contract_symbol or not raw_expiry:
                continue
            try:
                out[contract_symbol] = {
                    "expiry": date.fromisoformat(raw_expiry),
                    "strike": float(row["strike_price"]),
                    "right": Right.parse("C" if row.get("type") == "call" else "P"),
                    "open_interest": _size(row.get("open_interest")),
                    "close_price": _positive(row.get("close_price")),
                    "size": int(float(row.get("size") or 100)),
                }
            except (KeyError, TypeError, ValueError):
                # One unparseable contract row must not lose the other 8,000.
                log.debug("skipping unparseable contract row", symbol=contract_symbol)
        return out

    def get_chain(self, symbol: str, expiry: date) -> OptionChain:
        """Every contract at one expiry, quotes joined to open interest."""
        ticker = symbol.strip().upper()
        metadata = self._contract_metadata(ticker, expiry)
        if not metadata:
            raise NoDataAvailable(f"alpaca lists no {ticker} contracts expiring {expiry}")

        snapshots = self._paged(
            f"{self.settings.alpaca_data_url}" + OPTION_CHAIN_PATH.format(underlying=ticker),
            {
                "feed": self.feed,
                "limit": SNAPSHOT_PAGE_LIMIT,
                "expiration_date": expiry.isoformat(),
            },
            collect="snapshots",
        )
        if not isinstance(snapshots, dict):
            snapshots = {}

        contracts: list[OptionContract] = []
        for contract_symbol, meta in sorted(metadata.items()):
            snapshot = snapshots.get(contract_symbol) or {}
            quote = snapshot.get("latestQuote") or {}
            trade = snapshot.get("latestTrade") or {}
            daily = snapshot.get("dailyBar") or {}
            contracts.append(
                OptionContract(
                    symbol=ticker,
                    contract_symbol=contract_symbol,
                    expiry=meta["expiry"],
                    strike=meta["strike"],
                    right=meta["right"],
                    bid=_positive(quote.get("bp")),
                    ask=_positive(quote.get("ap")),
                    # The contract metadata's close_price is the previous session's
                    # settle and is present far more often than a latest trade, which
                    # only exists for contracts that traded today.
                    last=_positive(trade.get("p")) or meta["close_price"],
                    last_trade_at=_timestamp(trade.get("t")),
                    volume=_size(daily.get("v")),
                    open_interest=meta["open_interest"],
                    # Recorded, never used. This project solves its own vol from the
                    # mid; a vendor's number is kept only so the two can be compared.
                    vendor_iv=_positive(snapshot.get("impliedVolatility")),
                    contract_size=meta["size"],
                    fetched_at=self.now(),
                    source=self.name,
                )
            )

        underlying = None
        try:
            underlying = self.get_quote(ticker).price
        except (SymbolNotFound, ProviderUnavailable):
            # A chain without a spot is still a chain. The caller decides whether it
            # can do anything with one, and analytics already refuse without a spot.
            log.warning("chain fetched without an underlying price", symbol=ticker)

        return OptionChain(
            symbol=ticker,
            expiry=expiry,
            underlying_price=underlying,
            contracts=tuple(contracts),
            fetched_at=self.now(),
            source=self.name,
        )

    def get_history(self, symbol: str, days: int) -> list[PriceBar]:
        """Daily bars over roughly the last `days` calendar days, ascending.

        SIP, never IEX. See the module docstring: IEX returns 200 with about two
        percent of the volume and no indication that is what it did.
        """
        ticker = symbol.strip().upper()
        # Stops short of now rather than ending at it: SIP inside the last quarter of
        # an hour is refused outright on this plan. Today's session still comes back,
        # it is simply cut off fifteen minutes ago, which for a daily bar matters only
        # while the market is open and is exactly what `fetched_at` is for.
        end = self.now() - timedelta(minutes=SIP_RECENCY_MINUTES)
        start = end.date() - timedelta(days=max(days, 1))
        bars = self._paged(
            f"{self.settings.alpaca_data_url}{STOCK_BARS_PATH}",
            {
                "symbols": ticker,
                "timeframe": "1Day",
                "start": start.isoformat(),
                "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "feed": STOCK_BARS_FEED,
                "limit": SNAPSHOT_PAGE_LIMIT,
                "adjustment": "all",
                "sort": "asc",
            },
            collect="bars",
        )
        rows = (bars or {}).get(ticker) if isinstance(bars, dict) else None
        if not rows:
            raise NoDataAvailable(f"alpaca returned no daily bars for {ticker}")
        return [_bar(ticker, row, self.name) for row in rows]

    def get_events(self, symbol: str) -> SymbolEvents:
        """Ex dividend dates, and an honest refusal to guess at earnings.

        ## Half a calendar, said out loud

        Alpaca publishes corporate actions and does not publish an earnings calendar.
        Those are two different needs in this screen and only one is met:

        - **Ex dividend** drives `exclude_early_assignment_risk`. A short call that is
          in the money over an ex date is the classic assignment, and this is a real
          answer to it: measured on AAPL, `cash_dividends` carries `ex_date`, `rate`,
          `record_date` and `payable_date`, and flags specials separately.
        - **Earnings** drives `exclude_earnings`, which is the heavier of the two
          filters, and nothing here can serve it. `earnings_date` therefore stays
          None, which the screen already handles by widening and saying it could not
          check. It is not set to a guess, because a screen that believes it checked
          earnings and did not is worse than one that knows it could not.

        Only forthcoming ex dates matter to a position being opened now, so the window
        starts today. A dividend that already went is not a risk to anything.
        """
        ticker = symbol.strip().upper()
        today = self.now().date()
        body = self._get(
            f"{self.settings.alpaca_data_url}{CORPORATE_ACTIONS_PATH}",
            {
                "symbols": ticker,
                "start": today.isoformat(),
                "end": (today + timedelta(days=EVENT_LOOKAHEAD_DAYS)).isoformat(),
                "limit": 100,
            },
        )
        actions = body.get("corporate_actions") or {}
        dividends = actions.get("cash_dividends") or []

        upcoming = sorted(
            (row for row in dividends if _event_date(row.get("ex_date")) is not None),
            key=lambda row: _event_date(row["ex_date"]),
        )
        # The next one only. A screen asks "is there an ex date inside this expiry",
        # and the nearest one answers it; a list of four would invite the caller to
        # pick, which is a decision analytics should not be making here.
        nearest = next((row for row in upcoming if _event_date(row["ex_date"]) >= today), None)

        return SymbolEvents(
            symbol=ticker,
            earnings_date=None,
            earnings_estimated=False,
            ex_dividend_date=_event_date(nearest.get("ex_date")) if nearest else None,
            dividend_amount=_positive(nearest.get("rate")) if nearest else None,
            fetched_at=self.now(),
            source=self.name,
        )

    def get_daily_bars_bulk(
        self,
        symbols: Sequence[str],
        start: date,
        end: datetime,
        *,
        limit: int = 10_000,
        max_pages: int = 200,
    ) -> dict[str, list[dict[str, Any]]]:
        """Daily bars for many symbols in one request. Raw vendor rows, keyed by symbol.

        Outside `MarketDataProvider` on purpose. That interface is one symbol at a
        time, which is right for a screener reading one chain and wrong for a bulk
        backfill: measured 2026-09-07, 58 symbols came back in one request in 0.7
        seconds, against roughly 35 seconds and 58 requests the naive way.

        Raw rows rather than `PriceBar` because the caller stores three volume fields
        this project's bar model does not carry: `n` the trade count and `vw` the
        volume weighted price, alongside `v`. Volume alone cannot tell 30 million
        shares in 500,000 prints from the same 30 million in 5,000.

        **Symbols the vendor does not know are omitted with no error.** A 58 symbol
        request returned 57. The caller compares what it asked for against what came
        back; nothing here can distinguish a delisting from a typo.
        """
        tickers = [s.strip().upper() for s in symbols if s and s.strip()]
        if not tickers:
            return {}

        merged: dict[str, list[dict[str, Any]]] = {}
        token: str | None = None
        for page in range(max_pages):
            params: dict[str, Any] = {
                "symbols": ",".join(tickers),
                "timeframe": "1Day",
                "start": start.isoformat(),
                "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "feed": STOCK_BARS_FEED,
                "limit": limit,
                "adjustment": "all",
                "sort": "asc",
            }
            if token:
                params["page_token"] = token
            body = self._get(f"{self.settings.alpaca_data_url}{STOCK_BARS_PATH}", params)
            for symbol, rows in (body.get("bars") or {}).items():
                merged.setdefault(symbol, []).extend(rows or [])
            token = body.get("next_page_token")
            if not token:
                break
            if page == max_pages - 1:
                log.warning(
                    "bulk bar paging stopped at the cap",
                    symbols=len(tickers),
                    pages=max_pages,
                )
        return merged

    def get_option_bars(
        self,
        contract_symbol: str,
        start: date,
        end: date,
        *,
        timeframe: str = "1Day",
    ) -> list[PriceBar]:
        """Historical bars for one option contract. Not part of the provider interface.

        The only historical option data this vendor has. There is no historical quote
        endpoint at all, so these are **trade** prices: a bar exists on a day the
        contract traded and is missing on a day it did not, and its close is the last
        print rather than a mid. A thin contract's close can be hours old and on
        whichever side of the spread happened to lift.

        That distinction has to survive into anything built on this. Everything else
        in this project prices at the mid and refuses when a quote is crossed or
        absent, which is a materially stronger guarantee than "somebody traded here".
        """
        if start < ALPACA_OPTIONS_START:
            # Asking earlier returns an empty series rather than an error, which is
            # indistinguishable from a contract that simply did not trade.
            log.warning(
                "option history starts before alpaca has any",
                requested=str(start),
                available_from=str(ALPACA_OPTIONS_START),
            )
        underlying, _, _, _ = parse_occ_symbol(contract_symbol)
        bars = self._paged(
            f"{self.settings.alpaca_data_url}{OPTION_BARS_PATH}",
            {
                "symbols": contract_symbol,
                "timeframe": timeframe,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "limit": SNAPSHOT_PAGE_LIMIT,
                "sort": "asc",
            },
            collect="bars",
        )
        rows = (bars or {}).get(contract_symbol) if isinstance(bars, dict) else None
        return [_bar(underlying, row, self.name) for row in rows or []]


# ---------------------------------------------------------------------- parsing


def _positive(value: Any) -> float | None:
    """A positive float, or None. Zero and negatives become None.

    Alpaca publishes 0 for a side with no quote. Zero is not a price, and letting one
    through would make a mid of half the ask on every unquoted wing.
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _size(value: Any) -> int | None:
    """A non-negative integer, or None. Zero is kept: it is a real size."""
    if value is None or value == "":
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _event_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _bar(symbol: str, row: dict[str, Any], source: str) -> PriceBar:
    try:
        return PriceBar(
            symbol=symbol,
            ts=_timestamp(row["t"]),
            open=float(row["o"]),
            high=float(row["h"]),
            low=float(row["l"]),
            close=float(row["c"]),
            volume=_size(row.get("v")),
            fetched_at=datetime.now(UTC),
            source=source,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise MalformedResponse(f"alpaca bar is missing a field: {row}") from error
