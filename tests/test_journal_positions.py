"""Positions: contract round trips folded back into the trades that were placed.

The property that matters most is the first one tested: **grouping must not change the
money**. The journal's headline was audited to the cent against the ledger, and a
position layer that disagreed with it by a single fill would put two totals on one page.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime

import pytest

from optscan.analytics.ledger import build_trades, journal_entries
from optscan.analytics.positions import (
    STOP_MULTIPLE,
    Annotation,
    Leg,
    attach,
    build_book,
    build_positions,
    cash_flows,
    guess_strategy,
    risk_at_entry,
    streaks,
    trend_labels,
)
from optscan.imports.robinhood import parse_rows

HEADER = (
    '"Activity Date","Process Date","Settle Date","Instrument","Description",'
    '"Trans Code","Quantity","Price","Amount"\n'
)


def row(day: str, instrument: str, description: str, code: str, qty: str, price: str, amount: str):
    cells = [day, day, day, instrument, description, code, qty, price, amount]
    return ",".join(f'"{cell}"' for cell in cells) + "\n"


# Newest first within a day, as Robinhood writes it: the ledger reverses file order
# inside a day to recover the sequence, so closes sit above the opens they close.
STATEMENT = HEADER + "".join(
    [
        # AAPL put credit spread, opened 9/10, held to expiry 9/18 and expired worthless.
        row(
            "9/18/2026",
            "AAPL",
            "Option Expiration for AAPL 9/18/2026 Put $300.00",
            "OEXP",
            "1",
            "",
            "",
        ),
        row(
            "9/18/2026",
            "AAPL",
            "Option Expiration for AAPL 9/18/2026 Put $295.00",
            "OEXP",
            "1S",
            "",
            "",
        ),
        # QQQ 0DTE iron condor stopped out on 9/11 at three times the credit.
        row("9/11/2026", "QQQ", "QQQ 9/11/2026 Put $500.00", "BTC", "1", "$1.50", "($150.05)"),
        row("9/11/2026", "QQQ", "QQQ 9/11/2026 Put $495.00", "STC", "1", "$0.10", "$9.95"),
        row("9/11/2026", "QQQ", "QQQ 9/11/2026 Call $520.00", "BTC", "1", "$0.01", "($1.05)"),
        row("9/11/2026", "QQQ", "QQQ 9/11/2026 Call $525.00", "STC", "1", "$0.01", "$0.95"),
        row("9/11/2026", "QQQ", "QQQ 9/11/2026 Put $500.00", "STO", "1", "$0.50", "$49.95"),
        row("9/11/2026", "QQQ", "QQQ 9/11/2026 Put $495.00", "BTO", "1", "$0.20", "($20.05)"),
        row("9/11/2026", "QQQ", "QQQ 9/11/2026 Call $520.00", "STO", "1", "$0.20", "$19.95"),
        row("9/11/2026", "QQQ", "QQQ 9/11/2026 Call $525.00", "BTO", "1", "$0.05", "($5.05)"),
        # QQQ 0DTE iron condor on 9/10: 54.80 credit, closed for 94.20.
        row("9/10/2026", "QQQ", "QQQ 9/10/2026 Put $500.00", "BTC", "1", "$1.20", "($120.05)"),
        row("9/10/2026", "QQQ", "QQQ 9/10/2026 Put $495.00", "STC", "1", "$0.30", "$29.95"),
        row("9/10/2026", "QQQ", "QQQ 9/10/2026 Call $520.00", "BTC", "1", "$0.05", "($5.05)"),
        row("9/10/2026", "QQQ", "QQQ 9/10/2026 Call $525.00", "STC", "1", "$0.01", "$0.95"),
        row("9/10/2026", "QQQ", "QQQ 9/10/2026 Put $500.00", "STO", "1", "$0.50", "$49.95"),
        row("9/10/2026", "QQQ", "QQQ 9/10/2026 Put $495.00", "BTO", "1", "$0.20", "($20.05)"),
        row("9/10/2026", "QQQ", "QQQ 9/10/2026 Call $520.00", "STO", "1", "$0.40", "$39.95"),
        row("9/10/2026", "QQQ", "QQQ 9/10/2026 Call $525.00", "BTO", "1", "$0.15", "($15.05)"),
        row("9/10/2026", "AAPL", "AAPL 9/18/2026 Put $300.00", "STO", "1", "$1.50", "$149.95"),
        row("9/10/2026", "AAPL", "AAPL 9/18/2026 Put $295.00", "BTO", "1", "$0.60", "($60.05)"),
        # A stock round trip.
        row("9/9/2026", "TJX", "TJX Companies", "Sell", "1", "$110.00", "$110.00"),
        row("9/8/2026", "TJX", "TJX Companies", "Buy", "1", "$100.00", "($100.00)"),
        row("9/1/2026", "", "ACH Deposit", "ACH", "", "", "$1,000.00"),
    ]
)


@pytest.fixture(scope="module")
def txns():
    return parse_rows(list(csv.DictReader(io.StringIO(STATEMENT))))


@pytest.fixture
def positions(txns):
    return build_positions(build_trades(txns))


def by_key(positions, prefix):
    return next(p for p in positions if p.key.startswith(prefix))


# --------------------------------------------------------------------------------
# The money
# --------------------------------------------------------------------------------


def test_grouping_does_not_change_the_money(txns, positions) -> None:
    """Positions and the audited (symbol, closing day) journal must total the same."""
    entries = journal_entries(build_trades(txns))
    assert sum(p.realized for p in positions if not p.is_open) == pytest.approx(
        sum(e.profit for e in entries)
    )


def test_a_condor_is_one_position_of_four_legs(positions) -> None:
    condor = by_key(positions, "QQQ|2026-09-10")
    assert len(condor.legs) == 4
    assert condor.guess == "iron condor"
    assert condor.confident
    assert condor.dte_at_entry == 0
    assert condor.dte_class == "0DTE"
    assert condor.opening_cash == pytest.approx(54.80)
    assert condor.closing_cash == pytest.approx(-94.20)
    assert condor.realized == pytest.approx(-39.40)
    # Net per unit: 0.548 credit in, 0.942 debit out.
    assert condor.entry_price == pytest.approx(0.548)
    assert condor.exit_price == pytest.approx(0.942)


def test_r_is_the_trader_stop_not_the_spread_width(positions) -> None:
    """At a 2x stop, 1R is the credit. Max loss would make every result look tiny."""
    condor = by_key(positions, "QQQ|2026-09-10")
    assert condor.risk_unit == pytest.approx((STOP_MULTIPLE - 1.0) * 54.80)
    assert condor.r_multiple == pytest.approx(-39.40 / 54.80)
    assert condor.max_loss == pytest.approx(5 * 100 - 54.80)
    assert condor.close_multiple == pytest.approx(94.20 / 54.80)


def test_a_credit_too_small_to_risk_is_not_a_unit_of_r() -> None:
    """Three cents in and $17.70 out read as +6.19R on the real ledger. That is the
    denominator talking, so below the floor R is blank rather than enormous."""
    from optscan.analytics.positions import MIN_RISK_UNIT, Position

    tiny = Position(
        key="QQQ|2026-09-04|2026-09-04",
        symbol="QQQ",
        contract_expiry=date(2026, 9, 4),
        opened_at=date(2026, 9, 4),
        closed_at=date(2026, 9, 4),
        is_open=False,
        legs=[],
        trades=[],
        opening_cash=2.86,
        closing_cash=14.84,
        fees=0.0,
        units=1.0,
        max_loss=97.14,
        guess="multi-leg",
        confident=False,
    )
    assert tiny.risk_unit is None
    assert tiny.r_multiple is None
    assert MIN_RISK_UNIT > 2.86

    # And 1R never exceeds what the position could actually lose.
    capped = Position(**{**tiny.__dict__, "opening_cash": 300.0, "max_loss": 200.0})
    assert capped.risk_unit == 200.0


def test_a_spread_held_to_expiry_closes_at_zero(positions) -> None:
    spread = by_key(positions, "AAPL|2026-09-18")
    assert spread.guess == "put credit spread"
    assert spread.expired
    assert spread.exit_bucket == "held to expiry"
    assert spread.realized == pytest.approx(89.90)
    assert all(leg.close_price == 0.0 for leg in spread.legs)
    assert spread.dte_at_entry == 8
    assert spread.max_loss == pytest.approx(500 - 89.90)


def test_stock_is_its_own_strategy(positions) -> None:
    stock = by_key(positions, "TJX|-")
    assert stock.guess == "stock"
    assert stock.dte_class == "stock"
    assert stock.realized == pytest.approx(10.0)


def test_deposits_are_cash_flows_not_results(txns, positions) -> None:
    assert cash_flows(txns) == [(date(2026, 9, 1), 1000.0)]
    assert all(p.symbol for p in positions)


# --------------------------------------------------------------------------------
# What the journal raises by itself
# --------------------------------------------------------------------------------


def test_a_close_well_past_the_stop_is_flagged(positions) -> None:
    """The 9/11 condor took 44.80 in and paid 140.20 out: 3.1x, past a 2x stop."""
    book = build_book(positions, positions)
    stopped = by_key(positions, "QQQ|2026-09-11")
    assert "held past stop" in stopped.flags
    assert "held past stop" not in by_key(positions, "QQQ|2026-09-10").flags
    assert book.stops.beyond_stop == 1
    assert book.stops.stopped == 2
    assert book.stops.worst_multiple == pytest.approx(140.20 / 44.80)


def test_the_trader_tag_wins_over_the_guess(positions) -> None:
    attach(positions, annotations={"QQQ|2026-09-10|2026-09-10": Annotation(strategy="0dte condor")})
    assert by_key(positions, "QQQ|2026-09-10").strategy == "0dte condor"
    assert by_key(positions, "QQQ|2026-09-11").strategy == "iron condor"


# --------------------------------------------------------------------------------
# Times
# --------------------------------------------------------------------------------


def test_without_times_nothing_is_bucketed_by_time_of_day(positions) -> None:
    book = build_book(positions, positions)
    assert book.timed == 0
    assert book.by_entry_time == []
    assert any("No position has an entry time" in note for note in book.notes)


def test_the_1244_rule_is_on_the_traders_clock() -> None:
    """Measured: 0DTE exits cluster at 15:40-15:44 Eastern, which is the rule in Pacific.
    Read as Eastern, a close sixteen minutes before the bell counted as breaking it."""
    from optscan.analytics.positions import EXIT_BY, Position

    close = Position(
        key="QQQ|2026-09-10|2026-09-10",
        symbol="QQQ",
        contract_expiry=date(2026, 9, 10),
        opened_at=date(2026, 9, 10),
        closed_at=date(2026, 9, 10),
        is_open=False,
        legs=[],
        trades=[],
        opening_cash=40.0,
        closing_cash=-20.0,
        fees=0.0,
        units=1.0,
        max_loss=60.0,
        guess="iron condor",
        confident=True,
        exit_at=datetime(2026, 9, 10, 19, 43, tzinfo=UTC),  # 15:43 ET, 12:43 PT
    )
    assert close.exit_bucket == EXIT_BY


def test_typed_times_bucket_entries_and_test_the_1244_rule(positions) -> None:
    attach(
        positions,
        annotations={
            # Typed on the trader's own clock, Pacific.
            "QQQ|2026-09-10|2026-09-10": Annotation(entry_time="6:45", exit_time="12:50"),
            "QQQ|2026-09-11|2026-09-11": Annotation(entry_time="9:15", exit_time="12:40"),
        },
    )
    early = by_key(positions, "QQQ|2026-09-10")
    late = by_key(positions, "QQQ|2026-09-11")
    assert early.entry_bucket == "open 6:30-7:30 PT"
    assert early.exit_bucket == "closed after 12:44 PT"
    assert late.entry_bucket == "midday 9:00-11:00 PT"
    assert late.exit_bucket == "closed by 12:44 PT"
    assert early.time_source == "typed"


def test_order_history_times_attach_to_the_fills_they_belong_to(txns, positions) -> None:
    condor = by_key(positions, "QQQ|2026-09-10")
    opening = condor.legs[0].opening[0]
    closing = condor.legs[0].closing[0]
    moment_in = datetime(2026, 9, 10, 13, 50, tzinfo=UTC)  # 09:50 Eastern
    moment_out = datetime(2026, 9, 10, 16, 30, tzinfo=UTC)  # 12:30 Eastern
    attach(
        positions,
        fill_times={
            (opening.digest, opening.dup_index): moment_in,
            (closing.digest, closing.dup_index): moment_out,
        },
    )
    assert condor.entry_at == moment_in
    assert condor.exit_at == moment_out
    assert condor.time_source == "order history"
    assert condor.entry_bucket == "open 6:30-7:30 PT"


# --------------------------------------------------------------------------------
# Shapes and risk
# --------------------------------------------------------------------------------


def leg(right, strike, side, size=1.0) -> Leg:
    return Leg(right, strike, side, size, None, None, 0.0, False, (), ())


def test_shapes_the_journal_can_name_and_the_ones_it_cannot() -> None:
    assert guess_strategy([leg("call", 100, "short"), leg("call", 105, "long")], 50) == (
        "call credit spread",
        True,
    )
    assert guess_strategy([leg("put", 100, "short"), leg("call", 100, "short")], 500)[0] == (
        "short straddle"
    )
    butterfly = [
        leg("put", 95, "long"),
        leg("put", 100, "short"),
        leg("call", 100, "short"),
        leg("call", 105, "long"),
    ]
    assert guess_strategy(butterfly, 300) == ("iron butterfly", True)
    layered = [leg("put", 95, "long"), leg("put", 100, "short"), leg("put", 102, "short")]
    assert guess_strategy(layered, 80) == ("multi-leg", False)


def test_a_naked_short_call_has_no_maximum_loss() -> None:
    assert risk_at_entry([leg("call", 100, "short")], 150.0) is None
    assert risk_at_entry([leg("put", 100, "short")], 150.0) == pytest.approx(100 * 100 - 150)


# --------------------------------------------------------------------------------
# Streaks, sizing, regime
# --------------------------------------------------------------------------------


def test_streaks_count_runs_and_a_flat_result_breaks_them() -> None:
    result = streaks([1, 2, -1, -3, -2, 0, 5])
    assert result.longest_win == 2
    assert result.longest_loss == 3
    assert result.current == 1


def test_sizing_reads_the_account_as_of_each_trade(txns, positions) -> None:
    book = build_book(positions, positions, cash_flows=cash_flows(txns), starting_balance=4000.0)
    condor = by_key(positions, "QQQ|2026-09-10")
    point = next(p for p in book.sizing.points if p.key == condor.key)
    # 4000 starting + 1000 deposited + 10 from the TJX round trip closed before 9/10.
    assert point.account == pytest.approx(5010.0)
    assert point.share == pytest.approx((500 - 54.80) / 5010.0)


def test_without_a_balance_sizing_is_in_dollars_and_stock_is_never_flagged(txns, positions) -> None:
    """The statements start part way through the account, so without the balance before
    them there is no honest denominator: on the real ledger it produced 1,560 percent of
    an "account" one deposit old. Dollars answer the same question without inventing one.
    """
    book = build_book(positions, positions, cash_flows=cash_flows(txns))
    assert book.sizing.basis == "dollars"
    assert all(point.share is None and point.account is None for point in book.sizing.points)
    assert book.sizing.median_risk is not None
    assert "sized too big" not in by_key(positions, "TJX|-").flags


def test_capping_scales_only_the_trades_above_the_median_risk(positions) -> None:
    """Oversized trades come down to the median; smaller ones are never enlarged.

    1R here: the two condors 54.80 and 44.80, the AAPL spread 89.90, TJX shares 100.
    The median is 72.35, so only AAPL and TJX are scaled.
    """
    from optscan.analytics.positions import cap_to_median_risk, entries_from_positions

    ceiling, capped = cap_to_median_risk(positions, positions)
    assert ceiling == pytest.approx((54.80 + 89.90) / 2)
    assert capped == 2

    small = by_key(positions, "QQQ|2026-09-10")
    assert small.scaled is None, "a trade under the median is not enlarged"
    assert small.outcome == pytest.approx(-39.40)

    big = by_key(positions, "AAPL|2026-09-18")
    assert big.r_multiple == pytest.approx(1.0)
    assert big.outcome == pytest.approx(round(1.0 * ceiling, 2))
    assert big.realized == pytest.approx(89.90), "the real result is untouched"

    entries = entries_from_positions(positions)
    assert sum(e.profit for e in entries) == pytest.approx(
        sum(p.outcome for p in positions if not p.is_open), abs=0.02
    )


def test_without_capping_the_outcome_is_the_real_result(positions) -> None:
    from optscan.analytics.positions import entries_from_positions

    assert all(p.outcome == (p.realized or 0.0) for p in positions)
    entries = entries_from_positions(positions)
    assert sum(e.profit for e in entries) == pytest.approx(
        sum(p.realized for p in positions if not p.is_open)
    )


def test_spy_trend_labels_need_twenty_sessions() -> None:
    closes = [
        (date(2026, 1, 1).replace(day=1 + i % 28, month=1 + i // 28), 100.0 + i) for i in range(25)
    ]
    labels = trend_labels(closes)
    assert len(labels) == 5
    assert set(labels.values()) == {"SPY uptrend"}
