"""Marking held positions against a solved chain.

This is the code that answers "what is my open risk worth right now", so the tests it
needs are less about arithmetic and more about refusal: which side of the market a
short is valued at, and what happens when a leg cannot be priced honestly. A partial
mark is the failure mode that matters, because it reads as a real number.

Everything runs against the frozen SPY capture from 30 July 2026, whose spot was
740.53 and whose two expiries are 3 and 7 August.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from optscan.analytics.portfolio import Beta
from optscan.models import OptionContract, Right
from optscan.models.opportunity import Action
from optscan.models.position import Position, PositionLeg
from optscan.screener.context import SymbolAnalysis, analyze_snapshot
from optscan.screener.positions import (
    MarkConvention,
    build_portfolio,
    leg_mark,
    mark_position,
)

RATE = 0.043
ASOF = date(2026, 7, 30)
OPENED_AT = datetime(2026, 7, 30, 14, 0, tzinfo=UTC)
EXPIRY = date(2026, 8, 7)

#: Real quotes from the fixture: the 700 put is 0.40 by 0.41, the 710 is 0.75 by 0.77,
#: and the 805 call is quoted 0.00 by 0.01 so it has no two sided market at all.
SHORT_STRIKE = 700.0
LONG_STRIKE = 710.0
NO_BID_STRIKE = 805.0


@pytest.fixture
def analysis(frozen_snapshot) -> SymbolAnalysis:
    return analyze_snapshot(frozen_snapshot, rate=RATE)


def _leg(
    strike: float,
    action: Action = Action.SELL,
    right: Right = Right.PUT,
    *,
    expiry: date = EXPIRY,
    fill: float = 1.00,
    quantity: int = 1,
) -> PositionLeg:
    return PositionLeg(
        action=action,
        right=right,
        strike=strike,
        expiry=expiry,
        quantity=quantity,
        fill_price=fill,
    )


def _position(*legs: PositionLeg, symbol: str = "SPY") -> Position:
    return Position(symbol=symbol, legs=tuple(legs), opened_at=OPENED_AT)


def _contract(bid: float | None, ask: float | None, strike: float = 700.0) -> OptionContract:
    moment = datetime(2026, 7, 30, 18, 47, tzinfo=UTC)
    return OptionContract(
        symbol="SPY",
        expiry=EXPIRY,
        strike=strike,
        right=Right.PUT,
        bid=bid,
        ask=ask,
        fetched_at=moment,
        source="test",
    )


class TestLegMark:
    def test_a_missing_contract_has_no_mark(self) -> None:
        assert leg_mark(None, Action.SELL) is None

    def test_a_short_marks_at_the_ask(self) -> None:
        """The headline claim of the module: the ask is what it costs to close."""
        assert leg_mark(_contract(0.40, 0.41), Action.SELL) == 0.41

    def test_a_long_marks_at_the_bid(self) -> None:
        assert leg_mark(_contract(0.40, 0.41), Action.BUY) == 0.40

    def test_the_mid_is_available_but_is_not_the_default(self) -> None:
        contract = _contract(0.40, 0.50)
        assert leg_mark(contract, Action.SELL, MarkConvention.MID) == pytest.approx(0.45)
        assert leg_mark(contract, Action.SELL) == 0.50

    def test_marking_a_short_book_at_the_mid_overstates_it(self) -> None:
        """Half the spread per leg, which on a wide contract is most of the last of the
        credit, and that is exactly where a profit target fires."""
        contract = _contract(1.00, 2.00)
        closing = leg_mark(contract, Action.SELL)
        mid = leg_mark(contract, Action.SELL, MarkConvention.MID)
        assert closing is not None and mid is not None
        assert closing - mid == pytest.approx(0.50)

    def test_no_two_sided_market_has_no_mark(self) -> None:
        """A last print from three days ago is not a mark, so nothing falls back to it."""
        assert leg_mark(_contract(0.0, 0.01), Action.SELL) is None
        assert leg_mark(_contract(None, 0.41), Action.SELL) is None

    def test_a_crossed_quote_has_no_mark(self) -> None:
        assert leg_mark(_contract(0.50, 0.40), Action.SELL) is None

    def test_the_mid_convention_also_refuses_a_crossed_quote(self) -> None:
        """Delegated to the model's own mid, which returns None rather than a number."""
        assert leg_mark(_contract(0.50, 0.40), Action.SELL, MarkConvention.MID) is None


class TestMarkPosition:
    def test_without_a_snapshot_it_comes_back_unmarked_and_says_what_to_do(self) -> None:
        """Holding something off the watchlist is normal, so this is a sentence not a raise."""
        risk = mark_position(_position(_leg(SHORT_STRIKE)), None, asof=ASOF)

        assert risk.marks_complete is False
        assert risk.spot is None
        assert risk.unrealized is None
        assert len(risk.legs) == 1
        assert risk.legs[0].mark is None
        assert "optscan snapshot" in " ".join(risk.notes)

    def test_a_priced_position_marks_completely(self, analysis: SymbolAnalysis) -> None:
        risk = mark_position(_position(_leg(SHORT_STRIKE)), analysis, asof=ASOF)

        assert risk.marks_complete is True
        assert risk.notes == ()
        assert risk.spot == pytest.approx(740.53, abs=0.01)
        assert risk.legs[0].mark == pytest.approx(0.41)

    def test_a_short_put_carries_the_greeks_and_the_vol(self, analysis: SymbolAnalysis) -> None:
        risk = mark_position(_position(_leg(SHORT_STRIKE)), analysis, asof=ASOF)
        leg = risk.legs[0]

        assert leg.iv is not None and leg.iv > 0
        assert leg.delta is not None
        # A short put is long delta as a position: it profits when the underlying rises.
        assert leg.position_delta is not None and leg.position_delta > 0

    def test_profit_is_measured_against_the_fill(self, analysis: SymbolAnalysis) -> None:
        """Sold at 1.00, now costs 0.41 to buy back, so 0.59 per share on 100 shares."""
        risk = mark_position(_position(_leg(SHORT_STRIKE, fill=1.00)), analysis, asof=ASOF)

        assert risk.unrealized == pytest.approx(59.0)

    def test_a_spread_marks_both_legs(self, analysis: SymbolAnalysis) -> None:
        position = _position(
            _leg(SHORT_STRIKE, Action.SELL, fill=0.40),
            _leg(LONG_STRIKE, Action.BUY, fill=0.77),
        )
        risk = mark_position(position, analysis, asof=ASOF)

        assert risk.marks_complete is True
        assert risk.legs[0].mark == pytest.approx(0.41)
        # The long leg closes by selling, so it marks at the bid.
        assert risk.legs[1].mark == pytest.approx(0.75)

    def test_an_uncaptured_expiry_is_named(self, analysis: SymbolAnalysis) -> None:
        position = _position(_leg(SHORT_STRIKE, expiry=date(2026, 12, 18)))
        risk = mark_position(position, analysis, asof=ASOF)

        assert risk.marks_complete is False
        assert risk.unrealized is None
        assert "2026-12-18 was not captured" in " ".join(risk.notes)

    def test_a_strike_off_the_ladder_is_named(self, analysis: SymbolAnalysis) -> None:
        risk = mark_position(_position(_leg(701.5)), analysis, asof=ASOF)

        assert risk.marks_complete is False
        assert "not in the captured chain" in " ".join(risk.notes)

    def test_a_contract_with_no_two_sided_market_is_named(self, analysis: SymbolAnalysis) -> None:
        """The 805 call is quoted 0.00 by 0.01 in the real capture."""
        position = _position(_leg(NO_BID_STRIKE, right=Right.CALL))
        risk = mark_position(position, analysis, asof=ASOF)

        assert risk.marks_complete is False
        assert "no two sided market" in " ".join(risk.notes)

    def test_one_bad_leg_withholds_the_whole_position(self, analysis: SymbolAnalysis) -> None:
        """A partial mark would read as a real number, which is the point of refusing."""
        position = _position(
            _leg(SHORT_STRIKE, Action.SELL),
            _leg(NO_BID_STRIKE, Action.BUY, Right.CALL),
        )
        risk = mark_position(position, analysis, asof=ASOF)

        assert risk.marks_complete is False
        assert risk.unrealized is None
        assert risk.legs[0].mark is not None
        assert "A partial mark would read as a real number" in " ".join(risk.notes)

    def test_the_note_says_call_or_put(self, analysis: SymbolAnalysis) -> None:
        """ "unmarked" alone sends nobody anywhere useful."""
        risk = mark_position(_position(_leg(NO_BID_STRIKE, right=Right.CALL)), analysis, asof=ASOF)
        assert "the 805 call" in " ".join(risk.notes)

        risk = mark_position(_position(_leg(701.5)), analysis, asof=ASOF)
        assert "the 701.5 put" in " ".join(risk.notes)

    def test_the_mid_convention_is_carried_through(self, analysis: SymbolAnalysis) -> None:
        risk = mark_position(
            _position(_leg(SHORT_STRIKE)),
            analysis,
            asof=ASOF,
            convention=MarkConvention.MID,
        )
        assert risk.legs[0].mark == pytest.approx(0.405)

    def test_the_beta_is_attached(self, analysis: SymbolAnalysis) -> None:
        beta = Beta(value=1.0, observations=120, span_days=180, r_squared=0.9, reference="SPY")
        risk = mark_position(_position(_leg(SHORT_STRIKE)), analysis, asof=ASOF, beta=beta)

        assert risk.beta is beta


class TestBuildPortfolio:
    def test_an_empty_book_has_nothing_to_say(self) -> None:
        risks, notes = build_portfolio([], {}, asof=ASOF)

        assert risks == []
        assert notes == []

    def test_each_position_is_marked_against_its_own_symbol(self, analysis: SymbolAnalysis) -> None:
        positions = [_position(_leg(SHORT_STRIKE)), _position(_leg(LONG_STRIKE), symbol="QQQ")]
        risks, _ = build_portfolio(positions, {"SPY": analysis, "QQQ": None}, asof=ASOF)

        assert len(risks) == 2
        assert risks[0].marks_complete is True
        assert risks[1].marks_complete is False

    def test_unmarked_positions_are_counted_and_the_greeks_withheld(
        self, analysis: SymbolAnalysis
    ) -> None:
        positions = [_position(_leg(SHORT_STRIKE)), _position(_leg(LONG_STRIKE), symbol="QQQ")]
        _, notes = build_portfolio(positions, {"SPY": analysis, "QQQ": None}, asof=ASOF)

        assert any("1 of 2 positions could not be marked" in note for note in notes)

    def test_a_missing_beta_is_stated_rather_than_defaulted(self, analysis: SymbolAnalysis) -> None:
        """Defaulting to 1.0 would weight an unmeasured name as if it moved with the index."""
        _, notes = build_portfolio([_position(_leg(SHORT_STRIKE))], {"SPY": analysis}, asof=ASOF)

        assert any("No usable beta for SPY" in note for note in notes)

    def test_a_beta_from_too_short_a_sample_is_not_usable(self, analysis: SymbolAnalysis) -> None:
        thin = Beta(value=1.1, observations=10, span_days=14, r_squared=0.8, reference="SPY")
        _, notes = build_portfolio(
            [_position(_leg(SHORT_STRIKE))],
            {"SPY": analysis},
            asof=ASOF,
            betas={"SPY": thin},
        )

        assert any("No usable beta" in note for note in notes)

    def test_a_usable_beta_draws_no_complaint(self, analysis: SymbolAnalysis) -> None:
        good = Beta(value=1.0, observations=120, span_days=180, r_squared=0.9, reference="SPY")
        risks, notes = build_portfolio(
            [_position(_leg(SHORT_STRIKE))],
            {"SPY": analysis},
            asof=ASOF,
            betas={"SPY": good},
        )

        assert notes == []
        assert risks[0].beta is good

    def test_the_reference_symbol_appears_in_the_beta_note(self, analysis: SymbolAnalysis) -> None:
        _, notes = build_portfolio(
            [_position(_leg(SHORT_STRIKE))],
            {"SPY": analysis},
            asof=ASOF,
            reference="IWM",
        )

        assert any("against IWM" in note for note in notes)

    def test_symbols_without_beta_are_deduplicated(self, analysis: SymbolAnalysis) -> None:
        """Two positions on one symbol name it once, not twice."""
        positions = [_position(_leg(SHORT_STRIKE)), _position(_leg(LONG_STRIKE))]
        # Referenced against IWM so the only "SPY" in the sentence is the held symbol.
        _, notes = build_portfolio(positions, {"SPY": analysis}, asof=ASOF, reference="IWM")

        beta_note = next(note for note in notes if "No usable beta" in note)
        assert beta_note.count("SPY") == 1
