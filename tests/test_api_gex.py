"""The open interest and gamma endpoint.

Offline like the rest of the API suite: the fake provider serves the real captured SPY
chain, shifted forward so its expiries are still ahead of today. The shift matters here
more than anywhere else in the suite -- every gamma in this endpoint is a function of
time to expiry, so against the unshifted fixture the whole panel would correctly report
that it cannot price a single strike, and every assertion about a level would pass or
fail on the calendar rather than on the code.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from optscan.api.app import create_app
from optscan.api.deps import clear_caches, provider_factory_dep
from optscan.config import Settings
from optscan.models import ChainSnapshot, LiveQuote
from optscan.providers.errors import ProviderUnavailable
from tests.conftest import FakeProvider


@pytest.fixture(autouse=True)
def _clear() -> Iterator[None]:
    clear_caches()
    yield
    clear_caches()


@pytest.fixture
def settings(tmp_settings: Settings) -> Settings:
    tmp_settings.ensure_dirs()
    return tmp_settings


@pytest.fixture
def provider(future_snapshot: ChainSnapshot) -> FakeProvider:
    return FakeProvider(future_snapshot)


def client(settings: Settings, provider: FakeProvider) -> TestClient:
    app = create_app(settings)
    app.dependency_overrides[provider_factory_dep] = lambda: lambda: provider
    return TestClient(app)


def fetch(
    settings: Settings,
    provider: FakeProvider,
    symbol: str = "SPY",
    query: str = "",
) -> dict:
    response = client(settings, provider).get(f"/api/symbols/{symbol}/gex{query}")
    assert response.status_code == 200, response.text
    return response.json()


class TestPayload:
    def test_it_answers_with_every_level_at_once(
        self, settings: Settings, provider: FakeProvider
    ) -> None:
        """One request. The levels are all measured against one spot, and fetching them
        separately would let two of them be measured against different prices."""
        body = fetch(settings, provider)

        assert body["symbol"] == "SPY"
        assert body["strikes"]
        assert body["call_wall"] is not None
        assert body["put_wall"] is not None
        assert body["max_pain"] is not None
        assert body["regime"] in {"dampened", "volatile", "unknown"}

    def test_the_default_expiry_is_the_nearest_one_still_trading(
        self, settings: Settings, provider: FakeProvider, future_snapshot: ChainSnapshot
    ) -> None:
        body = fetch(settings, provider)

        assert body["expiry"] == min(future_snapshot.expiries).isoformat()
        assert body["expiries_used"] == [body["expiry"]]

    def test_every_listed_expiry_is_offered_to_the_selector(
        self, settings: Settings, provider: FakeProvider, future_snapshot: ChainSnapshot
    ) -> None:
        body = fetch(settings, provider)

        assert [item["expiry"] for item in body["expiries"]] == [
            item.isoformat() for item in future_snapshot.expiries
        ]
        assert all(item["dte"] >= 0 for item in body["expiries"])

    def test_levels_carry_their_distance_from_spot(
        self, settings: Settings, provider: FakeProvider
    ) -> None:
        """Computed on the server so that every level on the card is measured against
        one spot, rather than against whatever the browser last rendered."""
        body = fetch(settings, provider)
        wall = body["call_wall"]

        assert wall["price"] > body["spot"]
        assert wall["distance"] == pytest.approx(wall["price"] - body["spot"])
        assert wall["distance_pct"] == pytest.approx(wall["distance"] / body["spot"])
        assert wall["open_interest"] > 0

    def test_a_derived_level_carries_no_open_interest(
        self, settings: Settings, provider: FakeProvider
    ) -> None:
        """Max pain is a price, not a place anyone holds contracts."""
        body = fetch(settings, provider)

        assert body["max_pain"]["open_interest"] is None

    def test_the_overnight_caveat_is_on_every_response(
        self, settings: Settings, provider: FakeProvider
    ) -> None:
        """Not only when something looks wrong. The staleness is structural: these
        levels always describe the prior close, however fresh the fetch was."""
        body = fetch(settings, provider)

        assert any("published overnight" in note for note in body["notes"])

    def test_open_interest_is_dated_to_the_prior_session(
        self, settings: Settings, provider: FakeProvider
    ) -> None:
        """The fetch date would claim today's trading is in the counts. It is not."""
        body = fetch(settings, provider)

        assert body["open_interest_as_of"] is not None
        assert date.fromisoformat(body["open_interest_as_of"]) < date.today() + timedelta(days=1)

    def test_unknown_gamma_serializes_as_null_not_zero(
        self, settings: Settings, provider: FakeProvider
    ) -> None:
        """The rule that survives from the domain models: a chart that draws zero for
        "unknown" is lying in a way nobody can see."""
        body = fetch(settings, provider)

        for row in body["strikes"]:
            assert row["net_gex"] is None or isinstance(row["net_gex"], float)
            assert row["call_oi"] >= 0


class TestExpirySelection:
    def test_a_named_expiry_is_used(
        self, settings: Settings, provider: FakeProvider, future_snapshot: ChainSnapshot
    ) -> None:
        wanted = max(future_snapshot.expiries)
        body = fetch(settings, provider, query=f"?expiry={wanted.isoformat()}")

        assert body["expiry"] == wanted.isoformat()
        assert body["expiries_used"] == [wanted.isoformat()]

    def test_an_unlisted_expiry_is_refused_rather_than_replaced(
        self, settings: Settings, provider: FakeProvider
    ) -> None:
        """A wall labelled Friday and computed from next Friday is wrong in a way
        nobody would catch by looking at it."""
        response = client(settings, provider).get("/api/symbols/SPY/gex?expiry=2031-01-17")

        assert response.status_code == 404
        assert "no listed chain" in response.json()["detail"]

    def test_a_window_folds_in_every_expiry_inside_it(
        self, settings: Settings, provider: FakeProvider, future_snapshot: ChainSnapshot
    ) -> None:
        span = (max(future_snapshot.expiries) - date.today()).days + 1
        body = fetch(settings, provider, query=f"?within_days={span}")

        assert len(body["expiries_used"]) == len(future_snapshot.expiries)
        assert body["within_days"] == span

    def test_a_window_is_opt_in(self, settings: Settings, provider: FakeProvider) -> None:
        """Summing a month of expiries produces a number dominated by the monthly, which
        is a different statement from the one the selector's date makes."""
        body = fetch(settings, provider)

        assert body["within_days"] is None
        assert len(body["expiries_used"]) == 1

    def test_an_expiry_that_has_stopped_trading_is_skipped_by_default(
        self, settings: Settings, frozen_snapshot: ChainSnapshot
    ) -> None:
        """Vendors list today's expiry all evening, and after 16:00 every contract on it
        has settled: no time value, no gamma, and walls on contracts that no longer
        exist. Found on the first live run, which defaulted to a dead 0DTE.
        """
        today = date.today()
        chains = tuple(
            chain.model_copy(
                update={
                    "expiry": today + timedelta(days=offset),
                    "contracts": tuple(
                        contract.model_copy(update={"expiry": today + timedelta(days=offset)})
                        for contract in chain.contracts
                    ),
                }
            )
            for offset, chain in enumerate(frozen_snapshot.chains)
        )
        # The first chain expires today; at any hour the suite runs, the second does not.
        provider = FakeProvider(frozen_snapshot.model_copy(update={"chains": chains}))
        body = fetch(settings, provider)

        expired = today.isoformat()
        if body["expiry"] != expired:
            assert any("stopped trading" in note for note in body["notes"])


class TestSymbols:
    def test_an_index_is_asked_for_under_its_vendor_name(
        self, settings: Settings, provider: FakeProvider
    ) -> None:
        """Plain XSP returns an empty expiry list from Yahoo rather than an error, so
        without the alias the panel would report "no options" for a symbol with a full
        chain. SPX behaves the same way."""
        body = fetch(settings, provider, symbol="SPX")

        assert body["symbol"] == "SPX"
        assert body["vendor_symbol"] == "^SPX"
        assert any(call[1].startswith("^SPX") for call in provider.calls if call[0] == "get_chain")

    def test_an_ordinary_symbol_is_not_rewritten(
        self, settings: Settings, provider: FakeProvider
    ) -> None:
        body = fetch(settings, provider)

        assert body["vendor_symbol"] == "SPY"

    def test_something_that_is_not_a_ticker_is_refused(
        self, settings: Settings, provider: FakeProvider
    ) -> None:
        """The symbol becomes a vendor request and a cache key, so it is checked here
        the same way every other entry point checks it.

        A path-shaped symbol like `..%2F..%2Fetc` is not what this asserts, because it
        never reaches the endpoint: the router does not match it and the SPA catch-all
        answers with index.html. This is the case that does reach the handler.
        """
        response = client(settings, provider).get("/api/symbols/SP%20Y/gex")

        assert response.status_code == 400
        assert "not a ticker" in response.json()["detail"]


class TestSpot:
    def test_it_falls_back_to_the_chain_underlying_and_says_so(
        self, settings: Settings, provider: FakeProvider
    ) -> None:
        """The quote feed carries no ^SPX. Without this fallback the panel would fail on
        exactly the symbols it was built for, and silently using it would mean two
        panels showing two different spots with no way to tell which."""
        body = fetch(settings, provider)

        assert body["spot_source"] == "chain underlying"
        assert body["spot"] > 0
        assert any("quote feed carries no price" in note for note in body["notes"])

    def test_the_delayed_feed_wins_when_it_has_a_price(
        self,
        settings: Settings,
        provider: FakeProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A gamma chart whose spot line sat a dollar off the header price would be read
        as a bug in the levels rather than in the spot."""
        quote = LiveQuote(
            symbol="SPY",
            last=999.5,
            as_of=datetime.now(UTC),
            fetched_at=datetime.now(UTC),
            source="alpaca",
        )
        monkeypatch.setattr(
            "optscan.api.routers.levels.live_quotes",
            lambda _settings, _symbols: ({"SPY": quote}, None),
        )
        body = fetch(settings, provider)

        assert body["spot"] == pytest.approx(999.5)
        assert body["spot_source"] == "delayed quote feed"


class TestFailures:
    def test_a_dead_vendor_is_a_bad_gateway_rather_than_an_empty_chart(
        self, settings: Settings, future_snapshot: ChainSnapshot
    ) -> None:
        failing = FakeProvider(future_snapshot, fail_expiries=set(future_snapshot.expiries))
        response = client(settings, failing).get("/api/symbols/SPY/gex")

        assert response.status_code == 502

    def test_a_symbol_with_no_listed_options_is_a_404(
        self, settings: Settings, future_snapshot: ChainSnapshot
    ) -> None:
        class NoOptions(FakeProvider):
            def get_expirations(self, symbol: str) -> list[date]:
                return []

        response = client(settings, NoOptions(future_snapshot)).get("/api/symbols/SPY/gex")

        assert response.status_code == 404

    def test_an_expiry_list_that_cannot_be_fetched_is_a_404_with_the_reason(
        self, settings: Settings, future_snapshot: ChainSnapshot
    ) -> None:
        class Broken(FakeProvider):
            def get_expirations(self, symbol: str) -> list[date]:
                raise ProviderUnavailable("vendor is down")

        response = client(settings, Broken(future_snapshot)).get("/api/symbols/SPY/gex")

        assert response.status_code == 404
        assert "vendor is down" in response.json()["detail"]


class TestCaching:
    def test_the_chain_is_fetched_once_and_reused(
        self, settings: Settings, provider: FakeProvider
    ) -> None:
        """Open interest is published once a day, so a second fetch a moment later
        returns the same numbers. Switching tabs must not pay for it again."""
        api = client(settings, provider)
        api.get("/api/symbols/SPY/gex")
        api.get("/api/symbols/SPY/gex")

        chain_calls = [call for call in provider.calls if call[0] == "get_chain"]
        expiry_calls = [call for call in provider.calls if call[0] == "get_expirations"]
        assert len(chain_calls) == 1
        assert len(expiry_calls) == 1

    def test_a_different_expiry_is_its_own_entry(
        self, settings: Settings, provider: FakeProvider, future_snapshot: ChainSnapshot
    ) -> None:
        """One entry per expiry rather than one per symbol, which is what makes
        clicking between two expiries free after the first visit."""
        api = client(settings, provider)
        first, second = sorted(future_snapshot.expiries)[:2]
        api.get(f"/api/symbols/SPY/gex?expiry={first.isoformat()}")
        api.get(f"/api/symbols/SPY/gex?expiry={second.isoformat()}")
        api.get(f"/api/symbols/SPY/gex?expiry={first.isoformat()}")

        chain_calls = [call for call in provider.calls if call[0] == "get_chain"]
        assert len(chain_calls) == 2

    def test_clearing_the_caches_forces_a_refetch(
        self, settings: Settings, provider: FakeProvider
    ) -> None:
        """The refresh button has to reach this panel too, or it would be the one thing
        on the page that a refresh cannot update."""
        api = client(settings, provider)
        api.get("/api/symbols/SPY/gex")
        clear_caches()
        api.get("/api/symbols/SPY/gex")

        chain_calls = [call for call in provider.calls if call[0] == "get_chain"]
        assert len(chain_calls) == 2
