"""Entry rules, and the equivalence that keeps three implementations honest.

The arithmetic in `rules.py` is a rolling restatement of the scalar arithmetic in
`levels.py`, which is itself already pinned against the frontend's `compute.js`. Three
implementations of one formula is a standing hazard: the day they drift, the chart draws
one line, the alert fires on a second and the backtest measures a third, and nothing
fails loudly.

So every rolling indicator is checked against its scalar twin **at many points in the
series, not just the last one**. Checking only the final bar would pass for an
implementation that was wrong everywhere except at the end, which is the shape a
seeding or alignment bug actually takes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from optscan.analytics import levels, rules
from optscan.analytics.signals import is_squeezed, volume_ratio
from optscan.models import PriceBar

FIXTURE = Path(__file__).parent / "fixtures" / "spy_daily_bars.json"
BASE = datetime(2020, 1, 2, tzinfo=UTC)

#: Where in the series to compare. Spread out, and deliberately including points close
#: to the warmup where alignment bugs live.
CHECKPOINTS = (40, 60, 100, 250, 500, 900)


@pytest.fixture(scope="session")
def spy_bars() -> list[PriceBar]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [
        PriceBar(
            symbol=payload["symbol"],
            ts=datetime.fromisoformat(row["ts"]),
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            volume=row["volume"],
            fetched_at=BASE,
            source="fixture",
        )
        for row in payload["bars"]
    ]


def points(bars: list[PriceBar]) -> list[int]:
    return [index for index in CHECKPOINTS if index < len(bars)]


# --------------------------------------------------------------------------------
# The rolling forms must equal the scalar forms
# --------------------------------------------------------------------------------


def test_rsi_series_matches_the_scalar_rsi_at_every_checkpoint(spy_bars) -> None:
    series = rules.rsi_series(spy_bars, 14)
    for index in points(spy_bars):
        assert series[index] == pytest.approx(levels.rsi(spy_bars[: index + 1], 14))


def test_moving_average_series_matches_the_scalar(spy_bars) -> None:
    for period in (20, 50, 200):
        series = rules.moving_average_series(spy_bars, period)
        for index in points(spy_bars):
            if index + 1 < period:
                continue
            assert series[index] == pytest.approx(
                levels.moving_average(spy_bars[: index + 1], period)
            )


def test_ema_series_matches_the_scalar(spy_bars) -> None:
    series = rules.ema_series(spy_bars, 20)
    for index in points(spy_bars):
        assert series[index] == pytest.approx(levels.ema(spy_bars[: index + 1], 20))


def test_atr_series_matches_the_scalar(spy_bars) -> None:
    series = rules.atr_series(spy_bars, 14)
    for index in points(spy_bars):
        assert series[index] == pytest.approx(levels.atr(spy_bars[: index + 1], 14))


def test_squeeze_series_matches_the_signal_module(spy_bars) -> None:
    """The backtest and the alert must agree about what a squeeze is."""
    series = rules.squeeze_series(spy_bars, 20)
    for index in points(spy_bars):
        assert series[index] == is_squeezed(spy_bars[: index + 1])


def test_volume_ratio_series_matches_the_signal_module(spy_bars) -> None:
    series = rules.volume_ratio_series(spy_bars, 20)
    for index in points(spy_bars):
        assert series[index] == pytest.approx(volume_ratio(spy_bars[: index + 1], 20))


def test_a_rolling_indicator_is_none_before_its_window_is_full(spy_bars) -> None:
    """Leading with a partial window is a different statistic wearing the same label."""
    series = rules.rsi_series(spy_bars, 14)
    assert all(value is None for value in series[:14])
    assert series[14] is not None


# --------------------------------------------------------------------------------
# No lookahead
# --------------------------------------------------------------------------------


def test_a_rule_decision_does_not_depend_on_later_bars(spy_bars) -> None:
    """Truncating the series must not change any decision made before the cut.

    This is the property that makes a backtest a backtest. It is checked by running each
    rule on the full history and on a prefix, and requiring the prefix's answers to be a
    prefix of the full run's.
    """
    cut = 700
    for name in ("rsi_below", "rsi_above", "squeeze", "volume_surge", "above_ma"):
        rule = rules.resolve(name)
        full = [index for index in rule(spy_bars) if index < cut]
        truncated = rule(spy_bars[:cut])
        assert full == truncated, f"{name} changed its mind about the past"


def test_the_level_rule_does_not_see_future_levels(spy_bars) -> None:
    """The expensive one, and the one where reusing a single build would be lookahead."""
    cut = 700
    rule = rules.resolve("level_break")
    full = [index for index in rule(spy_bars) if index < cut]
    truncated = rule(spy_bars[:cut])
    assert full == truncated


# --------------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------------


def test_every_registered_rule_runs_and_returns_sorted_indices(spy_bars) -> None:
    for name in rules.REGISTRY:
        fired = rules.resolve(name)(spy_bars)
        assert fired == sorted(fired), f"{name} returned unsorted indices"
        assert all(0 <= index < len(spy_bars) for index in fired)
        assert all(index >= rules.WARMUP for index in fired), (
            f"{name} fired before the warmup, so some indicator was still filling"
        )


def test_an_unknown_rule_names_what_exists() -> None:
    with pytest.raises(ValueError, match="unknown entry rule"):
        rules.resolve("moon_phase")


def test_an_unknown_parameter_is_refused_rather_than_ignored() -> None:
    """Silently dropping a parameter would run a different strategy than the one asked
    for, and report it under the name that was asked for."""
    with pytest.raises(ValueError, match="not 'thresold'"):
        rules.resolve("rsi_below", {"thresold": 25})


def test_parameters_actually_change_the_result(spy_bars) -> None:
    loose = rules.resolve("rsi_below", {"threshold": 45})(spy_bars)
    tight = rules.resolve("rsi_below", {"threshold": 20})(spy_bars)
    assert len(loose) > len(tight)


def test_describe_lists_every_rule_with_its_defaults() -> None:
    described = {row["name"] for row in rules.describe()}
    assert described == set(rules.REGISTRY)
    for row in rules.describe():
        assert row["label"] and row["about"]


def test_the_control_rule_fires_on_essentially_every_bar(spy_bars) -> None:
    fired = rules.resolve("every_bar")(spy_bars)
    assert len(fired) == len(spy_bars) - rules.WARMUP


def test_squeeze_is_edge_triggered_not_state_triggered(spy_bars) -> None:
    """A squeeze runs a median of four sessions, so a state rule enters four times for
    one thing happening and every entry overlaps the last."""
    states = rules.squeeze_series(spy_bars, 20)
    holding = sum(1 for value in states if value)
    starts = rules.resolve("squeeze")(spy_bars)
    assert len(starts) < holding / 2
