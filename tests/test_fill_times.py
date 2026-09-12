"""Fill times: Robinhood's order history joined to the statement ledger, offline.

The login cannot be tested and should not be; everything after it can. The rule these
tests hold is the module's: a fill lands on the one row it produced or on none, never
on the nearest one, because a wrong time silently moves a trade between buckets.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime

import pytest

from optscan.analytics.ledger import build_trades
from optscan.analytics.positions import attach, build_positions
from optscan.imports.fill_times import ExecutedFill, match_fills
from optscan.imports.robinhood import parse_rows
from optscan.models.broker import Effect
from optscan.models.enums import Right
from optscan.models.opportunity import Action
from optscan.providers.robinhood_orders import parse_option_orders, parse_stock_orders
from tests.test_journal_positions import STATEMENT


@pytest.fixture(scope="module")
def txns():
    return parse_rows(list(csv.DictReader(io.StringIO(STATEMENT))))


def at(hour: int, minute: int, day: int = 10) -> datetime:
    """A moment on 2026-09-{day}, given in Eastern (EDT is UTC-4)."""
    return datetime(2026, 9, day, hour + 4, minute, tzinfo=UTC)


def fill(strike, right, action, effect, price, moment, quantity=1.0, expiry=date(2026, 9, 10)):
    return ExecutedFill(
        symbol="QQQ",
        expiry=expiry,
        right=right,
        strike=strike,
        action=action,
        effect=effect,
        quantity=quantity,
        price=price,
        executed_at=moment,
        order_id="o1",
    )


CONDOR_OPEN = [
    fill(500.0, Right.PUT, Action.SELL, Effect.OPEN, 0.50, at(9, 47)),
    fill(495.0, Right.PUT, Action.BUY, Effect.OPEN, 0.20, at(9, 47)),
    fill(520.0, Right.CALL, Action.SELL, Effect.OPEN, 0.40, at(9, 47)),
    fill(525.0, Right.CALL, Action.BUY, Effect.OPEN, 0.15, at(9, 47)),
]
CONDOR_CLOSE = [
    fill(500.0, Right.PUT, Action.BUY, Effect.CLOSE, 1.2004, at(12, 31)),
    fill(495.0, Right.PUT, Action.SELL, Effect.CLOSE, 0.30, at(12, 31)),
    fill(520.0, Right.CALL, Action.BUY, Effect.CLOSE, 0.05, at(12, 31)),
    fill(525.0, Right.CALL, Action.SELL, Effect.CLOSE, 0.01, at(12, 31)),
]


def test_every_leg_of_a_condor_finds_its_row(txns) -> None:
    report = match_fills(CONDOR_OPEN + CONDOR_CLOSE, txns)
    assert len(report.matched) == 8
    assert report.unmatched_fills == []
    for txn, executed in report.matched:
        assert txn.activity_date == executed.trading_day
        assert txn.strike == executed.strike


def test_a_fill_at_a_different_price_is_not_forced_onto_a_row(txns) -> None:
    """A wrong time is worse than none: it moves the trade between buckets silently."""
    wrong = fill(500.0, Right.PUT, Action.SELL, Effect.OPEN, 0.55, at(9, 47))
    report = match_fills([wrong], txns)
    assert report.matched == []
    assert report.unmatched_fills == [wrong]


def test_a_fill_on_another_day_does_not_match(txns) -> None:
    elsewhere = fill(500.0, Right.PUT, Action.SELL, Effect.OPEN, 0.50, at(9, 47, day=9))
    assert match_fills([elsewhere], txns).matched == []


def test_a_row_is_used_once(txns) -> None:
    """Two identical fills cannot both claim one row."""
    twice = [CONDOR_OPEN[0], fill(500.0, Right.PUT, Action.SELL, Effect.OPEN, 0.50, at(10, 5))]
    report = match_fills(twice, txns)
    assert len(report.matched) == 1
    assert len(report.unmatched_fills) == 1


def test_matched_times_reach_the_position(txns) -> None:
    report = match_fills(CONDOR_OPEN + CONDOR_CLOSE, txns)
    times = {(txn.digest, txn.dup_index): executed.executed_at for txn, executed in report.matched}
    positions = build_positions(build_trades(txns))
    attach(positions, fill_times=times)
    condor = next(p for p in positions if p.key == "QQQ|2026-09-10|2026-09-10")
    # 9:47 and 12:31 Eastern are 6:47 and 9:31 on the trader's Pacific clock.
    assert condor.entry_bucket == "open 6:30-7:30 PT"
    assert condor.exit_bucket == "closed by 12:44 PT"
    assert condor.time_source == "order history"


def test_share_fills_match_share_rows(txns) -> None:
    share = ExecutedFill(
        symbol="TJX",
        expiry=None,
        right=None,
        strike=None,
        action=Action.BUY,
        effect=None,
        quantity=1.0,
        price=100.0,
        executed_at=at(10, 2, day=8),
        order_id="s1",
    )
    report = match_fills([share], txns)
    assert len(report.matched) == 1
    assert report.matched[0][0].symbol == "TJX"


# --------------------------------------------------------------------------------
# Parsing the order history's shape
# --------------------------------------------------------------------------------


def test_option_orders_group_executions_by_day_and_average_the_price() -> None:
    orders = [
        {
            "id": "abc",
            "chain_symbol": "QQQ",
            "state": "filled",
            "legs": [
                {
                    "side": "sell",
                    "position_effect": "open",
                    "expiration_date": "2026-09-10",
                    "strike_price": "500.0000",
                    "option_type": "put",
                    "executions": [
                        {"timestamp": "2026-09-10T13:47:05.1Z", "quantity": "1", "price": "0.52"},
                        {"timestamp": "2026-09-10T13:48:00Z", "quantity": "1", "price": "0.48"},
                    ],
                },
                # Cancelled leg: no executions, skipped rather than failing the order.
                {"side": "buy", "position_effect": "open", "executions": []},
            ],
        }
    ]
    fills = parse_option_orders(orders, instrument=lambda url: {})
    assert len(fills) == 1
    only = fills[0]
    assert only.quantity == 2.0
    assert only.price == pytest.approx(0.50)
    assert only.executed_at == datetime(2026, 9, 10, 13, 47, 5, 100000, tzinfo=UTC)
    assert (only.action, only.effect, only.right) == (Action.SELL, Effect.OPEN, Right.PUT)


def test_a_leg_missing_its_contract_is_looked_up_from_its_url() -> None:
    orders = [
        {
            "id": "x",
            "chain_symbol": None,
            "legs": [
                {
                    "side": "buy",
                    "position_effect": "close",
                    "option": "https://api.robinhood.com/options/instruments/1/",
                    "executions": [
                        {"timestamp": "2026-09-10T16:31:00Z", "quantity": "1", "price": "1.20"}
                    ],
                }
            ],
        }
    ]
    looked_up = []

    def instrument(url):
        looked_up.append(url)
        return {
            "expiration_date": "2026-09-10",
            "strike_price": "500.0000",
            "type": "put",
            "chain_symbol": "QQQ",
        }

    fills = parse_option_orders(orders, instrument)
    assert looked_up == ["https://api.robinhood.com/options/instruments/1/"]
    assert fills[0].symbol == "QQQ"
    assert fills[0].strike == 500.0


def test_stock_orders_resolve_the_ticker() -> None:
    orders = [
        {
            "id": "s",
            "side": "sell",
            "instrument": "https://api.robinhood.com/instruments/tjx/",
            "executions": [{"timestamp": "2026-09-09T14:00:00Z", "quantity": "1", "price": "110"}],
        }
    ]
    fills = parse_stock_orders(orders, symbol=lambda url: "tjx")
    assert fills[0].symbol == "TJX"
    assert fills[0].action is Action.SELL
