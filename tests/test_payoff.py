"""Payoff diagrams.

Expected values are hand computed from the position's own arithmetic, which for an
expiry payoff is simple enough to do in the docstring every time.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from optscan.analytics.payoff import (
    breakevens,
    build_payoff,
    expiry_extremes,
    net_credit,
    pnl_at_expiry,
    pnl_at_time,
)
from optscan.models import Action, Leg, Right

NOW = datetime(2026, 7, 30, 19, 45, tzinfo=UTC)
EXPIRY = date(2026, 8, 21)


def leg(action: Action, right: Right, strike: float, mid: float, iv: float = 0.20) -> Leg:
    return Leg(
        action=action,
        right=right,
        strike=strike,
        expiry=EXPIRY,
        mid=mid,
        bid=mid - 0.05,
        ask=mid + 0.05,
        iv=iv,
        fetched_at=NOW,
        source="test",
    )


SHORT_PUT = (leg(Action.SELL, Right.PUT, 95.0, 2.00),)

PUT_SPREAD = (
    leg(Action.SELL, Right.PUT, 95.0, 2.00),
    leg(Action.BUY, Right.PUT, 90.0, 0.50),
)

IRON_CONDOR = (
    leg(Action.SELL, Right.PUT, 95.0, 2.00),
    leg(Action.BUY, Right.PUT, 90.0, 0.50),
    leg(Action.SELL, Right.CALL, 105.0, 2.00),
    leg(Action.BUY, Right.CALL, 110.0, 0.50),
)

STRANGLE = (
    leg(Action.SELL, Right.PUT, 95.0, 2.00),
    leg(Action.SELL, Right.CALL, 105.0, 2.00),
)


class TestNetCredit:
    def test_a_short_put_takes_in_its_premium(self) -> None:
        assert net_credit(SHORT_PUT) == pytest.approx(200.0)

    def test_a_spread_nets_the_two_legs(self) -> None:
        """Sell 2.00, buy 0.50, so 1.50 in, which is 150 dollars."""
        assert net_credit(PUT_SPREAD) == pytest.approx(150.0)

    def test_a_condor_nets_four(self) -> None:
        assert net_credit(IRON_CONDOR) == pytest.approx(300.0)

    def test_an_unpriced_leg_is_an_error(self) -> None:
        broken = (leg(Action.SELL, Right.PUT, 95.0, 2.00).model_copy(update={"mid": None}),)
        with pytest.raises(ValueError, match="no price"):
            net_credit(broken)


class TestExpiryPayoff:
    def test_short_put_above_the_strike_keeps_the_credit(self) -> None:
        assert pnl_at_expiry(SHORT_PUT, 100.0) == pytest.approx(200.0)
        assert pnl_at_expiry(SHORT_PUT, 95.0) == pytest.approx(200.0)

    def test_short_put_below_the_strike_gives_it_back(self) -> None:
        """At 93 the put is worth 2.00 intrinsic, exactly the credit taken in."""
        assert pnl_at_expiry(SHORT_PUT, 93.0) == pytest.approx(0.0)
        # At 90 it is 5.00 intrinsic against 2.00 of credit: a 300 dollar loss.
        assert pnl_at_expiry(SHORT_PUT, 90.0) == pytest.approx(-300.0)

    def test_spread_caps_the_loss_at_the_long_strike(self) -> None:
        """5 wide for 1.50 of credit, so 350 of maximum loss, reached at 90 and held."""
        assert pnl_at_expiry(PUT_SPREAD, 100.0) == pytest.approx(150.0)
        assert pnl_at_expiry(PUT_SPREAD, 90.0) == pytest.approx(-350.0)
        assert pnl_at_expiry(PUT_SPREAD, 50.0) == pytest.approx(-350.0)
        assert pnl_at_expiry(PUT_SPREAD, 0.0) == pytest.approx(-350.0)

    def test_condor_keeps_everything_between_the_shorts(self) -> None:
        assert pnl_at_expiry(IRON_CONDOR, 100.0) == pytest.approx(300.0)
        # 5 wide wings against 3.00 of credit: 200 of loss on either side.
        assert pnl_at_expiry(IRON_CONDOR, 90.0) == pytest.approx(-200.0)
        assert pnl_at_expiry(IRON_CONDOR, 110.0) == pytest.approx(-200.0)

    def test_strangle_loses_without_limit(self) -> None:
        assert pnl_at_expiry(STRANGLE, 100.0) == pytest.approx(400.0)
        assert pnl_at_expiry(STRANGLE, 60.0) == pytest.approx(400.0 - 3500.0)
        assert pnl_at_expiry(STRANGLE, 140.0) == pytest.approx(400.0 - 3500.0)


class TestBreakevens:
    def test_short_put(self) -> None:
        """Strike less credit: 95 - 2.00."""
        assert breakevens(SHORT_PUT) == pytest.approx((93.0,))

    def test_put_spread(self) -> None:
        """Short strike less net credit: 95 - 1.50."""
        assert breakevens(PUT_SPREAD) == pytest.approx((93.5,))

    def test_condor_has_two(self) -> None:
        """95 - 3.00 and 105 + 3.00."""
        assert breakevens(IRON_CONDOR) == pytest.approx((92.0, 108.0))

    def test_strangle_has_two(self) -> None:
        assert breakevens(STRANGLE) == pytest.approx((91.0, 109.0))

    def test_they_are_where_the_curve_actually_crosses(self) -> None:
        """Solved rather than sampled, so this has to hold exactly."""
        for legs in (SHORT_PUT, PUT_SPREAD, IRON_CONDOR, STRANGLE):
            for level in breakevens(legs):
                assert pnl_at_expiry(legs, level) == pytest.approx(0.0, abs=1e-6)

    def test_no_legs_no_breakevens(self) -> None:
        assert breakevens(()) == ()


class TestExtremes:
    def test_defined_risk_is_bounded_both_ways(self) -> None:
        max_profit, max_loss = expiry_extremes(PUT_SPREAD)
        assert max_profit == pytest.approx(150.0)
        assert max_loss == pytest.approx(-350.0)

    def test_condor_is_bounded_both_ways(self) -> None:
        max_profit, max_loss = expiry_extremes(IRON_CONDOR)
        assert max_profit == pytest.approx(300.0)
        assert max_loss == pytest.approx(-200.0)

    def test_a_strangle_has_no_maximum_loss(self) -> None:
        """Unbounded on the upside, so the answer is None rather than a large number.

        Reporting the loss at some arbitrary far strike would imply a bound that does
        not exist.
        """
        max_profit, max_loss = expiry_extremes(STRANGLE)
        assert max_profit == pytest.approx(400.0)
        assert max_loss is None

    def test_a_short_put_is_bounded_only_because_the_stock_stops_at_zero(self) -> None:
        max_profit, max_loss = expiry_extremes(SHORT_PUT)
        assert max_profit == pytest.approx(200.0)
        assert max_loss == pytest.approx(-9300.0)


class TestNowCurve:
    def test_time_value_keeps_the_short_position_below_its_expiry_payoff(self) -> None:
        """The thing the expiry diagram alone hides.

        A short put at the money is worth less than its full credit today, so marking
        it now shows a smaller profit than holding it to expiry would.
        """
        time = 22 / 365
        at_expiry = pnl_at_expiry(SHORT_PUT, 100.0)
        now = pnl_at_time(SHORT_PUT, 100.0, time, 0.043)
        assert now is not None
        assert now < at_expiry

    def test_the_two_curves_converge_as_time_runs_out(self) -> None:
        far = pnl_at_time(SHORT_PUT, 100.0, 90 / 365, 0.043)
        near = pnl_at_time(SHORT_PUT, 100.0, 1 / 365, 0.043)
        at_expiry = pnl_at_expiry(SHORT_PUT, 100.0)
        assert abs(near - at_expiry) < abs(far - at_expiry)

    def test_a_deep_loss_is_smaller_today_than_at_expiry(self) -> None:
        """Below the strike the position is losing, and time value cushions it."""
        now = pnl_at_time(PUT_SPREAD, 88.0, 22 / 365, 0.043)
        assert now is not None
        assert now > pnl_at_expiry(PUT_SPREAD, 88.0)

    def test_no_volatility_means_no_curve(self) -> None:
        """Half a diagram is worse than none."""
        legs = (leg(Action.SELL, Right.PUT, 95.0, 2.00).model_copy(update={"iv": None}),)
        assert pnl_at_time(legs, 100.0, 0.1, 0.043) is None


class TestBuildPayoff:
    def test_shape_and_range(self) -> None:
        payoff = build_payoff(PUT_SPREAD, 100.0, time=22 / 365, rate=0.043, points=51)
        assert len(payoff.points) == 51
        assert payoff.prices[0] == pytest.approx(80.0)
        assert payoff.prices[-1] == pytest.approx(120.0)

    def test_carries_the_numbers_a_table_would_show(self) -> None:
        payoff = build_payoff(IRON_CONDOR, 100.0, time=22 / 365, rate=0.043)
        assert payoff.breakevens == pytest.approx((92.0, 108.0))
        assert payoff.max_profit == pytest.approx(300.0)
        assert payoff.max_loss == pytest.approx(-200.0)
        assert payoff.net_credit == pytest.approx(300.0)
        assert payoff.spot == 100.0

    def test_both_curves_are_present_when_there_is_time_left(self) -> None:
        payoff = build_payoff(PUT_SPREAD, 100.0, time=22 / 365, rate=0.043)
        assert all(point.at_now is not None for point in payoff.points)

    def test_only_one_curve_at_expiry(self) -> None:
        payoff = build_payoff(PUT_SPREAD, 100.0, time=0.0)
        assert all(point.at_now is None for point in payoff.points)

    def test_bad_inputs_are_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one leg"):
            build_payoff((), 100.0)
        with pytest.raises(ValueError, match="spot must be positive"):
            build_payoff(PUT_SPREAD, 0.0)
        with pytest.raises(ValueError, match="at least two points"):
            build_payoff(PUT_SPREAD, 100.0, points=1)
