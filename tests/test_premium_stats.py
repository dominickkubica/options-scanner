"""The premium study's statistics, on trades small enough to check by hand."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from optscan.analytics.premium_stats import (
    MIN_SESSIONS,
    TAIL_DAYS,
    benjamini_hochberg,
    cell_stats,
    paired_difference,
)

START = date(2025, 1, 6)  # a Monday


def sessions(count: int) -> list[date]:
    days, day = [], START
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def trade(day: date, pnl: float, underlying: str = "QQQ", credit: float = 50.0):
    return SimpleNamespace(session=day, underlying=underlying, pnl=pnl, r=pnl / credit)


class TestCells:
    def test_a_short_cell_reports_no_interval(self) -> None:
        stats = cell_stats([trade(day, 10.0) for day in sessions(MIN_SESSIONS - 1)])
        assert stats is not None
        assert not stats.reportable
        assert stats.ci_low is None and stats.p_value is None

    def test_two_underlyings_on_one_day_are_one_session(self) -> None:
        days = sessions(40)
        both = [trade(day, 5.0, name) for day in days for name in ("QQQ", "SPY")]
        stats = cell_stats(both)
        assert stats is not None
        assert (stats.trades, stats.sessions) == (80, 40)

    def test_a_steady_winner_has_a_tight_interval_away_from_zero(self) -> None:
        stats = cell_stats([trade(day, 10.0 + (i % 3)) for i, day in enumerate(sessions(60))])
        assert stats is not None and stats.reportable
        assert 10.0 <= stats.ci_low <= stats.mean_pnl <= stats.ci_high <= 12.0
        assert stats.p_value == 0.0

    def test_the_tail_rows_and_the_drawdown(self) -> None:
        days = sessions(40)
        values = [10.0] * 40
        values[3] = -30.0
        values[4] = -10.0
        stats = cell_stats([trade(day, value) for day, value in zip(days, values, strict=True)])
        assert stats is not None
        assert stats.worst_day == -30.0
        assert stats.max_drawdown == pytest.approx(40.0)
        ordered = sorted(values)
        assert stats.without_worst == pytest.approx(sum(values) - sum(ordered[:TAIL_DAYS]))
        assert stats.without_best == pytest.approx(sum(values) - sum(ordered[-TAIL_DAYS:]))


class TestDifferences:
    def test_the_same_rule_differs_from_itself_by_nothing(self) -> None:
        trades = [trade(day, (-1) ** i * 20.0) for i, day in enumerate(sessions(40))]
        difference = paired_difference(trades, trades)
        assert difference is not None
        assert difference.mean == 0.0

    def test_a_rule_that_adds_ten_on_every_position_is_worth_exactly_ten(self) -> None:
        """Paired, the market noise both rules share cancels and only the rule is left."""
        days = sessions(40)
        base = [trade(day, (-1) ** i * 200.0) for i, day in enumerate(days)]
        better = [trade(day, t.pnl + 10.0) for day, t in zip(days, base, strict=True)]
        difference = paired_difference(better, base)
        assert difference is not None and difference.reportable
        assert difference.mean == pytest.approx(10.0)
        assert difference.ci_low == pytest.approx(10.0)
        assert difference.ci_high == pytest.approx(10.0)

    def test_positions_only_one_rule_traded_are_left_out(self) -> None:
        days = sessions(35)
        first = [trade(day, 1.0) for day in days]
        second = [trade(day, 0.0) for day in days[:31]]
        difference = paired_difference(first, second)
        assert difference is not None and difference.sessions == 31


def test_benjamini_hochberg_on_a_worked_example() -> None:
    """Sorted: 0.005, 0.01, 0.03, 0.04 over four tests. Raw q is p*4/rank: 0.02, 0.02,
    0.04, 0.04, already monotone. None is not a test and stays None."""
    q = benjamini_hochberg([0.01, 0.04, None, 0.03, 0.005])
    assert q[0] == pytest.approx(0.02)
    assert q[1] == pytest.approx(0.04)
    assert q[2] is None
    assert q[3] == pytest.approx(0.04)
    assert q[4] == pytest.approx(0.02)
