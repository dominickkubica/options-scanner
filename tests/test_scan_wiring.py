"""The seam between storage and the pure scan pipeline, and the scan CLI.

Covers jobs/load.py and jobs/scan.py, which is where a stored parquet row becomes a
model again. Nothing here touches the network: snapshots are written to a temporary
directory by the real storage layer and read back.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from optscan.config import Settings
from optscan.jobs.load import latest_snapshot, snapshot_from_frame
from optscan.jobs.scan import STALE_AFTER_HOURS, gather, run_scan
from optscan.models import ChainSnapshot, SymbolEvents
from optscan.screener.config import ScreenConfig
from optscan.screener.filters import Rejection
from optscan.storage import read_snapshots, write_snapshot
from tests.conftest import FakeProvider


@pytest.fixture
def settings(tmp_path: Path, clean_env: None) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path, default_watchlist=("SPY",))


@pytest.fixture
def wide_config() -> ScreenConfig:
    return ScreenConfig.model_validate(
        {
            "filters": {
                "dte": {"min_dte": 0, "max_dte": 60},
                "premium": {"min_annualized_return": 0.0},
            }
        }
    )


@pytest.fixture
def stored(frozen_snapshot: ChainSnapshot, settings: Settings) -> Settings:
    settings.ensure_dirs()
    write_snapshot(frozen_snapshot, settings.snapshot_path)
    return settings


class EventfulProvider(FakeProvider):
    """A provider that answers the corporate calendar."""

    name = "fake"

    def get_events(self, symbol: str) -> SymbolEvents:
        return SymbolEvents(
            symbol=symbol,
            earnings_date=date(2026, 8, 1),
            ex_dividend_date=date(2026, 8, 15),
            dividend_amount=1.90,
            fetched_at=datetime.now(UTC),
            source=self.name,
        )


class TestLoad:
    def test_round_trips_a_snapshot_through_storage(
        self, frozen_snapshot: ChainSnapshot, stored: Settings
    ) -> None:
        loaded = latest_snapshot(stored.snapshot_path, "SPY")
        assert loaded is not None
        assert loaded.symbol == frozen_snapshot.symbol
        assert loaded.session_date == frozen_snapshot.session_date
        assert loaded.contract_count == frozen_snapshot.contract_count
        assert len(loaded.chains) == len(frozen_snapshot.chains)

    def test_provenance_survives_the_round_trip(
        self, frozen_snapshot: ChainSnapshot, stored: Settings
    ) -> None:
        """A rebuilt snapshot that has lost its source or capture time is unusable."""
        loaded = latest_snapshot(stored.snapshot_path, "SPY")
        assert loaded.source == frozen_snapshot.source
        assert loaded.fetched_at == frozen_snapshot.fetched_at

    def test_contract_detail_survives(
        self, frozen_snapshot: ChainSnapshot, stored: Settings
    ) -> None:
        loaded = latest_snapshot(stored.snapshot_path, "SPY")
        original = next(iter(frozen_snapshot.contracts()))
        rebuilt = loaded.chains[0].get(original.strike, original.right)
        assert rebuilt is not None
        assert rebuilt.contract_symbol == original.contract_symbol
        assert rebuilt.open_interest == original.open_interest
        assert rebuilt.bid == pytest.approx(original.bid)
        assert rebuilt.vendor_iv == pytest.approx(original.vendor_iv)

    def test_the_most_recent_capture_wins(
        self, frozen_snapshot: ChainSnapshot, settings: Settings
    ) -> None:
        settings.ensure_dirs()
        older = frozen_snapshot.model_copy(
            update={"fetched_at": frozen_snapshot.fetched_at - timedelta(hours=3)}
        )
        write_snapshot(older, settings.snapshot_path)
        write_snapshot(frozen_snapshot, settings.snapshot_path)

        loaded = latest_snapshot(settings.snapshot_path, "SPY")
        assert loaded.fetched_at == frozen_snapshot.fetched_at

    def test_no_data_is_none_not_an_error(self, settings: Settings) -> None:
        assert latest_snapshot(settings.snapshot_path, "NOPE") is None

    def test_an_empty_frame_is_none(self) -> None:
        import pandas as pd

        assert snapshot_from_frame(pd.DataFrame()) is None

    def test_unparseable_rows_are_dropped_not_fatal(self, stored: Settings) -> None:
        """One bad row in a 500 row capture should cost that row."""
        frame = read_snapshots(stored.snapshot_path)
        frame.loc[frame.index[0], "strike"] = -1.0
        rebuilt = snapshot_from_frame(frame)
        assert rebuilt is not None
        assert rebuilt.contract_count == len(frame) - 1


class TestGather:
    def test_loads_stored_snapshots_and_history(self, stored: Settings) -> None:
        inputs = gather(stored, ["SPY"], with_events=False)
        assert len(inputs.snapshots) == 1
        assert inputs.missing == []
        assert "SPY" in inputs.iv_histories

    def test_a_symbol_with_no_data_is_reported_not_skipped_silently(self, stored: Settings) -> None:
        inputs = gather(stored, ["SPY", "NOPE"], with_events=False)
        assert inputs.missing == ["NOPE"]
        assert len(inputs.snapshots) == 1

    def test_events_are_fetched_when_asked(
        self, stored: Settings, frozen_snapshot: ChainSnapshot
    ) -> None:
        provider = EventfulProvider(frozen_snapshot)
        inputs = gather(stored, ["SPY"], provider=provider, with_events=True)
        assert inputs.events["SPY"].earnings == date(2026, 8, 1)
        assert inputs.events["SPY"].dividend_amount == pytest.approx(1.90)

    def test_a_provider_without_a_calendar_is_tolerated(
        self, stored: Settings, frozen_snapshot: ChainSnapshot
    ) -> None:
        """FakeProvider inherits the base class refusal. No events is not an error."""
        inputs = gather(stored, ["SPY"], provider=FakeProvider(frozen_snapshot))
        assert inputs.events == {}
        assert len(inputs.snapshots) == 1

    def test_live_fetches_from_the_provider_instead_of_disk(
        self, settings: Settings, future_snapshot: ChainSnapshot
    ) -> None:
        """Nothing is on disk, so anything found came from the provider.

        `future_snapshot` rather than the frozen one: the live path selects expiries
        that are still ahead, and the frozen capture's are now in the past.
        """
        settings.ensure_dirs()
        provider = FakeProvider(future_snapshot)
        inputs = gather(settings, ["SPY"], live=True, provider=provider, with_events=False)
        assert len(inputs.snapshots) == 1
        assert inputs.snapshots[0].source == "fake"


class TestRunScan:
    def test_uses_the_watchlist_when_no_symbols_are_given(
        self, stored: Settings, wide_config: ScreenConfig
    ) -> None:
        result, inputs = run_scan(stored, wide_config, with_events=False)
        assert inputs.snapshots
        assert result.opportunities
        assert result.symbols_scanned == ["SPY"]

    def test_no_data_produces_an_empty_result_not_a_crash(
        self, settings: Settings, wide_config: ScreenConfig
    ) -> None:
        result, inputs = run_scan(settings, wide_config, symbols=["NOPE"], with_events=False)
        assert result.opportunities == []
        assert inputs.missing == ["NOPE"]

    def test_events_reach_the_screen(
        self, stored: Settings, frozen_snapshot, wide_config: ScreenConfig
    ) -> None:
        """Earnings on 1 August falls before both fixture expiries, 3 and 7 August, so
        with the default event filter every candidate is excluded. Without the event
        fetch the same scan finds plenty, which is the point: the calendar is doing
        the work, not the filters."""
        provider = EventfulProvider(frozen_snapshot)
        with_events, _ = run_scan(stored, wide_config, provider=provider, with_events=True)
        without, _ = run_scan(stored, wide_config, with_events=False)

        assert without.opportunities
        assert with_events.opportunities == []
        assert with_events.tally.counts[Rejection.EARNINGS_BEFORE_EXPIRY] > 0

    def test_stale_quotes_are_reported(self, stored: Settings, wide_config) -> None:
        later = datetime(2026, 8, 4, 19, 45, tzinfo=UTC)
        result, _ = run_scan(stored, wide_config, with_events=False, now=later)
        assert result.stale_symbols["SPY"] / 3600.0 > STALE_AFTER_HOURS


class TestScanCommand:
    def test_prints_a_ranked_table(
        self, stored: Settings, monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
    ) -> None:
        from optscan.cli import main

        monkeypatch.setenv("OPTSCAN_DATA_DIR", str(tmp_path))
        config = tmp_path / "screen.yaml"
        config.write_text(
            "filters:\n  dte:\n    min_dte: 0\n  premium:\n    min_annualized_return: 0.0\n",
            encoding="utf-8",
        )
        assert main(["scan", "--symbol", "SPY", "--no-events", "--config", str(config)]) == 0

        out = capsys.readouterr().out
        assert "score" in out
        assert "candidates considered" in out
        assert "not validated against outcomes" in out

    def test_an_empty_result_still_explains_itself(
        self, stored: Settings, monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
    ) -> None:
        """A blank table with no rejection tally is how a screener loses its user.

        The screen is passed in so the emptiness is deliberate. This test used to lean
        on the default 21 day floor excluding the fixture, and it broke the day that
        floor moved to 0, which is the wrong reason for a test about explaining an
        empty table to fail.
        """
        from optscan.cli import main

        monkeypatch.setenv("OPTSCAN_DATA_DIR", str(tmp_path))
        config = tmp_path / "far_dated.yaml"
        config.write_text("filters:\n  dte:\n    min_dte: 300\n    max_dte: 400\n")

        assert main(["scan", "--symbol", "SPY", "--no-events", "--config", str(config)]) == 0
        out = capsys.readouterr().out
        assert "No candidates passed the screen" in out
        assert "dte_too_short" in out

    def test_missing_data_says_what_to_do(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
    ) -> None:
        from optscan.cli import main

        monkeypatch.setenv("OPTSCAN_DATA_DIR", str(tmp_path))
        assert main(["scan", "--symbol", "NOPE", "--no-events"]) == 0
        assert "Run `optscan snapshot` first" in capsys.readouterr().out

    def test_an_unknown_watchlist_is_an_error(
        self, monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
    ) -> None:
        from optscan.cli import main

        monkeypatch.setenv("OPTSCAN_DATA_DIR", str(tmp_path))
        assert main(["scan", "--watchlist", "aggressive"]) == 1
        assert "Unknown watchlist" in capsys.readouterr().out

    def test_config_command_prints_every_knob(
        self, monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
    ) -> None:
        from optscan.cli import main

        monkeypatch.setenv("OPTSCAN_DATA_DIR", str(tmp_path))
        assert main(["config"]) == 0
        out = capsys.readouterr().out
        assert "min_dte" in out
        assert "per_contract" in out
        assert "vertical_mispricing_enabled" in out


#: Keys the committed screen.yaml is allowed to differ from the code defaults on, and
#: what it must say instead. An allowlist rather than a loosened comparison: the test
#: below exists to catch drift, and "some values may differ" would catch nothing.
DELIBERATE_OVERRIDES = {
    # The default is a typical per-contract commission for an unknown broker. This
    # account trades through Robinhood, which charges none, so the committed file
    # carries pass-through regulatory fees instead. The default stays conservative
    # because understating fees is the dangerous direction: they are fixed per
    # contract, so a fee that is too low systematically flatters narrow trades.
    "costs.per_contract": 0.03,
}


def test_the_committed_screen_yaml_matches_the_documented_defaults() -> None:
    """screen.yaml exists to show every knob. If it drifts from the defaults it is
    documentation that lies.

    Deliberate differences are declared above and checked by value, so a real drift
    still fails and an intentional override has to be written down to pass.
    """
    from optscan.config import REPO_ROOT

    committed = ScreenConfig.load(REPO_ROOT / "screen.yaml")
    defaults = ScreenConfig()

    patched = {}
    for path, expected in DELIBERATE_OVERRIDES.items():
        section, key = path.split(".")
        assert getattr(getattr(committed, section), key) == expected, (
            f"{path} is declared as a deliberate override worth {expected}"
        )
        # Rebuild the section with the default put back, so everything else is still
        # compared exactly. The models are frozen, hence a copy rather than a set.
        patched[section] = getattr(committed, section).model_copy(
            update={key: getattr(getattr(defaults, section), key)}
        )

    assert committed.model_copy(update=patched) == defaults
