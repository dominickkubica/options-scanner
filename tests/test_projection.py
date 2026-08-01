"""Expected move cones and terminal distributions.

Hand-verified where the arithmetic is checkable: a one sigma lognormal band at a known
vol and time has a closed form, and so does the median of a driftless GBM.
"""

from __future__ import annotations

import math
from datetime import date

import pytest

from optscan.analytics.projection import build_cone, terminal_histogram

ASOF = date(2026, 7, 31)


class TestCone:
    def test_a_one_sigma_band_matches_the_closed_form(self) -> None:
        """spot * exp(+/- sigma * sqrt(t)). At 20 percent vol and 365 days, t is 1.0,
        so the band is 100 * exp(-0.2) to 100 * exp(0.2)."""
        cone = build_cone(100.0, ASOF, [(date(2027, 7, 31), 0.20)], deviations=(1.0,))

        low, high = cone.points[0].band(1.0)
        assert low == pytest.approx(100.0 * math.exp(-0.2), rel=1e-3)
        assert high == pytest.approx(100.0 * math.exp(0.2), rel=1e-3)

    def test_the_band_is_lognormal_and_therefore_not_symmetric(self) -> None:
        """The honest version. A symmetric cone drawn a year out puts its lower edge at
        a price the model assigns almost no probability to."""
        cone = build_cone(100.0, ASOF, [(date(2027, 7, 31), 0.40)], deviations=(1.0,))
        low, high = cone.points[0].band(1.0)

        assert 100.0 - low < high - 100.0
        assert low * high == pytest.approx(100.0**2, rel=1e-6)

    def test_each_expiry_uses_its_own_volatility(self) -> None:
        """The whole reason this is not one sigma over a sqrt(t) curve. With an
        inverted term structure the near cone must be wider in vol terms than a flat
        projection from the back month would draw it."""
        expiries = [(date(2026, 8, 30), 0.60), (date(2027, 7, 31), 0.20)]
        cone = build_cone(100.0, ASOF, expiries, deviations=(1.0,))

        near, far = cone.points
        assert near.sigma == pytest.approx(0.60)
        assert far.sigma == pytest.approx(0.20)

    def test_an_unsolved_expiry_is_skipped_and_named(self) -> None:
        """Interpolating a vol across a gap is how a cone comes to imply a precision
        the chain never supported."""
        expiries = [(date(2026, 8, 30), 0.0), (date(2026, 9, 30), 0.25)]
        cone = build_cone(100.0, ASOF, expiries)

        assert len(cone.points) == 1
        assert cone.points[0].expiry == date(2026, 9, 30)
        assert any("2026-08-30" in note for note in cone.notes)

    def test_an_expired_date_is_skipped(self) -> None:
        cone = build_cone(100.0, ASOF, [(date(2026, 7, 30), 0.25)])
        assert cone.points == ()

    def test_points_come_back_in_expiry_order(self) -> None:
        expiries = [(date(2027, 7, 31), 0.2), (date(2026, 8, 30), 0.3), (date(2026, 12, 18), 0.25)]
        cone = build_cone(100.0, ASOF, expiries)
        dates = [point.expiry for point in cone.points]
        assert dates == sorted(dates)

    def test_the_cone_widens_with_time_at_a_flat_volatility(self) -> None:
        expiries = [(date(2026, 8, 30), 0.25), (date(2027, 7, 31), 0.25)]
        cone = build_cone(100.0, ASOF, expiries, deviations=(1.0,))

        near_low, near_high = cone.points[0].band(1.0)
        far_low, far_high = cone.points[1].band(1.0)
        assert far_high > near_high
        assert far_low < near_low

    def test_two_sigma_is_wider_than_one(self) -> None:
        cone = build_cone(100.0, ASOF, [(date(2027, 7, 31), 0.25)], deviations=(1.0, 2.0))
        one_low, one_high = cone.points[0].band(1.0)
        two_low, two_high = cone.points[0].band(2.0)
        assert two_high > one_high
        assert two_low < one_low

    def test_the_interpolation_between_points_is_labelled_as_one(self) -> None:
        """Straight lines between expiries are an interpolation, and saying so is the
        difference between a chart and a claim."""
        expiries = [(date(2026, 8, 30), 0.3), (date(2026, 12, 18), 0.25)]
        cone = build_cone(100.0, ASOF, expiries)
        assert any("interpolation" in note for note in cone.notes)

    def test_widest_spans_the_whole_cone(self) -> None:
        expiries = [(date(2026, 8, 30), 0.25), (date(2027, 7, 31), 0.25)]
        cone = build_cone(100.0, ASOF, expiries, deviations=(1.0,))
        low, high = cone.widest(1.0)
        assert low == pytest.approx(cone.points[1].band(1.0)[0])
        assert high == pytest.approx(cone.points[1].band(1.0)[1])

    def test_a_negative_spot_is_refused(self) -> None:
        with pytest.raises(ValueError, match="spot must be positive"):
            build_cone(-1.0, ASOF, [(date(2027, 7, 31), 0.25)])


class TestTerminalDistribution:
    def test_the_probabilities_sum_to_one(self) -> None:
        result = terminal_histogram(100.0, 0.25, 0.30, paths=4000)
        assert result is not None
        assert sum(item.probability for item in result.bins) == pytest.approx(1.0)

    def test_the_median_of_a_driftless_walk_sits_below_spot(self) -> None:
        """Under zero rate the log price drifts at -sigma^2/2, so the median terminal
        price is spot * exp(-sigma^2 * t / 2), which is below spot. The mean is not.
        Confusing the two is how a projection quietly acquires a bullish tilt."""
        spot, time, sigma = 100.0, 1.0, 0.40
        result = terminal_histogram(spot, time, sigma, paths=40_000)

        assert result is not None
        expected = spot * math.exp(-0.5 * sigma * sigma * time)
        assert result.median == pytest.approx(expected, rel=0.03)

    def test_the_distribution_is_right_skewed(self) -> None:
        """A stock cannot fall more than 100 percent and can rise without limit, so the
        peak sits below the median and the upper tail is longer."""
        result = terminal_histogram(100.0, 1.0, 0.50, paths=40_000)
        assert result is not None
        assert result.mode is not None
        assert result.mode < result.median

    def test_quantiles_are_ordered(self) -> None:
        result = terminal_histogram(100.0, 0.5, 0.30, paths=8000)
        assert result is not None
        values = [result.quantiles[q] for q in sorted(result.quantiles)]
        assert values == sorted(values)

    def test_it_is_deterministic_for_a_given_seed(self) -> None:
        """A histogram that shifts on every page load invites the reader to see
        movement in the market that is only sampling noise."""
        first = terminal_histogram(100.0, 0.5, 0.30, paths=4000, seed=7)
        second = terminal_histogram(100.0, 0.5, 0.30, paths=4000, seed=7)
        assert first is not None and second is not None
        assert first.median == pytest.approx(second.median)
        assert [b.probability for b in first.bins] == [b.probability for b in second.bins]

    def test_impossible_inputs_refuse_rather_than_returning_an_empty_shape(self) -> None:
        assert terminal_histogram(100.0, 0.0, 0.30) is None
        assert terminal_histogram(100.0, 0.5, 0.0) is None
        assert terminal_histogram(0.0, 0.5, 0.30) is None

    def test_higher_volatility_widens_the_distribution(self) -> None:
        calm = terminal_histogram(100.0, 1.0, 0.15, paths=20_000)
        wild = terminal_histogram(100.0, 1.0, 0.60, paths=20_000)
        assert calm is not None and wild is not None
        calm_span = calm.quantiles[0.95] - calm.quantiles[0.05]
        wild_span = wild.quantiles[0.95] - wild.quantiles[0.05]
        assert wild_span > calm_span * 2
