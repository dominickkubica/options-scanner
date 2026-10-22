"""Lookups that have cost this project a defect, written as assertions.

A trap written in prose helps only someone who reads it before making the mistake. Each
one here is the checkable form of an entry in `map/notes.md`, which names the test, so
the map points at the guard rather than standing in for it.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from optscan.config import REPO_ROOT

SOURCES = [REPO_ROOT / "src" / "optscan", REPO_ROOT / "scripts"]


def _python_files() -> list[Path]:
    return [
        path
        for root in SOURCES
        for path in sorted(root.rglob("*.py"))
        if "__pycache__" not in path.parts
    ]


def _calls(tree: ast.AST, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == name)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
        )
    ]


def test_every_scorer_call_states_its_iv_history() -> None:
    """`analyze_snapshot` without `iv_history` silently scores with no IV rank.

    That is how the validation study logged a null rank on every row for six weeks.
    A caller that does not want the rank says `iv_history=None`, out loud.
    """
    silent = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _calls(tree, "analyze_snapshot"):
            if not any(keyword.arg == "iv_history" for keyword in call.keywords):
                silent.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{call.lineno}")
    assert not silent, f"analyze_snapshot called without iv_history at: {silent}"


def test_iv_history_is_never_read_through_the_price_source() -> None:
    """`preferred_source` names the *price* source, which is Alpaca for most symbols.

    Alpaca has no IV30, so a function that hands it to `iv30_series` gets an empty
    history for most of its symbols. A probe did exactly that and ran on 3 of 23. Use
    `jobs.backtest.load_implied`.
    """
    offenders = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if _calls(function, "iv30_series") and _calls(function, "preferred_source"):
                relative = path.relative_to(REPO_ROOT).as_posix()
                offenders.append(f"{relative}:{function.name}")
    assert not offenders, f"IV read alongside the price source in: {offenders}"


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.syspath_prepend(str(REPO_ROOT / "scripts" / "studies"))
    sys.modules.pop("harness", None)
    import harness as module

    yield module
    sys.modules.pop("harness", None)


def test_a_study_refuses_to_run_on_a_fraction_of_its_symbols(harness, monkeypatch) -> None:
    """The guard that would have stopped the 3-of-23 probe: lose most symbols, raise."""
    requested = [f"S{i}" for i in range(23)]

    def fake_load(settings, symbols):
        bar = SimpleNamespace(ts=SimpleNamespace(date=lambda: harness.DEV[0]))
        return {symbol: [bar] * (harness.MIN_WINDOW_BARS + 1) for symbol in symbols}, {}

    def fake_run(settings, strategy):
        harness.job.load_series(settings, requested)
        skipped = {symbol: "no IV" for symbol in requested[3:]}
        return SimpleNamespace(skipped=skipped, stats=None, edge=None)

    monkeypatch.setattr(harness.job, "load_series", fake_load)
    monkeypatch.setattr(harness.job, "run", fake_run)
    monkeypatch.setattr(harness, "get_settings", lambda: None)
    study = harness.Study("guard")

    with pytest.raises(harness.TooFewSymbols, match="3 of 23"):
        study.run("probe", entry="x")
    assert study.run("probe", entry="x", max_missing=0.9)["symbols"] == 3
