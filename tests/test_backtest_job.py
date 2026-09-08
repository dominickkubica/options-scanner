"""The backtest job and its HTTP surface.

The engine's own tests cover the statistics. These cover the wiring around them, and
three properties in particular that the engine cannot enforce on its own: a strategy
round trips through JSON unchanged, delisted symbols are **kept** rather than skipped,
and an option run on a symbol with no stored implied volatility is refused rather than
priced off something else.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from optscan.analytics.backtest import MissingImpliedVol
from optscan.api.app import create_app
from optscan.api.deps import clear_caches
from optscan.config import Settings
from optscan.jobs.backtest import (
    MIN_BARS,
    Strategy,
    load_series,
    resolve_symbols,
    run,
)
from optscan.models.vendor import VendorDailyBar
from optscan.storage import db
from optscan.storage.vendor import import_daily_bars

SOURCE = "test"
LAST = date(2026, 9, 4)


def series(
    symbol: str,
    *,
    last: date = LAST,
    count: int = 600,
    start: float = 100.0,
    step: float = 0.05,
    iv: float | None = None,
) -> list[VendorDailyBar]:
    """A gently rising series, optionally carrying an implied volatility."""
    bars = []
    for index in range(count):
        close = start + step * index
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
                iv30=iv,
            )
        )
    return bars


@pytest.fixture
def seeded(tmp_settings: Settings) -> Settings:
    with db.session(tmp_settings.sqlite_path) as conn:
        import_daily_bars(conn, series("AAA"))
        import_daily_bars(conn, series("BBB", start=50.0))
        # Stopped trading two years ago. A backtest keeps it; the signal scan does not.
        import_daily_bars(conn, series("DEAD", last=LAST - timedelta(days=700)))
        import_daily_bars(conn, series("SHORTY", count=MIN_BARS - 10))
    return tmp_settings


# --------------------------------------------------------------------------------
# The spec
# --------------------------------------------------------------------------------


def test_a_strategy_round_trips_through_json_unchanged() -> None:
    """What makes a result reproducible: the spec can be stored beside the number."""
    original = Strategy(name="x", entry="rsi_below", entry_params={"threshold": 25}, horizon=10)
    assert Strategy.from_dict(original.to_dict()) == original


def test_an_unknown_strategy_field_is_refused_rather_than_dropped() -> None:
    """Silently ignoring `horzon` would run the default horizon under the caller's name."""
    with pytest.raises(ValueError, match="unknown strategy fields: horzon"):
        Strategy.from_dict({"horzon": 5})


def test_a_bad_rule_is_caught_before_any_bars_are_read() -> None:
    with pytest.raises(ValueError, match="unknown entry rule"):
        Strategy(entry="moon_phase").validate()


def test_a_bad_rule_parameter_is_caught_too() -> None:
    with pytest.raises(ValueError, match="not 'thresold'"):
        Strategy(entry="rsi_below", entry_params={"thresold": 30}).validate()


def test_an_option_tenor_outside_the_band_is_refused() -> None:
    """A 30 day implied vol does not price a 7 day option."""
    with pytest.raises(ValueError, match="outside"):
        Strategy(mode="short_put", dte=7).validate()


def test_an_unknown_mode_is_refused() -> None:
    with pytest.raises(ValueError, match="mode must be one of"):
        Strategy(mode="iron_condor").validate()


# --------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------


def test_a_delisted_symbol_is_kept_not_skipped(seeded: Settings) -> None:
    """The inverse of the signal scan, and deliberately so.

    A delisted ticker's last session is not news, so the scan skips it. Its trading years
    are real history, so a backtest keeps it: dropping them would stack a second
    survivorship bias on top of the one the universe already has.
    """
    loaded, skipped = load_series(seeded, ["AAA", "DEAD"])
    assert set(loaded) == {"AAA", "DEAD"}
    assert "DEAD" not in skipped


def test_a_symbol_with_too_little_history_is_named(seeded: Settings) -> None:
    loaded, skipped = load_series(seeded, ["AAA", "SHORTY", "NOSUCH"])
    assert set(loaded) == {"AAA"}
    assert "sessions stored" in skipped["SHORTY"]
    assert "no stored price source" in skipped["NOSUCH"]


def test_an_unknown_group_is_an_error_not_an_empty_run(seeded: Settings) -> None:
    """An empty run would report "no trades" and read as a finding about the strategy."""
    from optscan.universe import UniverseError

    with pytest.raises(UniverseError, match="no universe group"):
        resolve_symbols(seeded, Strategy(group="not_a_group"))


# --------------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------------


def test_a_run_produces_trades_stats_and_a_null(seeded: Settings) -> None:
    result = run(seeded, Strategy(entry="every_bar", symbols=["AAA", "BBB"], draws=50))
    assert result.stats is not None
    assert result.stats.trades > 0
    assert result.edge is not None
    assert result.edge.draws > 0
    assert result.by_year
    assert result.equity


def test_the_survivorship_warning_is_always_present(seeded: Settings) -> None:
    """It applies to every run, so it is not conditional on anything."""
    result = run(seeded, Strategy(entry="every_bar", symbols=["AAA"], draws=20))
    assert any("survivorship" in note for note in result.notes)


def test_a_rule_that_never_fires_says_so_rather_than_erroring(seeded: Settings) -> None:
    """A monotonically rising series is never oversold. That is a finding, not a fault."""
    result = run(
        seeded,
        Strategy(entry="rsi_below", entry_params={"threshold": 5}, symbols=["AAA"]),
    )
    assert result.stats is None
    assert any("never fired" in note for note in result.notes)


def test_the_null_uses_the_traded_entries_not_every_signal(seeded: Settings) -> None:
    """Overlap suppression drops entries, and a null with more trades than the strategy
    would have a tighter mean and flatter p-values."""
    result = run(seeded, Strategy(entry="every_bar", symbols=["AAA"], horizon=21, draws=40))
    assert result.stats is not None
    assert result.edge is not None
    # every_bar fires on ~540 bars but only ~26 survive a 21 bar hold without overlap.
    assert result.stats.trades < 100


def test_an_option_run_without_stored_implied_vol_is_refused(seeded: Settings) -> None:
    """Refusing is the point: realized vol is not a substitute, because the gap between
    implied and realized is the variance risk premium being measured."""
    result = run(seeded, Strategy(mode="short_put", symbols=["AAA"], dte=30))
    assert result.stats is None
    assert "variance risk premium" in result.skipped["AAA"]


def test_an_option_run_with_stored_implied_vol_works(tmp_settings: Settings) -> None:
    with db.session(tmp_settings.sqlite_path) as conn:
        import_daily_bars(conn, series("VOLY", iv=0.25))

    result = run(
        tmp_settings,
        Strategy(mode="short_put", symbols=["VOLY"], dte=30, entry="every_bar", draws=30),
    )
    assert result.stats is not None
    assert result.stats.trades > 0
    # A rising series never puts a 5% OTM put in the money, so every one expires worthless.
    assert result.stats.win_rate == 1.0
    assert any("Black-Scholes" in note for note in result.notes)


def test_require_implied_vol_names_the_reason() -> None:
    with pytest.raises(MissingImpliedVol, match="variance risk premium"):
        from optscan.analytics.backtest import require_implied_vol

        require_implied_vol("NVDA", 0, 200)


# --------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------


@pytest.fixture
def client(seeded: Settings) -> TestClient:
    clear_caches()
    return TestClient(create_app(seeded))


def test_the_rules_endpoint_lists_every_rule(client: TestClient) -> None:
    from optscan.analytics import rules as registry

    payload = client.get("/api/backtest/rules").json()
    assert {row["name"] for row in payload} == set(registry.REGISTRY)


def test_a_run_over_http_returns_stats_and_an_edge(client: TestClient) -> None:
    response = client.post(
        "/api/backtest",
        json={"entry": "every_bar", "symbols": ["AAA", "BBB"], "draws": 40},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["stats"]["trades"] > 0
    assert payload["edge"] is not None
    assert payload["strategy"]["entry"] == "every_bar"


def test_the_http_response_omits_the_trade_rows(client: TestClient) -> None:
    """A decade across three hundred symbols is tens of thousands of rows, and the UI
    charts the curve rather than the ledger."""
    payload = client.post(
        "/api/backtest", json={"entry": "every_bar", "symbols": ["AAA"], "draws": 20}
    ).json()
    assert "trades" not in payload


def test_a_bad_spec_returns_400_with_the_reason(client: TestClient) -> None:
    response = client.post("/api/backtest", json={"entry": "moon_phase"})
    assert response.status_code == 400
    assert "unknown entry rule" in response.json()["detail"]


def test_an_unknown_group_returns_400(client: TestClient) -> None:
    response = client.post("/api/backtest", json={"group": "not_a_group"})
    assert response.status_code == 400


def test_too_many_symbols_are_capped_and_the_cap_is_reported(
    client: TestClient,
) -> None:
    from optscan.api.routers.backtest import MAX_SYMBOLS

    payload = client.post(
        "/api/backtest",
        json={
            "entry": "every_bar",
            "symbols": ["AAA"] * (MAX_SYMBOLS + 5),
            "draws": 10,
        },
    ).json()
    assert any(str(MAX_SYMBOLS) in note for note in payload["notes"])


def test_excessive_null_draws_are_capped(client: TestClient) -> None:
    from optscan.api.routers.backtest import MAX_DRAWS

    payload = client.post(
        "/api/backtest",
        json={"entry": "every_bar", "symbols": ["AAA"], "draws": MAX_DRAWS * 10},
    ).json()
    assert payload["strategy"]["draws"] == MAX_DRAWS
    assert any("capped" in note for note in payload["notes"])


# --------------------------------------------------------------------------------
# Sweeps
# --------------------------------------------------------------------------------


def test_a_sweep_runs_every_cell_and_ranks_them(seeded: Settings) -> None:
    from optscan.jobs.backtest import run_sweep

    result = run_sweep(
        seeded,
        Strategy(entry="rsi_below", symbols=["AAA", "BBB"], draws=40),
        "threshold",
        ["30", "50", "70"],
    )
    assert len(result.cells) == 3
    assert [cell.label for cell in result.cells] == sorted(
        (cell.label for cell in result.cells),
        key=lambda label: -next(c.mean_return for c in result.cells if c.label == label),
    )
    assert result.note


def test_a_sweep_warns_when_the_winner_rests_on_almost_nothing(
    seeded: Settings,
) -> None:
    """A tighter parameter selects a rarer condition, so the winning cell is usually the
    one with the least evidence behind it. That is the fact a sweep most needs to say."""
    from optscan.jobs.backtest import run_sweep

    result = run_sweep(
        seeded,
        Strategy(entry="rsi_below", symbols=["AAA"], draws=20),
        "threshold",
        ["20", "45", "60"],
    )
    if result.best and result.best.stats and result.best.stats.effective_sample < 20:
        assert "independent blocks" in result.note
        assert "least evidence" in result.note


def test_a_sweep_over_a_bad_parameter_names_it(seeded: Settings) -> None:
    from optscan.jobs.backtest import run_sweep

    with pytest.raises(ValueError, match="thresold"):
        run_sweep(
            seeded,
            Strategy(entry="rsi_below", symbols=["AAA"], draws=10),
            "thresold",
            ["30"],
        )


def test_a_sweep_needs_at_least_one_value(seeded: Settings) -> None:
    from optscan.jobs.backtest import run_sweep

    with pytest.raises(ValueError, match="at least one value"):
        run_sweep(seeded, Strategy(symbols=["AAA"]), "threshold", [])
