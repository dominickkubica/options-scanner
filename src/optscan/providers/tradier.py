"""Tradier adapter.

Everything in this file about Tradier's wire format was read from docs.tradier.com on
2026-07-31 and is recorded in DECISIONS.md with the URLs. Nothing here was written
from memory, because the one file in this project whose fragility is a known problem
is the yfinance adapter, and it got that way by matching on strings nobody documented.

Three facts about this vendor decide how the rest of Phase 5 is shaped:

- **Sandbox data is delayed by fifteen minutes.** Their FAQ states it plainly and there
  is no setting that changes it. `realtime` on this provider is therefore false in
  sandbox no matter what else is configured.
- **Sandbox cannot stream.** "Presently, we do not offer a delayed streaming endpoint
  for paper trading." There is a websocket, it needs a production session, and the
  free tier this project is built against cannot open one. So the live feed polls.
- **The rate limit is per access token, per minute**, 60 in sandbox and 120 in
  production, and the response headers carry the running count. Both are respected.

What this vendor does not sell is a corporate calendar, so `get_events` stays
unimplemented and inherits the base class refusal. Callers already handle that: the
screen widens and says it could not check earnings.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from typing import Any, ClassVar

import httpx

from optscan.logging import get_logger
from optscan.models import OptionChain, OptionContract, PriceBar, Quote, Right
from optscan.providers.base import MarketDataProvider
from optscan.providers.errors import (
    AuthenticationError,
    MalformedResponse,
    NoDataAvailable,
    ProviderUnavailable,
    RateLimited,
    SymbolNotFound,
)
from optscan.providers.ratelimit import RateLimiter, epoch_to_seconds

log = get_logger("optscan.providers.tradier")

API_VERSION = "v1"

QUOTES_PATH = f"/{API_VERSION}/markets/quotes"
EXPIRATIONS_PATH = f"/{API_VERSION}/markets/options/expirations"
CHAINS_PATH = f"/{API_VERSION}/markets/options/chains"
HISTORY_PATH = f"/{API_VERSION}/markets/history"

#: The two rate limit headers this adapter reads. Tradier also sends
#: `X-Ratelimit-Allowed` and `X-Ratelimit-Used`; both were declared here and never
#: read, under a comment claiming they were logged when a limit was hit. They were not.
#: Removed rather than wired up, because the available count is what the limiter needs
#: and a constant nothing references is a claim nobody checks.
HEADER_AVAILABLE = "X-Ratelimit-Available"
HEADER_EXPIRY = "X-Ratelimit-Expiry"

HTTP_TOO_MANY_REQUESTS = 429
HTTP_BAD_REQUEST = 400
HTTP_NOT_FOUND = 404
HTTP_SERVER_ERROR = 500

#: Auth failures. 403 is included because a token without a market data entitlement
#: fails this way rather than with a 401, and retrying either is pointless.
AUTH_STATUSES = frozenset({401, 403})


def _clean_float(value: Any) -> float | None:
    """None for missing or negative. Zero is preserved: a zero bid is a real state."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or number < 0:
        return None
    return number


def _clean_int(value: Any) -> int | None:
    number = _clean_float(value)
    return None if number is None else int(number)


def _clean_epoch(value: Any) -> datetime | None:
    """A Tradier epoch field as an aware UTC datetime, or None.

    Tradier documents trade_date, bid_date and ask_date as "Unix timestamp" without
    naming the unit, so the unit is inferred by magnitude rather than assumed. A zero
    is the vendor's way of saying there has been no trade, not midnight in 1970, and
    collapsing those two would put a decades old timestamp on a fresh contract and
    make every staleness check pass.
    """
    number = _clean_float(value)
    if number is None or number <= 0:
        return None
    try:
        return datetime.fromtimestamp(epoch_to_seconds(number), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _as_list(value: Any) -> list[Any]:
    """Normalize Tradier's one-or-many shape.

    Their schema uses oneOf: a single result comes back as an object and several come
    back as an array. Iterating the object form would walk its keys, which is the kind
    of bug that only shows up on the one symbol somebody looks at alone.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


class TradierProvider(MarketDataProvider):
    """Chains, quotes and daily bars from Tradier's REST API."""

    name: ClassVar[str] = "tradier"

    def __init__(
        self,
        token: str,
        *,
        base_url: str,
        realtime: bool = False,
        timeout: float = 10.0,
        requests_per_minute: int = 60,
        client: httpx.Client | None = None,
        limiter: RateLimiter | None = None,
    ) -> None:
        if not token or not token.strip():
            raise AuthenticationError(
                "No Tradier token is configured. Create a sandbox token at "
                "developer.tradier.com and set OPTSCAN_TRADIER_TOKEN in .env."
            )
        self._base_url = base_url.rstrip("/")
        self._limiter = limiter or RateLimiter(requests_per_minute)
        # Instance attribute, deliberately shadowing the class default. Whether this
        # vendor is real time is a property of the account and the environment, not of
        # the adapter, so it cannot be a ClassVar the way yfinance's can.
        self.realtime = realtime
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )

    def close(self) -> None:
        """Release the connection pool, if this instance opened one."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> TradierProvider:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ----------------------------------------------------------------------------
    # Transport
    # ----------------------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """One GET, budgeted, with every failure mapped onto our taxonomy.

        The token is never in the message of anything raised here. It is only ever in
        a header set once in the constructor, and the params are safe to quote.
        """
        self._limiter.acquire()
        try:
            response = self._client.get(path, params=params)
        except httpx.TimeoutException as error:
            raise ProviderUnavailable(f"Tradier timed out on {path}: {error}") from error
        except httpx.HTTPError as error:
            raise ProviderUnavailable(f"Tradier request failed on {path}: {error}") from error

        self._limiter.observe_vendor_counter(_clean_int(response.headers.get(HEADER_AVAILABLE)))

        if response.status_code in AUTH_STATUSES:
            raise AuthenticationError(
                f"Tradier rejected the token with {response.status_code}. "
                "Check OPTSCAN_TRADIER_TOKEN and OPTSCAN_TRADIER_ENVIRONMENT."
            )
        if response.status_code == HTTP_TOO_MANY_REQUESTS:
            raise RateLimited(
                f"Tradier rate limited {path}",
                retry_after_seconds=self._retry_after(response),
            )
        if response.status_code >= HTTP_SERVER_ERROR:
            raise ProviderUnavailable(f"Tradier returned {response.status_code} on {path}")
        if response.status_code == HTTP_NOT_FOUND:
            raise SymbolNotFound(f"Tradier has no resource at {path} for {params}")
        if response.status_code >= HTTP_BAD_REQUEST:
            raise MalformedResponse(
                f"Tradier rejected the request to {path} with {response.status_code}"
            )

        try:
            payload = response.json()
        except ValueError as error:
            raise MalformedResponse(f"Tradier returned non-JSON from {path}") from error
        if not isinstance(payload, dict):
            raise MalformedResponse(f"Tradier returned a {type(payload).__name__} from {path}")
        return payload

    def _retry_after(self, response: httpx.Response) -> float | None:
        """Seconds until the current rate limit window rolls, when we can tell.

        Tradier does not send a Retry-After. It sends the window expiry, so the wait is
        derived from that. Returning None leaves the caller on its own backoff, which
        is the right fallback rather than a made up number.
        """
        expiry = _clean_float(response.headers.get(HEADER_EXPIRY))
        if expiry is None:
            return None
        # self.now() rather than datetime.now, like every other timestamp in this file.
        # The base class makes it overridable so tests can freeze the clock, and a
        # backoff measured against a different clock than the one stamping the data is
        # a difference that only shows up under load.
        remaining = epoch_to_seconds(expiry) - self.now().timestamp()
        return remaining if remaining > 0 else None

    # ----------------------------------------------------------------------------
    # The interface
    # ----------------------------------------------------------------------------

    def get_quote(self, symbol: str) -> Quote:
        symbol = symbol.strip().upper()
        payload = self._get(QUOTES_PATH, {"symbols": symbol, "greeks": "false"})
        quotes = payload.get("quotes") or {}

        # Not documented. Their published schema shows no field for symbols the vendor
        # does not recognize, and does not say what a lookup of one returns either, so
        # both possibilities are handled: an unmatched_symbols block if it appears, and
        # an absent quote below if it does not. Checked rather than assumed because a
        # typo'd ticker silently returning someone else's quote is unrecoverable.
        unmatched = _as_list((quotes.get("unmatched_symbols") or {}).get("symbol"))
        if symbol in {str(item).upper() for item in unmatched}:
            raise SymbolNotFound(f"Tradier does not recognize {symbol}")

        rows = _as_list(quotes.get("quote"))
        if not rows:
            raise SymbolNotFound(f"Tradier returned no quote for {symbol}")

        row = rows[0]
        fetched_at = self.now()
        last = _clean_float(row.get("last"))
        close = _clean_float(row.get("close"))
        if last is None and close is None:
            raise SymbolNotFound(f"Tradier returned no price for {symbol}")

        return Quote(
            symbol=symbol,
            last=last if last is not None else close,
            bid=_clean_float(row.get("bid")),
            ask=_clean_float(row.get("ask")),
            bid_size=_clean_int(row.get("bidsize")),
            ask_size=_clean_int(row.get("asksize")),
            previous_close=_clean_float(row.get("prevclose")),
            volume=_clean_int(row.get("volume")),
            currency="USD",
            fetched_at=fetched_at,
            source=self.name,
        )

    def get_expirations(self, symbol: str) -> list[date]:
        symbol = symbol.strip().upper()
        payload = self._get(
            EXPIRATIONS_PATH,
            # includeAllRoots keeps adjusted and non standard roots in. Dropping them
            # would quietly hide expiries that exist, and a missing expiry is
            # indistinguishable from a symbol that does not list one.
            {"symbol": symbol, "includeAllRoots": "true"},
        )
        # Tradier answers a symbol with no listed options with a null expirations
        # object. That is a fact about the symbol, and the interface says so: an empty
        # list, not an exception.
        expirations = payload.get("expirations")
        if not expirations:
            return []

        dates: list[date] = []
        for item in _as_list(expirations.get("date")):
            try:
                dates.append(date.fromisoformat(str(item)))
            except ValueError as error:
                raise MalformedResponse(
                    f"Tradier returned an unparseable expiry {item!r} for {symbol}"
                ) from error
        return sorted(dates)

    def get_chain(self, symbol: str, expiry: date) -> OptionChain:
        symbol = symbol.strip().upper()
        payload = self._get(
            CHAINS_PATH,
            # greeks=true also brings the ORATS implied vols, which are kept only as
            # vendor_iv for comparison. Every number this project acts on is solved
            # from the mid with our own rate, exactly as it is for yfinance, so that a
            # provider switch cannot move an IV history.
            {"symbol": symbol, "expiration": expiry.isoformat(), "greeks": "true"},
        )
        options = payload.get("options")
        if not options:
            raise NoDataAvailable(f"Tradier lists no chain for {symbol} on {expiry}")

        rows = _as_list(options.get("option"))
        if not rows:
            raise NoDataAvailable(f"{symbol} chain for {expiry} came back empty")

        fetched_at = self.now()
        contracts: list[OptionContract] = []
        skipped = 0
        for row in rows:
            contract = self._row_to_contract(row, symbol, expiry, fetched_at)
            if contract is None:
                skipped += 1
            else:
                contracts.append(contract)

        if skipped:
            log.warning(
                "skipped unparseable contract rows",
                symbol=symbol,
                expiry=expiry.isoformat(),
                skipped=skipped,
                kept=len(contracts),
            )
        if not contracts:
            raise MalformedResponse(
                f"none of Tradier's {len(rows)} rows for {symbol} {expiry} could be parsed"
            )

        return OptionChain(
            symbol=symbol,
            expiry=expiry,
            # Tradier's chain payload names the underlying but does not price it. None
            # is the honest value; the snapshot job pairs every chain with a real quote.
            underlying_price=None,
            contracts=tuple(contracts),
            fetched_at=fetched_at,
            source=self.name,
        )

    def _row_to_contract(
        self,
        row: dict[str, Any],
        symbol: str,
        expiry: date,
        fetched_at: datetime,
    ) -> OptionContract | None:
        """One option row, or None if it cannot be normalized.

        A single bad row must not lose the other eight hundred, so it is counted and
        logged by the caller rather than raised. A whole chain of bad rows does raise,
        because that is a schema change and not a data quirk.
        """
        strike = _clean_float(row.get("strike"))
        if not strike:
            return None
        try:
            right = Right.parse(row.get("option_type") or "")
        except ValueError:
            return None

        greeks = row.get("greeks") or {}
        try:
            return OptionContract(
                symbol=symbol,
                contract_symbol=str(row.get("symbol") or "") or None,
                expiry=expiry,
                strike=strike,
                right=right,
                bid=_clean_float(row.get("bid")),
                ask=_clean_float(row.get("ask")),
                last=_clean_float(row.get("last")),
                last_trade_at=_clean_epoch(row.get("trade_date")),
                volume=_clean_int(row.get("volume")),
                open_interest=_clean_int(row.get("open_interest")),
                # mid_iv rather than smv_vol: it is the one derived from the quote we
                # are also storing, so a disagreement with our own solve is meaningful.
                vendor_iv=_clean_float(greeks.get("mid_iv")),
                in_the_money=None,
                contract_size=_clean_int(row.get("contract_size")) or 100,
                currency="USD",
                fetched_at=fetched_at,
                source=self.name,
            )
        except ValueError:
            return None

    def get_history(self, symbol: str, days: int) -> list[PriceBar]:
        symbol = symbol.strip().upper()
        if days < 1:
            raise ValueError("days must be at least 1")

        end = self.now().date()
        start = end - timedelta(days=days)
        payload = self._get(
            HISTORY_PATH,
            {
                "symbol": symbol,
                "interval": "daily",
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )
        history = payload.get("history")
        if not history:
            raise NoDataAvailable(f"Tradier returned no history for {symbol}")

        fetched_at = self.now()
        bars: list[PriceBar] = []
        for row in _as_list(history.get("day")):
            bar = self._row_to_bar(row, symbol, fetched_at)
            if bar is not None:
                bars.append(bar)

        if not bars:
            raise NoDataAvailable(f"Tradier returned no usable bars for {symbol}")
        return sorted(bars, key=lambda bar: bar.ts)

    def _row_to_bar(
        self,
        row: dict[str, Any],
        symbol: str,
        fetched_at: datetime,
    ) -> PriceBar | None:
        """One daily bar, or None when it is incomplete or incoherent.

        The model rejects a bar whose open or close sits outside its own high and low.
        That has to be caught rather than allowed to escape, because one bad session in
        a vendor's history would otherwise take out the whole price chart.
        """
        raw_date = row.get("date")
        close = _clean_float(row.get("close"))
        if raw_date is None or close is None:
            return None
        try:
            day = date.fromisoformat(str(raw_date))
        except ValueError:
            return None

        # Daily bars are stamped at the session close in UTC terms only to the day.
        # Midnight UTC is a placeholder for "this session", which is all the chart and
        # the realized vol calculation need, and it is consistent across vendors.
        ts = datetime(day.year, day.month, day.day, tzinfo=UTC)
        try:
            return PriceBar(
                symbol=symbol,
                ts=ts,
                open=_clean_float(row.get("open")) or close,
                high=_clean_float(row.get("high")) or close,
                low=_clean_float(row.get("low")) or close,
                close=close,
                volume=_clean_int(row.get("volume")),
                fetched_at=fetched_at,
                source=self.name,
            )
        except ValueError:
            log.warning("dropped an incoherent daily bar", symbol=symbol, date=str(raw_date))
            return None
