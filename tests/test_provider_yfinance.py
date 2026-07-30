"""yfinance adapter, tested offline.

The vendor call itself is stubbed. What is under test is the normalization and the
error classification, which is where the adapter can silently produce wrong numbers.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from optscan.models import Right
from optscan.providers.errors import (
    NoDataAvailable,
    ProviderUnavailable,
    RateLimited,
    SymbolNotFound,
)
from optscan.providers.yfinance_provider import (
    YFinanceProvider,
    _classify,
    _clean_float,
    _clean_int,
    _clean_timestamp,
)

EXPIRY = date(2026, 8, 21)


def chain_frame(**overrides) -> pd.DataFrame:
    """One row shaped exactly like a yfinance option_chain frame."""
    row = {
        "contractSymbol": "SPY260821P00740000",
        "lastTradeDate": pd.Timestamp("2026-07-30 16:30:36+00:00"),
        "strike": 740.0,
        "lastPrice": 4.10,
        "bid": 4.00,
        "ask": 4.20,
        "change": -0.15,
        "percentChange": -3.5,
        "volume": 1234.0,
        "openInterest": 5678,
        "impliedVolatility": 0.1855,
        "inTheMoney": False,
        "contractSize": "REGULAR",
        "currency": "USD",
    }
    row.update(overrides)
    return pd.DataFrame([row])


class TestCleaners:
    def test_clean_float_keeps_zero_and_drops_junk(self) -> None:
        assert _clean_float(0.0) == 0.0  # a real no bid, not a missing quote
        assert _clean_float(4.2) == 4.2
        assert _clean_float(None) is None
        assert _clean_float(float("nan")) is None
        assert _clean_float(-1.0) is None
        assert _clean_float("not a number") is None

    def test_clean_int(self) -> None:
        assert _clean_int(1234.0) == 1234
        assert _clean_int(None) is None

    def test_clean_timestamp_is_utc_aware(self) -> None:
        stamp = _clean_timestamp(pd.Timestamp("2026-07-30 16:30:36+00:00"))
        assert stamp == datetime(2026, 7, 30, 16, 30, 36, tzinfo=UTC)
        assert _clean_timestamp(None) is None
        assert _clean_timestamp(pd.NaT) is None

    def test_naive_timestamps_are_assumed_utc(self) -> None:
        assert _clean_timestamp(pd.Timestamp("2026-07-30 16:30:36")).tzinfo is not None


class TestClassify:
    @pytest.mark.parametrize(
        "message", ["Too Many Requests", "you hit the rate limit", "429 Client Error"]
    )
    def test_rate_limits(self, message: str) -> None:
        assert isinstance(_classify(Exception(message), "SPY"), RateLimited)

    @pytest.mark.parametrize("message", ["No data found, symbol may be delisted", "404 Not Found"])
    def test_missing_symbols(self, message: str) -> None:
        assert isinstance(_classify(Exception(message), "SPY"), SymbolNotFound)

    def test_anything_else_is_treated_as_retryable(self) -> None:
        """An unknown failure is more often a blip than a permanent condition."""
        error = _classify(Exception("connection reset"), "SPY")
        assert isinstance(error, ProviderUnavailable)
        assert error.retryable is True


class TestRowNormalization:
    def test_maps_a_real_row(self) -> None:
        provider = YFinanceProvider()
        contracts = provider._rows_to_contracts(
            chain_frame(), "SPY", EXPIRY, Right.PUT, datetime(2026, 7, 30, 19, 45, tzinfo=UTC)
        )
        assert len(contracts) == 1
        c = contracts[0]
        assert c.symbol == "SPY"
        assert c.right is Right.PUT
        assert c.strike == 740.0
        assert c.bid == 4.00
        assert c.ask == 4.20
        assert c.mid == pytest.approx(4.10)
        assert c.open_interest == 5678
        assert c.volume == 1234
        assert c.vendor_iv == pytest.approx(0.1855)
        assert c.in_the_money is False
        assert c.source == "yfinance"
        assert c.last_trade_at == datetime(2026, 7, 30, 16, 30, 36, tzinfo=UTC)

    def test_placeholder_iv_becomes_none(self) -> None:
        provider = YFinanceProvider()
        contracts = provider._rows_to_contracts(
            chain_frame(impliedVolatility=0.00001),
            "SPY",
            EXPIRY,
            Right.PUT,
            datetime(2026, 7, 30, 19, 45, tzinfo=UTC),
        )
        assert contracts[0].vendor_iv is None

    def test_a_bad_row_does_not_lose_the_good_ones(self) -> None:
        provider = YFinanceProvider()
        frame = pd.concat([chain_frame(), chain_frame(strike=float("nan"))], ignore_index=True)
        contracts = provider._rows_to_contracts(
            frame, "SPY", EXPIRY, Right.PUT, datetime(2026, 7, 30, 19, 45, tzinfo=UTC)
        )
        assert len(contracts) == 1

    def test_empty_frame(self) -> None:
        provider = YFinanceProvider()
        assert (
            provider._rows_to_contracts(
                pd.DataFrame(), "SPY", EXPIRY, Right.PUT, datetime(2026, 7, 30, 19, 45, tzinfo=UTC)
            )
            == []
        )


class TestChainAssembly:
    def _provider(self, monkeypatch: pytest.MonkeyPatch, calls, puts, underlying=None):
        provider = YFinanceProvider()
        options = SimpleNamespace(calls=calls, puts=puts, underlying=underlying or {})
        monkeypatch.setattr(
            provider,
            "_ticker",
            lambda symbol: SimpleNamespace(option_chain=lambda expiry: options),
        )
        return provider

    def test_builds_a_chain_with_both_rights(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = self._provider(
            monkeypatch,
            calls=chain_frame(contractSymbol="SPY260821C00740000"),
            puts=chain_frame(),
            underlying={"regularMarketPrice": 740.5},
        )
        chain = provider.get_chain("spy", EXPIRY)
        assert chain.symbol == "SPY"
        assert chain.expiry == EXPIRY
        assert chain.underlying_price == 740.5
        assert len(chain.calls) == 1
        assert len(chain.puts) == 1

    def test_zero_underlying_price_is_treated_as_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = self._provider(
            monkeypatch,
            calls=chain_frame(),
            puts=chain_frame(),
            underlying={"regularMarketPrice": 0},
        )
        assert provider.get_chain("SPY", EXPIRY).underlying_price is None

    def test_empty_chain_raises_rather_than_returning_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = self._provider(monkeypatch, calls=pd.DataFrame(), puts=pd.DataFrame())
        with pytest.raises(NoDataAvailable):
            provider.get_chain("SPY", EXPIRY)


class TestQuote:
    def test_normalizes_fast_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = YFinanceProvider()
        monkeypatch.setattr(
            provider,
            "_ticker",
            lambda symbol: SimpleNamespace(
                fast_info={
                    "last_price": 740.5,
                    "bid": 740.4,
                    "ask": 740.6,
                    "previous_close": 738.2,
                    "last_volume": 55_000_000,
                    "currency": "USD",
                }
            ),
        )
        quote = provider.get_quote(" spy ")
        assert quote.symbol == "SPY"
        assert quote.last == 740.5
        assert quote.mid == pytest.approx(740.5)
        assert quote.previous_close == 738.2
        assert quote.source == "yfinance"
        assert quote.fetched_at.tzinfo is not None

    def test_a_symbol_with_no_price_is_not_a_symbol(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returning a Quote with everything None would be worse than failing."""
        provider = YFinanceProvider()
        monkeypatch.setattr(provider, "_ticker", lambda symbol: SimpleNamespace(fast_info={}))
        with pytest.raises(SymbolNotFound):
            provider.get_quote("NOPE")


class TestHistory:
    def _frame(self) -> pd.DataFrame:
        index = pd.DatetimeIndex(
            [pd.Timestamp("2026-07-29 00:00:00"), pd.Timestamp("2026-07-30 00:00:00")]
        )
        return pd.DataFrame(
            {
                "Open": [738.0, 739.0],
                "High": [741.0, 742.0],
                "Low": [737.0, 738.5],
                "Close": [740.0, 741.5],
                "Volume": [50_000_000, 55_000_000],
            },
            index=index,
        )

    def test_bars_are_normalized_and_sorted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = YFinanceProvider()
        monkeypatch.setattr(
            provider, "_ticker", lambda symbol: SimpleNamespace(history=lambda **kw: self._frame())
        )
        bars = provider.get_history("SPY", 5)
        assert len(bars) == 2
        assert bars[0].ts < bars[1].ts
        assert bars[1].close == 741.5
        assert bars[1].volume == 55_000_000
        assert bars[0].source == "yfinance"

    def test_empty_history_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = YFinanceProvider()
        monkeypatch.setattr(
            provider, "_ticker", lambda symbol: SimpleNamespace(history=lambda **kw: pd.DataFrame())
        )
        with pytest.raises(NoDataAvailable):
            provider.get_history("SPY", 5)

    def test_days_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            YFinanceProvider().get_history("SPY", 0)


class TestProviderRegistry:
    def test_yfinance_is_the_configured_default(self, clean_env: None) -> None:
        from optscan.config import Settings
        from optscan.providers import get_provider

        provider = get_provider(Settings(_env_file=None))
        assert provider.name == "yfinance"
        assert provider.realtime is False

    def test_unimplemented_provider_says_which_phase(self, clean_env: None) -> None:
        from optscan.config import Settings
        from optscan.providers import get_provider

        with pytest.raises(NotImplementedError, match="Phase 5"):
            get_provider(Settings(_env_file=None, provider="tradier"))


class TestExpirations:
    def test_parses_and_sorts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = YFinanceProvider()
        monkeypatch.setattr(
            provider,
            "_ticker",
            lambda symbol: SimpleNamespace(options=("2026-09-18", "2026-08-21")),
        )
        assert provider.get_expirations("SPY") == [date(2026, 8, 21), date(2026, 9, 18)]

    def test_no_listed_options_is_an_empty_list_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = YFinanceProvider()
        monkeypatch.setattr(provider, "_ticker", lambda symbol: SimpleNamespace(options=()))
        assert provider.get_expirations("BRK-A") == []
