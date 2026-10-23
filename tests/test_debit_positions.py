"""Debit spreads as held positions: a planned exit, no credit rules, a forward test."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from optscan.analytics.portfolio import LegRisk, PositionRisk
from optscan.analytics.triggers import TriggerKind, evaluate
from optscan.config import Settings
from optscan.jobs import forward_test
from optscan.models import Right
from optscan.models.opportunity import Action, Strategy
from optscan.models.position import Position, PositionLeg
from optscan.screener.strategies import generators_for
from optscan.storage import db
from optscan.storage import positions as store

EXPIRY = date(2026, 11, 20)
OPENED = datetime(2026, 10, 22, 19, 34, tzinfo=UTC)
PLAN = {"debit": 2.30, "limit": 2.42, "entry_session": "2026-10-22", "exit_session": "2026-10-29"}


def spread(**overrides) -> Position:
    fields = {
        "symbol": "RIO",
        "legs": (
            PositionLeg(
                action=Action.BUY, right=Right.CALL, strike=85, expiry=EXPIRY, fill_price=2.46
            ),
            PositionLeg(
                action=Action.SELL, right=Right.CALL, strike=95, expiry=EXPIRY, fill_price=0.26
            ),
        ),
        "opened_at": OPENED,
        "strategy": Strategy.CALL_DEBIT_SPREAD,
        "commission_open": 0.08,
        "exit_on": date(2026, 10, 29),
        "plan": PLAN,
    }
    return Position(**{**fields, **overrides})


def risk_of(position: Position, *, spot: float, short_delta: float) -> PositionRisk:
    long, short = position.legs
    return PositionRisk(
        position=position,
        spot=spot,
        legs=(
            LegRisk(leg=long, mark=3.0, delta=0.6),
            LegRisk(leg=short, mark=0.5, delta=short_delta),
        ),
        asof=date(2026, 10, 23),
        marks_complete=True,
    )


def kinds(position: Position, asof: date, **kw) -> set[TriggerKind]:
    return {t.kind for t in evaluate(risk_of(position, **kw), asof=asof)}


class TestPlannedExit:
    def test_it_fires_on_the_exit_day(self) -> None:
        found = kinds(spread(), date(2026, 10, 29), spot=88, short_delta=0.2)
        assert TriggerKind.PLANNED_EXIT in found

    def test_it_says_late_after_the_exit_day(self) -> None:
        late = evaluate(risk_of(spread(), spot=88, short_delta=0.2), asof=date(2026, 10, 31))
        fired = next(t for t in late if t.kind is TriggerKind.PLANNED_EXIT)
        assert "2 days ago" in fired.message

    def test_it_is_quiet_before_the_exit_day(self) -> None:
        found = kinds(spread(), date(2026, 10, 27), spot=88, short_delta=0.2)
        assert TriggerKind.PLANNED_EXIT not in found


class TestCreditRulesStayOff:
    def test_a_debit_spread_through_its_short_strike_is_not_an_alarm(self) -> None:
        """For a call debit spread the short strike crossing is the best case."""
        found = kinds(spread(), date(2026, 10, 23), spot=97, short_delta=0.8)
        assert not found & {TriggerKind.TESTED, TriggerKind.DELTA_BREACH, TriggerKind.DTE}


def test_the_plan_and_exit_survive_storage(tmp_path) -> None:
    with db.session(tmp_path / "t.sqlite") as conn:
        stored = store.add_position(conn, spread())
        back = store.get_position(conn, stored.id)
    assert back.exit_on == date(2026, 10, 29)
    assert back.plan == PLAN
    assert back.strategy is Strategy.CALL_DEBIT_SPREAD


def test_a_plan_can_be_attached_later(tmp_path) -> None:
    with db.session(tmp_path / "t.sqlite") as conn:
        bare = store.add_position(conn, spread(strategy=None, exit_on=None, plan=None))
        store.set_plan(conn, bare.id, strategy="call_debit_spread", exit_on=date(2026, 10, 29))
        back = store.get_position(conn, bare.id)
    assert back.strategy is Strategy.CALL_DEBIT_SPREAD
    assert back.exit_on == date(2026, 10, 29)
    assert back.plan is None


def test_the_screener_refuses_a_position_only_strategy() -> None:
    with pytest.raises(ValueError, match="position type"):
        generators_for(("call_debit_spread",))


class TestForwardTest:
    def test_fill_against_the_plan(self) -> None:
        item = forward_test.trade(spread(id=4))
        assert item.debit == pytest.approx(2.20)
        assert item.slippage == pytest.approx(2.20 / 2.30 - 1)
        assert item.entered_late is False
        assert item.exit_status == "open"

    def test_a_closed_trade_on_plan(self) -> None:
        closed = spread(id=4).close(
            -350.0, commission=0.08, when=datetime(2026, 10, 29, 20, 0, tzinfo=UTC)
        )
        item = forward_test.trade(closed)
        assert item.exit_status == "closed on plan"
        # Paid 220.08, took back 350 less 0.08.
        assert item.return_on_debit == pytest.approx((350 - 220.08 - 0.08) / 220.08)

    def test_the_report_reads_the_database(self, tmp_path) -> None:
        settings = Settings(_env_file=None, data_dir=tmp_path)
        settings.ensure_dirs()
        with db.session(settings.sqlite_path) as conn:
            store.add_position(conn, spread())
            store.add_position(conn, spread(strategy=None, plan=None, exit_on=None))
        trades = forward_test.forward_trades(settings)
        assert len(trades) == 1
        assert any("RIO" in line for line in forward_test.report_lines(trades))
