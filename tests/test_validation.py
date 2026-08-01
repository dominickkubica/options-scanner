"""Settlement arithmetic, calibration statistics, and the validation pipeline.

The settlement values are hand computed: a short option at expiry is arithmetic rather
than a quote, so every expected number here can be checked on paper.

The statistics half is mostly about refusing. A validation study that produces a
confident answer from a small correlated sample is worse than one that produces none,
because the whole reason this phase exists is to stop the tool asserting things it has
not earned.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from optscan.analytics.calibration import (
    MIN_CLUSTERS_FOR_A_CLAIM,
    brier_score,
    count_clusters,
    probability_calibration,
    score_buckets,
    validate,
    wilson_interval,
)
from optscan.analytics.outcomes import Outcome, intrinsic_value, settle
from optscan.models import PriceBar
from optscan.storage import db
from optscan.storage import validation as store
from optscan.storage.validation import LoggedOpportunity


class TestIntrinsic:
    def test_a_put_is_worth_the_shortfall(self) -> None:
        assert intrinsic_value("P", 700.0, 680.0) == pytest.approx(20.0)
        assert intrinsic_value("P", 700.0, 720.0) == pytest.approx(0.0)

    def test_a_call_is_worth_the_excess(self) -> None:
        assert intrinsic_value("C", 700.0, 720.0) == pytest.approx(20.0)
        assert intrinsic_value("C", 700.0, 680.0) == pytest.approx(0.0)


class TestSettlement:
    """Hand computed. Credit 5.00 on a 10 wide put spread, short 700 long 690."""

    def _settle(self, price: float, **kwargs):
        return settle(
            short_right="P",
            short_strike=700.0,
            credit=5.00,
            settlement_price=price,
            width=10.0,
            **kwargs,
        )

    def test_finishing_clear_keeps_the_whole_credit(self) -> None:
        result = self._settle(750.0)
        assert result.outcome is Outcome.EXPIRED_WORTHLESS
        assert result.profit == pytest.approx(500.0)
        assert result.finished_beyond is False
        assert result.profit_fraction == pytest.approx(1.0)

    def test_a_breach_inside_the_credit_still_makes_money(self) -> None:
        """697: the short is worth 3.00, the long nothing, so 2.00 of the 5.00 is kept.
        Scoring this as a loss would mismeasure exactly the near misses the calibration
        check is about."""
        result = self._settle(697.0)
        assert result.outcome is Outcome.BREACHED_PROFITABLE
        assert result.profit == pytest.approx(200.0)
        assert result.finished_beyond is True
        assert result.won is True

    def test_a_breach_past_breakeven_loses_without_being_the_maximum(self) -> None:
        """693: the short is worth 7.00 against a 5.00 credit, so 2.00 is lost. The long
        at 690 is still worthless, so this is not max loss."""
        result = self._settle(693.0)
        assert result.outcome is Outcome.BREACHED_LOSS
        assert result.profit == pytest.approx(-200.0)

    def test_below_the_long_strike_is_the_maximum_loss(self) -> None:
        """680: short worth 20, long recovers 10, net 10 against a 5 credit."""
        result = self._settle(680.0)
        assert result.outcome is Outcome.MAX_LOSS
        assert result.profit == pytest.approx(-500.0)

    def test_the_loss_stops_at_the_long_strike(self) -> None:
        """The defining property of a defined risk spread. 600 is far below 690 and
        must not cost a penny more than 650 does."""
        assert self._settle(600.0).profit == pytest.approx(self._settle(650.0).profit)

    def test_a_naked_short_is_not_clamped(self) -> None:
        """No width means no bounded loss. Clamping would flatter every undefined risk
        strategy in the report, which is the one place this study must not tilt."""
        naked = settle(short_right="P", short_strike=700.0, credit=5.00, settlement_price=600.0)
        assert naked.profit == pytest.approx((5.0 - 100.0) * 100)
        assert naked.outcome is Outcome.BREACHED_LOSS

    def test_commission_comes_off_the_result(self) -> None:
        assert self._settle(750.0, commission=1.30).profit == pytest.approx(498.70)

    def test_a_short_call_settles_the_other_way(self) -> None:
        result = settle(
            short_right="C", short_strike=700.0, credit=5.00, settlement_price=750.0, width=10.0
        )
        assert result.outcome is Outcome.MAX_LOSS
        assert result.profit == pytest.approx(-500.0)


class TestWilson:
    def test_it_stays_inside_zero_and_one(self) -> None:
        """The reason it is used. A normal interval on 39 of 40 puts its upper bound
        above certainty, which is a formula being used outside its range."""
        low, high = wilson_interval(39, 40)
        assert 0.0 <= low <= high <= 1.0
        assert high < 1.0

    def test_it_widens_as_the_sample_shrinks(self) -> None:
        wide = wilson_interval(8, 10)
        narrow = wilson_interval(80, 100)
        assert (wide[1] - wide[0]) > (narrow[1] - narrow[0])

    def test_no_trials_is_total_ignorance(self) -> None:
        assert wilson_interval(0, 0) == (0.0, 1.0)


def logged(
    identifier: int,
    *,
    symbol: str = "SPY",
    expiry: date = date(2026, 8, 21),
    score: float = 0.5,
    profit: float | None = 100.0,
    pop: float | None = 0.80,
    finished_beyond: bool | None = False,
    scan_id: int = 1,
) -> LoggedOpportunity:
    return LoggedOpportunity(
        id=identifier,
        scan_id=scan_id,
        recorded_at=datetime(2026, 7, 1, tzinfo=UTC),
        session_date=date(2026, 7, 1),
        symbol=symbol,
        strategy="put_credit_spread",
        expiry=expiry,
        dte=51,
        underlying_price=740.0,
        short_strike=700.0,
        short_right="P",
        width=10.0,
        credit=5.0,
        max_profit=500.0,
        max_loss=500.0,
        probability_of_profit=pop,
        short_delta=-0.2,
        iv=0.15,
        iv_rank=None,
        liquidity_score=0.9,
        score=score,
        outcome="expired_worthless" if profit and profit > 0 else "breached_loss",
        settlement_price=750.0,
        finished_beyond=finished_beyond,
        profit=profit,
        profit_fraction=1.0 if profit and profit > 0 else -1.0,
    )


class TestIndependence:
    """The trap this phase brings: rows are not observations."""

    def test_rows_from_one_chain_are_one_cluster(self) -> None:
        """Forty candidates off one chain share an underlying and a session. If it
        rallies they lose together, so they are one observation of a market."""
        items = [logged(i) for i in range(40)]
        assert len(items) == 40
        assert count_clusters(items) == 1

    def test_different_expiries_are_different_clusters(self) -> None:
        items = [
            logged(1, expiry=date(2026, 8, 21)),
            logged(2, expiry=date(2026, 9, 18)),
        ]
        assert count_clusters(items) == 2

    def test_the_same_chain_scanned_twice_is_still_one_cluster(self) -> None:
        """Monday and Tuesday on the same symbol and expiry settle against the same
        price. Two scans, one observation."""
        items = [logged(1, scan_id=1), logged(2, scan_id=2)]
        assert count_clusters(items) == 1

    def test_the_interval_is_widened_to_the_cluster_count(self) -> None:
        """The load bearing assertion of the file. A hundred correlated rows must not
        produce the interval a hundred independent ones would."""
        many_rows = [logged(i, profit=100.0) for i in range(100)]
        report = validate(many_rows)

        assert report.resolved == 100
        assert report.clusters == 1
        # One cluster of wins cannot pin a win rate down at all.
        assert report.overall_win_rate.low < 0.3
        assert report.overall_win_rate.value == pytest.approx(1.0)


class TestBuckets:
    def test_candidates_split_by_score(self) -> None:
        items = [
            logged(i, score=i / 100.0, expiry=date(2026, 8, 1) + timedelta(days=i))
            for i in range(100)
        ]
        buckets = score_buckets(items, buckets=4)

        assert len(buckets) == 4
        assert buckets[0].mean_score < buckets[-1].mean_score
        assert sum(bucket.count for bucket in buckets) == 100

    def test_buckets_are_equal_count_not_equal_width(self) -> None:
        """The score distribution is bunched. Fixed width bands put everything in one
        of them, which looks like a finding about the score and is a fact about the
        binning."""
        scores = [0.10] * 40 + [0.11] * 40 + [0.90] * 20
        items = [
            logged(i, score=value, expiry=date(2026, 8, 1) + timedelta(days=i))
            for i, value in enumerate(scores)
        ]
        buckets = score_buckets(items, buckets=4)
        sizes = [bucket.count for bucket in buckets]
        assert max(sizes) - min(sizes) <= 1


class TestCalibration:
    def test_a_perfectly_calibrated_model_has_no_error(self) -> None:
        """80 percent predicted, 80 of 100 finish clear."""
        items = [
            logged(
                i,
                pop=0.80,
                finished_beyond=i >= 80,
                expiry=date(2026, 8, 1) + timedelta(days=i),
            )
            for i in range(100)
        ]
        points = probability_calibration(items)
        band = next(p for p in points if p.low == 0.80)

        assert band.predicted == pytest.approx(0.80)
        assert band.actual == pytest.approx(0.80)
        assert band.error == pytest.approx(0.0)

    def test_an_optimistic_model_shows_positive_error(self) -> None:
        """The direction probability.py says to expect: lognormal tails are too thin,
        so the far strikes get breached more often than predicted."""
        items = [
            logged(
                i,
                pop=0.90,
                finished_beyond=i >= 70,
                expiry=date(2026, 8, 1) + timedelta(days=i),
            )
            for i in range(100)
        ]
        band = next(p for p in probability_calibration(items) if p.low == 0.90)
        assert band.error > 0.15

    def test_calibration_is_measured_on_the_event_not_on_profit(self) -> None:
        """A position can make money after being breached. Scoring on profit would let
        a model be wrong about where price finished and still look calibrated."""
        items = [
            logged(
                i,
                pop=0.90,
                finished_beyond=True,
                profit=100.0,
                expiry=date(2026, 8, 1) + timedelta(days=i),
            )
            for i in range(50)
        ]
        band = next(p for p in probability_calibration(items) if p.low == 0.90)
        assert band.actual == pytest.approx(0.0)
        assert band.error == pytest.approx(0.90)

    def test_brier_rewards_confident_and_correct(self) -> None:
        confident = [logged(i, pop=0.99, finished_beyond=False) for i in range(50)]
        hedged = [logged(i, pop=0.50, finished_beyond=False) for i in range(50)]
        assert brier_score(confident) < brier_score(hedged)

    def test_brier_is_none_without_forecasts(self) -> None:
        assert brier_score([logged(1, pop=None)]) is None


class TestVerdict:
    def test_an_empty_study_refuses_and_says_why(self) -> None:
        report = validate([])
        assert report.resolved == 0
        assert report.scores_separate is None
        assert report.conclusive is False
        assert "settle after their expiry" in " ".join(report.notes)

    def test_a_small_correlated_sample_draws_no_conclusion(self) -> None:
        """Two hundred rows off two chains. Plenty of rows, two observations."""
        items = [
            logged(i, symbol="SPY", score=i / 200.0, profit=100.0 if i > 100 else -100.0)
            for i in range(200)
        ]
        report = validate(items)

        assert report.resolved == 200
        assert report.clusters == 1
        assert report.scores_separate is None
        assert "no conclusion is drawn" in " ".join(report.notes)

    def test_a_wide_sample_with_a_real_separation_concludes(self) -> None:
        """Enough independent clusters, and the top bucket wins while the bottom loses.
        This is the shape a real finding would have."""
        items = []
        for index in range(80):
            winner = index >= 40
            items.append(
                logged(
                    index,
                    symbol=f"S{index}",
                    expiry=date(2026, 8, 1) + timedelta(days=index),
                    score=0.9 if winner else 0.1,
                    profit=100.0 if winner else -100.0,
                )
            )
        report = validate(items)

        assert report.clusters >= MIN_CLUSTERS_FOR_A_CLAIM
        assert report.scores_separate is True

    def test_a_wide_sample_with_no_separation_says_so(self) -> None:
        """The result the roadmap says to be genuinely willing to find."""
        items = []
        for index in range(80):
            items.append(
                logged(
                    index,
                    symbol=f"S{index}",
                    expiry=date(2026, 8, 1) + timedelta(days=index),
                    score=index / 80.0,
                    profit=100.0 if index % 2 else -100.0,
                )
            )
        report = validate(items)

        assert report.clusters >= MIN_CLUSTERS_FOR_A_CLAIM
        assert report.scores_separate is False
        assert "does not show the score separating" in " ".join(report.notes)

    def test_separation_is_judged_on_halves_not_on_extreme_buckets(self) -> None:
        """A regression for a real bug found by demonstration.

        Comparing only the top bucket against the bottom one throws away the middle
        half and leaves both intervals wide. On this shape, where the score is bimodal
        and the top quartile is not the best quartile, that reported no separation while
        the high scores were winning 91 percent against 54.
        """
        items = []
        for index in range(240):
            high = index % 2 == 0
            # Bimodal, so the quartile split lands inside each group and the very top
            # quartile is no better than the one below it.
            score = 0.70 + (index % 50) / 200.0 if high else 0.10 + (index % 50) / 200.0
            wins = (index % 10) != 0 if high else (index % 2) == 0
            items.append(
                logged(
                    index,
                    symbol=f"S{index}",
                    expiry=date(2026, 2, 1) + timedelta(days=index),
                    score=score,
                    profit=100.0 if wins else -400.0,
                    finished_beyond=not wins,
                )
            )

        report = validate(items)
        assert report.clusters >= MIN_CLUSTERS_FOR_A_CLAIM
        assert report.scores_separate is True
        assert "Top half by score" in " ".join(report.notes)

    def test_a_high_win_rate_with_negative_expectancy_is_called_out(self) -> None:
        """The classic premium selling trap. Short premium wins most of the time by
        construction, so a win rate on its own is not evidence of anything."""
        items = [
            logged(
                index,
                symbol=f"S{index}",
                expiry=date(2026, 2, 1) + timedelta(days=index),
                profit=100.0 if index % 5 else -1000.0,
            )
            for index in range(50)
        ]
        report = validate(items)

        assert report.overall_win_rate.value == pytest.approx(0.8)
        assert report.mean_profit < 0
        assert "Read the profit column, not the win rate" in " ".join(report.notes)

    def test_the_hold_to_expiry_caveat_is_always_stated(self) -> None:
        """The number is a counterfactual under one policy nobody trades. It must never
        be printed without saying so."""
        report = validate([logged(1)])
        assert "held to expiry" in " ".join(report.notes)


class TestStorage:
    @pytest.fixture
    def conn(self, tmp_path):
        with db.session(tmp_path / "optscan.sqlite") as connection:
            yield connection

    def test_counts_start_empty(self, conn) -> None:
        assert store.counts(conn) == {"scans": 0, "logged": 0, "resolved": 0, "pending": 0}

    def test_an_outcome_is_recorded_once(self, conn, frozen_snapshot) -> None:
        """Re-resolving with a different price source would silently rewrite history,
        and the point of this table is to be the record nobody adjusted."""
        from optscan.screener.config import DteFilter, Filters, ScreenConfig
        from optscan.screener.context import analyze_snapshot
        from optscan.screener.scan import scan_analysis

        # The frozen capture is 4 and 8 DTE, which the default 21 to 60 day screen
        # rejects entirely. Widened here so this exercises a real scored candidate
        # rather than skipping, the same override test_api.py uses.
        config = ScreenConfig(filters=Filters(dte=DteFilter(min_dte=0, max_dte=60)))
        analysis = analyze_snapshot(frozen_snapshot, rate=0.043)
        result = scan_analysis(analysis, config)
        assert result.opportunities, "the widened screen should surface candidates"

        scan_id = store.start_scan(conn, session_date=date(2026, 7, 30), symbols=["SPY"])
        store.record_opportunities(conn, scan_id, date(2026, 7, 30), list(result.opportunities))

        first = store.all_logged(conn)[0]
        assert store.record_outcome(
            conn,
            first.id,
            settlement_date=date(2026, 8, 21),
            settlement_price=750.0,
            settlement_source="test",
            outcome="expired_worthless",
            finished_beyond=False,
            profit=500.0,
            profit_fraction=1.0,
        )
        assert not store.record_outcome(
            conn,
            first.id,
            settlement_date=date(2026, 8, 21),
            settlement_price=1.0,
            settlement_source="test",
            outcome="max_loss",
            finished_beyond=True,
            profit=-500.0,
            profit_fraction=-1.0,
        )
        assert store.counts(conn)["resolved"] == 1


class TestResolveJob:
    """The pipeline end to end, offline."""

    @pytest.fixture
    def settings(self, tmp_settings):
        tmp_settings.ensure_dirs()
        return tmp_settings

    def test_expired_candidates_settle_from_the_underlying_close(
        self, settings, frozen_snapshot
    ) -> None:
        from optscan.jobs.validate import run_resolve
        from tests.conftest import FakeProvider

        expiry = date(2026, 5, 15)
        with db.session(settings.sqlite_path) as conn:
            scan_id = store.start_scan(conn, session_date=date(2026, 4, 1), symbols=["SPY"])
            conn.execute(
                """
                INSERT INTO opportunity_log (
                    scan_id, recorded_at, session_date, symbol, strategy, expiry, dte,
                    underlying_price, legs, short_strike, short_right, width,
                    credit, max_profit, max_loss, commission, score
                ) VALUES (?, ?, ?, 'SPY', 'put_credit_spread', ?, 44, 740.0, '[]',
                          700.0, 'P', 10.0, 5.0, 500.0, 500.0, 0.0, 0.8)
                """,
                (
                    scan_id,
                    datetime(2026, 4, 1, tzinfo=UTC).isoformat(),
                    date(2026, 4, 1).isoformat(),
                    expiry.isoformat(),
                ),
            )
            conn.commit()

        class SettlingProvider(FakeProvider):
            def get_history(self, symbol: str, days: int) -> list[PriceBar]:
                moment = datetime(expiry.year, expiry.month, expiry.day, tzinfo=UTC)
                return [
                    PriceBar(
                        symbol=symbol,
                        ts=moment,
                        open=750.0,
                        high=750.0,
                        low=750.0,
                        close=750.0,
                        volume=1,
                        fetched_at=moment,
                        source="fake",
                    )
                ]

        result = run_resolve(
            settings,
            provider=SettlingProvider(frozen_snapshot),
            asof=date(2026, 6, 1),
        )

        assert result.resolved == 1
        assert result.pending == 0

        with db.session(settings.sqlite_path) as conn:
            settled = store.resolved(conn)
        assert len(settled) == 1
        # Finished at 750 against a 700 short put: clear, so the whole credit is kept.
        assert settled[0].profit == pytest.approx(500.0)
        assert settled[0].outcome == "expired_worthless"

    def test_nothing_settles_without_a_provider(self, settings) -> None:
        from optscan.jobs.validate import run_resolve

        result = run_resolve(settings, provider=None)
        assert result.resolved == 0
        assert "nothing could be settled" in " ".join(result.notes)
