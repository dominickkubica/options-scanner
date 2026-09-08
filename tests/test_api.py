"""The dashboard API, exercised without a network or a running server.

Every test here runs against the frozen SPY capture written into a throwaway data
directory, driven through TestClient. The provider is a fake, injected by overriding
one dependency, which is the whole reason `provider_factory_dep` is a factory rather
than a module level call.

The frozen chain is two expiries at 4 and 8 DTE, so the default 21 to 60 day screen
matches nothing against it. That is not a defect in the fixture: it is what makes the
rejection tally testable, and the tests that need candidates widen the window
explicitly through a config override.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from optscan.api.app import create_app
from optscan.api.deps import (
    clear_caches,
    make_provenance,
    provider_factory_dep,
    screen_config_dep,
    solved_symbol,
)
from optscan.config import Settings
from optscan.models import ChainSnapshot, SymbolEvents
from optscan.providers.errors import ProviderUnavailable
from optscan.screener.config import DteFilter, Filters, PremiumFilter, ScreenConfig
from optscan.storage import write_snapshot
from tests.conftest import FakeProvider


@pytest.fixture
def api_settings(tmp_settings: Settings, frozen_snapshot: ChainSnapshot) -> Settings:
    """A data directory holding exactly one capture: the frozen SPY chain."""
    tmp_settings.ensure_dirs()
    write_snapshot(frozen_snapshot, tmp_settings.snapshot_path)
    return tmp_settings


@pytest.fixture(autouse=True)
def _clear_api_caches() -> Iterator[None]:
    """The solved symbol cache is process wide, so it must not cross tests."""
    clear_caches()
    yield
    clear_caches()


def build_client(settings: Settings, provider=None, config: ScreenConfig | None = None):
    app = create_app(settings)
    if provider is not None:
        app.dependency_overrides[provider_factory_dep] = lambda: lambda: provider
    if config is not None:
        app.dependency_overrides[screen_config_dep] = lambda: config
    return TestClient(app)


@pytest.fixture
def client(api_settings: Settings, fake_provider: FakeProvider) -> Iterator[TestClient]:
    with build_client(api_settings, fake_provider) as test_client:
        yield test_client


#: A screen wide enough to match the frozen fixture's 4 and 8 day expiries, and
#: cheap enough to clear the minimum profit on a four day spread.
WIDE_SCREEN = ScreenConfig(
    filters=Filters(
        dte=DteFilter(min_dte=0, max_dte=90),
        premium=PremiumFilter(min_max_profit=0.0, min_annualized_return=0.0),
    )
)

#: A screen no expiry in the frozen fixture can satisfy, so the result is empty by
#: construction rather than by coincidence with the shipped DTE band.
FAR_DATED_SCREEN = ScreenConfig(filters=Filters(dte=DteFilter(min_dte=300, max_dte=400)))


class EventfulProvider(FakeProvider):
    """A provider that does have a corporate calendar, unlike the default fake."""

    def get_events(self, symbol: str) -> SymbolEvents:
        return SymbolEvents(
            symbol=symbol,
            earnings_date=date(2026, 8, 5),
            ex_dividend_date=date(2026, 9, 19),
            dividend_amount=1.75,
            fetched_at=datetime.now(UTC),
            source="fake",
        )


class BrokenProvider(FakeProvider):
    """A provider that is reachable and useless, which is the common vendor failure."""

    def get_history(self, symbol: str, days: int):
        raise ProviderUnavailable("the vendor is having a day")


# ---------------------------------------------------------------------------
# Health and watchlist
# ---------------------------------------------------------------------------


def test_health_reports_the_provider_and_refuses_to_claim_realtime(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["provider"] == "yfinance"
    # The dashboard is built on delayed snapshots. Saying otherwise in the header is
    # the single most consequential lie this UI could tell.
    assert body["realtime"] is False


def test_watchlist_marks_which_symbols_have_data(client: TestClient) -> None:
    body = client.get("/api/watchlist").json()
    assert "SPY" in body["symbols"]
    assert body["captured"]["SPY"] == "2026-07-30"
    # Seeded but never captured, and the sidebar has to be able to say so.
    assert body["captured"]["QQQ"] is None


# ---------------------------------------------------------------------------
# Symbol summary
# ---------------------------------------------------------------------------


def test_symbol_summary_carries_spot_expiries_and_provenance(client: TestClient) -> None:
    body = client.get("/api/symbols/spy").json()

    assert body["symbol"] == "SPY"
    assert body["spot"] == pytest.approx(740.53, abs=0.01)
    assert body["session_date"] == "2026-07-30"
    assert [item["dte"] for item in body["expiries"]] == [4, 8]

    provenance = body["provenance"]
    assert provenance["source"] == "yfinance"
    assert provenance["age_seconds"] > 0
    assert isinstance(provenance["stale"], bool)


def test_symbol_summary_solve_rate_is_reported_per_expiry(client: TestClient) -> None:
    body = client.get("/api/symbols/SPY").json()
    front = body["expiries"][0]
    assert front["contracts"] == 197
    assert 0.0 < front["solve_rate"] <= 1.0
    assert front["solved"] < front["contracts"], "a real chain always has unsolvable strikes"


def test_no_iv_rank_without_a_comparable_tenor_and_it_says_why(client: TestClient) -> None:
    """The fixture holds only 4 and 8 day expiries, so no thirty day vol can be read
    off it and no thirty day rank exists.

    The UI must be told which of the two reasons applies rather than left to render an
    empty gauge. This capture has the history problem *and* the tenor problem, and the
    tenor one is reported because it is the one that no amount of waiting fixes.

    This used to rank the front expiry against the history regardless of its tenor,
    which on a chain with a zero day expiry compares a 43 vol against a range topping
    out near 30 and reads full every single day."""
    body = client.get("/api/symbols/SPY").json()
    assert body["iv_rank"] is None
    note = body["iv_rank_note"]
    assert note is not None
    assert "no expiry near it" in note
    assert "[4, 8]" in note, "the tenors actually on the board belong in the message"


def test_missing_symbol_404s_with_something_to_do_about_it(client: TestClient) -> None:
    response = client.get("/api/symbols/NVDA")
    assert response.status_code == 404
    assert "optscan snapshot" in response.json()["detail"]


def test_events_unchecked_is_visible_rather_than_silent(client: TestClient) -> None:
    """The default fake has no calendar. A screen that skipped its earnings exclusion
    must say so: otherwise it quietly returns candidates the CLI would reject."""
    body = client.get("/api/symbols/SPY").json()
    assert body["events_checked"] is False
    assert body["events_note"]
    assert body["earnings_date"] is None


def test_events_are_reported_when_the_provider_has_them(api_settings: Settings) -> None:
    with build_client(api_settings, EventfulProvider(None)) as client:
        body = client.get("/api/symbols/SPY").json()
    assert body["events_checked"] is True
    assert body["earnings_date"] == "2026-08-05"
    assert body["ex_dividend_date"] == "2026-09-19"
    assert body["events_note"] is None


# ---------------------------------------------------------------------------
# Chain
# ---------------------------------------------------------------------------


def test_chain_defaults_to_the_first_screenable_expiry(client: TestClient) -> None:
    """Nothing in the fixture reaches the screen's 21 day minimum, so it falls back
    to the front rather than returning nothing."""
    body = client.get("/api/symbols/SPY/chain").json()
    assert body["expiry"] == "2026-08-03"
    assert body["dte"] == 4


def test_chain_returns_both_sides_and_a_skew_curve(client: TestClient) -> None:
    body = client.get("/api/symbols/SPY/chain", params={"expiry": "2026-08-07"}).json()
    assert body["expiry"] == "2026-08-07"
    assert len(body["calls"]) + len(body["puts"]) == 287
    assert len(body["put_skew"]) > 10
    assert len(body["call_skew"]) > 10
    # Skew points are the solved subset, so they can never outnumber the rows.
    assert len(body["put_skew"]) <= len(body["puts"])


def test_an_unsolvable_contract_is_null_with_a_reason_never_zero(client: TestClient) -> None:
    """The rule this whole API is built around, checked where it is easiest to break:
    a heatmap cell that reads zero for 'unknown' is invisible."""
    body = client.get("/api/symbols/SPY/chain").json()
    rows = body["calls"] + body["puts"]

    unsolved = [row for row in rows if row["iv"] is None]
    assert unsolved, "the frozen chain has strikes that cannot support a vol"
    for row in unsolved:
        assert row["reject_reason"], f"{row['strike']}{row['right']} refused without a reason"
        assert row["delta"] is None, "no vol means no greeks, not zero greeks"

    for row in rows:
        assert row["iv"] != 0.0
        assert row["reject_reason"] is None or row["iv"] is None


def test_every_listed_strike_appears_even_when_it_cannot_be_solved(client: TestClient) -> None:
    """Dropping unsolvable rows would leave holes in the ladder that read as strikes
    which are not listed at all."""
    body = client.get("/api/symbols/SPY/chain").json()
    reasons = {row["reject_reason"] for row in body["calls"] + body["puts"]}
    assert reasons - {None}, "some rows must carry a named refusal"


def test_uncaptured_expiry_404s_and_lists_what_was_captured(client: TestClient) -> None:
    response = client.get("/api/symbols/SPY/chain", params={"expiry": "2026-12-18"})
    assert response.status_code == 404
    assert "2026-08-03" in response.json()["detail"]


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def test_history_returns_bars_shaped_for_the_chart_library(client: TestClient) -> None:
    body = client.get("/api/symbols/SPY/history", params={"days": 30}).json()
    assert body["bars"]
    bar = body["bars"][0]
    assert set(bar) == {"time", "open", "high", "low", "close", "volume"}
    assert bar["time"] == datetime.now(UTC).date().isoformat()
    assert body["provenance"]["source"] == "fake"
    assert body["note"] is None


def test_history_degrades_to_a_stated_reason_rather_than_a_500(api_settings: Settings) -> None:
    """Candles are the least load bearing panel on the page. Losing them must not
    take the chain and the candidates down too."""
    with build_client(api_settings, BrokenProvider(None)) as client:
        response = client.get("/api/symbols/SPY/history")
    assert response.status_code == 200
    body = response.json()
    assert body["bars"] == []
    assert "ProviderUnavailable" in body["note"]


def test_history_window_is_bounded(client: TestClient) -> None:
    assert client.get("/api/symbols/SPY/history", params={"days": 0}).status_code == 422
    assert client.get("/api/symbols/SPY/history", params={"days": 99999}).status_code == 422


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


def test_scan_explains_an_empty_table(api_settings: Settings, fake_provider: FakeProvider) -> None:
    """An empty result with no explanation is how a screener loses its user.

    The emptiness is forced by the screen passed in rather than by whatever the shipped
    defaults happen to be. This test used to rely on the default 21 day floor excluding
    the fixture's 4 and 8 day expiries, and it broke the day that floor moved to 0,
    which is the wrong reason for a test about explaining an empty table to fail.
    """
    with build_client(api_settings, fake_provider, FAR_DATED_SCREEN) as client:
        body = client.get("/api/scan", params={"symbols": ["SPY"]}).json()

    assert body["opportunities"] == []
    assert body["considered"] > 0
    reasons = {item["reason"]: item["count"] for item in body["rejections"]}
    assert reasons["dte_too_short"] > 0


def test_scan_ranks_candidates_and_breaks_the_score_down(api_settings: Settings) -> None:
    with build_client(api_settings, EventfulProvider(None), WIDE_SCREEN) as client:
        body = client.get("/api/scan", params={"symbols": ["SPY"], "limit": 10}).json()

    assert body["opportunities"], "a 0 to 90 day screen must match the frozen chain"
    assert len(body["opportunities"]) <= 10
    scores = [row["score"] for row in body["opportunities"]]
    assert scores == sorted(scores, reverse=True)

    top = body["opportunities"][0]
    assert top["symbol"] == "SPY"
    assert top["legs"]
    assert set(top["components"]) == {
        "premium",
        "iv_rank",
        "liquidity",
        "probability",
        "event_risk",
    }
    assert body["disclaimer"].startswith("Scores rank candidates")


def test_opportunity_ids_are_stable_across_identical_scans(api_settings: Settings) -> None:
    """A row id has to survive a re-sort and a re-scan, or expanding a row lands on a
    different position than the one that was clicked."""
    with build_client(api_settings, EventfulProvider(None), WIDE_SCREEN) as client:
        first = client.get("/api/scan", params={"symbols": ["SPY"]}).json()
        second = client.get("/api/scan", params={"symbols": ["SPY"]}).json()

    ids = [row["id"] for row in first["opportunities"]]
    assert ids == [row["id"] for row in second["opportunities"]]
    assert len(set(ids)) == len(ids), "ids must be unique within one scan"


def test_scan_flags_that_the_earnings_exclusion_did_not_run(client: TestClient) -> None:
    body = client.get("/api/scan", params={"symbols": ["SPY"]}).json()
    assert body["events_checked"] is False
    assert any("earnings" in note.lower() for note in body["notes"])


def test_scan_reports_symbols_with_no_capture(client: TestClient) -> None:
    body = client.get("/api/scan", params={"symbols": ["SPY", "MSFT"]}).json()
    assert body["symbols_scanned"] == ["SPY"]
    assert any("MSFT" in note for note in body["notes"])


def test_scan_reports_quote_age_per_symbol(client: TestClient) -> None:
    body = client.get("/api/scan", params={"symbols": ["SPY"]}).json()
    assert body["stale"]["SPY"] > 0


# ---------------------------------------------------------------------------
# Gaps
# ---------------------------------------------------------------------------


def test_gaps_returns_flags_with_their_caveats(client: TestClient) -> None:
    body = client.get("/api/gaps", params={"symbols": ["SPY"]}).json()
    assert body["symbols_scanned"] == ["SPY"]
    assert "not edge" in body["disclaimer"]
    for gap in body["gaps"]:
        assert gap["kind"]
        assert gap["description"]
        assert isinstance(gap["actionable"], bool)


def test_gaps_says_the_vertical_detector_is_off(client: TestClient) -> None:
    """It is off for a reason that took three rewrites to find, and a user looking at
    an empty vertical list deserves to know it was never run."""
    body = client.get("/api/gaps", params={"symbols": ["SPY"]}).json()
    assert any("Vertical mispricing" in note for note in body["notes"])


# ---------------------------------------------------------------------------
# Payoff
# ---------------------------------------------------------------------------


def _spread_strikes(client: TestClient, expiry: str = "2026-08-03") -> tuple[float, float]:
    """Two put strikes below spot that both have a two sided market."""
    chain = client.get("/api/symbols/SPY/chain", params={"expiry": expiry}).json()
    spot = chain["spot"]
    tradeable = sorted(
        row["strike"]
        for row in chain["puts"]
        if row["mid"] is not None and row["iv"] is not None and row["strike"] < spot * 0.99
    )
    assert len(tradeable) >= 2
    return tradeable[-1], tradeable[-6]


def test_payoff_prices_the_legs_from_the_stored_chain(client: TestClient) -> None:
    short_strike, long_strike = _spread_strikes(client)
    body = client.post(
        "/api/payoff",
        json={
            "symbol": "SPY",
            "expiry": "2026-08-03",
            "legs": [
                {"action": "sell", "right": "P", "strike": short_strike},
                {"action": "buy", "right": "P", "strike": long_strike},
            ],
        },
    ).json()

    assert body["net_credit"] > 0, "a put credit spread takes in a credit"
    assert body["max_profit"] == pytest.approx(body["net_credit"], abs=1e-6)
    assert body["max_loss"] is not None and body["max_loss"] < 0
    assert len(body["breakevens"]) == 1
    assert long_strike < body["breakevens"][0] < short_strike

    # Every leg comes back with the price it was evaluated at, so the curve can be
    # checked against its own inputs rather than taken on faith.
    assert all(leg["mid"] is not None for leg in body["legs"])
    assert body["provenance"]["source"] == "yfinance"


def test_payoff_draws_a_t_plus_zero_curve_below_the_expiry_curve(client: TestClient) -> None:
    """The visual form of the fact that short premium is paid for waiting. Checked at
    the middle of the range, where the two curves differ most."""
    short_strike, long_strike = _spread_strikes(client)
    body = client.post(
        "/api/payoff",
        json={
            "symbol": "SPY",
            "expiry": "2026-08-03",
            "legs": [
                {"action": "sell", "right": "P", "strike": short_strike},
                {"action": "buy", "right": "P", "strike": long_strike},
            ],
        },
    ).json()

    points = body["points"]
    assert all(point["at_now"] is not None for point in points)
    assert body["note"] is None

    at_spot = min(points, key=lambda point: abs(point["price"] - body["spot"]))
    assert at_spot["at_now"] < at_spot["at_expiry"]


def test_payoff_refuses_a_strike_that_is_not_listed(client: TestClient) -> None:
    response = client.post(
        "/api/payoff",
        json={
            "symbol": "SPY",
            "expiry": "2026-08-03",
            "legs": [{"action": "sell", "right": "P", "strike": 1.0}],
        },
    )
    assert response.status_code == 404
    assert "no listed" in response.json()["detail"]


def test_payoff_rejects_an_empty_position(client: TestClient) -> None:
    response = client.post(
        "/api/payoff",
        json={"symbol": "SPY", "expiry": "2026-08-03", "legs": []},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Caching and provenance
# ---------------------------------------------------------------------------


def test_solving_the_same_capture_twice_returns_the_cached_object(
    api_settings: Settings,
) -> None:
    """Three panels ask for the same symbol on one page load. Without this they would
    each pay several hundred milliseconds to solve the identical chain."""
    first = solved_symbol("SPY", api_settings, WIDE_SCREEN, with_events=False)
    second = solved_symbol("SPY", api_settings, WIDE_SCREEN, with_events=False)
    assert first is second


def test_concurrent_requests_for_one_symbol_solve_it_once(
    api_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case the cache actually exists for.

    Opening a symbol fires the summary, the chain, and the scan at the same instant,
    and uvicorn runs sync endpoints in a thread pool. Without a lock around the solve
    all three miss a cache none of them has finished filling, and the cache saves
    nothing on the one page load where it matters most.
    """
    from optscan.api import deps

    solves = 0
    real = deps.analyze_snapshot

    def counting(*args, **kwargs):
        nonlocal solves
        solves += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(deps, "analyze_snapshot", counting)

    barrier = threading.Barrier(4)

    def worker() -> object:
        barrier.wait()
        return solved_symbol("SPY", api_settings, WIDE_SCREEN, with_events=False)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = [future.result() for future in [pool.submit(worker) for _ in range(4)]]

    assert solves == 1
    assert all(result is results[0] for result in results)


def test_a_new_capture_invalidates_the_cache_without_being_asked(
    api_settings: Settings, frozen_snapshot: ChainSnapshot
) -> None:
    """The cache is keyed on the snapshot's own fetched_at, so nobody has to remember
    to clear it. A cache keyed on the symbol alone would serve yesterday's chain until
    the server was restarted."""
    first = solved_symbol("SPY", api_settings, WIDE_SCREEN, with_events=False)
    assert first is not None

    later = frozen_snapshot.model_copy(
        update={"fetched_at": frozen_snapshot.fetched_at + timedelta(hours=1)}
    )
    write_snapshot(later, api_settings.snapshot_path)

    second = solved_symbol("SPY", api_settings, WIDE_SCREEN, with_events=False)
    assert second is not None
    assert second is not first
    assert second.snapshot.fetched_at > first.snapshot.fetched_at


def test_provenance_marks_an_old_capture_stale() -> None:
    now = datetime(2026, 7, 30, 20, 0, tzinfo=UTC)
    fresh = make_provenance("yfinance", now - timedelta(hours=1), now)
    old = make_provenance("yfinance", now - timedelta(hours=30), now)

    assert fresh.stale is False
    assert fresh.age_seconds == pytest.approx(3600.0)
    assert old.stale is True


# ---------------------------------------------------------------------------
# Serving the built frontend
# ---------------------------------------------------------------------------


def test_a_built_frontend_is_mounted_with_a_client_side_route_fallback(
    api_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refresh on a client side route must not 404, and a real file under dist must
    still win over the fallback."""
    import optscan.api.app as app_module

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>optscan</title>", encoding="utf-8")
    (dist / "assets" / "index.js").write_text("console.log(1)", encoding="utf-8")
    monkeypatch.setattr(app_module, "frontend_dist", lambda: dist)

    with build_client(api_settings) as client:
        assert "optscan" in client.get("/").text
        assert client.get("/assets/index.js").text == "console.log(1)"
        # An unknown path is a client side route, not a missing file.
        assert "optscan" in client.get("/payoff/SPY").text
        # The API still wins over the catch all.
        assert client.get("/api/health").json()["status"] == "ok"


def test_the_watchlist_carries_a_quote_for_every_symbol_with_prices(tmp_settings):
    """The pinned sidebar reads price and daily move from here.

    One query for the whole list rather than one request per symbol, because this
    endpoint loads on every page.
    """
    from datetime import date, timedelta

    from fastapi.testclient import TestClient

    from optscan.api.app import create_app
    from optscan.models.vendor import VendorDailyBar
    from optscan.storage import db
    from optscan.storage.vendor import import_daily_bars

    with db.session(tmp_settings.sqlite_path) as conn:
        db.seed_watchlist(conn, ["ZZZ"])
        base = date(2026, 9, 4)
        import_daily_bars(
            conn,
            [
                VendorDailyBar(
                    source="test",
                    symbol="ZZZ",
                    session_date=base - timedelta(days=1),
                    open=100.0,
                    high=100.0,
                    low=100.0,
                    close=100.0,
                    volume=1,
                ),
                VendorDailyBar(
                    source="test",
                    symbol="ZZZ",
                    session_date=base,
                    open=110.0,
                    high=110.0,
                    low=110.0,
                    close=110.0,
                    volume=1,
                ),
            ],
        )

    payload = TestClient(create_app(tmp_settings)).get("/api/watchlist").json()
    quote = payload["quotes"]["ZZZ"]
    assert quote["last"] == 110.0
    assert quote["change"] == pytest.approx(10.0)
    # Both forms come from the server so the rounding happens once, rather than the
    # browser deriving one from the other.
    assert quote["change_pct"] == pytest.approx(0.10)


def test_a_symbol_with_no_price_history_has_no_quote(tmp_settings):
    """Absent rather than zero: no history is ignorance, not a flat day."""
    from fastapi.testclient import TestClient

    from optscan.api.app import create_app
    from optscan.storage import db

    with db.session(tmp_settings.sqlite_path) as conn:
        db.seed_watchlist(conn, ["NOPE"])

    payload = TestClient(create_app(tmp_settings)).get("/api/watchlist").json()
    assert "NOPE" in payload["symbols"]
    assert "NOPE" not in payload["quotes"]
