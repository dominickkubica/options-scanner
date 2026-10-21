"""The premium engine on inputs small enough to check by hand.

The marks are tested on a synthetic tape whose every print is an exact model price at
one volatility, so the smile has a known right answer. The exits are tested on paths
built directly, so each rule's trigger minute, fill minute and dollars can be read off
the arrays in the test.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from optscan.analytics.premium import (
    BUY_FEE,
    FRESH_MINUTES,
    SELL_FEE,
    STALE_MINUTES,
    Entry,
    EntryRule,
    ExitReason,
    ExitRule,
    Leg,
    Path,
    SpreadModel,
    Structure,
    build_marks,
    expiry_seconds,
    run_exit,
    select_entry,
)
from optscan.analytics.vector_bsm import bsm_price

RATE = 0.043
SESSION = date(2025, 4, 7)
OPEN = datetime(2025, 4, 7, 13, 30, tzinfo=UTC)
CLOSE = datetime(2025, 4, 7, 20, 0, tzinfo=UTC)
SIGMA = 0.20
STRIKES = np.arange(90.0, 111.0)


def synthetic_tape(
    minutes: int = 390, last_print: int | None = None
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Spot flat at 100 and every strike printing its exact model price each minute."""
    stamps = [OPEN + timedelta(minutes=i) for i in range(minutes + 1)]  # +1: the 16:00 bar
    underlying = pd.DataFrame({"ts": pd.to_datetime(stamps, utc=True), "close": 100.0})
    rows = []
    for i, stamp in enumerate(stamps[:minutes]):
        if last_print is not None and i > last_print:
            break
        years = (expiry_seconds(SESSION) - stamp.timestamp()) / (365 * 24 * 3600)
        for right in ("P", "C"):
            prices = bsm_price(right == "C", 100.0, STRIKES, years, RATE, SIGMA)
            for strike, price in zip(STRIKES, prices, strict=True):
                rows.append(
                    {
                        "expiry": SESSION,
                        "right": right,
                        "strike": float(strike),
                        "ts": stamp,
                        "close": float(price),
                        "vwap": float(price),
                    }
                )
    options = pd.DataFrame(rows)
    options["ts"] = pd.to_datetime(options["ts"], utc=True)
    return underlying, options


class TestMarks:
    def test_exact_prints_give_back_their_volatility(self) -> None:
        underlying, options = synthetic_tape(minutes=40)
        marks = build_marks(
            "QQQ", SESSION, underlying, options, close=OPEN + timedelta(minutes=40), rate=RATE
        )
        smile = marks.smiles[SESSION]
        assert len(marks.times) == 40, "the bar at the close is dropped"
        # Deep wings price below a cent and are refused as unidentifiable, which is the
        # point; everything the solver accepts must come back at 20 vol.
        solved = smile.iv[np.isfinite(smile.iv)]
        assert solved.size > 0
        assert solved == pytest.approx(SIGMA, rel=1e-6)

    def test_a_smile_is_carried_briefly_and_then_refused(self) -> None:
        underlying, options = synthetic_tape(minutes=60, last_print=10)
        marks = build_marks(
            "QQQ", SESSION, underlying, options, close=OPEN + timedelta(minutes=60), rate=RATE
        )
        column = marks.smiles[SESSION].column(100.0)
        built_through = 10 + FRESH_MINUTES - 1
        assert np.isfinite(column[built_through + STALE_MINUTES])
        assert np.isnan(column[built_through + STALE_MINUTES + 1])


class TestEntry:
    def test_the_short_strike_is_the_one_nearest_the_delta_asked_for(self) -> None:
        underlying, options = synthetic_tape(minutes=60)
        marks = build_marks("QQQ", SESSION, underlying, options, close=CLOSE, rate=RATE)
        entry = select_entry(
            marks,
            SESSION,
            Structure.PUT_SPREAD,
            EntryRule(minute=30, short_delta=0.2, width=2.0),
            SpreadModel(),
            RATE,
        )
        assert isinstance(entry, Entry)
        short, long = entry.legs
        assert short.short and not short.is_call and not long.short
        assert long.strike == short.strike - 2.0
        years = (expiry_seconds(SESSION) - (OPEN + timedelta(minutes=30)).timestamp()) / (
            365 * 24 * 3600
        )
        # At 20 vol with six hours left a dollar is nearly two standard deviations, so
        # the nearest put to 0.20 delta on a hundred dollar ladder is far from 0.20. What
        # the engine owes is the nearest one, and a brute force search says which.
        from optscan.analytics.vector_bsm import bsm_delta

        puts = STRIKES[STRIKES < 100.0]
        deltas = np.abs(bsm_delta(False, 100.0, puts, years, RATE, SIGMA))
        nearest = int(np.argmin(np.abs(deltas - 0.2)))
        assert short.strike == puts[nearest]
        assert entry.short_deltas[0] == pytest.approx(float(deltas[nearest]), rel=1e-6)
        expected = bsm_price(False, 100.0, short.strike, years, RATE, SIGMA) - bsm_price(
            False, 100.0, long.strike, years, RATE, SIGMA
        )
        assert entry.credit_mid == pytest.approx(float(expected), rel=1e-6)

    def test_a_missing_expiry_is_a_stated_refusal(self) -> None:
        underlying, options = synthetic_tape(minutes=40)
        marks = build_marks("QQQ", SESSION, underlying, options, close=CLOSE, rate=RATE)
        refusal = select_entry(
            marks, date(2025, 4, 8), Structure.CALL_SPREAD, EntryRule(), SpreadModel(), RATE
        )
        assert isinstance(refusal, str) and "no trades" in refusal


def put_spread_entry(credit_mid: float = 0.50, half: float = 0.01) -> Entry:
    return Entry(
        underlying="QQQ",
        session=SESSION,
        expiry=SESSION,
        dte=0,
        structure=Structure.PUT_SPREAD,
        legs=(Leg(False, 98.0, True), Leg(False, 96.0, False)),
        credit_mid=credit_mid,
        entry_half=(half, half),
        short_deltas=(0.2,),
        spot=100.0,
        width=2.0,
    )


def path_from_debits(
    debits: list[float],
    spot: list[float] | None = None,
    flatten_at: int | None = None,
    half: float = 0.01,
) -> Path:
    """A path whose short leg carries the whole debit and whose wing is worthless."""
    n = len(debits)
    price = np.column_stack([np.array(debits, dtype=float), np.zeros(n)])
    return Path(
        spot=np.array(spot if spot is not None else [100.0] * n, dtype=float),
        price=price,
        delta=np.column_stack([np.full(n, -0.2), np.full(n, -0.05)]),
        half=np.full((n, 2), half),
        flatten_at=flatten_at if flatten_at is not None else n - 1,
        settle_spot=100.0,
        _signs=np.array([1.0, -1.0]),
    )


ROUND_TRIP_FEES = (SELL_FEE + BUY_FEE) * 2


class TestExits:
    def test_a_target_fills_the_minute_after_it_is_reached(self) -> None:
        """Like every exit. Filling at the trigger minute took the favourable side of
        the mark's noise and was worth $2.50 to $6.60 a condor on the real tape."""
        # Credit 0.50 at k=0; a 50% target triggers at a debit of 0.25 (minute 2).
        path = path_from_debits([0.50, 0.40, 0.25, 0.10])
        trade = run_exit(put_spread_entry(), path, ExitRule(target=0.5), k=0.0)
        assert trade is not None and trade.reason is ExitReason.TARGET
        assert trade.minutes_held == 3
        assert trade.pnl == pytest.approx((0.50 - 0.10) * 100 - ROUND_TRIP_FEES)

    def test_a_stop_fills_at_the_next_minute_and_reports_the_slip(self) -> None:
        # A 1x stop triggers at a debit of 1.00 (minute 2) and fills at minute 3.
        path = path_from_debits([0.50, 0.80, 1.05, 1.40, 0.20])
        trade = run_exit(put_spread_entry(), path, ExitRule(stop=1.0), k=0.0)
        assert trade is not None and trade.reason is ExitReason.STOP
        assert trade.minutes_held == 3
        assert trade.exit_debit == pytest.approx(1.40)
        assert trade.slip == pytest.approx(0.40)
        assert trade.r == pytest.approx(trade.pnl / 50.0)

    def test_the_earliest_trigger_wins(self) -> None:
        # Touch at minute 1 (97.9 under the 98 strike), target at 2, stop at 3.
        path = path_from_debits([0.50, 0.60, 0.20, 1.20], spot=[100, 97.9, 97.5, 97])
        trade = run_exit(
            put_spread_entry(), path, ExitRule(stop=1.0, touch=True, target=0.5), k=0.0
        )
        assert trade is not None and trade.reason is ExitReason.TOUCH
        assert trade.minutes_held == 2, "touched at minute 1, filled the minute after"

    def test_inside_one_minute_the_target_is_named_the_reason(self) -> None:
        """A target and a touch seen in the same minute both fill the minute after; the
        resting target was in the book first, so it is recorded as the reason."""
        path = path_from_debits([0.50, 0.60, 0.20, 1.20], spot=[100, 99, 97.5, 97])
        trade = run_exit(put_spread_entry(), path, ExitRule(touch=True, target=0.5), k=0.0)
        assert trade is not None and trade.reason is ExitReason.TARGET
        assert trade.minutes_held == 3

    def test_an_unmanaged_intraday_position_is_flattened(self) -> None:
        path = path_from_debits([0.50, 0.45, 0.30, 0.35, 0.10], flatten_at=3)
        trade = run_exit(put_spread_entry(), path, ExitRule(), k=0.0)
        assert trade is not None and trade.reason is ExitReason.FLATTEN
        assert trade.exit_debit == pytest.approx(0.35)

    def test_held_to_expiry_settles_at_intrinsic_with_no_closing_fees(self) -> None:
        path = path_from_debits([0.50, 0.70, 0.90])
        path.settle_spot = 97.0  # 98 put worth 1.00, 96 put worthless
        trade = run_exit(put_spread_entry(), path, ExitRule(settle=True), k=0.0)
        assert trade is not None and trade.reason is ExitReason.EXPIRY
        assert trade.exit_debit == pytest.approx(1.0)
        assert trade.pnl == pytest.approx((0.50 - 1.0) * 100 - (SELL_FEE + BUY_FEE))

    def test_k_is_paid_on_the_way_in_and_on_the_way_out(self) -> None:
        # Triggers at minute 1, fills at minute 2.
        path = path_from_debits([0.50, 0.25, 0.25], half=0.01)
        trade = run_exit(put_spread_entry(half=0.01), path, ExitRule(target=0.1), k=1.0)
        assert trade is not None
        assert trade.credit == pytest.approx(0.48)
        assert trade.exit_debit == pytest.approx(0.27)

    def test_a_credit_the_spread_eats_is_refused_rather_than_traded(self) -> None:
        path = path_from_debits([0.02, 0.01])
        assert (
            run_exit(put_spread_entry(credit_mid=0.02, half=0.02), path, ExitRule(), k=1.0) is None
        )


class TestButterflyEntry:
    """The body sits on the money, not on a delta, which is what makes it a butterfly."""

    def entry(self, width: float = 2.0) -> Entry:
        underlying, options = synthetic_tape(minutes=60)
        marks = build_marks("QQQ", SESSION, underlying, options, close=CLOSE, rate=RATE)
        entry = select_entry(
            marks,
            SESSION,
            Structure.IRON_BUTTERFLY,
            EntryRule(minute=30, short_delta=0.2, width=width),
            SpreadModel(),
            RATE,
        )
        assert isinstance(entry, Entry)
        return entry

    def test_both_shorts_sit_on_the_strike_nearest_spot(self) -> None:
        """Spot is flat at 100 in the fixture, so the body is 100 and not a 0.20 delta
        strike. A butterfly that picked its body by delta would be a condor."""
        entry = self.entry()
        shorts = [leg for leg in entry.legs if leg.short]

        assert len(shorts) == 2
        assert {leg.strike for leg in shorts} == {100.0}
        assert {leg.is_call for leg in shorts} == {False, True}

    def test_the_wings_sit_a_width_either_side_of_the_body(self) -> None:
        entry = self.entry(width=3.0)
        longs = {(leg.is_call, leg.strike) for leg in entry.legs if not leg.short}

        assert longs == {(False, 97.0), (True, 103.0)}

    def test_the_short_delta_rule_is_ignored_rather_than_honoured(self) -> None:
        """`short_delta` is meaningless here and the entry says so by reporting what the
        body's delta actually is: about a half, never the 0.20 that was asked for."""
        entry = self.entry()

        assert all(0.4 < delta < 0.6 for delta in entry.short_deltas)

    def test_the_credit_is_the_straddle_less_the_strangle(self) -> None:
        entry = self.entry()
        years = (expiry_seconds(SESSION) - (OPEN + timedelta(minutes=30)).timestamp()) / (
            365 * 24 * 3600
        )
        body = float(
            bsm_price(False, 100.0, 100.0, years, RATE, SIGMA)
            + bsm_price(True, 100.0, 100.0, years, RATE, SIGMA)
        )
        wings = float(
            bsm_price(False, 100.0, 98.0, years, RATE, SIGMA)
            + bsm_price(True, 100.0, 102.0, years, RATE, SIGMA)
        )

        assert entry.credit_mid == pytest.approx(body - wings, rel=1e-6)

    def test_the_credit_cannot_exceed_the_wing_width(self) -> None:
        """The no-arbitrage bound on the structure. A credit above the width would be a
        riskless profit, and finding one means the marks are wrong rather than that the
        trade is good."""
        for width in (1.0, 2.0, 5.0):
            assert self.entry(width=width).credit_mid < width

    def test_a_wing_with_no_mark_is_a_stated_refusal(self) -> None:
        underlying, options = synthetic_tape(minutes=60)
        marks = build_marks("QQQ", SESSION, underlying, options, close=CLOSE, rate=RATE)
        refusal = select_entry(
            marks,
            SESSION,
            Structure.IRON_BUTTERFLY,
            # The ladder stops at 90 and 110, so a 50 wide wing is not listed.
            EntryRule(minute=30, width=50.0),
            SpreadModel(),
            RATE,
        )

        assert isinstance(refusal, str) and "wing" in refusal

    def test_settlement_on_the_body_pays_the_whole_credit(self) -> None:
        """The structure's best case, and the one the study's assignment work is about:
        it is also the settlement that leaves a short sitting exactly at the money."""
        entry = self.entry()
        path = Path(
            spot=np.array([100.0, 100.0]),
            price=np.zeros((2, 4)),
            delta=np.zeros((2, 4)),
            half=np.zeros((2, 4)),
            flatten_at=1,
            settle_spot=100.0,
            _signs=np.array([leg.sign for leg in entry.legs]),
        )
        trade = run_exit(entry, path, ExitRule(settle=True), k=0.0)

        assert trade is not None
        assert trade.exit_debit == pytest.approx(0.0)
