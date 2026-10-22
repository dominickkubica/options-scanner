"""The call debit spread plan attached to oversold bounce triggers.

A plan is the studied rule translated onto listed contracts, so these tests pin each
translation: entry at the next session, exit five sessions later, the expiry nearest
fifteen days that lands after the exit, the long leg at the money and the short 0.84
sigmas above, and fill guidance derived from the study's slippage findings.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import pytest

from optscan.analytics.bounce_spread import (
    LIMIT_SLIPPAGE,
    STATUS,
    WALK_AWAY_SLIPPAGE,
    WIDTH_SIGMAS,
    SpreadPlan,
    choose_expiry,
    next_session,
    plan_spread,
    session_after,
)
from optscan.jobs.ideas import Idea, as_report
from optscan.models import OptionChain, OptionContract, Right

NOW = datetime(2026, 10, 21, 20, 0, tzinfo=UTC)
SIGNAL = date(2026, 10, 21)  # a Wednesday
ENTRY = date(2026, 10, 22)
EXIT = date(2026, 10, 29)


def weekdays(day: date) -> bool:
    return day.weekday() < 5


def contract(strike, *, bid, ask, iv=0.30, right=Right.CALL, expiry=date(2026, 11, 6)):
    return OptionContract(
        symbol="TEST",
        contract_symbol=f"TEST{strike:g}{right}",
        expiry=expiry,
        strike=strike,
        right=right,
        bid=bid,
        ask=ask,
        vendor_iv=iv,
        fetched_at=NOW,
        source="test",
    )


def chain(contracts, expiry=date(2026, 11, 6)):
    return OptionChain(
        symbol="TEST",
        expiry=expiry,
        underlying_price=100.0,
        contracts=tuple(contracts),
        fetched_at=NOW,
        source="test",
    )


def ladder(*, quoted=True, iv=0.30):
    """Calls every dollar from 90 to 115, priced roughly like a 15 day 30 vol ladder."""
    rows = []
    for strike in range(90, 116):
        intrinsic = max(100 - strike, 0)
        mid = intrinsic + max(2.4 - 0.25 * abs(strike - 100), 0.05)
        if quoted:
            rows.append(
                contract(float(strike), bid=round(mid - 0.05, 2), ask=round(mid + 0.05, 2), iv=iv)
            )
        else:
            rows.append(contract(float(strike), bid=None, ask=None, iv=iv))
    return rows


class TestTheCalendar:
    def test_entry_is_the_next_session(self) -> None:
        """The study fills at the next open. A Friday signal enters on Monday."""
        assert next_session(SIGNAL, weekdays) == ENTRY
        assert next_session(date(2026, 10, 23), weekdays) == date(2026, 10, 26)

    def test_exit_is_five_sessions_after_entry(self) -> None:
        assert session_after(ENTRY, 5, weekdays) == EXIT


class TestTheExpiry:
    def test_the_nearest_to_fifteen_days_is_chosen(self) -> None:
        listed = [date(2026, 10, 30), date(2026, 11, 6), date(2026, 11, 13)]

        assert choose_expiry(listed, ENTRY, EXIT) == date(2026, 11, 6)

    def test_an_expiry_before_the_exit_is_never_chosen(self) -> None:
        """A spread that expires before it is due to be closed is a different trade."""
        listed = [date(2026, 10, 28), date(2026, 11, 20)]

        assert choose_expiry(listed, ENTRY, EXIT) == date(2026, 11, 20)

    def test_nothing_in_the_window_is_none(self) -> None:
        assert choose_expiry([date(2026, 12, 18)], ENTRY, EXIT) is None


class TestThePlan:
    def plan(self, **kwargs) -> SpreadPlan:
        result = plan_spread("TEST", chain(ladder(**kwargs)), 100.0, SIGNAL, ENTRY, EXIT)
        assert isinstance(result, SpreadPlan)
        return result

    def test_the_long_leg_is_at_the_money(self) -> None:
        assert self.plan().long.strike == 100.0

    def test_the_short_leg_sits_0_84_sigmas_above(self) -> None:
        plan = self.plan()
        target = 100.0 * (1 + WIDTH_SIGMAS * 0.30 * math.sqrt(15 / 365))

        assert plan.short.strike == round(target)

    def test_the_debit_is_the_difference_of_the_mids(self) -> None:
        plan = self.plan()

        assert plan.quoted
        assert plan.debit == pytest.approx(plan.long.mid - plan.short.mid)

    def test_fill_guidance_comes_from_the_studys_slippage_findings(self) -> None:
        """Mid + 5% was the study's base case; mid + 10% was about breakeven."""
        plan = self.plan()

        assert plan.limit == pytest.approx(round(plan.debit * (1 + LIMIT_SLIPPAGE), 2))
        assert plan.walk_away == pytest.approx(round(plan.debit * (1 + WALK_AWAY_SLIPPAGE), 2))

    def test_the_maximum_loss_is_the_whole_debit_at_the_limit(self) -> None:
        plan = self.plan()

        assert plan.max_loss == pytest.approx(plan.limit * 100)
        assert plan.max_gain == pytest.approx((plan.width - plan.limit) * 100)

    def test_a_market_too_wide_to_fill_is_flagged(self) -> None:
        rows = ladder()
        widened = [
            contract(c.strike, bid=c.bid - 0.40, ask=c.ask + 0.40) if c.strike == 100.0 else c
            for c in rows
        ]
        plan = plan_spread("TEST", chain(widened), 100.0, SIGNAL, ENTRY, EXIT)

        assert isinstance(plan, SpreadPlan)
        assert plan.market_too_wide
        assert any("too wide" in note or "wide" in note for note in plan.notes)

    def test_without_quotes_the_debit_is_a_labelled_model_estimate(self) -> None:
        plan = self.plan(quoted=False)

        assert not plan.quoted
        assert plan.natural is None
        assert plan.debit > 0
        assert any("model estimate" in note for note in plan.notes)

    def test_after_hours_quotes_are_flagged(self) -> None:
        result = plan_spread(
            "TEST", chain(ladder()), 100.0, SIGNAL, ENTRY, EXIT, quoted_outside_session=True
        )

        assert isinstance(result, SpreadPlan)
        assert any("outside regular hours" in note for note in result.notes)

    def test_a_long_tenor_is_named_on_the_plan(self) -> None:
        late = date(2026, 11, 20)
        result = plan_spread(
            "TEST",
            chain([contract(c.strike, bid=c.bid, ask=c.ask, expiry=late) for c in ladder()], late),
            100.0,
            SIGNAL,
            ENTRY,
            EXIT,
        )

        assert isinstance(result, SpreadPlan)
        assert any("longer than studied" in note for note in result.notes)

    def test_the_status_travels_with_every_plan(self) -> None:
        assert self.plan().as_dict()["status"] == STATUS
        assert "not a validated trade" in STATUS


class TestRefusals:
    def test_too_few_calls(self) -> None:
        result = plan_spread(
            "TEST", chain([contract(100.0, bid=2.0, ask=2.1)]), 100.0, SIGNAL, ENTRY, EXIT
        )

        assert isinstance(result, str)

    def test_no_implied_vol_anywhere(self) -> None:
        result = plan_spread("TEST", chain(ladder(iv=None)), 100.0, SIGNAL, ENTRY, EXIT)

        assert isinstance(result, str)
        assert "implied vol" in result

    def test_no_strike_above_the_long_leg(self) -> None:
        rows = [c for c in ladder() if c.strike <= 100.0]
        result = plan_spread("TEST", chain(rows), 100.0, SIGNAL, ENTRY, EXIT)

        assert isinstance(result, str)


class TestTheReport:
    def idea(self, strategy="oversold_bounce"):
        return Idea(
            symbol="TEST",
            strategy_key=strategy,
            label="x",
            session=SIGNAL,
            price=100.0,
            age=0,
            cost=0.003,
            gross_edge=0.007,
            horizon=5,
            direction="long",
        )

    def test_a_plan_is_attached_to_a_bounce_row(self) -> None:
        plan = plan_spread("TEST", chain(ladder()), 100.0, SIGNAL, ENTRY, EXIT)
        report = as_report([self.idea()], [], plans={"TEST": plan.as_dict()})

        assert report["ideas"][0]["spread_plan"]["long_strike"] == 100.0

    def test_a_refusal_is_carried_as_a_note(self) -> None:
        report = as_report([self.idea()], [], plans={"TEST": {"refused": "no chain"}})

        assert report["ideas"][0]["spread_plan_note"] == "no chain"
        assert "spread_plan" not in report["ideas"][0]

    def test_other_strategies_get_no_plan(self) -> None:
        plan = plan_spread("TEST", chain(ladder()), 100.0, SIGNAL, ENTRY, EXIT)
        report = as_report([self.idea("other")], [], plans={"TEST": plan.as_dict()})

        assert "spread_plan" not in report["ideas"][0]


def test_the_window_edges() -> None:
    """Ten to thirty-one days. Inside it a plan; outside it, honestly nothing."""
    assert choose_expiry([ENTRY + timedelta(days=10)], ENTRY, EXIT) is not None
    assert choose_expiry([ENTRY + timedelta(days=31)], ENTRY, EXIT) is not None
    assert choose_expiry([ENTRY + timedelta(days=32)], ENTRY, EXIT) is None
