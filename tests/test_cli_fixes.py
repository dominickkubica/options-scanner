"""CLI paths the 2026-09-12 audit found broken or unguarded (BUG-0004, SEC-0003)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from optscan.cli import _signal_symbols, main
from optscan.universe import load_universe


@pytest.fixture
def isolated(clean_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OPTSCAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPTSCAN_DEFAULT_WATCHLIST", "SPY")
    return tmp_path


def test_a_group_resolves_to_its_symbols() -> None:
    """The path that raised AttributeError on every run with --group."""
    args = argparse.Namespace(symbols=None, group="etf")
    assert _signal_symbols(None, args) == load_universe().symbols("etf")


def test_an_unknown_group_names_the_real_ones_instead_of_a_traceback(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["signals", "--group", "no-such-group", "--dry-run"]) == 1
    out = capsys.readouterr().out
    assert "unknown group" in out
    assert "Available" in out


def test_watchlist_add_refuses_a_path_rather_than_storing_it(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["watchlist", "add", "A/../../pwn"]) == 1
    assert "refused" in capsys.readouterr().out
    assert main(["watchlist", "list"]) == 0
    assert "PWN" not in capsys.readouterr().out
