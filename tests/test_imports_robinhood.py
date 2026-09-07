"""The Robinhood parser, and the ledger derived from it.

Every expected value here was worked out by hand from the reference export before the
code was run against it. The fixture is a trimmed copy of a real download rather than
something synthesised, for the same reason the chain and bar fixtures are real: a made
up statement agrees with whatever the parser happens to do.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from optscan.analytics.ledger import ContractKey, build_trades, summarize
from optscan.imports import (
    RobinhoodParseError,
    UnknownTransactionCode,
    parse_rows,
)
from optscan.models.broker import TxnKind
from optscan.models.enums import Right
from optscan.models.opportunity import Action
from optscan.storage import db
from optscan.storage.ledger import all_transactions, import_transactions

HEADER = (
    "Activity Date",
    "Process Date",
    "Settle Date",
    "Instrument",
    "Description",
    "Trans Code",
    "Quantity",
    "Price",
    "Amount",
)


def row(activity, instrument, description, code, qty, price, amount, process=None, settle=None):
    return dict(
        zip(
            HEADER,
            (
                activity,
                process or activity,
                settle or activity,
                instrument,
                description,
                code,
                qty,
                price,
                amount,
            ),
            strict=True,
        )
    )


# A closed QQQ put credit spread: sell the 718, buy the 717, then close both.
SPREAD = [
    row("9/4/2026", "QQQ", "QQQ 9/4/2026 Put $718.00", "STO", "1", "$0.61", "$60.94"),
    row("9/4/2026", "QQQ", "QQQ 9/4/2026 Put $717.00", "BTO", "1", "$0.34", "($34.04)"),
    row("9/4/2026", "QQQ", "QQQ 9/4/2026 Put $718.00", "BTC", "1", "$0.28", "($28.04)"),
    row("9/4/2026", "QQQ", "QQQ 9/4/2026 Put $717.00", "STC", "1", "$0.14", "$13.94"),
]


class TestParsing:
    def test_parses_an_option_trade_into_its_parts(self):
        txn = parse_rows([SPREAD[0]])[0]
        assert txn.kind is TxnKind.OPTION_TRADE
        assert txn.symbol == "QQQ"
        assert txn.expiry == date(2026, 9, 4)
        assert txn.right is Right.PUT
        assert txn.strike == 718.0
        assert txn.action is Action.SELL
        assert txn.quantity == 1
        assert txn.price == 0.61

    def test_parenthesised_amount_is_cash_leaving_the_account(self):
        """The single most damaging thing to get wrong: every debit becomes a credit."""
        credit, debit = parse_rows([SPREAD[0], SPREAD[1]])
        assert credit.amount == 60.94
        assert debit.amount == -34.04

    def test_fee_is_the_gap_between_the_stated_price_and_the_cash_moved(self):
        """Sold 1 contract at 0.61: gross 61.00, received 60.94, so 6 cents kept."""
        sold, bought = parse_rows([SPREAD[0], SPREAD[1]])
        assert sold.fee == pytest.approx(0.06)
        # Bought 1 at 0.34: gross 34.00, paid 34.04, so 4 cents on top.
        assert bought.fee == pytest.approx(0.04)

    def test_expiration_uses_the_other_description_format(self):
        """A parser written against trade descriptions alone loses every expiration."""
        txn = parse_rows(
            [
                row(
                    "8/14/2026",
                    "IBIT",
                    "Option Expiration for IBIT 8/14/2026 Call $37.00",
                    "OEXP",
                    "1",
                    "",
                    "",
                )
            ]
        )[0]
        assert txn.kind is TxnKind.OPTION_EXPIRATION
        assert txn.symbol == "IBIT"
        assert txn.strike == 37.0
        assert txn.right is Right.CALL

    def test_expiration_states_no_direction_of_its_own(self):
        """The trailing S is captured but never used to decide direction.

        In the reference export the S sat on the bought leg in all three expiring
        spreads, and on the higher strike in all three. Three cases cannot separate
        those rules, so the row alone must refuse to answer.
        """
        plain, marked = parse_rows(
            [
                row(
                    "8/14/2026",
                    "IBIT",
                    "Option Expiration for IBIT 8/14/2026 Call $37.00",
                    "OEXP",
                    "1",
                    "",
                    "",
                ),
                row(
                    "8/14/2026",
                    "IBIT",
                    "Option Expiration for IBIT 8/14/2026 Call $38.00",
                    "OEXP",
                    "1S",
                    "",
                    "",
                ),
            ]
        )
        assert plain.is_short is False
        assert marked.is_short is True
        assert plain.signed_quantity is None
        assert marked.signed_quantity is None

    def test_blank_amount_is_unknown_and_not_zero(self):
        txn = parse_rows(
            [
                row(
                    "8/14/2026",
                    "IBIT",
                    "Option Expiration for IBIT 8/14/2026 Call $37.00",
                    "OEXP",
                    "1",
                    "",
                    "",
                )
            ]
        )[0]
        assert txn.amount is None
        assert txn.fee is None

    def test_unknown_transaction_code_is_fatal(self):
        """Silently skipping a code that moves contracts hides legs from the profit."""
        with pytest.raises(UnknownTransactionCode) as caught:
            parse_rows([row("9/4/2026", "QQQ", "QQQ 9/4/2026 Put $718.00", "WAT", "1", "$1", "$1")])
        assert "WAT" in str(caught.value)

    def test_option_code_with_an_unparseable_description_raises(self):
        with pytest.raises(RobinhoodParseError):
            parse_rows([row("9/4/2026", "QQQ", "Some Company Inc", "STO", "1", "$1", "$1")])

    def test_trailing_disclaimer_row_is_skipped(self):
        blank = dict.fromkeys(HEADER, "")
        assert parse_rows([SPREAD[0], blank]) == parse_rows([SPREAD[0]])

    def test_identical_rows_are_kept_apart_by_occurrence(self):
        """Two identical fills on one day are two trades, not one."""
        first, second = parse_rows([SPREAD[0], dict(SPREAD[0])])
        assert first.digest == second.digest
        assert (first.dup_index, second.dup_index) == (0, 1)


class TestTrades:
    def test_a_closed_spread_reconciles_to_its_cash(self):
        """60.94 - 34.04 - 28.04 + 13.94 = 12.80 across the two contracts."""
        trades = build_trades(parse_rows(SPREAD))
        assert len(trades) == 2
        assert all(not t.open_at_end for t in trades)
        assert sum(t.cash for t in trades) == pytest.approx(12.80)
        # Four legs at 6, 4, 4 and 6 cents.
        assert sum(t.fees for t in trades) == pytest.approx(0.20)

    def test_expiration_closes_whatever_the_ledger_says_is_open(self):
        """The direction comes from prior state, not from the S marker."""
        rows = [
            row("7/14/2026", "IBIT", "IBIT 7/17/2026 Call $37.50", "STO", "1", "$0.22", "$21.94"),
            row("7/14/2026", "IBIT", "IBIT 7/17/2026 Call $38.00", "BTO", "1", "$0.12", "($12.04)"),
            row(
                "7/17/2026",
                "IBIT",
                "Option Expiration for IBIT 7/17/2026 Call $37.50",
                "OEXP",
                "1",
                "",
                "",
            ),
            row(
                "7/17/2026",
                "IBIT",
                "Option Expiration for IBIT 7/17/2026 Call $38.00",
                "OEXP",
                "1S",
                "",
                "",
            ),
        ]
        trades = build_trades(parse_rows(rows))
        assert [t.open_at_end for t in trades] == [False, False]
        assert all(t.ended_by_event for t in trades)
        assert sum(t.cash for t in trades) == pytest.approx(9.90)

    def test_an_open_position_reports_no_profit(self):
        """Cash received so far is not a result, or every loser looks like a winner."""
        trades = build_trades(parse_rows([SPREAD[0]]))
        assert len(trades) == 1
        assert trades[0].open_at_end is True
        assert trades[0].realized is None

    def test_cash_movements_are_not_trades_and_not_profit(self):
        rows = [*SPREAD, row("9/2/2026", "", "ACH Deposit", "ACH", "", "", "$500.00")]
        txns = parse_rows(rows)
        summary = summarize(txns, build_trades(txns))
        assert summary.option_realized == pytest.approx(12.80)
        assert summary.cash_flows == {"ACH": 500.0}

    def test_equity_and_option_results_are_reported_separately(self):
        rows = [
            *SPREAD,
            row("8/3/2026", "GPRO", "Gopro Inc", "Buy", "10", "$2.00", "($20.00)"),
            row("8/5/2026", "GPRO", "Gopro Inc", "Sell", "10", "$2.50", "$24.98"),
        ]
        txns = parse_rows(rows)
        summary = summarize(txns, build_trades(txns))
        assert summary.option_realized == pytest.approx(12.80)
        assert summary.equity_realized == pytest.approx(4.98)
        assert summary.option_trades == 2
        assert summary.equity_trades == 1

    def test_contract_key_separates_strikes_and_rights(self):
        keys = {
            ContractKey("QQQ", date(2026, 9, 4), Right.PUT, 718.0),
            ContractKey("QQQ", date(2026, 9, 4), Right.PUT, 717.0),
            ContractKey("QQQ", date(2026, 9, 4), Right.CALL, 718.0),
        }
        assert len(keys) == 3


class TestLedgerStorage:
    @pytest.fixture
    def conn(self, tmp_path) -> sqlite3.Connection:
        return db.connect(tmp_path / "test.sqlite")

    def test_import_then_reimport_inserts_nothing_the_second_time(self, conn):
        """Exports overlap. A second import of the same file must be a no-op."""
        txns = parse_rows(SPREAD)
        first = import_transactions(conn, txns)
        assert (first.rows_inserted, first.rows_duplicate) == (4, 0)

        second = import_transactions(conn, txns)
        assert (second.rows_inserted, second.rows_duplicate) == (0, 4)
        assert second.already_known is True
        assert len(all_transactions(conn)) == 4

    def test_an_overlapping_export_adds_only_what_is_new(self, conn):
        import_transactions(conn, parse_rows(SPREAD[:2]))
        report = import_transactions(conn, parse_rows(SPREAD))
        assert (report.rows_inserted, report.rows_duplicate) == (2, 2)
        assert len(all_transactions(conn)) == 4

    def test_two_identical_fills_both_survive_a_round_trip(self, conn):
        """The dup_index half of the identity. Without it one real trade vanishes."""
        import_transactions(conn, parse_rows([SPREAD[0], dict(SPREAD[0])]))
        assert len(all_transactions(conn)) == 2

    def test_stored_rows_rebuild_the_same_trades(self, conn):
        import_transactions(conn, parse_rows(SPREAD))
        trades = build_trades(all_transactions(conn))
        assert len(trades) == 2
        assert sum(t.cash for t in trades) == pytest.approx(12.80)
