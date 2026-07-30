"""Shared fixtures.

Settings are read from the process environment, so tests that care about config must
isolate themselves from whatever is in the developer's .env.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from optscan.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """Keep the settings singleton from leaking between tests."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every OPTSCAN_ variable so defaults are what is under test."""
    for key in list(os.environ):
        if key.startswith("OPTSCAN_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def tmp_settings(tmp_path: Path, clean_env: None) -> Settings:
    """Settings pointed at a throwaway data directory, ignoring any local .env."""
    return Settings(_env_file=None, data_dir=tmp_path)
