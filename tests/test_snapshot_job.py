"""The daily snapshot job, driven by a fake provider so it stays offline."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from optscan.config import Settings
from optscan.jobs.snapshot import capture_symbol, run_snapshot, select_expiries
from optscan.storage import db, snapshots
from tests.conftest import FakeProvider

# Thursday 30 July 2026, 15:45 in New York, inside the regular session.
DURING_SESSION = datetime(2026, 7, 30, 19, 45, tzinfo=UTC)
BEFORE_OPEN = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
SATURDAY = datetime(2026, 8, 1, 19, 45, tzinfo=UTC)


@pytest.fixture
def settings(tmp_path: Path, clean_env: None) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path, default_watchlist=("SPY",), max_retries=1)


class TestSelectExpiries:
    def test_takes_the_nearest_inside_the_window(self) -> None:
        asof = date(2026, 7, 30)
        expiries = [
            date(2026, 7, 31),
            date(2026, 8, 21),
            date(2026, 9, 18),
            date(2027, 12, 17),  # 505 days out
        ]
        assert select_expiries(expiries, asof, max_dte=400, max_expiries=10) == expiries[:3]

    def test_respects_the_count_cap(self) -> None:
        asof = date(2026, 7, 30)
        expiries = [date(2026, 8, d) for d in range(1, 20)]
        assert len(select_expiries(expiries, asof, max_dte=400, max_expiries=5)) == 5

    def test_drops_expiries_already_past(self) -> None:
        """Vendors keep listing yesterday's expiry for a while."""
        asof = date(2026, 7, 30)
        selected = select_expiries(
            [date(2026, 7, 29), date(2026, 7, 30), date(2026, 8, 21)],
            asof,
            max_dte=400,
            max_expiries=10,
        )
        assert selected == [date(2026, 7, 30), date(2026, 8, 21)]

    def test_empty_when_nothing_qualifies(self) -> None:
        assert select_expiries([], date(2026, 7, 30), max_dte=400, max_expiries=10) == []


class TestCaptureSymbol:
    def test_captures_every_selected_expiry(
        self, settings: Settings, fake_provider: FakeProvider
    ) -> None:
        snapshot = capture_symbol(fake_provider, "SPY", settings, date(2026, 7, 30))
        assert snapshot.symbol == "SPY"
        assert len(snapshot.chains) == 2
        assert snapshot.partial is False
        assert snapshot.contract_count > 100
        assert snapshot.source == "fake"

    def test_a_failed_expiry_yields_a_flagged_partial(
        self, settings: Settings, frozen_snapshot
    ) -> None:
        """Ten of twelve expiries is worth keeping. Calling it complete is not."""
        provider = FakeProvider(frozen_snapshot, fail_expiries={frozen_snapshot.expiries[1]})
        snapshot = capture_symbol(provider, "SPY", settings, date(2026, 7, 30))
        assert snapshot.partial is True
        assert len(snapshot.chains) == 1
        assert snapshot.notes and str(frozen_snapshot.expiries[1]) in snapshot.notes[0]

    def test_total_failure_raises(self, settings: Settings, frozen_snapshot) -> None:
        provider = FakeProvider(frozen_snapshot, fail_expiries=set(frozen_snapshot.expiries))
        with pytest.raises(Exception, match="every expiry failed"):
            capture_symbol(provider, "SPY", settings, date(2026, 7, 30))


class TestRunSnapshot:
    def test_writes_parquet_and_records_the_run(
        self, settings: Settings, fake_provider: FakeProvider
    ) -> None:
        report = run_snapshot(settings, provider=fake_provider, now=DURING_SESSION, symbols=["SPY"])

        assert report.session_date == date(2026, 7, 30)
        assert len(report.succeeded) == 1
        assert report.total_contracts > 100

        files = snapshots.snapshot_files(settings.snapshot_path)
        assert len(files) == 1

        frame = snapshots.read_snapshots(settings.snapshot_path)
        assert len(frame) == report.total_contracts

        with db.session(settings.sqlite_path) as conn:
            assert db.has_run(conn, "SPY", date(2026, 7, 30)) is True

    def test_refuses_to_capture_before_the_open(
        self, settings: Settings, fake_provider: FakeProvider
    ) -> None:
        """A pre open capture would file yesterday's close under today's session."""
        report = run_snapshot(settings, provider=fake_provider, now=BEFORE_OPEN, symbols=["SPY"])
        assert report.skipped_reason is not None
        assert report.session_date is None
        assert snapshots.snapshot_files(settings.snapshot_path) == []

    def test_refuses_to_capture_on_a_weekend(
        self, settings: Settings, fake_provider: FakeProvider
    ) -> None:
        report = run_snapshot(settings, provider=fake_provider, now=SATURDAY, symbols=["SPY"])
        assert report.skipped_reason is not None
        assert "force" in report.skipped_reason

    def test_force_captures_off_session_and_labels_it_honestly(
        self, settings: Settings, fake_provider: FakeProvider
    ) -> None:
        report = run_snapshot(
            settings, provider=fake_provider, now=SATURDAY, symbols=["SPY"], force=True
        )
        assert report.session_date == date(2026, 8, 1)
        assert len(report.succeeded) == 1

    def test_skips_a_symbol_already_captured_today(
        self, settings: Settings, fake_provider: FakeProvider
    ) -> None:
        run_snapshot(settings, provider=fake_provider, now=DURING_SESSION, symbols=["SPY"])
        second = run_snapshot(settings, provider=fake_provider, now=DURING_SESSION, symbols=["SPY"])
        assert second.results == []
        assert len(snapshots.snapshot_files(settings.snapshot_path)) == 1

    def test_recapture_writes_a_second_file(
        self, settings: Settings, fake_provider: FakeProvider
    ) -> None:
        """Both files survive even when the two runs land in the same clock second."""
        run_snapshot(settings, provider=fake_provider, now=DURING_SESSION, symbols=["SPY"])
        run_snapshot(
            settings,
            provider=fake_provider,
            now=DURING_SESSION,
            symbols=["SPY"],
            skip_existing=False,
        )
        assert len(snapshots.snapshot_files(settings.snapshot_path)) == 2

    def test_one_bad_symbol_does_not_stop_the_others(
        self, settings: Settings, frozen_snapshot
    ) -> None:
        provider = FakeProvider(frozen_snapshot, fail_symbols={"BADSYM"})
        report = run_snapshot(
            settings, provider=provider, now=DURING_SESSION, symbols=["BADSYM", "SPY"]
        )
        assert [r.symbol for r in report.failed] == ["BADSYM"]
        assert [r.symbol for r in report.succeeded] == ["SPY"]

    def test_a_failure_is_recorded_so_the_gap_is_explainable(
        self, settings: Settings, frozen_snapshot
    ) -> None:
        provider = FakeProvider(frozen_snapshot, fail_symbols={"BADSYM"})
        run_snapshot(settings, provider=provider, now=DURING_SESSION, symbols=["BADSYM"])
        with db.session(settings.sqlite_path) as conn:
            row = db.recent_runs(conn)[0]
        assert row["symbol"] == "BADSYM"
        assert "SymbolNotFound" in row["error"]

    def test_uses_the_watchlist_when_no_symbols_are_given(
        self, settings: Settings, fake_provider: FakeProvider
    ) -> None:
        with db.session(settings.sqlite_path) as conn:
            db.seed_watchlist(conn, settings.default_watchlist)
            db.add_symbol(conn, "QQQ")
        report = run_snapshot(settings, provider=fake_provider, now=DURING_SESSION)
        assert sorted(r.symbol for r in report.results) == ["QQQ", "SPY"]

    def test_empty_watchlist_is_reported_not_crashed(
        self, tmp_path: Path, clean_env: None, fake_provider: FakeProvider
    ) -> None:
        settings = Settings(_env_file=None, data_dir=tmp_path, default_watchlist=())
        report = run_snapshot(settings, provider=fake_provider, now=DURING_SESSION)
        assert report.skipped_reason == "watchlist is empty"
