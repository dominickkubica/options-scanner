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

from datetime import UTC, date, datetime
from typing import Any, ClassVar

import pandas as pd
import yfinance as yf

from optscan.logging import get_logger
from optscan.models import OptionChain, OptionContract, PriceBar, Quote, Right, SymbolEvents
from optscan.providers.base import MarketDataProvider
from optscan.providers.errors import (
    MalformedResponse,
    NoDataAvailable,
    ProviderUnavailable,
    RateLimited,
    SymbolNotFound,
)

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


def _clean_float(value: Any) -> float | None:
    """None for missing, NaN, or negative. Zero is preserved: it is a real quote state."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(number) or number < 0:
        return None
    return number


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
