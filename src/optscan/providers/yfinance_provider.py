"""yfinance adapter.

This is a prototype source, not a foundation. It is delayed, undocumented, rate limits
without saying so, and changes shape between releases. It exists so Phases 1 to 3 can
be built and tested today, and so the snapshot history starts accumulating now. Phase 5
replaces it with Schwab or Tradier and nothing outside this file should notice.

Known limits, stated rather than papered over:
- Quotes are delayed by roughly 15 minutes.
- There are no vendor greeks, and the vendor implied vol is unreliable enough that it
  is kept only for comparison.
- There is no documented rate limit, so failures are inferred from the exception text.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, ClassVar

import pandas as pd
import yfinance as yf

from optscan.logging import get_logger
from optscan.models import (
    LiveQuote,
    OptionChain,
    OptionContract,
    PriceBar,
    Quote,
    Right,
    SymbolEvents,
)
from optscan.providers.base import MarketDataProvider
from optscan.providers.errors import (
    MalformedResponse,
    NoDataAvailable,
    ProviderUnavailable,
    RateLimited,
    SymbolNotFound,
)
from optscan.providers.parsing import non_negative, whole

log = get_logger("optscan.providers.yfinance")

_RATE_LIMIT_MARKERS = ("too many requests", "rate limit", "429")
_NOT_FOUND_MARKERS = ("no data found", "symbol may be delisted", "not found", "404")

#: An earnings date further out than this is Yahoo's guess at the next quarter
#: rather than a company confirmed date.
ESTIMATED_EARNINGS_HORIZON_DAYS = 100


def _classify(error: Exception, symbol: str) -> Exception:
    """Map an opaque yfinance failure onto our taxonomy by inspecting its text.

    Fragile by nature. It is fragile in one file instead of everywhere, and a
    misclassification degrades to a retry decision, never to wrong data.
    """
    text = str(error).lower()
    if any(marker in text for marker in _RATE_LIMIT_MARKERS):
        return RateLimited(f"yfinance rate limited on {symbol}: {error}")
    if any(marker in text for marker in _NOT_FOUND_MARKERS):
        return SymbolNotFound(f"yfinance has no data for {symbol}: {error}")
    return ProviderUnavailable(f"yfinance call failed for {symbol}: {error}")


#: Local alias for the shared coercer. yfinance used `pd.isna` here rather than
#: `math.isnan`; after `float()` the value is a plain float and the two agree, so
#: the pandas dependency was doing nothing at this boundary.
_clean_float = non_negative


def _clean_int(value: Any) -> int | None:
    number = _clean_float(value)
    return None if number is None else int(number)


def _first_date(value: Any) -> date | None:
    """Yahoo returns either a date or a list of them. Take the soonest."""
    if value is None:
        return None
    if isinstance(value, list):
        dates = [item for item in value if isinstance(item, date)]
        return min(dates) if dates else None
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else None


def _clean_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(stamp):
        return None
    stamp = stamp.tz_localize(UTC) if stamp.tzinfo is None else stamp.tz_convert(UTC)
    return stamp.to_pydatetime()


class YFinanceProvider(MarketDataProvider):
    """Delayed chains and quotes from Yahoo Finance."""

    name: ClassVar[str] = "yfinance"
    realtime: ClassVar[bool] = False

    def _ticker(self, symbol: str) -> yf.Ticker:
        return yf.Ticker(symbol.strip().upper())

    #: Yahoo's batch quote endpoint. One request for many symbols, which is the only
    #: shape of yfinance access safe to poll: the per-Ticker path costs one request per
    #: symbol, and yfinance publishes no rate limit and throttles silently. The job that
    #: breaks when it does is the 15:45 capture, whose IV history cannot be backfilled.
    QUOTE_URL: ClassVar[str] = "https://query2.finance.yahoo.com/v7/finance/quote"

    #: Alpaca's interval names to Yahoo's. Kept as an explicit table rather than
    #: lowercasing and stripping, because a silently mistranslated interval draws a
    #: chart at the wrong resolution and nothing about it looks wrong.
    INTRADAY_INTERVALS: ClassVar[dict[str, str]] = {
        "1Min": "1m",
        "2Min": "2m",
        "5Min": "5m",
        "15Min": "15m",
        "30Min": "30m",
        "1Hour": "60m",
    }

    def get_intraday_bars(
        self,
        symbol: str,
        interval: str,
        days: int = 1,
        *,
        session: date | None = None,
    ) -> list[PriceBar]:
        """Intraday candles, as the fallback when the primary vendor is down.

        Worth having for more than redundancy. Alpaca's free plan serves the
        consolidated tape on a fifteen minute delay, and on 2026-09-11 it was serving
        nothing at all -- 504 on every endpoint for hours, which left the intraday chart
        blank while the market traded. Yahoo's intraday bars carried the current minute
        through the same window.

        Yahoo publishes no delay guarantee either way, so nothing here claims to be real
        time; it claims to be a second source, which is what an outage needs.
        """
        mapped = self.INTRADAY_INTERVALS.get(interval)
        if mapped is None:
            raise NoDataAvailable(f"yahoo has no {interval} interval")

        try:
            ticker = self._ticker(symbol)
            if session is not None:
                frame = ticker.history(
                    start=session, end=session + timedelta(days=1), interval=mapped
                )
            else:
                frame = ticker.history(period=f"{max(days, 1)}d", interval=mapped)
        except Exception as error:  # yfinance raises bare exceptions
            raise _classify(error, symbol) from error

        if frame is None or frame.empty:
            raise NoDataAvailable(f"yahoo returned no {interval} bars for {symbol}")

        fetched = datetime.now(UTC)
        bars: list[PriceBar] = []
        for stamp, row in frame.iterrows():
            ts = _clean_timestamp(stamp)
            close = non_negative(row.get("Close"))
            if ts is None or close is None:
                continue
            bars.append(
                PriceBar(
                    symbol=symbol.upper(),
                    ts=ts,
                    open=non_negative(row.get("Open")) or close,
                    high=max(non_negative(row.get("High")) or close, close),
                    low=min(non_negative(row.get("Low")) or close, close),
                    close=close,
                    volume=_clean_int(row.get("Volume")) or 0,
                    fetched_at=fetched,
                    source=self.name,
                )
            )
        return bars

    def get_live_quotes(self, symbols: Sequence[str]) -> dict[str, LiveQuote]:
        """Headline prices for many symbols in one request.

        Exists as a second source, not a preference. Alpaca is the primary because it
        publishes a rate limit; this is what keeps the screen alive when that vendor is
        down, which it was on 2026-09-11 -- every data endpoint returning 504 for hours
        while the market traded, and the whole app silently showing yesterday's closes
        because nothing else could quote.

        Yahoo publishes no delay and no guarantee, so `delay_minutes` stays None: unknown
        is not zero, and a vendor that says nothing about its latency has not thereby
        promised real time.
        """
        wanted = [s.strip().upper() for s in symbols if s and s.strip()]
        if not wanted:
            return {}

        from yfinance.data import YfData  # noqa: PLC0415

        try:
            body = YfData().get_raw_json(self.QUOTE_URL, params={"symbols": ",".join(wanted)})
        except Exception as error:
            raise ProviderUnavailable(f"yahoo quote request failed: {error}") from error

        fetched = datetime.now(UTC)
        out: dict[str, LiveQuote] = {}
        for row in (body.get("quoteResponse") or {}).get("result") or []:
            ticker = (row.get("symbol") or "").upper()
            last = non_negative(row.get("regularMarketPrice"))
            if not ticker or last is None:
                continue
            # Yahoo's marketState tells us whether the post/pre price is a separate fact
            # or the same trade as the session close. Merging them would invent an
            # after-hours move of zero all day.
            state = (row.get("marketState") or "").upper()
            outside = state.startswith(("PRE", "POST", "CLOSED"))
            extended = non_negative(
                row.get("postMarketPrice")
                if state.startswith(("POST", "CLOSED"))
                else row.get("preMarketPrice")
            )
            stamp = row.get("regularMarketTime")
            out[ticker] = LiveQuote(
                symbol=ticker,
                last=last,
                previous_close=non_negative(row.get("regularMarketPreviousClose")),
                extended=extended if outside else None,
                volume=whole(row.get("regularMarketVolume")),
                day_open=non_negative(row.get("regularMarketOpen")),
                day_high=non_negative(row.get("regularMarketDayHigh")),
                day_low=non_negative(row.get("regularMarketDayLow")),
                session_date=(datetime.fromtimestamp(stamp, tz=UTC).date() if stamp else None),
                as_of=datetime.fromtimestamp(stamp, tz=UTC) if stamp else fetched,
                realtime=False,
                feed="yahoo",
                delay_minutes=None,
                fetched_at=fetched,
                source=self.name,
            )
        return out

    def get_quote(self, symbol: str) -> Quote:
        symbol = symbol.strip().upper()
        ticker = self._ticker(symbol)
        try:
            info: dict[str, Any] = dict(ticker.fast_info)
        except Exception as error:  # yfinance raises bare Exception, so this is the net
            raise _classify(error, symbol) from error

        fetched_at = self.now()
        last = _clean_float(info.get("last_price") or info.get("lastPrice"))
        if last is None:
            raise SymbolNotFound(f"yfinance returned no price for {symbol}")

        return Quote(
            symbol=symbol,
            last=last,
            bid=_clean_float(info.get("bid")),
            ask=_clean_float(info.get("ask")),
            previous_close=_clean_float(info.get("previous_close") or info.get("previousClose")),
            volume=_clean_int(info.get("last_volume") or info.get("lastVolume")),
            currency=str(info.get("currency") or "USD"),
            fetched_at=fetched_at,
            source=self.name,
        )

    def get_expirations(self, symbol: str) -> list[date]:
        symbol = symbol.strip().upper()
        try:
            raw = self._ticker(symbol).options
        except Exception as error:  # yfinance raises bare Exception, so this is the net
            raise _classify(error, symbol) from error

        expirations: list[date] = []
        for item in raw or ():
            try:
                expirations.append(date.fromisoformat(str(item)))
            except ValueError as error:
                raise MalformedResponse(
                    f"yfinance returned an unparseable expiry {item!r} for {symbol}"
                ) from error
        return sorted(expirations)

    def get_chain(self, symbol: str, expiry: date) -> OptionChain:
        symbol = symbol.strip().upper()
        try:
            raw = self._ticker(symbol).option_chain(expiry.isoformat())
        except Exception as error:  # yfinance raises bare Exception, so this is the net
            text = str(error).lower()
            if "expiration" in text or "no option chain" in text:
                raise NoDataAvailable(f"{symbol} has no chain for {expiry}") from error
            raise _classify(error, symbol) from error

        fetched_at = self.now()
        underlying = getattr(raw, "underlying", None)
        underlying_price = None
        if isinstance(underlying, dict):
            underlying_price = _clean_float(
                underlying.get("regularMarketPrice") or underlying.get("bid")
            )
            if underlying_price == 0:
                underlying_price = None

        contracts: list[OptionContract] = []
        for frame, right in ((raw.calls, Right.CALL), (raw.puts, Right.PUT)):
            contracts.extend(self._rows_to_contracts(frame, symbol, expiry, right, fetched_at))

        if not contracts:
            raise NoDataAvailable(f"{symbol} chain for {expiry} came back empty")

        return OptionChain(
            symbol=symbol,
            expiry=expiry,
            underlying_price=underlying_price,
            contracts=tuple(contracts),
            fetched_at=fetched_at,
            source=self.name,
        )

    def _rows_to_contracts(
        self,
        frame: pd.DataFrame,
        symbol: str,
        expiry: date,
        right: Right,
        fetched_at: datetime,
    ) -> list[OptionContract]:
        if frame is None or frame.empty:
            return []

        contracts: list[OptionContract] = []
        skipped = 0
        for row in frame.to_dict("records"):
            strike = _clean_float(row.get("strike"))
            if not strike:
                skipped += 1
                continue
            try:
                contracts.append(
                    OptionContract(
                        symbol=symbol,
                        contract_symbol=str(row.get("contractSymbol") or "") or None,
                        expiry=expiry,
                        strike=strike,
                        right=right,
                        bid=_clean_float(row.get("bid")),
                        ask=_clean_float(row.get("ask")),
                        last=_clean_float(row.get("lastPrice")),
                        last_trade_at=_clean_timestamp(row.get("lastTradeDate")),
                        volume=_clean_int(row.get("volume")),
                        open_interest=_clean_int(row.get("openInterest")),
                        vendor_iv=_clean_float(row.get("impliedVolatility")),
                        in_the_money=bool(row["inTheMoney"])
                        if row.get("inTheMoney") is not None
                        else None,
                        contract_size=100,
                        currency=str(row.get("currency") or "USD"),
                        fetched_at=fetched_at,
                        source=self.name,
                    )
                )
            except ValueError:
                # One malformed row should not lose the other 800. It is counted and
                # logged rather than swallowed, so a systematic problem is visible.
                skipped += 1

        if skipped:
            log.warning(
                "skipped unparseable contract rows",
                symbol=symbol,
                expiry=expiry.isoformat(),
                right=str(right),
                skipped=skipped,
                kept=len(contracts),
            )
        return contracts

    def get_events(self, symbol: str) -> SymbolEvents:
        """Earnings and ex dividend dates from the vendor's calendar.

        Yahoo publishes an estimated earnings date for quarters the company has not
        announced yet and does not label it as an estimate. A date more than a quarter
        out is almost certainly one of those, so it is flagged rather than trusted.
        """
        symbol = symbol.strip().upper()
        ticker = self._ticker(symbol)
        try:
            calendar = ticker.calendar or {}
        except Exception as error:  # yfinance raises bare Exception, so this is the net
            raise _classify(error, symbol) from error

        fetched_at = self.now()
        earnings = _first_date(calendar.get("Earnings Date"))
        ex_dividend = _first_date(calendar.get("Ex-Dividend Date"))

        estimated = False
        if earnings is not None:
            estimated = (earnings - fetched_at.date()).days > ESTIMATED_EARNINGS_HORIZON_DAYS

        return SymbolEvents(
            symbol=symbol,
            earnings_date=earnings,
            earnings_estimated=estimated,
            ex_dividend_date=ex_dividend,
            dividend_amount=self._recent_dividend(ticker),
            fetched_at=fetched_at,
            source=self.name,
        )

    def _recent_dividend(self, ticker: Any) -> float | None:
        """The most recent dividend paid, as a stand in for the next one.

        A stand in and not a forecast. It is right whenever the company holds its
        dividend flat, which is most of the time, and it is the input to an assignment
        risk flag rather than to a valuation, so being one raise behind is tolerable.
        """
        try:
            dividends = ticker.dividends
        except Exception:  # yfinance raises bare Exception, so this is the net
            return None
        if dividends is None or len(dividends) == 0:
            return None
        return _clean_float(dividends.iloc[-1])

    def get_history(self, symbol: str, days: int) -> list[PriceBar]:
        symbol = symbol.strip().upper()
        if days < 1:
            raise ValueError("days must be at least 1")
        try:
            frame = self._ticker(symbol).history(
                period=f"{days}d", interval="1d", auto_adjust=False
            )
        except Exception as error:  # yfinance raises bare Exception, so this is the net
            raise _classify(error, symbol) from error

        if frame is None or frame.empty:
            raise NoDataAvailable(f"yfinance returned no history for {symbol}")

        fetched_at = self.now()
        bars: list[PriceBar] = []
        for index, row in frame.iterrows():
            ts = _clean_timestamp(index)
            close = _clean_float(row.get("Close"))
            if ts is None or close is None:
                continue
            bars.append(
                PriceBar(
                    symbol=symbol,
                    ts=ts,
                    open=_clean_float(row.get("Open")) or close,
                    high=_clean_float(row.get("High")) or close,
                    low=_clean_float(row.get("Low")) or close,
                    close=close,
                    volume=_clean_int(row.get("Volume")),
                    fetched_at=fetched_at,
                    source=self.name,
                )
            )
        return sorted(bars, key=lambda bar: bar.ts)
