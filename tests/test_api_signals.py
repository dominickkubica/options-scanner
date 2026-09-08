"""The signals endpoints: one evaluates, the other reads the record.

The contract worth testing hardest is that `/signals` **delivers nothing**. A dashboard
refresh that consumed the once-per-session suppression would leave the scheduled scan
silent, which is the worst failure an alerting tool has, because it still looks like it
is working.

Offline like the rest of the API suite: every route here reads stored bars only, so no
provider is involved at all.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from optscan.api.app import create_app
from optscan.api.deps import clear_caches
from optscan.api.routers.signals import MAX_SCAN_SYMBOLS
from optscan.config import Settings
from optscan.models.vendor import VendorDailyBar
from optscan.storage import db
from optscan.storage.vendor import import_daily_bars

SOURCE = "test"
TODAY = date(2026, 9, 4)


@pytest.fixture(autouse=True)
def _clear() -> Iterator[None]:
    clear_caches()
    yield
    clear_caches()


def series(symbol: str, last: date, count: int = 80, step: float = 0.0):
    bars = []
    for index in range(count):
        close = 100.0 + step * index
        bars.append(
            VendorDailyBar(
                source=SOURCE,
                symbol=symbol,
                session_date=last - timedelta(days=count - 1 - index),
                open=close,
                high=close + 1.0,
                low=close - 1.0,
                close=close,
                volume=1_000_000,
            )
        )
    return bars


def seed(settings: Settings, symbols: list[str], last: date = TODAY) -> None:
    with db.session(settings.sqlite_path) as conn:
        for symbol in symbols:
            import_daily_bars(conn, series(symbol, last))
            # added_at is NOT NULL, and INSERT OR IGNORE swallows that violation as
            # readily as a duplicate key, so omitting it inserts nothing at all.
            conn.execute(
                "INSERT OR IGNORE INTO watchlist (symbol, added_at) VALUES (?, ?)",
                (symbol, "2026-09-04T00:00:00+00:00"),
            )
        conn.commit()


@pytest.fixture
def client(tmp_settings: Settings) -> TestClient:
    return TestClient(create_app(tmp_settings))


def test_current_signals_evaluates_and_returns_a_payload(
    client: TestClient, tmp_settings: Settings
) -> None:
    seed(tmp_settings, ["SPY", "QQQ"])
    response = client.get("/api/signals")
    assert response.status_code == 200
    payload = response.json()
    assert payload["scanned"] == 2
    assert isinstance(payload["signals"], list)


def test_current_signals_delivers_nothing(client: TestClient, tmp_settings: Settings) -> None:
    """The contract of the route. Opening the panel must not consume suppression."""
    seed(tmp_settings, ["SPY"])
    assert client.get("/api/signals").status_code == 200

    recent = client.get("/api/signals/recent").json()
    assert recent["signals"] == []
    with db.session(tmp_settings.sqlite_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM signal_sent").fetchone()[0] == 0


def test_current_signals_can_be_pointed_at_one_symbol(
    client: TestClient, tmp_settings: Settings
) -> None:
    seed(tmp_settings, ["SPY", "QQQ"])
    payload = client.get("/api/signals", params={"symbol": "spy"}).json()
    assert payload["scanned"] == 1


def test_an_empty_watchlist_explains_itself_rather_than_returning_nothing(
    client: TestClient,
) -> None:
    """An empty table with no sentence reads as a broken feature."""
    payload = client.get("/api/signals").json()
    assert payload["signals"] == []
    assert payload["notes"]
    assert "prices sync" in payload["notes"][0]


def test_a_large_watchlist_is_bounded_and_says_so(
    client: TestClient, tmp_settings: Settings
) -> None:
    """This route runs on a page load, so it is capped rather than left to take
    seconds. The cap is stated in the payload instead of silently truncating."""
    seed(tmp_settings, [f"SYM{index:03d}" for index in range(MAX_SCAN_SYMBOLS + 5)])
    payload = client.get("/api/signals").json()
    assert payload["scanned"] == MAX_SCAN_SYMBOLS
    assert any("scheduled scan" in note for note in payload["notes"])


def test_stale_symbols_are_reported_in_the_notes(
    client: TestClient, tmp_settings: Settings
) -> None:
    """A delisted ticker's final session must not be presented as today's news."""
    seed(tmp_settings, ["SPY"])
    seed(tmp_settings, ["DEAD"], last=TODAY - timedelta(days=400))

    payload = client.get("/api/signals").json()
    assert payload["scanned"] == 1
    assert any("DEAD" in note for note in payload["notes"])


def test_recent_signals_reads_the_record(client: TestClient, tmp_settings: Settings) -> None:
    with db.session(tmp_settings.sqlite_path) as conn:
        conn.execute(
            "INSERT INTO signal_sent "
            "(symbol, kind, session_date, fired_at, severity, price, detail) "
            "VALUES ('SPY', 'level_break', '2026-09-04', "
            "'2026-09-04T21:00:00+00:00', 3, 640.0, 'broke a level')"
        )
        conn.commit()

    payload = client.get("/api/signals/recent").json()
    assert len(payload["signals"]) == 1
    row = payload["signals"][0]
    assert row["symbol"] == "SPY"
    assert row["severity"] == 3
    assert row["fired_at"] == "2026-09-04T21:00:00+00:00"
    assert payload["counts"] == {"level_break": 1}


def test_recent_signals_explains_an_empty_record(client: TestClient) -> None:
    payload = client.get("/api/signals/recent").json()
    assert payload["signals"] == []
    assert any("scheduled scan" in note for note in payload["notes"])


def test_recent_signals_rejects_a_nonsense_window(client: TestClient) -> None:
    assert client.get("/api/signals/recent", params={"days": 0}).status_code == 422
    assert client.get("/api/signals/recent", params={"days": 5000}).status_code == 422
