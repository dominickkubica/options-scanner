"""Management triggers, and the conditions they must not fire on.

Half of these test that nothing happened. A trigger that fires when it should not is
worse than one that misses, because the response to a noisy alert is to stop reading
alerts, and then the one that matters arrives into a muted channel.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from optscan.alerts import Alert, AlertSink, deliver, raise_alerts
from optscan.analytics.events import EventWindow
from optscan.analytics.portfolio import LegRisk, PositionRisk
from optscan.analytics.triggers import (
    Trigger,
    TriggerKind,
    evaluate,
    worst,
)
from optscan.models import Right
from optscan.models.opportunity import Action
from optscan.models.position import Position, PositionLeg
from optscan.storage import db
from optscan.storage import positions as store

TODAY = date(2026, 7, 31)
OPENED = datetime(2026, 7, 1, tzinfo=UTC)


def leg(
    action: Action = Action.SELL,
    right: Right = Right.PUT,
    strike: float = 700.0,
    fill: float = 5.00,
    expiry: date = date(2026, 9, 18),
) -> PositionLeg:
    return PositionLeg(action=action, right=right, strike=strike, expiry=expiry, fill_price=fill)


def risk(
    *legs: LegRisk,
    spot: float = 740.0,
    marks_complete: bool = True,
    identifier: int = 1,
) -> PositionRisk:
    held = Position(
        id=identifier,
        symbol="SPY",
        legs=tuple(item.leg for item in legs),
        opened_at=OPENED,
    )
    return PositionRisk(
        position=held,
        spot=spot,
        legs=legs,
        asof=TODAY,
        marks_complete=marks_complete,
    )


def kinds(triggers: list[Trigger]) -> set[TriggerKind]:
    return {item.kind for item in triggers}


class TestProfitTarget:
    def test_it_fires_at_the_target(self) -> None:
        """Sold at 5.00, now 2.50: exactly half the credit banked."""
        item = risk(LegRisk(leg=leg(), mark=2.50, delta=-0.15))
        assert TriggerKind.PROFIT_TARGET in kinds(evaluate(item, asof=TODAY))

    def test_it_does_not_fire_below_the_target(self) -> None:
        item = risk(LegRisk(leg=leg(), mark=3.50, delta=-0.20))
        assert TriggerKind.PROFIT_TARGET not in kinds(evaluate(item, asof=TODAY))

    def test_it_carries_the_margin_not_just_the_verdict(self) -> None:
        item = risk(LegRisk(leg=leg(), mark=1.00, delta=-0.10))
        fired = next(t for t in evaluate(item, asof=TODAY) if t.kind is TriggerKind.PROFIT_TARGET)

        assert fired.value == pytest.approx(0.80)
        assert fired.threshold == pytest.approx(0.50)

    def test_an_unmarked_position_does_not_fire_it(self) -> None:
        """The worst possible way to be wrong: an unmarked position is not a position
        at zero profit."""
        item = risk(LegRisk(leg=leg(), delta=-0.15), marks_complete=False)
        assert TriggerKind.PROFIT_TARGET not in kinds(evaluate(item, asof=TODAY))


class TestDte:
    def test_it_fires_inside_the_threshold(self) -> None:
        item = risk(LegRisk(leg=leg(expiry=date(2026, 8, 14)), mark=4.0, delta=-0.2))
        assert TriggerKind.DTE in kinds(evaluate(item, asof=TODAY))

    def test_it_does_not_fire_well_out(self) -> None:
        item = risk(LegRisk(leg=leg(expiry=date(2026, 12, 18)), mark=4.0, delta=-0.2))
        assert TriggerKind.DTE not in kinds(evaluate(item, asof=TODAY))

    def test_an_expired_position_says_so_loudly(self) -> None:
        """Still open past expiry means the portfolio totals are counting a position
        that no longer exists."""
        item = risk(LegRisk(leg=leg(expiry=date(2026, 7, 17)), mark=0.0, delta=0.0))
        fired = evaluate(item, asof=TODAY)

        assert TriggerKind.EXPIRED in kinds(fired)
        assert worst(fired).kind is TriggerKind.EXPIRED


class TestDeltaBreach:
    def test_it_fires_on_a_short_leg_past_the_threshold(self) -> None:
        item = risk(LegRisk(leg=leg(), mark=8.0, delta=-0.45))
        fired = evaluate(item, asof=TODAY)

        assert TriggerKind.DELTA_BREACH in kinds(fired)
        breach = next(t for t in fired if t.kind is TriggerKind.DELTA_BREACH)
        assert breach.value == pytest.approx(0.45)

    def test_a_long_leg_past_the_threshold_does_not_fire_it(self) -> None:
        """Delta breach is about the strike that can be assigned."""
        item = risk(LegRisk(leg=leg(action=Action.BUY), mark=8.0, delta=-0.60))
        assert TriggerKind.DELTA_BREACH not in kinds(evaluate(item, asof=TODAY))

    def test_it_measures_per_leg_not_on_the_net(self) -> None:
        """A condor's net delta can read flat while one side is badly tested, and the
        net would report the book balanced right up until assignment."""
        item = risk(
            LegRisk(leg=leg(right=Right.PUT, strike=700.0), mark=9.0, delta=-0.50),
            LegRisk(leg=leg(right=Right.CALL, strike=780.0), mark=1.0, delta=0.50),
        )
        # The two legs cancel to roughly zero net position delta.
        assert item.delta == pytest.approx(0.0, abs=1e-6)
        assert TriggerKind.DELTA_BREACH in kinds(evaluate(item, asof=TODAY))


class TestTested:
    def test_it_fires_when_spot_crosses_a_short_strike(self) -> None:
        item = risk(LegRisk(leg=leg(strike=750.0), mark=12.0, delta=-0.55), spot=740.0)
        assert TriggerKind.TESTED in kinds(evaluate(item, asof=TODAY))

    def test_it_stays_quiet_while_spot_is_clear(self) -> None:
        item = risk(LegRisk(leg=leg(strike=700.0), mark=2.0, delta=-0.15), spot=740.0)
        assert TriggerKind.TESTED not in kinds(evaluate(item, asof=TODAY))


class TestEvents:
    def test_earnings_before_expiry_fires(self) -> None:
        events = EventWindow(earnings=date(2026, 8, 15))
        item = risk(LegRisk(leg=leg(), mark=4.0, delta=-0.2))
        assert TriggerKind.EARNINGS in kinds(evaluate(item, asof=TODAY, events=events))

    def test_earnings_after_expiry_does_not(self) -> None:
        events = EventWindow(earnings=date(2027, 1, 15))
        item = risk(LegRisk(leg=leg(), mark=4.0, delta=-0.2))
        assert TriggerKind.EARNINGS not in kinds(evaluate(item, asof=TODAY, events=events))

    def test_assignment_fires_when_the_dividend_beats_the_extrinsic(self) -> None:
        """The honest test. A deep in the money short call with 0.10 of time value left
        against a 1.50 dividend will be exercised, and moneyness alone does not decide
        it, the extrinsic does."""
        events = EventWindow(ex_dividend=date(2026, 8, 3), dividend_amount=1.50)
        # Spot 740, strike 700, so 40 of intrinsic. Marked 40.10, leaving 0.10 extrinsic.
        item = risk(
            LegRisk(leg=leg(right=Right.CALL, strike=700.0), mark=40.10, delta=0.97),
            spot=740.0,
        )
        assert TriggerKind.ASSIGNMENT_RISK in kinds(evaluate(item, asof=TODAY, events=events))

    def test_assignment_does_not_fire_with_extrinsic_left(self) -> None:
        """In the money and near an ex date is not enough. Giving up 3.00 of time value
        to collect 1.50 is not rational, so nobody does it."""
        events = EventWindow(ex_dividend=date(2026, 8, 3), dividend_amount=1.50)
        item = risk(
            LegRisk(leg=leg(right=Right.CALL, strike=700.0), mark=43.00, delta=0.80),
            spot=740.0,
        )
        assert TriggerKind.ASSIGNMENT_RISK not in kinds(evaluate(item, asof=TODAY, events=events))

    def test_a_short_put_never_raises_dividend_assignment(self) -> None:
        """Early exercise of a put is driven by interest on the strike, not dividends."""
        events = EventWindow(ex_dividend=date(2026, 8, 3), dividend_amount=1.50)
        item = risk(LegRisk(leg=leg(right=Right.PUT, strike=800.0), mark=60.05), spot=740.0)
        assert TriggerKind.ASSIGNMENT_RISK not in kinds(evaluate(item, asof=TODAY, events=events))

    def test_no_calendar_means_no_event_triggers(self) -> None:
        item = risk(LegRisk(leg=leg(), mark=4.0, delta=-0.2))
        fired = kinds(evaluate(item, asof=TODAY, events=None))
        assert TriggerKind.EARNINGS not in fired
        assert TriggerKind.ASSIGNMENT_RISK not in fired


class TestOrdering:
    def test_the_loudest_comes_first(self) -> None:
        """Assignment outranks a profit target: one is an obligation, the other an
        opportunity."""
        events = EventWindow(ex_dividend=date(2026, 8, 3), dividend_amount=1.50)
        item = risk(
            LegRisk(leg=leg(right=Right.CALL, strike=700.0, fill=45.0), mark=40.10, delta=0.97),
            spot=740.0,
        )
        fired = evaluate(item, asof=TODAY, events=events)
        assert fired[0].kind is TriggerKind.ASSIGNMENT_RISK


class RecordingSink(AlertSink):
    name = "recording"

    def __init__(self, accept: bool = True) -> None:
        self.accept = accept
        self.sent: list[Alert] = []

    def send(self, alert: Alert) -> bool:
        self.sent.append(alert)
        return self.accept


class ExplodingSink(AlertSink):
    name = "exploding"

    def send(self, alert: Alert) -> bool:
        raise RuntimeError("the webhook is on fire")


class TestAlertDelivery:
    @pytest.fixture
    def conn(self, tmp_path):
        with db.session(tmp_path / "optscan.sqlite") as connection:
            yield connection

    @pytest.fixture
    def stored(self, conn):
        return store.add_position(
            conn,
            Position(symbol="SPY", legs=(leg(),), opened_at=OPENED),
        )

    def test_a_sink_that_raises_does_not_take_the_run_down(self) -> None:
        """The run matters more than any one notification."""
        good = RecordingSink()
        alert = Alert(
            position_id=1,
            symbol="SPY",
            trigger=Trigger(kind=TriggerKind.TESTED, message="tested"),
            raised_at=datetime.now(UTC),
        )
        assert deliver(alert, [ExplodingSink(), good]) is True
        assert len(good.sent) == 1

    def test_an_alert_fires_once_per_condition(self, conn, stored) -> None:
        sink = RecordingSink()
        triggers = [Trigger(kind=TriggerKind.TESTED, message="tested")]

        first = raise_alerts(conn, stored.id, "SPY", triggers, [sink])
        second = raise_alerts(conn, stored.id, "SPY", triggers, [sink])

        assert len(first) == 1
        assert second == []
        assert len(sink.sent) == 1

    def test_a_quiet_trigger_does_not_chase_anybody(self, conn, stored) -> None:
        """A profit target belongs in the dashboard, not in a notification."""
        sink = RecordingSink()
        triggers = [Trigger(kind=TriggerKind.PROFIT_TARGET, message="half")]

        assert raise_alerts(conn, stored.id, "SPY", triggers, [sink]) == []
        assert sink.sent == []

    def test_a_total_delivery_failure_is_not_recorded_as_sent(self, conn, stored) -> None:
        """A webhook outage must not consume the only notification this condition will
        ever send."""
        failing = RecordingSink(accept=False)
        triggers = [Trigger(kind=TriggerKind.TESTED, message="tested")]

        assert raise_alerts(conn, stored.id, "SPY", triggers, [failing]) == []
        assert store.already_alerted(conn, stored.id, "tested") is False

        working = RecordingSink()
        assert len(raise_alerts(conn, stored.id, "SPY", triggers, [working])) == 1
