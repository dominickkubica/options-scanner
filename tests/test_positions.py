"""Positions: the model's arithmetic, storage, and the risk it reports.

The sign convention is the load bearing thing in this file. A short call has positive
delta per contract and negative delta as a position, and a book that added the first
would read as long while it is short. Every greek gets that checked in both directions,
because it is invisible once it is wrong.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from optscan.analytics.portfolio import (
    MIN_BETA_OBSERVATIONS,
    Beta,
    LegRisk,
    PortfolioRisk,
    PositionRisk,
    beta,
)
from optscan.models import PriceBar, Right
from optscan.models.opportunity import Action, Strategy
from optscan.models.position import Position, PositionLeg, PositionStatus
from optscan.storage import db
from optscan.storage import positions as store

OPENED = datetime(2026, 7, 1, 14, 30, tzinfo=UTC)
TODAY = date(2026, 7, 31)
EXPIRY = date(2026, 8, 21)


def leg(
    action: Action = Action.SELL,
    right: Right = Right.PUT,
    strike: float = 700.0,
    fill: float = 5.00,
    quantity: int = 1,
) -> PositionLeg:
    return PositionLeg(
        action=action,
        right=right,
        strike=strike,
        expiry=EXPIRY,
        quantity=quantity,
        fill_price=fill,
    )


def position(*legs: PositionLeg, **kwargs) -> Position:
    return Position(
        symbol="SPY",
        legs=legs or (leg(),),
        opened_at=OPENED,
        **kwargs,
    )


class TestFillArithmetic:
    def test_a_sold_put_is_a_credit(self) -> None:
        """One contract sold at 5.00 is 500 dollars in."""
        assert leg().signed_fill == pytest.approx(500.0)

    def test_a_bought_put_is_a_debit(self) -> None:
        assert leg(action=Action.BUY).signed_fill == pytest.approx(-500.0)

    def test_profit_on_a_short_is_the_credit_less_the_buyback(self) -> None:
        """Sold at 5.00, now marked 2.00, so 300 dollars of the credit is kept."""
        assert leg().profit(2.00) == pytest.approx(300.0)

    def test_profit_on_a_long_is_the_gain_in_value(self) -> None:
        """Bought at 5.00, now worth 8.00, so 300 dollars made. Same formula, and that
        is the point of the sign convention."""
        assert leg(action=Action.BUY).profit(8.00) == pytest.approx(300.0)

    def test_an_unmarkable_leg_has_no_profit_rather_than_zero(self) -> None:
        assert leg().profit(None) is None

    def test_a_negative_fill_is_refused(self) -> None:
        """Direction lives in the action, so a negative fill is a typo, not a short."""
        with pytest.raises(ValueError):
            leg(fill=-5.0)


class TestPositionArithmetic:
    def test_entry_credit_nets_the_legs(self) -> None:
        """A put credit spread: sell the 700 at 5.00, buy the 690 at 2.00."""
        spread = position(
            leg(strike=700.0, fill=5.00),
            leg(action=Action.BUY, strike=690.0, fill=2.00),
        )
        assert spread.entry_credit == pytest.approx(300.0)

    def test_commission_comes_off_the_credit(self) -> None:
        held = position(commission_open=1.30)
        assert held.net_credit == pytest.approx(498.70)

    def test_unrealized_uses_the_net_credit(self) -> None:
        held = position(commission_open=1.30)
        marks = {(700.0, Right.PUT): 2.00}
        assert held.unrealized(marks) == pytest.approx(298.70)

    def test_the_closing_commission_is_not_charged_while_open(self) -> None:
        """It has not been paid and may never be. Most of these expire worthless, and
        charging it early would understate every winner."""
        held = position(commission_open=1.30, commission_close=1.30)
        marks = {(700.0, Right.PUT): 2.00}
        assert held.unrealized(marks) == pytest.approx(298.70)

    def test_a_partially_marked_position_has_no_value_at_all(self) -> None:
        """Two thirds of a three legged position is not two thirds of a number."""
        spread = position(
            leg(strike=700.0),
            leg(action=Action.BUY, strike=690.0, fill=2.00),
        )
        assert spread.current_value({(700.0, Right.PUT): 2.00}) is None
        assert spread.unrealized({(700.0, Right.PUT): 2.00}) is None

    def test_profit_fraction_reads_against_maximum_profit(self) -> None:
        """Sold for 5.00, now worth 2.50, so exactly half the credit is banked."""
        held = position()
        assert held.profit_fraction({(700.0, Right.PUT): 2.50}) == pytest.approx(0.5)

    def test_a_debit_position_has_no_profit_fraction(self) -> None:
        """A rule written for credit positions must not silently answer for a debit."""
        held = position(leg(action=Action.BUY))
        assert held.max_profit() is None
        assert held.profit_fraction({(700.0, Right.PUT): 8.00}) is None

    def test_realized_charges_both_commissions(self) -> None:
        held = position(commission_open=1.30)
        closed = held.close(200.0, commission=1.30, when=datetime(2026, 7, 20, tzinfo=UTC))
        assert closed.realized == pytest.approx(500.0 - 1.30 - 200.0 - 1.30)

    def test_an_open_position_has_no_realized_profit(self) -> None:
        assert position().realized is None

    def test_a_closed_position_must_say_when(self) -> None:
        with pytest.raises(ValueError, match="when it was closed"):
            position(status=PositionStatus.CLOSED)

    def test_mixed_contract_sizes_are_refused(self) -> None:
        """Almost always a data entry mistake, and it makes every aggregate wrong."""
        odd = PositionLeg(
            action=Action.BUY,
            right=Right.PUT,
            strike=690.0,
            expiry=EXPIRY,
            fill_price=2.0,
            contract_size=137,
        )
        with pytest.raises(ValueError, match="different contract sizes"):
            position(leg(), odd)


class TestTested:
    def test_a_short_put_is_tested_below_its_strike(self) -> None:
        held = position(leg(right=Right.PUT, strike=700.0))
        assert held.tested(699.0) is True
        assert held.tested(701.0) is False

    def test_a_short_call_is_tested_above_its_strike(self) -> None:
        held = position(leg(right=Right.CALL, strike=760.0))
        assert held.tested(761.0) is True
        assert held.tested(759.0) is False

    def test_long_legs_do_not_make_a_position_tested(self) -> None:
        """Tested is about the strikes that can be assigned."""
        held = position(leg(action=Action.BUY, right=Right.PUT, strike=700.0))
        assert held.tested(600.0) is False


class TestGreekSigns:
    """The invisible failure. Every sign checked in both directions."""

    def _risk(self, action: Action, right: Right, **greeks) -> LegRisk:
        return LegRisk(leg=leg(action=action, right=right), **greeks)

    def test_a_short_call_is_short_delta(self) -> None:
        """Call delta is positive per contract; a short call is a bearish position."""
        item = self._risk(Action.SELL, Right.CALL, delta=0.30)
        assert item.position_delta == pytest.approx(-30.0)

    def test_a_short_put_is_long_delta(self) -> None:
        """Put delta is negative per contract; a short put is a bullish position."""
        item = self._risk(Action.SELL, Right.PUT, delta=-0.30)
        assert item.position_delta == pytest.approx(30.0)

    def test_a_long_call_is_long_delta(self) -> None:
        item = self._risk(Action.BUY, Right.CALL, delta=0.30)
        assert item.position_delta == pytest.approx(30.0)

    def test_a_short_option_collects_theta(self) -> None:
        """Per contract theta is negative for a long option. Short flips it positive,
        which is the whole reason anyone sells premium."""
        item = self._risk(Action.SELL, Right.PUT, theta=-0.05)
        assert item.position_theta == pytest.approx(5.0)

    def test_a_short_option_is_short_vega_and_gamma(self) -> None:
        item = self._risk(Action.SELL, Right.PUT, vega=0.20, gamma=0.01)
        assert item.position_vega == pytest.approx(-20.0)
        assert item.position_gamma == pytest.approx(-1.0)

    def test_a_missing_greek_stays_missing(self) -> None:
        assert self._risk(Action.SELL, Right.PUT).position_delta is None


class TestPositionRisk:
    def _risk(self, *legs: LegRisk, spot: float = 740.0, **kwargs) -> PositionRisk:
        return PositionRisk(
            position=position(*[item.leg for item in legs]),
            spot=spot,
            legs=legs,
            asof=TODAY,
            marks_complete=all(item.mark is not None for item in legs),
            **kwargs,
        )

    def test_greeks_add_across_legs(self) -> None:
        risk = self._risk(
            LegRisk(leg=leg(strike=700.0), mark=2.0, delta=-0.20, theta=-0.05),
            LegRisk(
                leg=leg(action=Action.BUY, strike=690.0, fill=2.0),
                mark=1.0,
                delta=-0.10,
                theta=-0.03,
            ),
        )
        # Short put +20, long put -10.
        assert risk.delta == pytest.approx(10.0)
        # Short collects 5, long pays 3.
        assert risk.theta == pytest.approx(2.0)

    def test_one_unsolved_leg_withholds_the_whole_greek(self) -> None:
        """A condor summed over three of four legs is off by whichever wing failed."""
        risk = self._risk(
            LegRisk(leg=leg(strike=700.0), mark=2.0, delta=-0.20),
            LegRisk(leg=leg(action=Action.BUY, strike=690.0, fill=2.0), mark=1.0),
        )
        assert risk.delta is None

    def test_beta_weighted_delta_is_dollars_of_reference_exposure(self) -> None:
        fit = Beta(value=1.2, observations=250, span_days=365, r_squared=0.8, reference="SPY")
        risk = self._risk(
            LegRisk(leg=leg(), mark=2.0, delta=-0.20),
            spot=740.0,
            beta=fit,
        )
        # 20 share delta, times beta 1.2, times 740 spot.
        assert risk.beta_weighted_delta == pytest.approx(20.0 * 1.2 * 740.0)

    def test_an_unusable_beta_withholds_the_weighting(self) -> None:
        fit = Beta(value=None, observations=10, span_days=20, r_squared=0.0, reference="SPY")
        risk = self._risk(LegRisk(leg=leg(), mark=2.0, delta=-0.20), beta=fit)
        assert risk.beta_weighted_delta is None


class TestPortfolioRisk:
    def test_profit_totals_across_what_could_be_marked(self) -> None:
        """A sum rather than a refusal, because an incomplete profit is still the
        profit of the positions in it, and `unmarked` says how many are missing."""
        marked = PositionRisk(
            position=position(),
            spot=740.0,
            legs=(LegRisk(leg=leg(), mark=2.0, delta=-0.2),),
            asof=TODAY,
            marks_complete=True,
        )
        unmarked = PositionRisk(
            position=position(),
            spot=740.0,
            legs=(LegRisk(leg=leg()),),
            asof=TODAY,
            marks_complete=False,
        )
        book = PortfolioRisk(positions=(marked, unmarked), asof=TODAY)

        assert book.unrealized == pytest.approx(300.0)
        assert book.unmarked == 1

    def test_greeks_refuse_when_any_position_is_missing_one(self) -> None:
        """Unlike profit. A delta that silently omits a position is used to size a
        hedge, which is a different kind of wrong."""
        marked = PositionRisk(
            position=position(),
            spot=740.0,
            legs=(LegRisk(leg=leg(), mark=2.0, delta=-0.2),),
            asof=TODAY,
            marks_complete=True,
        )
        unmarked = PositionRisk(
            position=position(),
            spot=740.0,
            legs=(LegRisk(leg=leg()),),
            asof=TODAY,
            marks_complete=False,
        )
        assert PortfolioRisk(positions=(marked, unmarked), asof=TODAY).delta is None


class TestBeta:
    def _bars(self, symbol: str, returns: list[float], start: float = 100.0) -> list[PriceBar]:
        bars = []
        price = start
        for index, value in enumerate(returns):
            price *= 1.0 + value
            bars.append(
                PriceBar(
                    symbol=symbol,
                    ts=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index),
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=1000,
                    fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
                    source="test",
                )
            )
        return bars

    def test_a_symbol_that_moves_twice_as_hard_has_a_beta_of_two(self) -> None:
        import random

        random.seed(11)
        reference = [random.gauss(0.0, 0.01) for _ in range(200)]
        symbol = [value * 2.0 for value in reference]

        fit = beta(self._bars("X", symbol), self._bars("SPY", reference))
        assert fit is not None
        assert fit.value == pytest.approx(2.0, rel=0.02)
        assert fit.r_squared == pytest.approx(1.0, abs=0.01)

    def test_too_little_overlap_gives_no_value_rather_than_one(self) -> None:
        """A default of 1.0 is a measured looking number for an unmeasured thing."""
        import random

        random.seed(3)
        returns = [random.gauss(0.0, 0.01) for _ in range(20)]
        fit = beta(self._bars("X", returns), self._bars("SPY", returns))

        assert fit is not None
        assert fit.value is None
        assert fit.usable is False
        assert "too few" in (fit.caveat() or "")

    def test_only_overlapping_sessions_are_used(self) -> None:
        """Two vendors can disagree about holidays, and a one day offset turns a beta
        of 1.0 into noise."""
        import random

        random.seed(5)
        returns = [random.gauss(0.0, 0.01) for _ in range(200)]
        reference_bars = self._bars("SPY", returns)
        symbol_bars = self._bars("X", returns)[:120]

        fit = beta(symbol_bars, reference_bars)
        assert fit is not None
        assert fit.observations <= 120

    def test_the_minimum_is_a_quarter_of_sessions(self) -> None:
        assert MIN_BETA_OBSERVATIONS == 60

    def test_a_loose_fit_says_so(self) -> None:
        import random

        random.seed(9)
        reference = [random.gauss(0.0, 0.01) for _ in range(300)]
        symbol = [random.gauss(0.0, 0.01) for _ in range(300)]

        fit = beta(self._bars("X", symbol), self._bars("SPY", reference))
        assert fit is not None
        assert fit.r_squared < 0.25
        assert "loosely" in (fit.caveat() or "")


class TestStorage:
    @pytest.fixture
    def conn(self, tmp_path):
        with db.session(tmp_path / "optscan.sqlite") as connection:
            yield connection

    def test_a_position_round_trips(self, conn) -> None:
        spread = position(
            leg(strike=700.0, fill=5.00),
            leg(action=Action.BUY, strike=690.0, fill=2.00),
            strategy=Strategy.PUT_CREDIT_SPREAD,
            commission_open=1.30,
            note="test entry",
        )
        stored = store.add_position(conn, spread)

        assert stored.id is not None
        loaded = store.get_position(conn, stored.id)
        assert loaded is not None
        assert loaded.symbol == "SPY"
        assert len(loaded.legs) == 2
        assert loaded.entry_credit == pytest.approx(300.0)
        assert loaded.strategy is Strategy.PUT_CREDIT_SPREAD
        assert loaded.note == "test entry"

    def test_listing_defaults_to_open_only(self, conn) -> None:
        """A portfolio total that quietly included last year's closed trades would be
        nonsense."""
        first = store.add_position(conn, position())
        store.add_position(conn, position())
        store.close_position(conn, first.id, 200.0)

        assert len(store.list_positions(conn)) == 1
        assert len(store.list_positions(conn, status=None)) == 2

    def test_closing_records_the_value_rather_than_re_marking(self, conn) -> None:
        stored = store.add_position(conn, position(commission_open=1.30))
        closed = store.close_position(conn, stored.id, 200.0, commission=1.30)

        assert closed is not None
        assert closed.status is PositionStatus.CLOSED
        assert closed.realized == pytest.approx(500.0 - 1.30 - 200.0 - 1.30)
        assert store.get_position(conn, stored.id).realized == pytest.approx(closed.realized)

    def test_closing_twice_is_refused(self, conn) -> None:
        stored = store.add_position(conn, position())
        store.close_position(conn, stored.id, 200.0)
        with pytest.raises(ValueError, match="already closed"):
            store.close_position(conn, stored.id, 100.0)

    def test_deleting_takes_the_legs_with_it(self, conn) -> None:
        stored = store.add_position(conn, position(leg(), leg(strike=690.0)))
        assert store.delete_position(conn, stored.id) == 1

        rows = conn.execute(
            "SELECT COUNT(*) FROM position_leg WHERE position_id = ?", (stored.id,)
        ).fetchone()[0]
        assert rows == 0


class TestAlertSuppression:
    @pytest.fixture
    def conn(self, tmp_path):
        with db.session(tmp_path / "optscan.sqlite") as connection:
            yield connection

    def test_an_alert_fires_once(self, conn) -> None:
        """The difference between a tool that is trusted and one that is muted."""
        stored = store.add_position(conn, position())

        assert store.record_alert(conn, stored.id, "tested") is True
        assert store.record_alert(conn, stored.id, "tested") is False
        assert store.already_alerted(conn, stored.id, "tested") is True

    def test_different_kinds_are_independent(self, conn) -> None:
        stored = store.add_position(conn, position())
        assert store.record_alert(conn, stored.id, "tested") is True
        assert store.record_alert(conn, stored.id, "dte") is True

    def test_clearing_lets_them_fire_again(self, conn) -> None:
        """Wanted after a roll: the position is materially different, and suppressing
        its first breach because the old one already fired would hide it."""
        stored = store.add_position(conn, position())
        store.record_alert(conn, stored.id, "tested")

        assert store.clear_alerts(conn, stored.id) == 1
        assert store.record_alert(conn, stored.id, "tested") is True

    def test_suppression_survives_a_reconnect(self, tmp_path) -> None:
        path = tmp_path / "optscan.sqlite"
        with db.session(path) as conn:
            stored = store.add_position(conn, position())
            store.record_alert(conn, stored.id, "tested")
            identifier = stored.id

        with db.session(path) as conn:
            assert store.already_alerted(conn, identifier, "tested") is True
