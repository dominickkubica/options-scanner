"""Alpaca adapter tests.

Offline, like every other test here: httpx.MockTransport stands in for the network, so
these exercise the real client, the real headers and the real URL building without a
socket.

The response shapes below are **trimmed real captures** from the live API on
2026-09-07, not invented ones. That matters more than usual for this vendor, because
three of the things worth testing are behaviours the documentation gets wrong, and a
fixture written from the docs would agree with the bug rather than catch it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import ClassVar

import httpx
import pytest

from optscan.config import Settings
from optscan.models import OptionChain, Right
from optscan.providers.alpaca import (
    EXPIRY_LOOKAHEAD_DAYS,
    STOCK_BARS_FEED,
    STOCK_SNAPSHOT_FEED,
    AlpacaProvider,
    occ_symbol,
    parse_occ_symbol,
)
from optscan.providers.errors import (
    AuthenticationError,
    MalformedResponse,
    NoDataAvailable,
    RateLimited,
    SymbolNotFound,
)

FROZEN_NOW = datetime(2026, 9, 8, 15, 45, tzinfo=UTC)

SNAPSHOT = {
    "QQQ": {
        "dailyBar": {"c": 718.96, "h": 721.86, "l": 716.56, "o": 719.04, "v": 33232087},
        "prevDailyBar": {"c": 717.67},
        "latestQuote": {"ap": 717.54, "as": 12, "bp": 717.5, "bs": 9},
        "latestTrade": {"p": 717.5, "t": "2026-09-04T23:59:58.209627495Z"},
    }
}

CONTRACTS = {
    "option_contracts": [
        {
            "symbol": "QQQ260918P00700000",
            "expiration_date": "2026-09-18",
            "strike_price": "700",
            "type": "put",
            "open_interest": "497",
            "open_interest_date": "2026-09-03",
            "close_price": "6.40",
            "size": "100",
        },
        {
            "symbol": "QQQ260918C00740000",
            "expiration_date": "2026-09-18",
            "strike_price": "740",
            "type": "call",
            "open_interest": None,
            "close_price": None,
            "size": "100",
        },
    ],
    "next_page_token": None,
}

CHAIN = {
    "snapshots": {
        "QQQ260918P00700000": {
            "latestQuote": {"bp": 6.34, "ap": 6.45},
            "latestTrade": {"p": 6.40, "t": "2026-09-04T19:59:57.920973183Z"},
            "dailyBar": {"v": 359},
            "impliedVolatility": 0.1344,
            "greeks": {"delta": -0.19, "gamma": 0.009},
        },
        # No quote at all, which is what an untraded deep contract really looks like.
        "QQQ260918C00740000": {},
    },
    "next_page_token": None,
}


def settings(**overrides) -> Settings:
    base = {
        "alpaca_key_id": "test-key",
        "alpaca_secret_key": "test-secret",
        "alpaca_requests_per_minute": 10_000,
    }
    return Settings(**{**base, **overrides})


def build(handler, **overrides) -> AlpacaProvider:
    provider = AlpacaProvider(
        settings(**overrides),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    provider.now = lambda: FROZEN_NOW  # type: ignore[method-assign]
    return provider


def router(routes: dict[str, object], record: list | None = None):
    """Dispatch on path suffix, recording every request for assertions."""

    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        for suffix, body in routes.items():
            if request.url.path.endswith(suffix):
                if isinstance(body, httpx.Response):
                    return body
                return httpx.Response(200, json=body)
        return httpx.Response(404, json={"message": "Not Found"})

    return handler


class TestOccSymbols:
    """The identifier every option endpoint keys on."""

    def test_round_trip(self) -> None:
        built = occ_symbol("QQQ", date(2026, 9, 18), Right.PUT, 700.0)
        assert built == "QQQ260918P00700000"
        assert parse_occ_symbol(built) == ("QQQ", date(2026, 9, 18), Right.PUT, 700.0)

    def test_a_half_dollar_strike_is_thousandths_not_hundredths(self) -> None:
        """400.5 is 00400500. Getting this wrong puts the strike at 400.05."""
        assert occ_symbol("SPY", date(2026, 1, 16), Right.CALL, 400.5).endswith("C00400500")
        assert parse_occ_symbol("SPY260116C00400500")[3] == pytest.approx(400.5)

    def test_the_root_is_found_from_the_right(self) -> None:
        """Roots run one to six characters, so a fixed offset mangles the short ones."""
        assert parse_occ_symbol("F260116C00012000")[0] == "F"
        assert parse_occ_symbol("GOOGL260116C00200000")[0] == "GOOGL"

    @pytest.mark.parametrize("bad", ["", "QQQ", "QQQ260918X00700000", "NOTASYMBOL12345"])
    def test_junk_is_refused(self, bad: str) -> None:
        with pytest.raises(MalformedResponse):
            parse_occ_symbol(bad)


class TestFeeds:
    """The feed name differs per endpoint and each is rejected where the other works.

    This is the measured trap, not a preference: `sip` on a snapshot is a 403, and
    `delayed_sip` on bars is a 400. Pinning the pair stops a later tidy-up from
    unifying them into one constant that breaks half the adapter.
    """

    def test_a_quote_asks_for_the_delayed_consolidated_feed(self) -> None:
        seen: list[httpx.Request] = []
        provider = build(router({"/v2/stocks/snapshots": SNAPSHOT}, seen))
        provider.get_quote("QQQ")
        assert seen[0].url.params["feed"] == STOCK_SNAPSHOT_FEED == "delayed_sip"

    def test_history_asks_for_the_consolidated_tape(self) -> None:
        seen: list[httpx.Request] = []
        bars = {
            "bars": {
                "QQQ": [
                    {
                        "t": "2026-09-04T04:00:00Z",
                        "o": 719.04,
                        "h": 721.86,
                        "l": 716.56,
                        "c": 718.96,
                        "v": 33032085,
                    }
                ]
            }
        }
        provider = build(router({"/v2/stocks/bars": bars}, seen))
        provider.get_history("QQQ", 30)
        assert seen[0].url.params["feed"] == STOCK_BARS_FEED == "sip"

    def test_history_stops_short_of_now(self) -> None:
        """SIP inside the last quarter hour is a 403 on the free plan, so the window
        has to end before now rather than at it."""
        seen: list[httpx.Request] = []
        bars = {
            "bars": {
                "QQQ": [
                    {"t": "2026-09-04T04:00:00Z", "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 10}
                ]
            }
        }
        provider = build(router({"/v2/stocks/bars": bars}, seen))
        provider.get_history("QQQ", 30)
        end = datetime.fromisoformat(seen[0].url.params["end"].replace("Z", "+00:00"))
        assert end < FROZEN_NOW, "requesting up to now is refused by the plan"


class TestQuote:
    def test_it_reads_the_consolidated_numbers(self) -> None:
        provider = build(router({"/v2/stocks/snapshots": SNAPSHOT}))
        quote = provider.get_quote("qqq")
        assert quote.symbol == "QQQ"
        assert quote.bid == pytest.approx(717.5)
        assert quote.ask == pytest.approx(717.54)
        assert quote.previous_close == pytest.approx(717.67)
        assert quote.volume == 33_232_087
        assert quote.mid == pytest.approx(717.52)

    def test_an_unknown_symbol_is_not_found(self) -> None:
        provider = build(router({"/v2/stocks/snapshots": {}}))
        with pytest.raises(SymbolNotFound):
            provider.get_quote("NOPE")


class TestExpirations:
    def test_the_expiry_window_is_always_sent(self) -> None:
        """The measured trap. With no bounds this endpoint answers for four expiries,
        200 OK and no page token, and a screener would believe that was the board."""
        seen: list[httpx.Request] = []
        provider = build(router({"/v2/options/contracts": CONTRACTS}, seen))
        provider.get_expirations("QQQ")

        params = seen[0].url.params
        assert "expiration_date_gte" in params, "omitting bounds silently truncates"
        assert params["expiration_date_gte"] == "2026-09-08"
        assert params["expiration_date_lte"] == str(
            date(2026, 9, 8) + timedelta(days=EXPIRY_LOOKAHEAD_DAYS)
        )

    def test_expiries_are_deduplicated_and_sorted(self) -> None:
        provider = build(router({"/v2/options/contracts": CONTRACTS}))
        assert provider.get_expirations("QQQ") == [date(2026, 9, 18)]


class TestChain:
    def test_open_interest_is_joined_from_the_trading_host(self) -> None:
        """The screen filters on open interest and the data host does not carry it."""
        provider = build(
            router(
                {
                    "/v2/options/contracts": CONTRACTS,
                    "/v1beta1/options/snapshots/QQQ": CHAIN,
                    "/v2/stocks/snapshots": SNAPSHOT,
                }
            )
        )
        chain = provider.get_chain("QQQ", date(2026, 9, 18))
        by_strike = {c.strike: c for c in chain.contracts}

        assert by_strike[700.0].open_interest == 497
        assert by_strike[700.0].bid == pytest.approx(6.34)
        assert by_strike[700.0].ask == pytest.approx(6.45)
        assert chain.underlying_price == pytest.approx(717.52)

    def test_a_missing_open_interest_is_none_and_never_zero(self) -> None:
        """Zero open interest is a real and different fact from an unknown one, and
        the liquidity filter treats them very differently."""
        provider = build(
            router(
                {
                    "/v2/options/contracts": CONTRACTS,
                    "/v1beta1/options/snapshots/QQQ": CHAIN,
                    "/v2/stocks/snapshots": SNAPSHOT,
                }
            )
        )
        chain = provider.get_chain("QQQ", date(2026, 9, 18))
        untraded = next(c for c in chain.contracts if c.strike == 740.0)
        assert untraded.open_interest is None
        assert untraded.bid is None and untraded.ask is None

    def test_the_vendor_iv_is_recorded_but_kept_separate(self) -> None:
        """Alpaca does publish an implied vol on the free feed. It is stored for
        comparison only; this project solves its own from the mid."""
        provider = build(
            router(
                {
                    "/v2/options/contracts": CONTRACTS,
                    "/v1beta1/options/snapshots/QQQ": CHAIN,
                    "/v2/stocks/snapshots": SNAPSHOT,
                }
            )
        )
        chain = provider.get_chain("QQQ", date(2026, 9, 18))
        put = next(c for c in chain.contracts if c.strike == 700.0)
        assert put.vendor_iv == pytest.approx(0.1344)

    def test_a_contract_with_no_trade_falls_back_to_the_settle(self) -> None:
        provider = build(
            router(
                {
                    "/v2/options/contracts": CONTRACTS,
                    "/v1beta1/options/snapshots/QQQ": CHAIN,
                    "/v2/stocks/snapshots": SNAPSHOT,
                }
            )
        )
        chain = provider.get_chain("QQQ", date(2026, 9, 18))
        put = next(c for c in chain.contracts if c.strike == 700.0)
        assert put.last == pytest.approx(6.40)

    def test_an_unlisted_expiry_says_so(self) -> None:
        provider = build(router({"/v2/options/contracts": {"option_contracts": []}}))
        with pytest.raises(NoDataAvailable):
            provider.get_chain("QQQ", date(2030, 1, 18))


class TestEvents:
    ACTIONS: ClassVar[dict] = {
        "corporate_actions": {
            "cash_dividends": [
                {"symbol": "NVDA", "ex_date": "2026-09-10", "rate": 0.25},
                {"symbol": "NVDA", "ex_date": "2026-12-10", "rate": 0.25},
            ]
        }
    }

    def test_the_nearest_forthcoming_ex_date_is_returned(self) -> None:
        provider = build(router({"/v1/corporate-actions": self.ACTIONS}))
        events = provider.get_events("NVDA")
        assert events.ex_dividend_date == date(2026, 9, 10)
        assert events.dividend_amount == pytest.approx(0.25)

    def test_earnings_is_never_guessed(self) -> None:
        """Alpaca has no earnings calendar, and `exclude_earnings` is the heavier of
        the two event filters. None makes the screen widen and say it could not
        check, which is the honest outcome; a guess would make it believe it did."""
        provider = build(router({"/v1/corporate-actions": self.ACTIONS}))
        events = provider.get_events("NVDA")
        assert events.earnings_date is None
        assert events.earnings_estimated is False

    def test_no_dividends_is_not_an_error(self) -> None:
        provider = build(router({"/v1/corporate-actions": {"corporate_actions": {}}}))
        assert provider.get_events("QQQ").ex_dividend_date is None


class TestFailures:
    """Each status code has a different remedy, so each gets a different exception."""

    def test_a_bad_key_pair_is_an_auth_error(self) -> None:
        provider = build(router({"/v2/stocks/snapshots": httpx.Response(401, json={})}))
        with pytest.raises(AuthenticationError, match="key pair"):
            provider.get_quote("QQQ")

    def test_the_unsigned_opra_agreement_says_what_to_actually_do(self) -> None:
        """A 403 here is not a credential problem and must not send someone to
        regenerate a key that is working perfectly."""
        response = httpx.Response(403, json={"message": "OPRA agreement is not signed"})
        provider = build(router({"/v2/stocks/snapshots": response}))
        with pytest.raises(AuthenticationError, match="agreement"):
            provider.get_quote("QQQ")

    def test_rate_limiting_is_its_own_error(self) -> None:
        provider = build(router({"/v2/stocks/snapshots": httpx.Response(429, json={})}))
        with pytest.raises(RateLimited):
            provider.get_quote("QQQ")

    def test_missing_credentials_refuse_at_construction(self) -> None:
        """Half a key pair cannot authenticate, and saying so at construction beats
        a 401 on the first request that happens to run.

        Both halves are named explicitly because Settings reads the real .env, and
        on a machine that has the keys configured an omitted field is filled in
        from the environment rather than left empty."""
        half = Settings(alpaca_key_id="only-one-half", alpaca_secret_key=None)
        with pytest.raises(AuthenticationError, match="both"):
            AlpacaProvider(half)

    def test_no_bars_is_a_stated_absence(self) -> None:
        provider = build(router({"/v2/stocks/bars": {"bars": {}}}))
        with pytest.raises(NoDataAvailable):
            provider.get_history("QQQ", 30)


class TestOptionBars:
    """The only historical option data this vendor has, and it is trades not quotes."""

    BARS: ClassVar[dict] = {
        "bars": {
            "QQQ260918P00700000": [
                {"t": "2026-09-04T04:00:00Z", "o": 5.93, "h": 6.69, "l": 5.19, "c": 6.05, "v": 359}
            ]
        }
    }

    def test_bars_come_back_as_price_bars_on_the_underlying(self) -> None:
        provider = build(router({"/v1beta1/options/bars": self.BARS}))
        bars = provider.get_option_bars("QQQ260918P00700000", date(2026, 9, 1), date(2026, 9, 5))
        assert len(bars) == 1
        assert bars[0].symbol == "QQQ"
        assert bars[0].close == pytest.approx(6.05)

    def test_asking_before_the_vendor_has_any_warns_rather_than_returning_silence(
        self, caplog
    ) -> None:
        """An out of range start returns an empty series, which is indistinguishable
        from a contract that simply did not trade."""
        provider = build(router({"/v1beta1/options/bars": {"bars": {}}}))
        provider.get_option_bars("QQQ260918P00700000", date(2020, 1, 1), date(2020, 2, 1))
        assert any("before alpaca has any" in r.message for r in caplog.records)


class TestBatchedChains:
    """One request set for every expiry, instead of three per expiry.

    The naive shape costs contract metadata, snapshots and an underlying quote per
    expiry, because `get_chain` fetches the quote every time it is called. At sixteen
    expiries that is fifty requests for one symbol, and capturing a few hundred symbols
    that way is fourteen thousand: over an hour against a 200 per minute limit.
    """

    def test_a_range_fetch_costs_a_handful_not_three_per_expiry(self) -> None:
        seen: list[httpx.Request] = []
        provider = build(
            router(
                {
                    "/v2/options/contracts": CONTRACTS,
                    "/v1beta1/options/snapshots/QQQ": CHAIN,
                    "/v2/stocks/snapshots": SNAPSHOT,
                },
                seen,
            )
        )
        expiries = [date(2026, 9, 18), date(2026, 9, 25), date(2026, 10, 2)]
        provider.get_chains("QQQ", expiries)

        # One contracts call, one snapshots call, one quote. Not three per expiry.
        assert len(seen) <= 4, [str(r.url) for r in seen]
        contracts = [r for r in seen if "options/contracts" in r.url.path]
        assert len(contracts) == 1
        assert contracts[0].url.params["expiration_date_gte"] == "2026-09-18"
        assert contracts[0].url.params["expiration_date_lte"] == "2026-10-02"

    def test_only_requested_expiries_come_back(self) -> None:
        """The range spans the ends, which can include expiries in between that the
        caller did not ask for. Returning those would silently widen the capture."""
        provider = build(
            router(
                {
                    "/v2/options/contracts": CONTRACTS,
                    "/v1beta1/options/snapshots/QQQ": CHAIN,
                    "/v2/stocks/snapshots": SNAPSHOT,
                }
            )
        )
        chains = provider.get_chains("QQQ", [date(2026, 9, 18)])
        assert set(chains) == {date(2026, 9, 18)}

    def test_open_interest_survives_the_batch_path(self) -> None:
        """Both paths share `_parse_contract_rows` and `_contract` precisely so this
        cannot drift: open interest is the whole reason the two hosts are joined."""
        provider = build(
            router(
                {
                    "/v2/options/contracts": CONTRACTS,
                    "/v1beta1/options/snapshots/QQQ": CHAIN,
                    "/v2/stocks/snapshots": SNAPSHOT,
                }
            )
        )
        chain = provider.get_chains("QQQ", [date(2026, 9, 18)])[date(2026, 9, 18)]
        put = next(c for c in chain.contracts if c.strike == 700.0)
        assert put.open_interest == 497
        assert put.bid == pytest.approx(6.34)
        assert put.vendor_iv == pytest.approx(0.1344)

    def test_no_expiries_is_not_a_request(self) -> None:
        seen: list[httpx.Request] = []
        provider = build(router({}, seen))
        assert provider.get_chains("QQQ", []) == {}
        assert seen == []

    def test_the_base_class_default_loops_and_tolerates_a_bad_expiry(self) -> None:
        """Every adapter has a working implementation the day it is written; only one
        that cares about request count needs to override it. A failing expiry is
        omitted rather than raising, because fifteen good ones are worth storing."""
        from optscan.providers.base import MarketDataProvider

        class Looping(MarketDataProvider):
            name = "looping"

            def get_quote(self, symbol): ...
            def get_expirations(self, symbol): ...
            def get_history(self, symbol, days): ...

            def get_chain(self, symbol, expiry):
                if expiry.day == 25:
                    raise NoDataAvailable("nothing listed")
                return OptionChain(
                    symbol=symbol,
                    expiry=expiry,
                    fetched_at=FROZEN_NOW,
                    source=self.name,
                )

        got = Looping().get_chains("QQQ", [date(2026, 9, 18), date(2026, 9, 25)])
        assert set(got) == {date(2026, 9, 18)}
