"""The Phase 0 exit criterion: the app boots and says so."""

from __future__ import annotations

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
