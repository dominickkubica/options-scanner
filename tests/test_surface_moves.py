"""Term structure, skew, expected move, and the Monte Carlo estimator."""

from __future__ import annotations

import math
from datetime import date

import pytest

from optscan.analytics.greeks import bsm_price
from optscan.analytics.montecarlo import (
    MonteCarloEstimate,
    probability_of_target,
    simulate_paths,
    terminal_distribution,
)
from optscan.analytics.moves import (
    FOLKLORE_STRADDLE_MULTIPLIER,
    MEAN_ABSOLUTE_OVER_SIGMA,
    STRADDLE_TO_ONE_SIGMA,
    expected_move,
    sigma_move,
    straddle_implied_sigma,
    straddle_implied_sigma_move,
    straddle_price,
)
from optscan.analytics.surface import (
    build_skew,
    build_term_structure,
    butterfly,
    risk_reversal,
    summarize_skew,
)
from optscan.models import Right

TODAY = date(2026, 7, 30)


class TestTermStructure:
    def _normal(self):
        return build_term_structure(
            {
                date(2026, 8, 6): 0.18,
                date(2026, 8, 21): 0.20,
                date(2026, 9, 18): 0.22,
            },
            TODAY,
        )

    def test_points_are_front_to_back_with_dte(self) -> None:
        term = self._normal()
        assert [p.dte for p in term.points] == [7, 22, 50]
        assert term.front.iv == 0.18
        assert term.back.iv == 0.22

    def test_normal_structure_slopes_up(self) -> None:
        """More time, more uncertainty, more vol."""
        term = self._normal()
        assert term.slope() == pytest.approx(0.04)
        assert not term.is_backwardated()

    def test_backwardation_is_detected(self) -> None:
        """Front at 45 vol against 22 in the back. Something is happening this week."""
        term = build_term_structure(
            {date(2026, 8, 6): 0.45, date(2026, 8, 21): 0.28, date(2026, 9, 18): 0.22},
            TODAY,
        )
        assert term.slope() == pytest.approx(-0.23)
        assert term.is_backwardated()

    def test_small_inversions_are_noise_not_backwardation(self) -> None:
        """Two separately solved vols differing by half a point means nothing."""
        term = build_term_structure({date(2026, 8, 6): 0.205, date(2026, 9, 18): 0.20}, TODAY)
        assert not term.is_backwardated()
        assert term.is_backwardated(threshold=0.001)

    def test_interpolation_is_linear_in_variance(self) -> None:
        """Between 7 days at 18 vol and 50 days at 22 vol, the 30 day point.

        Variance: 0.18^2 * 7 = 0.2268, 0.22^2 * 50 = 2.42.
        Weight at 30 days: (30-7)/(50-7) = 0.534884.
        Interpolated variance: 0.2268*0.465116 + 2.42*0.534884 = 1.399907.
        Vol: sqrt(1.399907/30) = 0.216018.

        Note it sits below the 0.2166 a straight linear interpolation of the vols
        would give. That sag is exactly what interpolating in variance avoids.
        """
        term = build_term_structure({date(2026, 8, 6): 0.18, date(2026, 9, 18): 0.22}, TODAY)
        assert term.iv_at_dte(30) == pytest.approx(0.216018, abs=1e-6)

    def test_interpolation_clamps_at_the_ends(self) -> None:
        term = self._normal()
        assert term.iv_at_dte(1) == 0.18
        assert term.iv_at_dte(500) == 0.22

    def test_expired_and_unusable_points_are_dropped(self) -> None:
        term = build_term_structure(
            {date(2026, 7, 1): 0.30, date(2026, 8, 21): 0.20, date(2026, 9, 18): 0.0},
            TODAY,
        )
        assert len(term.points) == 1

    def test_the_short_dated_front_is_excluded_from_the_comparison(self) -> None:
        """Very short dated ATM vol is mechanically elevated, not a signal.

        These are real SPY numbers from 30 July 2026: 0DTE at 26.9 percent against
        11.4 percent four days out and 14.1 percent at 22 days, with nothing
        happening. Including the front makes it read as heavily backwardated.
        """
        term = build_term_structure(
            {
                date(2026, 7, 30): 0.269,
                date(2026, 8, 3): 0.114,
                date(2026, 8, 7): 0.140,
                date(2026, 8, 21): 0.141,
            },
            TODAY,
        )
        assert term.slope(min_dte=0) == pytest.approx(0.141 - 0.269)
        assert term.is_backwardated(min_dte=0) is True

        # From a week out the curve is upward sloping, which is the truth.
        assert term.slope() == pytest.approx(0.141 - 0.140, abs=1e-9)
        assert term.is_backwardated() is False

    def test_a_real_event_still_shows_through_the_exclusion(self) -> None:
        """AAPL on its earnings day: 36.8 percent at 8 days against 26.6 at 50.

        The point of the exclusion is to remove a structural effect without removing
        the signal, so a genuine event has to survive it.
        """
        term = build_term_structure(
            {
                date(2026, 8, 7): 0.368,
                date(2026, 8, 14): 0.305,
                date(2026, 9, 18): 0.266,
            },
            TODAY,
        )
        assert term.is_backwardated() is True

    def test_comparable_points_drops_only_the_front(self) -> None:
        term = build_term_structure(
            {date(2026, 7, 31): 0.30, date(2026, 8, 21): 0.20, date(2026, 9, 18): 0.22},
            TODAY,
        )
        assert len(term.points) == 3
        assert [p.dte for p in term.comparable_points()] == [22, 50]

    def test_nothing_left_after_the_exclusion_is_not_a_slope(self) -> None:
        term = build_term_structure({date(2026, 7, 31): 0.30, date(2026, 8, 2): 0.28}, TODAY)
        assert term.slope() is None
        assert term.is_backwardated() is False

    def test_an_empty_structure_says_nothing(self) -> None:
        term = build_term_structure({}, TODAY)
        assert term.front is None
        assert term.slope() is None
        assert term.iv_at_dte(30) is None
        assert not term.is_backwardated()


class TestSkew:
    def _put_skew(self):
        """A normal equity smile: downside strikes carry more vol."""
        return build_skew(
            Right.PUT,
            {80.0: 0.32, 90.0: 0.26, 95.0: 0.23, 100.0: 0.20, 105.0: 0.19},
            spot=100.0,
            time=0.25,
            rate=0.05,
        )

    def test_each_strike_gets_a_delta_at_its_own_vol(self) -> None:
        skew = self._put_skew()
        assert len(skew.points) == 5
        deltas = [p.delta for p in skew.points]
        assert all(-1.0 <= d <= 0.0 for d in deltas)
        # Lower strikes are further out of the money, so smaller absolute delta.
        assert abs(deltas[0]) < abs(deltas[-1])

    def test_interpolates_iv_at_the_wing_delta(self) -> None:
        skew = self._put_skew()
        wing = skew.iv_at_delta(0.25)
        assert wing is not None
        # 25 delta sits between the 90 and 95 strikes on this smile.
        assert 0.23 <= wing <= 0.27

    def test_clamps_outside_the_available_deltas(self) -> None:
        skew = self._put_skew()
        assert skew.iv_at_delta(0.001) == pytest.approx(0.32)
        assert skew.iv_at_delta(0.99) == pytest.approx(0.19)

    def test_unusable_vols_are_dropped(self) -> None:
        skew = build_skew(Right.PUT, {90.0: 0.26, 95.0: 0.0, 100.0: 0.20}, 100.0, 0.25, 0.05)
        assert len(skew.points) == 2

    def test_empty(self) -> None:
        assert build_skew(Right.PUT, {}, 100.0, 0.25, 0.05).iv_at_delta() is None


class TestSkewSummary:
    def test_risk_reversal_is_positive_for_a_normal_equity_smile(self) -> None:
        """Puts bid over calls, because crashes happen faster than rallies."""
        assert risk_reversal(0.26, 0.19) == pytest.approx(0.07)

    def test_butterfly_measures_the_curve(self) -> None:
        """Wings at 26 and 19, body at 20: average wing 22.5, so 2.5 points of curve."""
        assert butterfly(0.26, 0.19, 0.20) == pytest.approx(0.025)

    def test_missing_inputs_give_none_not_zero(self) -> None:
        assert risk_reversal(None, 0.19) is None
        assert butterfly(0.26, None, 0.20) is None

    def test_summary_ties_them_together(self) -> None:
        puts = build_skew(Right.PUT, {85.0: 0.30, 92.0: 0.26, 100.0: 0.20}, 100.0, 0.25, 0.05)
        calls = build_skew(Right.CALL, {100.0: 0.20, 108.0: 0.19, 115.0: 0.185}, 100.0, 0.25, 0.05)
        summary = summarize_skew(puts, calls, atm_iv=0.20)
        assert summary.risk_reversal is not None and summary.risk_reversal > 0
        assert summary.butterfly is not None
        assert summary.wing_delta == 0.25


class TestExpectedMove:
    @staticmethod
    def _atm_straddle(sigma: float, time: float, spot: float = 100.0) -> float:
        return bsm_price("c", spot, spot, time, 0.0, sigma) + bsm_price(
            "p", spot, spot, time, 0.0, sigma
        )

    def test_the_straddle_is_the_mean_absolute_move(self) -> None:
        """The relationship the module is built on, checked numerically.

        An ATM straddle equals the mean absolute move, and for a normal distribution
        the mean absolute deviation is sqrt(2/pi) = 0.79788 of the standard deviation.
        This is a first order identity, so it is checked at the modest total
        volatilities this tool actually screens.
        """
        for sigma in (0.10, 0.20, 0.40):
            for time in (4 / 365, 30 / 365):
                ratio = self._atm_straddle(sigma, time) / sigma_move(100.0, sigma, time)
                assert ratio == pytest.approx(MEAN_ABSOLUTE_OVER_SIGMA, abs=1e-3)

    def test_the_approximation_degrades_at_high_total_volatility(self) -> None:
        """80 percent vol over six months breaks the first order identity by 1.3
        percent. Documented rather than hidden, because it is why the inversion is
        done exactly instead of with the constant."""
        sigma, time = 0.80, 0.5
        ratio = self._atm_straddle(sigma, time) / sigma_move(100.0, sigma, time)
        assert ratio == pytest.approx(0.9871 * MEAN_ABSOLUTE_OVER_SIGMA, rel=1e-3)

    def test_the_exact_inversion_recovers_sigma_at_any_volatility(self) -> None:
        """Inverting the ATM straddle formula directly is exact everywhere the
        constant is only approximate."""
        for sigma in (0.10, 0.20, 0.40, 0.80):
            for time in (4 / 365, 30 / 365, 0.5):
                straddle = self._atm_straddle(sigma, time)
                recovered = straddle_implied_sigma_move(straddle, 100.0)
                assert recovered == pytest.approx(sigma_move(100.0, sigma, time), rel=1e-9)

    def test_an_uninvertible_straddle_returns_nothing(self) -> None:
        """A straddle worth more than twice the spot is not a market."""
        assert straddle_implied_sigma_move(250.0, 100.0) is None
        assert straddle_implied_sigma_move(0.0, 100.0) is None
        assert straddle_implied_sigma_move(None, 100.0) is None

    def test_the_two_conversion_factors_are_inverses(self) -> None:
        assert pytest.approx(1.0) == MEAN_ABSOLUTE_OVER_SIGMA * STRADDLE_TO_ONE_SIGMA
        assert pytest.approx(1.2533141, abs=1e-7) == STRADDLE_TO_ONE_SIGMA

    def test_the_folklore_multiplier_lands_at_two_thirds_of_a_sigma(self) -> None:
        """Why 0.85 is not used: it produces 0.68 of a standard deviation, so
        comparing it against spot*IV*sqrt(t) shows a permanent 32 percent gap that
        says nothing about the market."""
        implied = MEAN_ABSOLUTE_OVER_SIGMA * FOLKLORE_STRADDLE_MULTIPLIER
        assert implied == pytest.approx(0.678, abs=0.001)

    def test_sigma_move_is_spot_times_vol_times_root_time(self) -> None:
        """100 spot, 20 vol, 30 days: 100 * 0.20 * sqrt(30/365) = 5.733822."""
        assert sigma_move(100.0, 0.20, 30 / 365) == pytest.approx(5.733822, abs=1e-6)

    def test_sigma_move_scales_with_the_square_root_of_time(self) -> None:
        """Four times the days is twice the move, not four times."""
        thirty = sigma_move(100.0, 0.20, 30 / 365)
        one_twenty = sigma_move(100.0, 0.20, 120 / 365)
        assert one_twenty == pytest.approx(thirty * 2, abs=1e-9)

    def test_straddle_needs_both_legs(self) -> None:
        assert straddle_price(3.0, 2.8) == pytest.approx(5.8)
        assert straddle_price(3.0, None) is None
        assert straddle_price(3.0, 0.0) is None

    def test_a_consistent_chain_shows_almost_no_disagreement(self) -> None:
        """The check that caught the original bug.

        Price a real ATM straddle at 20 vol, then hand that same vol in as the ATM IV.
        Both sides now describe the same distribution, so the disagreement must be
        essentially zero. Under the old 0.85 conversion this read minus 32 percent.
        """
        time = 30 / 365
        call = bsm_price("c", 100.0, 100.0, time, 0.0, 0.20)
        put = bsm_price("p", 100.0, 100.0, time, 0.0, 0.20)
        move = expected_move(100.0, time, atm_iv=0.20, call_mid=call, put_mid=put)
        assert move.disagreement == pytest.approx(0.0, abs=0.002)

    def test_a_rich_straddle_shows_up_as_a_positive_disagreement(self) -> None:
        """Straddle 20 percent above what the ATM vol supports."""
        time = 30 / 365
        fair = bsm_price("c", 100.0, 100.0, time, 0.0, 0.20)
        rich = fair * 1.2
        move = expected_move(100.0, time, atm_iv=0.20, call_mid=rich, put_mid=rich)
        assert move.disagreement is not None
        assert move.disagreement == pytest.approx(0.2, abs=0.01)

    def test_all_three_quantities_are_reported(self) -> None:
        """5.80 straddle is the average absolute move; one sigma is a shade above
        5.80 * 1.25331, since the exact inversion is very slightly convex."""
        move = expected_move(100.0, 30 / 365, atm_iv=0.20, call_mid=3.0, put_mid=2.8)
        assert move.expected_absolute_move == pytest.approx(5.8)
        assert move.market_sigma_move == pytest.approx(5.8 * STRADDLE_TO_ONE_SIGMA, rel=1e-3)
        assert move.model_sigma_move == pytest.approx(5.733822, abs=1e-6)
        assert move.model_absolute_move == pytest.approx(5.733822 * MEAN_ABSOLUTE_OVER_SIGMA)

    def test_straddle_implied_sigma_helper(self) -> None:
        assert straddle_implied_sigma(3.0, 2.8, 100.0) == pytest.approx(7.27082, abs=1e-5)
        assert straddle_implied_sigma(3.0, None, 100.0) is None

    def test_disagreement_needs_both_estimates(self) -> None:
        assert expected_move(100.0, 0.1, atm_iv=0.2).disagreement is None
        assert expected_move(100.0, 0.1, call_mid=3.0, put_mid=2.8).disagreement is None

    def test_market_price_beats_model(self) -> None:
        move = expected_move(100.0, 30 / 365, atm_iv=0.20, call_mid=3.0, put_mid=2.8)
        assert move.best_sigma_move == move.market_sigma_move
        model_only = expected_move(100.0, 30 / 365, atm_iv=0.20)
        assert model_only.best_sigma_move == model_only.model_sigma_move

    def test_symmetric_band(self) -> None:
        move = expected_move(100.0, 30 / 365, atm_iv=0.20)
        low, high = move.band()
        assert low == pytest.approx(94.266178, abs=1e-6)
        assert high == pytest.approx(105.733822, abs=1e-6)

    def test_a_two_sigma_band_is_twice_as_wide(self) -> None:
        move = expected_move(100.0, 30 / 365, atm_iv=0.20)
        one = move.band()
        two = move.band(deviations=2.0)
        assert 100 - two[0] == pytest.approx(2 * (100 - one[0]))

    def test_lognormal_band_is_asymmetric(self) -> None:
        """A stock cannot fall more than 100 percent and can rise without limit.

        100 * exp(-0.2*sqrt(1)) = 81.87 down, 100 * exp(0.2) = 122.14 up.
        """
        move = expected_move(100.0, 1.0, atm_iv=0.20)
        low, high = move.lognormal_band(0.20)
        assert low == pytest.approx(100 * math.exp(-0.2), abs=1e-6)
        assert high == pytest.approx(100 * math.exp(0.2), abs=1e-6)
        assert (high - 100) > (100 - low)

    def test_no_move_from_nothing(self) -> None:
        assert sigma_move(100.0, 0.0, 0.25) is None
        assert expected_move(100.0, 0.25).best_sigma_move is None


class TestMonteCarlo:
    def test_paths_have_the_right_shape_and_start_at_spot(self) -> None:
        paths = simulate_paths(100.0, 1.0, 0.20, paths=50, steps=10, seed=1)
        assert paths.shape == (50, 11)
        assert (paths[:, 0] == 100.0).all()
        assert (paths > 0).all()

    def test_deterministic_given_a_seed(self) -> None:
        first = simulate_paths(100.0, 1.0, 0.2, paths=20, steps=5, seed=42)
        second = simulate_paths(100.0, 1.0, 0.2, paths=20, steps=5, seed=42)
        assert (first == second).all()

    def test_terminal_distribution_has_the_right_mean(self) -> None:
        """Under the risk neutral measure the mean terminal price is spot * e^(r-q)t."""
        terminal = terminal_distribution(100.0, 1.0, 0.20, rate=0.05, paths=200_000, seed=3)
        assert terminal.mean() == pytest.approx(100.0 * math.exp(0.05), rel=0.01)

    def test_p50_is_between_zero_and_one_with_an_error_bar(self) -> None:
        estimate = probability_of_target("P", 95.0, 2.0, 100.0, 45 / 365, 0.25, paths=500, seed=1)
        assert isinstance(estimate, MonteCarloEstimate)
        assert 0.0 <= estimate.probability <= 1.0
        assert estimate.standard_error > 0
        low, high = estimate.interval_95
        assert low <= estimate.probability <= high

    def test_p50_exceeds_the_probability_of_holding_to_expiry(self) -> None:
        """Reaching half of max profit at any point is easier than keeping all of it.

        This is the whole argument for managing winners early, so if the simulation
        did not show it the simulation would be wrong.
        """
        p50 = probability_of_target(
            "P", 90.0, 1.5, 100.0, 45 / 365, 0.25, target_fraction=0.5, paths=2000, seed=5
        )
        p100 = probability_of_target(
            "P", 90.0, 1.5, 100.0, 45 / 365, 0.25, target_fraction=1.0, paths=2000, seed=5
        )
        assert p50.probability > p100.probability

    def test_a_further_strike_is_easier_to_manage(self) -> None:
        near = probability_of_target("P", 98.0, 3.0, 100.0, 45 / 365, 0.25, paths=2000, seed=5)
        far = probability_of_target("P", 85.0, 0.6, 100.0, 45 / 365, 0.25, paths=2000, seed=5)
        assert far.probability > near.probability

    def test_expired_or_zero_vol_has_no_estimate(self) -> None:
        assert probability_of_target("P", 95.0, 2.0, 100.0, 0.0, 0.25).probability == 0.0
        assert probability_of_target("P", 95.0, 2.0, 100.0, 0.5, 0.0).probability == 0.0

    def test_bad_inputs_raise(self) -> None:
        with pytest.raises(ValueError, match="target_fraction"):
            probability_of_target("P", 95.0, 2.0, 100.0, 0.25, 0.2, target_fraction=1.5)
        with pytest.raises(ValueError, match="credit must be positive"):
            probability_of_target("P", 95.0, 0.0, 100.0, 0.25, 0.2)
        with pytest.raises(ValueError, match="spot must be positive"):
            simulate_paths(0.0, 1.0, 0.2)

    def test_the_estimate_reports_its_uncertainty(self) -> None:
        """An unqualified percentage from a few hundred paths is overconfident."""
        small = probability_of_target("P", 95.0, 2.0, 100.0, 45 / 365, 0.25, paths=200, seed=1)
        large = probability_of_target("P", 95.0, 2.0, 100.0, 45 / 365, 0.25, paths=4000, seed=1)
        assert small.standard_error > large.standard_error
        assert "+/-" in str(small)
