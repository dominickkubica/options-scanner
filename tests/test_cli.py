"""CLI: the app boots and says so, and every subcommand is wired to something real."""

from __future__ import annotations

from pathlib import Path

import pytest

from optscan import __version__
from optscan.cli import build_parser, main, startup
from optscan.config import Settings


def test_version_flag_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_startup_logs_a_line_and_creates_dirs(
    tmp_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    startup(tmp_settings)
    assert tmp_settings.snapshot_path.is_dir()
    assert "optscan starting" in capsys.readouterr().out


def test_startup_with_check_does_not_touch_disk(tmp_settings: Settings) -> None:
    startup(tmp_settings, create_dirs=False)
    assert not tmp_settings.snapshot_path.exists()


def test_startup_log_does_not_leak_secrets(
    clean_env: None, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path, tradier_token="do-not-log-me")
    startup(settings)
    assert "do-not-log-me" not in capsys.readouterr().out


def test_main_returns_zero(clean_env: None, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPTSCAN_DATA_DIR", str(tmp_path))
    assert main([]) == 0


@pytest.fixture
def isolated(clean_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at a throwaway data directory with a one symbol watchlist."""
    monkeypatch.setenv("OPTSCAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPTSCAN_DEFAULT_WATCHLIST", "SPY")
    return tmp_path


class TestWatchlistCommand:
    def test_list_seeds_from_config_on_first_use(
        self, isolated: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["watchlist", "list"]) == 0
        assert "SPY" in capsys.readouterr().out

    def test_add_and_remove(self, isolated: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["watchlist", "add", "qqq", "iwm"]) == 0
        assert main(["watchlist", "list"]) == 0
        out = capsys.readouterr().out
        assert "IWM, QQQ, SPY" in out

        assert main(["watchlist", "remove", "QQQ"]) == 0
        assert "IWM, SPY" in capsys.readouterr().out


class TestStatusCommand:
    def test_reports_market_and_watchlist(
        self, isolated: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["status"]) == 0
        out = capsys.readouterr().out
        assert "provider" in out
        assert "watchlist" in out
        assert "capture at" in out


class TestScheduleCommand:
    def test_prints_the_command_without_installing(
        self, isolated: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Registering a scheduled task changes the machine, so it needs a flag."""
        assert main(["schedule"]) == 0
        out = capsys.readouterr().out
        assert "schtasks /Create" in out
        assert "optscan snapshot" in out


class TestSnapshotCommand:
    def test_reports_a_skip_without_touching_disk(
        self, isolated: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Runs on a closed market: the job declines and the CLI says so, exit code 0."""
        from datetime import UTC, datetime

        import optscan.jobs.snapshot as job

        saturday = datetime(2026, 8, 1, 19, 45, tzinfo=UTC)
        real_run = job.run_snapshot
        monkeypatch.setattr(
            job,
            "run_snapshot",
            lambda settings, **kw: real_run(settings, **{**kw, "now": saturday}),
        )
        assert main(["snapshot"]) == 0
        assert "Skipped" in capsys.readouterr().out
