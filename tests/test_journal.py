"""Journal aggregation: the arithmetic a report surface is read for.

The failure mode this guards against is not a crash. It is a plausible looking number.
A drawdown that reports zero because it only measured from the first peak, an
expectancy interval computed across rows instead of clusters, a profit factor rendered
as infinity when nothing lost - each of those draws a confident chart over a wrong
figure, and none of them raise.

The cluster arithmetic gets the most attention here because it is the whole basis for
the report refusing to make a claim. If `expectancy` divided by the row count the
interval would be roughly twelve times too narrow on the current sample, zero would
fall outside it, and the view would present a screen with three settlement dates as a
proven edge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from optscan.analytics.journal import (
    SCORE_BANDS,
    DayPoint,
    build_report,
    cluster_totals,
    daily_series,
    expectancy,
    group_by,
    group_by_band,
    max_drawdown,
)


@dataclass(frozen=True)
class FakeOutcome:
    """The subset of LoggedOpportunity the journal actually reads."""

    symbol: str
    expiry: date
    strategy: str = "iron_condor"
    dte: int = 25
    score: float = 0.8
    profit: float | None = 0.0


def make(symbol: str, expiry: str, profit: float, **kwargs) -> FakeOutcome:
    return FakeOutcome(symbol=symbol, expiry=date.fromisoformat(expiry), profit=profit, **kwargs)


class TestClustering:
    def test_rows_on_one_chain_are_one_observation(self) -> None:
        """The premise the whole report rests on."""
        rows = [make("SPY", "2026-01-16", 10.0) for _ in range(50)]
        assert len(cluster_totals(rows)) == 1

    def test_cluster_totals_sum_rather_than_average(self) -> None:
        """Several candidates on one chain are several positions into one settlement."""
        rows = [make("SPY", "2026-01-16", 10.0), make("SPY", "2026-01-16", 30.0)]
        assert cluster_totals(rows) == [40.0]

    def test_symbol_and_expiry_both_separate_clusters(self) -> None:
        rows = [
            make("SPY", "2026-01-16", 1.0),
            make("QQQ", "2026-01-16", 1.0),
            make("SPY", "2026-02-20", 1.0),
        ]
        assert len(cluster_totals(rows)) == 3


class TestExpectancy:
    def test_point_estimate_is_per_row(self) -> None:
        """What a person sizes in, even though the interval is not."""
        rows = [make("SPY", "2026-01-16", 100.0), make("QQQ", "2026-02-20", 200.0)]
        assert expectancy(rows).value == 150.0

    def test_interval_widens_to_the_cluster_count(self) -> None:
        """The point of the whole module.

        Same rows, same mean, but the second set is one chain repeated. Fewer
        independent observations must not produce a tighter interval.
        """
        spread = [make("SPY", "2026-01-16", 100.0), make("QQQ", "2026-02-20", -50.0)] * 10
        clumped = [make("SPY", "2026-01-16", 100.0), make("SPY", "2026-01-16", -50.0)] * 10

        wide = expectancy(spread)
        narrow = expectancy(clumped)
        assert wide.clusters == 2
        assert narrow.clusters == 1
        # One cluster has no spread to measure, so it gets no interval at all rather
        # than a zero width one that would read as certainty.
        assert narrow.low is None and narrow.high is None

    def test_single_cluster_is_flagged_rather_than_claimed(self) -> None:
        estimate = expectancy([make("SPY", "2026-01-16", 100.0)])
        assert estimate.clusters == 1
        assert estimate.indistinguishable_from_zero is True

    def test_zero_inside_the_interval_is_reported_as_such(self) -> None:
        """A mean that could be zero must never be presented as an edge."""
        rows = [
            make("SPY", "2026-01-16", 100.0),
            make("QQQ", "2026-02-20", -98.0),
            make("IWM", "2026-03-20", 60.0),
            make("AAPL", "2026-04-17", -55.0),
        ]
        assert expectancy(rows).indistinguishable_from_zero is True

    def test_no_rows_is_none_not_zero(self) -> None:
        assert expectancy([]) is None


class TestDailySeries:
    def test_keyed_on_expiry_and_cumulative_accumulates(self) -> None:
        rows = [
            make("SPY", "2026-01-16", 100.0),
            make("QQQ", "2026-01-16", 50.0),
            make("SPY", "2026-02-20", -30.0),
        ]
        points = daily_series(rows)
        assert [point.day.isoformat() for point in points] == ["2026-01-16", "2026-02-20"]
        assert [point.profit for point in points] == [150.0, -30.0]
        assert [point.cumulative for point in points] == [150.0, 120.0]
        assert points[0].trades == 2
        assert points[0].clusters == 2


class TestDrawdown:
    def test_measures_peak_to_trough(self) -> None:
        points = [
            DayPoint(date(2026, 1, 1), 100.0, 1, 1, 100.0),
            DayPoint(date(2026, 1, 2), -40.0, 1, 1, 60.0),
            DayPoint(date(2026, 1, 3), 10.0, 1, 1, 70.0),
        ]
        assert max_drawdown(points) == 40.0

    def test_a_curve_that_only_falls_reports_its_whole_fall(self) -> None:
        """Measured from zero, not from the first peak.

        Seeding the peak at the first point instead of at zero would report 30 here
        and call the first 50 of the loss free.
        """
        points = [
            DayPoint(date(2026, 1, 1), -50.0, 1, 1, -50.0),
            DayPoint(date(2026, 1, 2), -30.0, 1, 1, -80.0),
        ]
        assert max_drawdown(points) == 80.0

    def test_a_curve_that_only_rises_has_no_drawdown(self) -> None:
        points = [
            DayPoint(date(2026, 1, 1), 10.0, 1, 1, 10.0),
            DayPoint(date(2026, 1, 2), 20.0, 1, 1, 30.0),
        ]
        assert max_drawdown(points) == 0.0


class TestBreakdowns:
    def test_group_by_orders_by_total_profit(self) -> None:
        rows = [
            make("SPY", "2026-01-16", 10.0, strategy="a"),
            make("QQQ", "2026-02-20", 90.0, strategy="b"),
        ]
        assert [row.key for row in group_by(rows, "strategy")] == ["b", "a"]

    def test_bands_keep_band_order_not_profit_order(self) -> None:
        """A gradient re-sorted by profit stops showing whether it is monotonic."""
        rows = [
            make("SPY", "2026-01-16", 500.0, score=0.30),
            make("QQQ", "2026-02-20", 10.0, score=0.90),
        ]
        bands = group_by_band(rows, "score", SCORE_BANDS)
        assert [row.key for row in bands] == ["0.00-0.60", "0.85-1.00"]

    def test_a_group_carries_its_own_cluster_count(self) -> None:
        """A filter can gut the sample without changing anything else on screen."""
        rows = [make("SPY", "2026-01-16", 1.0, strategy="a") for _ in range(20)]
        [group] = group_by(rows, "strategy")
        assert group.trades == 20
        assert group.clusters == 1
        assert group.reportable is False


class TestReport:
    def test_unresolved_candidates_are_dropped_not_counted_flat(self) -> None:
        """An open position is not a flat one."""
        rows = [make("SPY", "2026-01-16", 100.0), make("QQQ", "2026-02-20", None)]
        report = build_report(rows)
        assert report.trades == 1
        assert report.total_profit == 100.0

    def test_profit_factor_is_undefined_rather_than_infinite(self) -> None:
        """Rendering an empty denominator as a huge number reads as a great result."""
        report = build_report([make("SPY", "2026-01-16", 100.0)])
        assert report.profit_factor is None

    def test_profit_factor_is_gross_win_over_gross_loss(self) -> None:
        rows = [make("SPY", "2026-01-16", 100.0), make("QQQ", "2026-02-20", -25.0)]
        assert build_report(rows).profit_factor == 4.0

    def test_small_samples_are_not_reportable_and_say_why(self) -> None:
        report = build_report([make("SPY", "2026-01-16", 100.0)])
        assert report.reportable is False
        assert any("cluster" in note for note in report.notes)

    def test_settlement_date_count_is_reported_separately_from_clusters(self) -> None:
        """Six symbols on one Friday are six clusters but one market move."""
        rows = [
            make(symbol, "2026-01-16", 10.0)
            for symbol in ("SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA")
        ]
        report = build_report(rows)
        assert report.clusters == 6
        assert report.settlement_dates == 1
        assert any("settlement date" in note for note in report.notes)

    def test_empty_input_is_an_empty_report_not_a_crash(self) -> None:
        report = build_report([])
        assert report.trades == 0
        assert report.total_profit == 0.0
        assert report.expectancy is None
        assert report.days == []
        assert report.notes
