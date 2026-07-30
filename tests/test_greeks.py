"""Black-Scholes-Merton pricing and greeks.

Two independent checks on every number:

1. Hand computed expected values for the textbook case S=100, K=100, T=1, r=5%,
   sigma=20%, no dividend. Worked below so a future reader can re-derive them:

     d1 = (ln(1) + (0.05 + 0.5*0.04) * 1) / (0.2 * 1) = 0.35
     d2 = 0.35 - 0.2 = 0.15
     N(d1) = 0.6368307,  N(d2) = 0.5596177,  n(d1) = 0.3752403
     call  = 100*0.6368307 - 100*e^-0.05*0.5596177 = 10.4505836
     put   = call - S + K*e^-rT = 10.4505836 - 100 + 95.1229425 = 5.5735260
     delta = N(d1) = 0.6368307
     gamma = n(d1)/(S*sigma*sqrt(T)) = 0.3752403/20 = 0.0187620
     vega  = S*n(d1)*sqrt(T)/100 = 37.52403/100 = 0.3752403 per vol point
     theta = (-S*n(d1)*sigma/2 - r*K*e^-rT*N(d2))/365
           = (-3.7524035 - 2.6616240)/365 = -0.0175727 per day
     rho   = K*T*e^-rT*N(d2)/100 = 53.2324815/100 = 0.5323248 per rate point

2. vollib, an independent implementation, across a grid of inputs. It is a dev
   dependency for exactly this purpose. If our math and vollib's disagree, one of us
   is wrong and the test says so.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime

import pytest
from vollib.black_scholes import black_scholes as vollib_price
from vollib.black_scholes.greeks import analytical as vollib_greeks
from vollib.black_scholes_merton import black_scholes_merton as vollib_price_q

from optscan.analytics.greeks import (
    DAYS_PER_YEAR,
    Greeks,
    bsm_greeks,
    bsm_price,
    d1_d2,
    expiry_moment,
    forward_price,
    intrinsic_value,
    norm_cdf,
    norm_pdf,
    time_to_expiry,
)
from optscan.models import Right

# The textbook case.
S, K, T, R, SIGMA = 100.0, 100.0, 1.0, 0.05, 0.20

TOLERANCE = 1e-7


class TestNormalFunctions:
    def test_cdf_at_known_points(self) -> None:
        assert norm_cdf(0.0) == pytest.approx(0.5)
        assert norm_cdf(0.35) == pytest.approx(0.6368307, abs=1e-7)
        assert norm_cdf(0.15) == pytest.approx(0.5596177, abs=1e-7)
        assert norm_cdf(-1.96) == pytest.approx(0.0249979, abs=1e-7)

    def test_cdf_is_symmetric(self) -> None:
        for x in (0.1, 0.35, 1.0, 2.5):
            assert norm_cdf(x) + norm_cdf(-x) == pytest.approx(1.0)

    def test_pdf_at_known_points(self) -> None:
        assert norm_pdf(0.0) == pytest.approx(0.3989423, abs=1e-7)
        assert norm_pdf(0.35) == pytest.approx(0.3752403, abs=1e-7)


class TestD1D2:
    def test_textbook_values(self) -> None:
        first, second = d1_d2(S, K, T, R, SIGMA)
        assert first == pytest.approx(0.35, abs=TOLERANCE)
        assert second == pytest.approx(0.15, abs=TOLERANCE)

    @pytest.mark.parametrize(
        ("spot", "strike", "time", "sigma"),
        [(0.0, 100, 1, 0.2), (100, 0.0, 1, 0.2), (100, 100, 0.0, 0.2), (100, 100, 1, 0.0)],
    )
    def test_undefined_inputs_raise(
        self, spot: float, strike: float, time: float, sigma: float
    ) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            d1_d2(spot, strike, time, R, sigma)


class TestPrice:
    def test_call_matches_hand_calculation(self) -> None:
        assert bsm_price(Right.CALL, S, K, T, R, SIGMA) == pytest.approx(10.4505836, abs=1e-7)

    def test_put_matches_hand_calculation(self) -> None:
        assert bsm_price(Right.PUT, S, K, T, R, SIGMA) == pytest.approx(5.5735260, abs=1e-7)

    def test_put_call_parity(self) -> None:
        """C - P = S - K*e^-rT. Holds by construction, and catches a sign error fast."""
        call = bsm_price("C", S, K, T, R, SIGMA)
        put = bsm_price("P", S, K, T, R, SIGMA)
        assert call - put == pytest.approx(S - K * math.exp(-R * T), abs=1e-9)

    def test_put_call_parity_with_dividends(self) -> None:
        """C - P = S*e^-qT - K*e^-rT once a carry is present."""
        q = 0.03
        call = bsm_price("C", S, K, T, R, SIGMA, q)
        put = bsm_price("P", S, K, T, R, SIGMA, q)
        expected = S * math.exp(-q * T) - K * math.exp(-R * T)
        assert call - put == pytest.approx(expected, abs=1e-9)

    @pytest.mark.parametrize("right", ["c", "p"])
    @pytest.mark.parametrize("spot", [80.0, 95.0, 100.0, 105.0, 130.0])
    @pytest.mark.parametrize("time", [0.02, 0.25, 1.0, 2.0])
    @pytest.mark.parametrize("sigma", [0.08, 0.2, 0.75])
    def test_agrees_with_vollib_across_a_grid(
        self, right: str, spot: float, time: float, sigma: float
    ) -> None:
        ours = bsm_price(right, spot, K, time, R, sigma)
        theirs = vollib_price(right, spot, K, time, R, sigma)
        assert ours == pytest.approx(theirs, abs=1e-9)

    @pytest.mark.parametrize("right", ["c", "p"])
    @pytest.mark.parametrize("q", [0.0, 0.015, 0.06])
    def test_agrees_with_vollib_on_dividends(self, right: str, q: float) -> None:
        ours = bsm_price(right, S, K, T, R, SIGMA, q)
        theirs = vollib_price_q(right, S, K, T, R, sigma=SIGMA, q=q)
        assert ours == pytest.approx(theirs, abs=1e-9)

    def test_expired_options_are_worth_intrinsic(self) -> None:
        assert bsm_price("C", 105.0, 100.0, 0.0, R, SIGMA) == pytest.approx(5.0)
        assert bsm_price("C", 95.0, 100.0, 0.0, R, SIGMA) == 0.0
        assert bsm_price("P", 95.0, 100.0, 0.0, R, SIGMA) == pytest.approx(5.0)
        assert bsm_price("P", 105.0, 100.0, 0.0, R, SIGMA) == 0.0

    def test_zero_volatility_is_the_discounted_forward_payoff(self) -> None:
        """No vol means the outcome is certain, so the option is worth its carry."""
        price = bsm_price("C", 100.0, 90.0, 1.0, R, 0.0)
        assert price == pytest.approx(100.0 - 90.0 * math.exp(-R), abs=1e-9)

    def test_price_never_goes_below_zero(self) -> None:
        assert bsm_price("C", 50.0, 100.0, 0.01, R, 0.05) >= 0.0
        assert bsm_price("P", 200.0, 100.0, 0.01, R, 0.05) >= 0.0

    def test_deep_itm_call_approaches_forward_minus_discounted_strike(self) -> None:
        price = bsm_price("C", 1000.0, 100.0, 1.0, R, SIGMA)
        assert price == pytest.approx(1000.0 - 100.0 * math.exp(-R), abs=1e-6)


class TestGreeks:
    def test_call_matches_hand_calculation(self) -> None:
        greeks = bsm_greeks(Right.CALL, S, K, T, R, SIGMA)
        assert greeks.price == pytest.approx(10.4505836, abs=1e-7)
        assert greeks.delta == pytest.approx(0.6368307, abs=1e-7)
        assert greeks.gamma == pytest.approx(0.0187620, abs=1e-7)
        assert greeks.vega == pytest.approx(0.3752403, abs=1e-7)
        assert greeks.theta == pytest.approx(-0.0175727, abs=1e-7)
        assert greeks.rho == pytest.approx(0.5323248, abs=1e-7)

    def test_put_matches_hand_calculation(self) -> None:
        """Put delta = call delta - 1. Put rho is negative: rates hurt a put."""
        greeks = bsm_greeks(Right.PUT, S, K, T, R, SIGMA)
        assert greeks.price == pytest.approx(5.5735260, abs=1e-7)
        assert greeks.delta == pytest.approx(0.6368307 - 1.0, abs=1e-7)
        assert greeks.gamma == pytest.approx(0.0187620, abs=1e-7)
        assert greeks.vega == pytest.approx(0.3752403, abs=1e-7)
        assert greeks.rho < 0

    @pytest.mark.parametrize("right", ["c", "p"])
    @pytest.mark.parametrize("spot", [85.0, 100.0, 115.0])
    @pytest.mark.parametrize("time", [0.05, 0.5, 2.0])
    @pytest.mark.parametrize("sigma", [0.12, 0.35])
    def test_agrees_with_vollib_across_a_grid(
        self, right: str, spot: float, time: float, sigma: float
    ) -> None:
        ours = bsm_greeks(right, spot, K, time, R, sigma)
        assert ours.delta == pytest.approx(vollib_greeks.delta(right, spot, K, time, R, sigma))
        assert ours.gamma == pytest.approx(vollib_greeks.gamma(right, spot, K, time, R, sigma))
        assert ours.vega == pytest.approx(vollib_greeks.vega(right, spot, K, time, R, sigma))
        assert ours.theta == pytest.approx(vollib_greeks.theta(right, spot, K, time, R, sigma))
        assert ours.rho == pytest.approx(vollib_greeks.rho(right, spot, K, time, R, sigma))

    def test_gamma_and_vega_are_the_same_for_calls_and_puts(self) -> None:
        call = bsm_greeks("C", 105.0, K, 0.3, R, 0.25)
        put = bsm_greeks("P", 105.0, K, 0.3, R, 0.25)
        assert call.gamma == pytest.approx(put.gamma)
        assert call.vega == pytest.approx(put.vega)

    def test_delta_bounds(self) -> None:
        assert 0.0 <= bsm_greeks("C", 60.0, K, 1.0, R, SIGMA).delta <= 1.0
        assert -1.0 <= bsm_greeks("P", 140.0, K, 1.0, R, SIGMA).delta <= 0.0

    def test_short_premium_signs(self) -> None:
        """The whole premise of the tool: long options bleed theta and own vega."""
        greeks = bsm_greeks("P", 100.0, 95.0, 30 / DAYS_PER_YEAR, R, 0.25)
        assert greeks.theta < 0
        assert greeks.vega > 0
        assert greeks.gamma > 0

    def test_theta_is_per_day_not_per_year(self) -> None:
        """A 100 strike ATM option does not lose 6 dollars a day."""
        greeks = bsm_greeks("C", S, K, T, R, SIGMA)
        assert -0.02 < greeks.theta < 0.0

    def test_vega_is_per_point_not_per_unit(self) -> None:
        """Bumping sigma by one point should move price by about vega."""
        base = bsm_price("C", S, K, T, R, SIGMA)
        bumped = bsm_price("C", S, K, T, R, SIGMA + 0.01)
        assert bumped - base == pytest.approx(bsm_greeks("C", S, K, T, R, SIGMA).vega, rel=0.01)

    def test_delta_matches_a_numeric_bump(self) -> None:
        step = 1e-5
        up = bsm_price("C", S + step, K, T, R, SIGMA)
        down = bsm_price("C", S - step, K, T, R, SIGMA)
        assert (up - down) / (2 * step) == pytest.approx(
            bsm_greeks("C", S, K, T, R, SIGMA).delta, abs=1e-6
        )

    def test_gamma_matches_a_numeric_bump(self) -> None:
        step = 1e-3
        up = bsm_greeks("C", S + step, K, T, R, SIGMA).delta
        down = bsm_greeks("C", S - step, K, T, R, SIGMA).delta
        assert (up - down) / (2 * step) == pytest.approx(
            bsm_greeks("C", S, K, T, R, SIGMA).gamma, abs=1e-6
        )

    def test_expired_greeks_are_degenerate_not_exploding(self) -> None:
        itm = bsm_greeks("C", 105.0, 100.0, 0.0, R, SIGMA)
        assert itm.delta == 1.0
        assert itm.gamma == 0.0
        assert itm.theta == 0.0
        assert itm.vega == 0.0

        otm = bsm_greeks("P", 105.0, 100.0, 0.0, R, SIGMA)
        assert otm.delta == 0.0

    def test_expired_at_the_money_delta_is_the_conventional_half(self) -> None:
        assert bsm_greeks("C", 100.0, 100.0, 0.0, R, SIGMA).delta == 0.5
        assert bsm_greeks("P", 100.0, 100.0, 0.0, R, SIGMA).delta == -0.5

    def test_scaled_to_a_position(self) -> None:
        per_share = bsm_greeks("P", 100.0, 95.0, 0.1, R, 0.25)
        position = per_share.scaled(contracts=3, contract_size=100)
        assert position.delta == pytest.approx(per_share.delta * 300)
        assert position.theta == pytest.approx(per_share.theta * 300)
        assert isinstance(position, Greeks)


class TestTimeToExpiry:
    def test_expiry_is_sixteen_hundred_new_york(self) -> None:
        """20:00 UTC in summer, when New York is on daylight time."""
        assert expiry_moment(date(2026, 8, 21)) == datetime(2026, 8, 21, 20, 0, tzinfo=UTC)
        # And 21:00 UTC in winter, on standard time.
        assert expiry_moment(date(2026, 12, 18)) == datetime(2026, 12, 18, 21, 0, tzinfo=UTC)

    def test_one_day_before_the_close(self) -> None:
        asof = datetime(2026, 8, 20, 20, 0, tzinfo=UTC)
        assert time_to_expiry(date(2026, 8, 21), asof) == pytest.approx(1 / DAYS_PER_YEAR)

    def test_zero_dte_keeps_intraday_precision(self) -> None:
        """At 15:45 New York on expiry day there are 15 minutes left, not zero and not a day."""
        asof = datetime(2026, 8, 21, 19, 45, tzinfo=UTC)
        expected = (15 * 60) / (DAYS_PER_YEAR * 24 * 3600)
        assert time_to_expiry(date(2026, 8, 21), asof) == pytest.approx(expected)

    def test_after_expiry_is_zero_not_negative(self) -> None:
        asof = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
        assert time_to_expiry(date(2026, 8, 21), asof) == 0.0

    def test_naive_asof_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone aware"):
            time_to_expiry(date(2026, 8, 21), datetime(2026, 8, 20, 20, 0))


class TestHelpers:
    def test_intrinsic_value(self) -> None:
        assert intrinsic_value("C", 105.0, 100.0) == 5.0
        assert intrinsic_value("C", 95.0, 100.0) == 0.0
        assert intrinsic_value("P", 95.0, 100.0) == 5.0
        assert intrinsic_value("P", 105.0, 100.0) == 0.0

    def test_forward_price(self) -> None:
        assert forward_price(100.0, 1.0, 0.05) == pytest.approx(105.1271096, abs=1e-7)
        assert forward_price(100.0, 1.0, 0.05, 0.05) == pytest.approx(100.0)
