"""The symbol catalogue, search, and watchlist editing over HTTP.

The distinction every test here is really about: **price history is not screenable.**
After a bulk sync there are hundreds of symbols with a decade of daily bars and a
handful with captured option chains, and confusing the two is what would make somebody
pin twenty tickers and wonder why the screener never changed.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from optscan.api.app import create_app
from optscan.catalogue import build_catalogue, search
from optscan.config import Settings
from optscan.models.vendor import VendorDailyBar
from optscan.storage import db
from optscan.storage.vendor import import_daily_bars
from optscan.universe import Universe


def bars(symbol: str, source: str, days: int, *, start_day: int = 1, iv: float | None = None):
    return [
        VendorDailyBar(
            source=source,
            symbol=symbol,
            session_date=date(2026, 1, start_day + offset),
            # A rising series whose high and low move with it. The first draft held
            # high fixed at 11 while the close climbed past it, and the coherence
            # validator refused every bar after the second, which is the validator
            # working rather than the fixture being awkward.
            open=10.0 + offset,
            high=11.0 + offset,
            low=9.0 + offset,
            close=10.5 + offset,
            volume=1000,
            trade_count=20,
            iv30=iv,
        )
        for offset in range(days)
    ]


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path, default_watchlist=["AAPL"])


@pytest.fixture
def seeded(settings) -> Settings:
    with db.session(settings.sqlite_path) as conn:
        db.seed_watchlist(conn, settings.default_watchlist)
        import_daily_bars(conn, bars("AAPL", "alpaca", 5))
        import_daily_bars(conn, bars("NVDA", "alpaca", 5))
        import_daily_bars(conn, bars("MP", "alpaca", 5))
        import_daily_bars(conn, bars("MPWR", "alpaca", 5))
    return settings


UNIVERSE = Universe(
    groups={"tech": ("NVDA", "MPWR", "AAPL"), "rare_earth": ("MP",)},
    date_checked=date(2026, 9, 7),
)


class TestCatalogue:
    def test_price_history_alone_is_not_screenable(self, seeded) -> None:
        """The distinction the whole module exists for. NVDA has five sessions of bars
        and no captured chain, so the screener cannot produce a candidate for it."""
        built = build_catalogue(seeded, UNIVERSE)
        nvda = built.by_symbol("NVDA")
        assert nvda.has_prices
        assert not nvda.screenable
        assert "no chains" in nvda.status()

    def test_a_watchlist_symbol_without_a_capture_says_it_is_waiting(self, seeded) -> None:
        """Distinct from "no chains": pinning it means one is coming."""
        built = build_catalogue(seeded, UNIVERSE)
        aapl = built.by_symbol("AAPL")
        assert aapl.on_watchlist
        assert not aapl.screenable
        assert "awaiting first capture" in aapl.status()

    def test_sessions_are_distinct_dates_not_a_sum_over_vendors(self, settings) -> None:
        """Two vendors covering the same days is one history, not two.

        Measured on the real database before this was fixed: AAPL held 2,511 Alpaca
        sessions and 3,188 Market Chameleon ones over mostly the same dates, and the
        catalogue reported 5,699 sessions of history for a symbol that has 3,188. The
        two richest histories were the two the number lied about.
        """
        with db.session(settings.sqlite_path) as conn:
            import_daily_bars(conn, bars("AAPL", "alpaca", 5))
            import_daily_bars(conn, bars("AAPL", "marketchameleon", 5, iv=0.2))

        entry = build_catalogue(settings, UNIVERSE).by_symbol("AAPL")
        assert entry.price_sessions == 5, "the same five days from two vendors is five"
        assert entry.price_sources == ("alpaca", "marketchameleon")

    def test_an_iv_series_is_flagged_because_only_it_allows_a_rank(self, settings) -> None:
        with db.session(settings.sqlite_path) as conn:
            import_daily_bars(conn, bars("AAPL", "alpaca", 3))
            import_daily_bars(conn, bars("NVDA", "marketchameleon", 3, iv=0.25))

        built = build_catalogue(settings, UNIVERSE)
        assert not built.by_symbol("AAPL").has_iv_history
        assert built.by_symbol("NVDA").has_iv_history

    def test_a_symbol_in_a_group_with_no_data_still_appears(self, settings) -> None:
        """A group member with nothing stored is a real thing to know about: it is
        what tells somebody the sync missed it."""
        entry = build_catalogue(settings, UNIVERSE).by_symbol("MPWR")
        assert entry is not None
        assert entry.groups == ("tech",)
        assert entry.status() == "no data held"


class TestSearch:
    def test_an_exact_ticker_outranks_a_longer_match(self, seeded) -> None:
        """Typing MP must find MP, not bury it under MPWR alphabetically."""
        built = build_catalogue(seeded, UNIVERSE)
        assert next(e.symbol for e in search(built, "MP")) == "MP"

    def test_a_prefix_outranks_a_substring(self) -> None:
        catalogue = _catalogue_of("AMP", "MPWR", "MP")
        assert [e.symbol for e in search(catalogue, "MP")] == ["MP", "MPWR", "AMP"]

    def test_filtering_by_group(self, seeded) -> None:
        built = build_catalogue(seeded, UNIVERSE)
        assert [e.symbol for e in search(built, "", group="rare_earth")] == ["MP"]

    def test_only_screenable_hides_everything_with_prices_alone(self, seeded) -> None:
        """On a fresh install this is empty, and that is the true answer."""
        built = build_catalogue(seeded, UNIVERSE)
        assert search(built, "", only_screenable=True) == []

    def test_the_limit_is_honoured(self, seeded) -> None:
        built = build_catalogue(seeded, UNIVERSE)
        assert len(search(built, "", limit=2)) == 2


def _catalogue_of(*symbols: str):
    from optscan.catalogue import Catalogue, SymbolEntry

    return Catalogue(entries=tuple(SymbolEntry(symbol=s) for s in sorted(symbols)))


class TestApi:
    @pytest.fixture
    def client(self, seeded) -> TestClient:
        return TestClient(create_app(settings=seeded))

    def test_home_separates_known_from_screenable(self, client) -> None:
        body = client.get("/api/home").json()
        assert body["total_symbols"] >= 4
        assert body["with_prices"] >= 4
        assert body["screenable"] == 0, "nothing has a captured chain in a fresh install"

    def test_home_says_what_a_pinned_symbol_is_waiting_for(self, client) -> None:
        notes = " ".join(client.get("/api/home").json()["notes"])
        assert "no captured chain yet" in notes
        assert "snapshot" in notes

    def test_search_returns_status_per_symbol(self, client) -> None:
        body = client.get("/api/catalogue", params={"q": "NV"}).json()
        entry = next(e for e in body["entries"] if e["symbol"] == "NVDA")
        assert entry["has_prices"] is True
        assert entry["screenable"] is False
        assert "no chains" in entry["status"]

    def test_an_unknown_group_404s_and_names_the_real_ones(self, client) -> None:
        response = client.get("/api/catalogue", params={"group": "nonsense"})
        assert response.status_code == 404
        assert "universe.yaml" in response.json()["detail"]

    def test_pinning_is_idempotent_and_says_what_happens_next(self, client) -> None:
        first = client.post("/api/watchlist/NVDA").json()
        assert first["changed"] is True
        assert first["on_watchlist"] is True
        # The note is the point: pinning does not capture a chain.
        assert "next `optscan snapshot`" in first["note"]

        again = client.post("/api/watchlist/NVDA").json()
        assert again["changed"] is False
        assert again["on_watchlist"] is True

    def test_unpinning_keeps_the_stored_history(self, client, seeded) -> None:
        """A snapshot cannot be recreated for a day that has passed, so removing a
        symbol must never delete what was captured while it was pinned."""
        client.post("/api/watchlist/NVDA")
        result = client.delete("/api/watchlist/NVDA").json()
        assert result["on_watchlist"] is False
        assert "cannot be captured again" in result["note"]

        entry = build_catalogue(seeded, UNIVERSE).by_symbol("NVDA")
        assert entry.price_sessions == 5, "price history survived the unpin"

    def test_a_junk_ticker_is_refused(self, client) -> None:
        assert client.post("/api/watchlist/not a ticker").status_code in (400, 404)

    def test_the_daily_move_comes_from_one_vendor(self, client, seeded) -> None:
        """Two vendors holding the same last session must not produce a 0% change.

        Measured on the real database: AAPL and QQQ were the only symbols with two
        sources and the only two reporting +0.00%, because the two most recent rows
        were the same day from different vendors rather than two consecutive days.
        """
        with db.session(seeded.sqlite_path) as conn:
            import_daily_bars(conn, bars("AAPL", "marketchameleon", 5, iv=0.2))

        entry = next(
            e
            for e in client.get("/api/catalogue", params={"q": "AAPL"}).json()["entries"]
            if e["symbol"] == "AAPL"
        )
        assert entry["change_pct"] is not None
        assert entry["change_pct"] != 0.0, "a same-day cross-vendor comparison reads as flat"
