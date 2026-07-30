"""Config is load bearing: a wrong path or a leaked secret is a Phase 1 outage."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from optscan.config import Settings, get_settings


def test_defaults_are_the_documented_ones(tmp_settings: Settings) -> None:
    assert tmp_settings.env == "dev"
    assert tmp_settings.provider == "yfinance"
    assert tmp_settings.log_level == "INFO"


def test_paths_derive_from_data_dir(tmp_settings: Settings) -> None:
    assert tmp_settings.snapshot_path.parent == tmp_settings.data_path
    assert tmp_settings.sqlite_path.name == "optscan.sqlite"
    assert tmp_settings.snapshot_path.is_absolute()


def test_relative_data_dir_resolves_against_repo_root(clean_env: None) -> None:
    settings = Settings(_env_file=None, data_dir="data")
    assert settings.data_path.is_absolute()
    assert settings.data_path.name == "data"


def test_ensure_dirs_creates_snapshot_dir(tmp_settings: Settings) -> None:
    assert not tmp_settings.snapshot_path.exists()
    tmp_settings.ensure_dirs()
    assert tmp_settings.snapshot_path.is_dir()


def test_env_vars_override_defaults(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPTSCAN_PROVIDER", "tradier")
    monkeypatch.setenv("OPTSCAN_RISK_FREE_RATE", "0.05")
    settings = Settings(_env_file=None)
    assert settings.provider == "tradier"
    assert settings.risk_free_rate == 0.05


def test_log_level_is_normalized(clean_env: None) -> None:
    assert Settings(_env_file=None, log_level="debug").log_level == "DEBUG"


def test_bad_log_level_fails_loudly(clean_env: None) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, log_level="chatty")


def test_unknown_provider_fails_loudly(clean_env: None) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, provider="some-guy-on-reddit")


def test_absurd_risk_free_rate_fails_loudly(clean_env: None) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, risk_free_rate=4.3)  # 430 percent, almost certainly a typo


def test_safe_summary_never_contains_a_secret(clean_env: None) -> None:
    settings = Settings(_env_file=None, tradier_token="super-secret-value")
    summary = settings.safe_summary()
    assert "super-secret-value" not in repr(summary)
    assert summary["credentials_set"] == ["tradier_token"]


def test_get_settings_is_cached(clean_env: None) -> None:
    assert get_settings() is get_settings()
