"""Candles: stored daily bars for any symbol, and intraday for one named session.

Two things this covers that broke in obvious ways when they were missing.

**A symbol with prices and no captured chain is chartable.** After a bulk price sync
that describes almost every symbol, and gating the chart on a stored option snapshot
made every unpinned ticker a dead page reading "No stored snapshot for AA" while 2,492
sessions of its bars sat in the database.

**An intraday series is keyed by timestamp, not by date.** A date identifies a session,
so every bar within one day carries the same value and the chart library collapses
seventy eight candles onto a single point. It renders as an empty chart with the daily
axis still on it, which looks like a data problem and is not.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from optscan.api.app import create_app
from optscan.api.deps import DAILY_INTERVAL, INTRADAY_INTERVALS, provider_factory_dep
from optscan.api.views import history_view
from optscan.config import Settings
from optscan.models import PriceBar
from optscan.models.vendor import VendorDailyBar
from optscan.providers import NoDataAvailable
from optscan.storage import db
from optscan.storage.vendor import import_daily_bars, preferred_source, recent_daily_bars


def daily(symbol: str, source: str, days: int, start: int = 1):
    return [
        VendorDailyBar(
            source=source,
            symbol=symbol,
            session_date=date(2026, 6, start + offset),
            open=10.0 + offset,
            high=11.0 + offset,
            low=9.0 + offset,
            close=10.5 + offset,
            volume=1000 + offset,
            trade_count=20,
        )
        for offset in range(days)
    ]


def bar_at(stamp: datetime) -> PriceBar:
    return PriceBar(
        symbol="AAPL",
        ts=stamp,
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=10,
        fetched_at=datetime(2026, 9, 8, tzinfo=UTC),
        source="alpaca",
    )


class NoNetworkProvider:
    """Stands in for the real provider so nothing here reaches a vendor.

    The rule is that tests never touch the network, and this file exercises the path
    that falls back to a provider when nothing is stored. Without the override the
    first run of these tests really did call yfinance and log
    "$NOPE: possibly delisted", which is the rule being broken quietly by a test that
    otherwise passed.
    """

    name = "offline"

    def get_history(self, symbol: str, days: int):
        raise NoDataAvailable(f"offline provider has no bars for {symbol}")


def offline_client(settings: Settings) -> TestClient:
    """A client that cannot reach the network, and is checked to stay that way.

    The provider override alone was not enough once the daily chart started appending
    today's forming bar: that path fetched a quote through its own entry point and made
    a real request from inside the offline suite. Both seams are closed here.
    """
    from optscan.api.deps import set_quote_provider

    set_quote_provider(lambda _settings: None)
    app = create_app(settings=settings)
    app.dependency_overrides[provider_factory_dep] = lambda: NoNetworkProvider
    return TestClient(app)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path, default_watchlist=["AAPL"])


@pytest.fixture
def stocked(settings) -> Settings:
    with db.session(settings.sqlite_path) as conn:
        import_daily_bars(conn, daily("AA", "alpaca", 20))
    return settings


class TestStoredDailyBars:
    def test_a_symbol_with_no_chain_still_has_candles(self, stocked) -> None:
        """The regression. AA has never been captured and must still chart."""
        client = offline_client(stocked)
        body = client.get("/api/symbols/AA/history", params={"days": 20}).json()
        assert len(body["bars"]) == 20
        assert body["note"] is None

    def test_bars_come_back_oldest_first(self, stocked) -> None:
        with db.session(stocked.sqlite_path) as conn:
            bars, _ = recent_daily_bars(conn, "AA", 20)
        assert [b.session_date for b in bars] == sorted(b.session_date for b in bars)

    def test_only_the_last_n_sessions_are_returned(self, stocked) -> None:
        with db.session(stocked.sqlite_path) as conn:
            bars, _ = recent_daily_bars(conn, "AA", 5)
        assert len(bars) == 5
        assert bars[-1].session_date == date(2026, 6, 20), "the newest, not the oldest"

    def test_one_vendor_is_chosen_rather_than_both_interleaved(self, settings) -> None:
        """Two vendors' bars for one day are not two observations. Interleaving them
        produces duplicate dates and a change that is the vendors disagreeing."""
        with db.session(settings.sqlite_path) as conn:
            import_daily_bars(conn, daily("AAPL", "alpaca", 10))
            import_daily_bars(conn, daily("AAPL", "marketchameleon", 10))
            bars, source = recent_daily_bars(conn, "AAPL", 10)

        assert len(bars) == 10, "ten sessions from two vendors is still ten"
        assert len({b.session_date for b in bars}) == 10
        assert {b.source for b in bars} == {source}

    def test_the_latest_vendor_wins_and_the_choice_is_stable(self, settings) -> None:
        with db.session(settings.sqlite_path) as conn:
            import_daily_bars(conn, daily("AAPL", "alpaca", 5, start=1))
            import_daily_bars(conn, daily("AAPL", "marketchameleon", 5, start=10))
            first = preferred_source(conn, "AAPL")
            second = preferred_source(conn, "AAPL")

        assert first == "marketchameleon", "it has the more recent session"
        assert first == second, "the answer must not depend on scan order"

    def test_an_unknown_symbol_says_what_to_run(self, settings) -> None:
        client = offline_client(settings)
        body = client.get("/api/symbols/NOPE/history", params={"days": 20}).json()
        assert body["bars"] == []
        # Nothing stored, and the provider had nothing either, so the note comes from
        # the provider rather than from the "run prices sync" advice.
        assert body["note"]


class TestBarTimeFormat:
    """The collapse bug, pinned."""

    def test_daily_bars_are_keyed_by_date(self) -> None:
        bars = [bar_at(datetime(2026, 6, day, tzinfo=UTC)) for day in (1, 2, 3)]
        out = history_view("AAPL", bars, None)
        assert [b.time for b in out.bars] == ["2026-06-01", "2026-06-02", "2026-06-03"]

    def test_intraday_bars_are_keyed_by_timestamp(self) -> None:
        """A date would give all three the same key and the chart would draw one bar."""
        stamps = [
            datetime(2026, 6, 1, 13, 30, tzinfo=UTC),
            datetime(2026, 6, 1, 13, 35, tzinfo=UTC),
            datetime(2026, 6, 1, 13, 40, tzinfo=UTC),
        ]
        out = history_view("AAPL", [bar_at(s) for s in stamps], None, intraday=True)
        times = [b.time for b in out.bars]

        assert all(isinstance(t, int) for t in times)
        assert len(set(times)) == 3, "three bars in one day are three distinct points"
        assert times == sorted(times)

    def test_a_daily_bar_stamped_at_an_hour_is_still_daily(self) -> None:
        """The bug the first draft had. It inferred intraday from the timestamp having
        a time of day, and **Alpaca stamps daily bars at 04:00Z**, so every symbol
        served from the provider rather than from storage would have had its whole
        series keyed by epoch seconds against a chart expecting dates.

        The interval belongs to the caller, so it is passed rather than guessed.
        """
        stamped = bar_at(datetime(2026, 6, 1, 4, 0, tzinfo=UTC))
        assert isinstance(history_view("AAPL", [stamped], None).bars[0].time, str)
        assert isinstance(history_view("AAPL", [stamped], None, intraday=True).bars[0].time, int)

    def test_an_empty_series_does_not_crash_the_detection(self) -> None:
        assert history_view("AAPL", [], "nothing here").bars == []


class TestIntradayRefusals:
    """Each refusal sends the reader somewhere different, so each gets its own words."""

    def test_an_unserved_interval_is_named(self, stocked) -> None:
        client = offline_client(stocked)
        body = client.get(
            "/api/symbols/AA/history", params={"interval": "7Min", "session": "2026-06-01"}
        ).json()
        assert body["bars"] == []
        assert "7Min is not an interval" in body["note"]

    def test_missing_credentials_say_which_ones(self, tmp_path) -> None:
        """Named explicitly because Settings reads the real .env, and on a machine
        with the keys configured an omitted field is filled from the environment."""
        bare = Settings(data_dir=tmp_path, alpaca_key_id=None, alpaca_secret_key=None)
        client = offline_client(bare)
        body = client.get(
            "/api/symbols/AAPL/history", params={"interval": "1Min", "session": "2026-06-01"}
        ).json()
        assert "OPTSCAN_ALPACA_KEY_ID" in body["note"]
        assert "daily candles work without them" in body["note"]

    def test_daily_is_the_default_interval(self) -> None:
        assert DAILY_INTERVAL == "1Day"
        assert DAILY_INTERVAL not in INTRADAY_INTERVALS

    def test_the_intervals_the_ui_offers_are_all_served(self) -> None:
        """The drill-in prompt offers these three; a mismatch would be a dead button."""
        for offered in ("1Min", "2Min", "5Min"):
            assert offered in INTRADAY_INTERVALS
