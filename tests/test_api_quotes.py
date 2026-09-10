"""Live headline prices, and every way they are allowed to be late.

The contract worth testing hardest is not that a quote comes back. It is that a quote
which is **not** live never looks live. This endpoint exists because the watchlist spent
a day showing Friday's close on a Tuesday while looking perfectly healthy, so a stored
fallback that rendered identically to a fresh quote would reintroduce exactly that bug
one layer up.

Offline. The vendor is stubbed everywhere; no test here touches the network.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from optscan.api.app import create_app
from optscan.api.deps import MAX_LIVE_QUOTE_SYMBOLS, clear_caches, live_quotes
from optscan.config import Settings
from optscan.models import LiveQuote
from optscan.models.vendor import VendorDailyBar
from optscan.providers.errors import ProviderUnavailable
from optscan.storage import db
from optscan.storage.vendor import import_daily_bars

SOURCE = "test"
TODAY = date(2026, 9, 8)


@pytest.fixture(autouse=True)
def _clear() -> Iterator[None]:
    clear_caches()
    yield
    clear_caches()


def seed(settings: Settings, symbol: str, closes: list[float], last: date = TODAY) -> None:
    """Store one bar per close, oldest first, ending on `last`."""
    bars = [
        VendorDailyBar(
            source=SOURCE,
            symbol=symbol,
            session_date=last - timedelta(days=len(closes) - 1 - index),
            open=close,
            high=close + 1.0,
            low=close - 1.0,
            close=close,
            volume=1_000_000,
        )
        for index, close in enumerate(closes)
    ]
    with db.session(settings.sqlite_path) as conn:
        import_daily_bars(conn, bars)
        conn.execute(
            "INSERT OR IGNORE INTO watchlist (symbol, added_at) VALUES (?, ?)",
            (symbol, "2026-09-08T00:00:00+00:00"),
        )
        conn.commit()


def quote(symbol: str, **kwargs) -> LiveQuote:
    defaults = {
        "last": 100.0,
        "previous_close": 99.0,
        "as_of": datetime(2026, 9, 8, 19, 55, tzinfo=UTC),
        "feed": "delayed_sip",
        "delay_minutes": 15,
        "fetched_at": datetime.now(UTC),
        "source": "alpaca",
    }
    return LiveQuote(symbol=symbol, **{**defaults, **kwargs})


class StubProvider:
    """Stands in for the vendor. Raises whatever it was given, or answers from a dict."""

    def __init__(self, quotes) -> None:
        self._quotes = quotes
        self.asked: list[str] = []
        self.closed = False

    def get_live_quotes(self, symbols):
        self.asked = list(symbols)
        if isinstance(self._quotes, Exception):
            raise self._quotes
        wanted = set(symbols)
        return {name: value for name, value in self._quotes.items() if name in wanted}

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def client(tmp_settings: Settings) -> TestClient:
    return TestClient(create_app(tmp_settings))


def use(monkeypatch: pytest.MonkeyPatch, provider) -> None:
    """Install a stub quote provider through the same seam production uses."""
    from optscan.api.deps import set_quote_provider

    set_quote_provider(lambda _settings: provider)


# --------------------------------------------------------------------- the happy path


def test_a_live_quote_carries_its_move_and_its_timestamp(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    use(monkeypatch, StubProvider({"TJX": quote("TJX", last=128.92, previous_close=132.08)}))

    payload = client.get("/api/quotes", params={"symbols": "TJX"}).json()
    row = payload["quotes"]["TJX"]

    assert row["last"] == pytest.approx(128.92)
    assert row["change"] == pytest.approx(-3.16)
    assert row["change_pct"] == pytest.approx(-0.023925, abs=1e-6)
    assert row["as_of"] is not None
    assert payload["note"] is None
    assert payload["delay_minutes"] == 15


def test_the_extended_print_stays_beside_the_close_not_merged_into_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two facts with two baselines. The night move is measured against today's close."""
    use(
        monkeypatch,
        StubProvider({"TJX": quote("TJX", last=128.92, previous_close=132.08, extended=129.16)}),
    )
    row = client.get("/api/quotes", params={"symbols": "TJX"}).json()["quotes"]["TJX"]

    assert row["last"] == pytest.approx(128.92)
    assert row["extended"] == pytest.approx(129.16)
    # A stock down 2.4% on the day was up 0.24 after the bell. Folding the two together
    # would report neither number correctly.
    assert row["extended_change"] == pytest.approx(0.24)


def test_the_batch_is_one_vendor_call_for_every_symbol(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason polling is affordable: one request per cycle, not one per symbol."""
    provider = StubProvider({name: quote(name) for name in ("AAPL", "QQQ", "TJX")})
    use(monkeypatch, provider)

    client.get("/api/quotes", params={"symbols": "AAPL,QQQ,TJX"})

    assert provider.asked == ["AAPL", "QQQ", "TJX"]
    assert provider.closed


# ------------------------------------------------------------------- being late safely


def test_a_vendor_failure_falls_back_to_stored_closes_and_says_so(
    client: TestClient, tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(tmp_settings, "QQQ", [717.67, 718.96, 718.36])
    use(monkeypatch, StubProvider(ProviderUnavailable("upstream is down")))

    payload = client.get("/api/quotes", params={"symbols": "QQQ"}).json()
    row = payload["quotes"]["QQQ"]

    assert row["last"] == pytest.approx(718.36)
    assert row["change"] == pytest.approx(-0.60)
    assert row["feed"] == "stored"
    assert "stored closes" in payload["note"]


def test_a_stored_fallback_is_dated_to_its_session_not_to_now(
    client: TestClient, tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point. A price stamped `now` cannot be told apart from a fresh one."""
    seed(tmp_settings, "QQQ", [718.96, 718.36])
    use(monkeypatch, StubProvider(ProviderUnavailable("down")))

    payload = client.get("/api/quotes", params={"symbols": "QQQ"}).json()

    assert payload["quotes"]["QQQ"]["as_of"].startswith(TODAY.isoformat())
    assert payload["quotes"]["QQQ"]["feed"] == "stored"
    # A stored close is not "fifteen minutes late", it is a session late, so it must not
    # borrow the live feed's delay. `as_of` is what reports its real age.
    assert payload["delay_minutes"] is None


def test_no_configured_vendor_is_explained_rather_than_empty(
    client: TestClient, tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(tmp_settings, "QQQ", [718.96, 718.36])
    use(monkeypatch, None)

    payload = client.get("/api/quotes", params={"symbols": "QQQ"}).json()
    assert payload["quotes"]["QQQ"]["last"] == pytest.approx(718.36)
    assert "Alpaca" in payload["note"]


def test_one_unknown_symbol_does_not_blank_the_others(
    client: TestClient, tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delisted pin must not take the rest of the watchlist down with it."""
    seed(tmp_settings, "DELISTED", [10.0, 9.0])
    use(monkeypatch, StubProvider({"QQQ": quote("QQQ", last=718.36)}))

    quotes = client.get("/api/quotes", params={"symbols": "QQQ,DELISTED"}).json()["quotes"]

    assert quotes["QQQ"]["feed"] == "delayed_sip"
    # Fell back on its own rather than dragging QQQ back with it.
    assert quotes["DELISTED"]["feed"] == "stored"


def test_a_symbol_with_no_price_anywhere_is_omitted_not_zero(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pill reading 0.00 is a claim about the market."""
    use(monkeypatch, StubProvider({}))
    quotes = client.get("/api/quotes", params={"symbols": "NOSUCH"}).json()["quotes"]
    assert "NOSUCH" not in quotes


def test_too_many_symbols_stays_on_stored_closes(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Browse lists hundreds. Quoting them live would put a vendor call in front of it."""
    symbols = [f"S{index}" for index in range(MAX_LIVE_QUOTE_SYMBOLS + 1)]
    provider = StubProvider({})
    use(monkeypatch, provider)

    _quotes, note = live_quotes(tmp_settings, symbols)

    assert note is not None and "stored closes" in note
    assert provider.asked == []


# ------------------------------------------------------------------------- the cadence


def test_the_poll_interval_comes_from_the_server(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The market calendar lives here. The browser must not grow a second copy of it."""
    use(monkeypatch, StubProvider({"QQQ": quote("QQQ")}))
    payload = client.get("/api/quotes", params={"symbols": "QQQ"}).json()
    assert payload["poll_seconds"] > 0


def test_repeated_calls_share_one_vendor_fetch(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three panels asking at the same instant is one request, not three."""
    calls: list[int] = []

    class Counting(StubProvider):
        def get_live_quotes(self, symbols):
            calls.append(1)
            return super().get_live_quotes(symbols)

    use(monkeypatch, Counting({"QQQ": quote("QQQ")}))
    for _ in range(3):
        client.get("/api/quotes", params={"symbols": "QQQ"})

    assert len(calls) == 1


# ---------------------------------------------------------------- racing the two feeds


def race(snapshot_at: datetime, trade_at: datetime, price: float = 200.0):
    """Run the delayed quote against a real time print stamped `trade_at`."""
    from optscan.providers.alpaca import AlpacaProvider

    delayed = quote("QQQ", last=100.0, previous_close=99.0, as_of=snapshot_at, volume=5_000_000)
    return AlpacaProvider._prefer_newer(delayed, (price, trade_at))


def test_a_newer_realtime_print_wins(_clear: None) -> None:
    """Midday on a liquid name: IEX printed a second ago, the tape is fifteen behind."""
    result = race(
        snapshot_at=datetime(2026, 9, 8, 17, 45, tzinfo=UTC),
        trade_at=datetime(2026, 9, 8, 18, 0, tzinfo=UTC),
    )
    assert result.last == pytest.approx(200.0)
    assert result.realtime is True
    assert result.feed == "iex"
    # It is not delayed, so it must not claim to be.
    assert result.delay_minutes is None


def test_an_older_realtime_print_loses(_clear: None) -> None:
    """A thin name: IEX has not traded for an hour, the delayed tape is fresher."""
    result = race(
        snapshot_at=datetime(2026, 9, 8, 18, 0, tzinfo=UTC),
        trade_at=datetime(2026, 9, 8, 17, 0, tzinfo=UTC),
    )
    assert result.last == pytest.approx(100.0)
    assert result.realtime is False
    assert result.feed == "delayed_sip"


def test_the_realtime_price_never_overwrites_the_official_close(_clear: None) -> None:
    """After hours it is an extended print, and the close is not up for revision."""
    result = race(
        snapshot_at=datetime(2026, 9, 8, 19, 0, tzinfo=UTC),
        trade_at=datetime(2026, 9, 8, 22, 30, tzinfo=UTC),  # 18:30 ET, post market
    )
    assert result.last == pytest.approx(100.0)
    assert result.extended == pytest.approx(200.0)


def test_the_realtime_price_never_brings_its_own_volume(_clear: None) -> None:
    """IEX volume is two percent of the real number: a wrong answer, not a late one."""
    result = race(
        snapshot_at=datetime(2026, 9, 8, 17, 45, tzinfo=UTC),
        trade_at=datetime(2026, 9, 8, 18, 0, tzinfo=UTC),
    )
    assert result.volume == 5_000_000
    assert result.previous_close == pytest.approx(99.0)
