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
    def test_lists_every_job_without_installing(
        self, isolated: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Registering a scheduled task changes the machine, so it needs a flag."""
        from optscan.jobs import schedule

        # Stubbed so the listing does not shell out to Windows during the test run.
        monkeypatch.setattr(schedule, "task_states", lambda names: {})

        assert main(["schedule"]) == 0
        out = capsys.readouterr().out
        for key in ("snapshot", "record", "resolve", "backup", "manage"):
            assert key in out
        assert "not registered" in out
        assert "--install" in out

    def test_says_why_manage_is_not_installed_by_default(
        self, isolated: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one job with a cost states it, and stays out of the default set."""
        from optscan.jobs import schedule

        monkeypatch.setattr(schedule, "task_states", lambda names: {})

        assert main(["schedule"]) == 0
        out = capsys.readouterr().out
        assert "throttles silently" in out
        assert "manage" not in out.rsplit("Install with:", 1)[-1]


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


class TestServeCommand:
    def test_binds_the_configured_host_and_port_without_starting_a_server(
        self, isolated: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The command is wiring, so the test checks the wiring and never opens a port."""
        import uvicorn

        from optscan.api import deps

        captured: dict[str, object] = {}
        monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(app=app, **kw))
        # Whether a build exists depends on whether anyone has run `npm run build`, so
        # the branch is chosen here rather than inherited from the working tree.
        monkeypatch.setattr(deps, "frontend_dist", lambda: None)

        assert main(["serve", "--port", "8123"]) == 0
        assert captured["app"] == "optscan.api.app:app"
        assert captured["port"] == 8123
        assert captured["reload"] is False

        out = capsys.readouterr().out
        assert "8123/api/health" in out
        # Without a build the message has to say where the UI actually is.
        assert "5173" in out

    def test_points_at_itself_when_the_frontend_is_built(
        self, isolated: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import uvicorn

        from optscan.api import deps

        monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)
        monkeypatch.setattr(deps, "frontend_dist", lambda: tmp_dist(isolated))

        assert main(["serve"]) == 0
        out = capsys.readouterr().out
        assert "Dashboard on" in out
        assert "5173" not in out


def tmp_dist(root: Path) -> Path:
    dist = root / "dist"
    dist.mkdir(exist_ok=True)
    return dist
