"""Probability metrics.

Hand verifiable anchors used throughout:

- With zero drift, P(S_T > S_0) is exactly 0.5 by symmetry of the log distribution.
- With zero drift, the reflection principle gives P(touch) = 2 * P(finish beyond).
- P(finish below K) is N(-d2), the same term that appears in a BSM put price, so it
  can be checked against the pricing module rather than restated.
"""

from __future__ import annotations

import math

import pytest

from optscan.analytics.greeks import bsm_greeks, d1_d2, norm_cdf
from optscan.analytics.montecarlo import simulate_paths
from optscan.analytics.probability import (
    breakeven,
    delta_as_probability,
    prob_finish_above,
    prob_finish_below,
    prob_of_touch,
    probability_of_profit,
    probability_of_profit_spread,
    touch_and_finish,
)

SPOT, TIME, SIGMA = 100.0, 1.0, 0.20


class TestFinishProbabilities:
    def test_at_the_money_with_no_drift_is_a_coin_flip(self) -> None:
        """Log price is symmetric around its drift, and here the drift is -sigma^2/2.

        So P(S_T > S_0) is slightly under half even with a zero rate: the median of a
        lognormal sits below its mean. 0.20 vol over a year puts it at N(-0.1) = 0.4602.
        """
        above = prob_finish_above(SPOT, SPOT, TIME, SIGMA, rate=0.0)
        assert above == pytest.approx(norm_cdf(-0.1), abs=1e-9)
        assert above == pytest.approx(0.4602, abs=1e-4)

    def test_matches_the_n_minus_d2_term_from_pricing(self) -> None:
        """P(finish below K) is N(-d2), which the BSM put price also uses."""
        rate = 0.05
        _, second = d1_d2(SPOT, 95.0, TIME, rate, SIGMA)
        assert prob_finish_below(95.0, SPOT, TIME, SIGMA, rate) == pytest.approx(
            norm_cdf(-second), abs=1e-12
        )

    def test_above_and_below_sum_to_one(self) -> None:
        below = prob_finish_below(105.0, SPOT, TIME, SIGMA, 0.05)
        above = prob_finish_above(105.0, SPOT, TIME, SIGMA, 0.05)
        assert below + above == pytest.approx(1.0)

    def test_further_strikes_are_less_likely(self) -> None:
        near = prob_finish_below(95.0, SPOT, TIME, SIGMA)
        far = prob_finish_below(80.0, SPOT, TIME, SIGMA)
        assert far < near

    def test_more_volatility_widens_the_distribution(self) -> None:
        calm = prob_finish_below(80.0, SPOT, TIME, 0.10)
        wild = prob_finish_below(80.0, SPOT, TIME, 0.60)
        assert wild > calm

    def test_expired_is_deterministic(self) -> None:
        assert prob_finish_below(105.0, 100.0, 0.0, SIGMA) == 1.0
        assert prob_finish_below(95.0, 100.0, 0.0, SIGMA) == 0.0

    def test_invalid_inputs_raise(self) -> None:
        with pytest.raises(ValueError, match="barrier must be positive"):
            prob_finish_below(0.0, SPOT, TIME, SIGMA)
        with pytest.raises(ValueError, match="spot must be positive"):
            prob_finish_below(100.0, 0.0, TIME, SIGMA)


class TestProbabilityOfTouch:
    def test_driftless_touch_is_exactly_twice_finish(self) -> None:
        """The reflection principle, which holds when the log drift is zero.

        Log drift is rate - q - sigma^2/2, so it vanishes when rate = sigma^2/2.
        With sigma = 0.20 that is a rate of 2 percent.
        """
        rate = 0.5 * SIGMA * SIGMA
        finish = prob_finish_below(90.0, SPOT, TIME, SIGMA, rate)
        touch = prob_of_touch(90.0, SPOT, TIME, SIGMA, rate)
        assert touch == pytest.approx(2 * finish, abs=1e-9)

    def test_with_drift_the_rule_of_thumb_breaks(self) -> None:
        """The doubling shortcut is exact only at zero drift.

        With upward drift and a barrier below spot, touch over finish rises above 2:
        a path can dip through 90 and get carried back up by the drift, which counts
        as a touch but not as a finish. At 5 percent rates the ratio is about 2.21,
        so the shortcut understates how often the strike gets tested.
        """
        finish = prob_finish_below(90.0, SPOT, TIME, SIGMA, rate=0.05)
        touch = prob_of_touch(90.0, SPOT, TIME, SIGMA, rate=0.05)
        assert touch / finish == pytest.approx(2.21, abs=0.02)
        assert touch > 2 * finish

    def test_matches_a_monte_carlo_simulation(self) -> None:
        """Independent numerical check on the closed form first passage formula.

        The simulation monitors the barrier at 2000 discrete points and so misses
        crossings that happen and reverse between steps. The continuous formula must
        therefore sit slightly above the simulated frequency, and it does, by about
        half a point. Matching exactly would mean one of the two is wrong.
        """
        paths = simulate_paths(SPOT, TIME, SIGMA, rate=0.05, paths=20_000, steps=2000, seed=7)
        simulated = float((paths.min(axis=1) <= 90.0).mean())
        closed_form = prob_of_touch(90.0, SPOT, TIME, SIGMA, rate=0.05)
        assert closed_form == pytest.approx(simulated, abs=0.02)
        assert closed_form > simulated

    def test_touch_always_exceeds_finish(self) -> None:
        for barrier in (80.0, 90.0, 95.0, 105.0, 115.0):
            finish = (
                prob_finish_below(barrier, SPOT, TIME, SIGMA, 0.05)
                if barrier < SPOT
                else prob_finish_above(barrier, SPOT, TIME, SIGMA, 0.05)
            )
            touch = prob_of_touch(barrier, SPOT, TIME, SIGMA, 0.05)
            assert touch >= finish - 1e-12

    def test_a_barrier_at_spot_is_already_touched(self) -> None:
        assert prob_of_touch(SPOT, SPOT, TIME, SIGMA) == 1.0

    def test_stays_a_probability(self) -> None:
        for barrier in (1.0, 50.0, 99.9, 100.1, 200.0, 10_000.0):
            value = prob_of_touch(barrier, SPOT, TIME, SIGMA, 0.05)
            assert 0.0 <= value <= 1.0

    def test_no_time_means_no_touch(self) -> None:
        assert prob_of_touch(90.0, SPOT, 0.0, SIGMA) == 0.0

    def test_the_short_strike_is_tested_far_more_often_than_it_is_breached(self) -> None:
        """The number premium sellers underestimate: roughly double."""
        result = touch_and_finish("P", 90.0, SPOT, TIME, SIGMA, rate=0.02)
        assert result.touch > 1.8 * result.finish_beyond


class TestBreakeven:
    def test_short_put(self) -> None:
        assert breakeven("P", 95.0, 1.50) == 93.50

    def test_short_call(self) -> None:
        assert breakeven("C", 105.0, 1.50) == 106.50

    def test_negative_credit_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be negative"):
            breakeven("P", 95.0, -1.0)


class TestProbabilityOfProfit:
    def test_measured_at_the_breakeven_not_the_strike(self) -> None:
        """The credit is a buffer, so POP is strictly better than the ITM probability."""
        at_strike = prob_finish_above(95.0, SPOT, 0.25, SIGMA, 0.05)
        pop = probability_of_profit("P", 95.0, 2.0, SPOT, 0.25, SIGMA, 0.05)
        assert pop > at_strike

    def test_a_bigger_credit_buys_a_better_probability(self) -> None:
        small = probability_of_profit("P", 95.0, 0.5, SPOT, 0.25, SIGMA, 0.05)
        large = probability_of_profit("P", 95.0, 3.0, SPOT, 0.25, SIGMA, 0.05)
        assert large > small

    def test_call_side_is_the_mirror(self) -> None:
        pop = probability_of_profit("C", 105.0, 2.0, SPOT, 0.25, SIGMA, 0.05)
        assert pop == pytest.approx(prob_finish_below(107.0, SPOT, 0.25, SIGMA, 0.05))

    def test_credit_above_the_strike_cannot_lose(self) -> None:
        """Absurd in practice, and it must not produce a negative barrier."""
        assert probability_of_profit("P", 5.0, 6.0, SPOT, 0.25, SIGMA) == 1.0

    def test_spread_pop_matches_the_single_leg(self) -> None:
        """The long leg caps the loss without moving the breakeven."""
        single = probability_of_profit("P", 95.0, 1.0, SPOT, 0.25, SIGMA, 0.05)
        spread = probability_of_profit_spread("P", 95.0, 90.0, 1.0, SPOT, 0.25, SIGMA, 0.05)
        assert spread == pytest.approx(single)

    def test_spread_legs_must_be_the_right_way_round(self) -> None:
        with pytest.raises(ValueError, match="lower strike"):
            probability_of_profit_spread("P", 95.0, 100.0, 1.0, SPOT, 0.25, SIGMA)
        with pytest.raises(ValueError, match="higher strike"):
            probability_of_profit_spread("C", 105.0, 100.0, 1.0, SPOT, 0.25, SIGMA)


class TestDeltaProxy:
    def test_delta_overstates_the_itm_probability(self) -> None:
        """Delta is N(d1) and the ITM probability is N(d2), and d1 > d2 always.

        At 100 strike, 1 year, 20 vol, 5 percent rates the gap is about 8 points,
        which is the whole reason this is exposed as a proxy rather than used.
        """
        greeks = bsm_greeks("C", SPOT, 100.0, TIME, 0.05, SIGMA)
        proxy = delta_as_probability(greeks.delta)
        actual = prob_finish_above(100.0, SPOT, TIME, SIGMA, 0.05)
        assert proxy > actual
        assert proxy - actual == pytest.approx(0.077, abs=0.01)

    def test_the_gap_grows_with_time_and_volatility(self) -> None:
        def gap(time: float, sigma: float) -> float:
            delta = bsm_greeks("C", SPOT, 100.0, time, 0.05, sigma).delta
            return delta_as_probability(delta) - prob_finish_above(100.0, SPOT, time, sigma, 0.05)

        assert gap(1.0, 0.20) > gap(0.1, 0.20)
        assert gap(1.0, 0.60) > gap(1.0, 0.20)

    def test_put_delta_is_taken_as_a_magnitude(self) -> None:
        assert delta_as_probability(-0.30) == pytest.approx(0.30)

    def test_clamped_to_one(self) -> None:
        assert delta_as_probability(-1.4) == 1.0


def test_lognormal_median_sits_below_spot() -> None:
    """A sanity check on the drift convention. The median of the terminal
    distribution is spot * exp((r - q - sigma^2/2) * t), below spot when the rate
    is under half the variance. Getting this sign wrong biases every probability."""
    rate = 0.0
    median = SPOT * math.exp((rate - 0.5 * SIGMA**2) * TIME)
    assert median < SPOT
    assert prob_finish_below(median, SPOT, TIME, SIGMA, rate) == pytest.approx(0.5, abs=1e-9)
