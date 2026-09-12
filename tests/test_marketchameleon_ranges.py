"""A Market Chameleon row whose open or close sits outside its own high-low range.

Three cases, and the point is that they are told apart by how many rows they touch as
much as by how far off each one is:

  - rounding (AMZN 2019-03-18, 0.014%): repaired quietly, as before
  - one mistyped row (GLD 2021-05-05, 0.14%): repaired and logged. Refusing it threw
    away twelve years of GLD's IV30 over a number nothing reads.
  - a split or adjustment mix-up: hundreds of rows, each off by the split ratio. Still
    refused, by either test.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from optscan.imports.marketchameleon import (
    MAX_ISOLATED_REPAIRS,
    MarketChameleonParseError,
    parse_rows,
)


def row(day: date, open_: float, high: float, low: float, close: float) -> dict[str, str]:
    return {
        "Date": f"{day.month}/{day.day}/{day.year}",
        "Open": str(open_),
        "High": str(high),
        "Low": str(low),
        "Close": str(close),
        "Adj Close": str(close),
        "Volume": "1000",
        "Day VWAP": "",
        "IV30": "13.0",
        "Call Option Volume": "",
        "Put Option Volume": "",
        "Call Open Interest": "",
        "Put Open Interest": "",
    }


START = date(2021, 5, 3)


def clean(count: int) -> list[dict[str, str]]:
    return [row(START + timedelta(days=i), 167.0, 168.0, 166.0, 167.5) for i in range(count)]


def test_one_mistyped_row_is_repaired_rather_than_losing_the_file() -> None:
    """The real GLD row: open 166.63 against a low of 166.87."""
    rows = [*clean(3), row(date(2021, 5, 10), 166.63, 167.31, 166.87, 167.27)]
    bars = parse_rows(rows, "GLD")
    assert len(bars) == 4
    fixed = next(bar for bar in bars if bar.session_date == date(2021, 5, 10))
    assert fixed.low == 166.63
    assert fixed.iv30 == pytest.approx(0.13)


def test_rounding_is_still_repaired_quietly() -> None:
    rows = [row(START, 85.6195, 86.0, 85.6315, 85.9)]
    assert parse_rows(rows, "AMZN")[0].low == 85.6195


def test_many_rows_off_is_a_different_price_series_and_is_refused() -> None:
    """An adjustment mix-up breaks every session on one side of the event."""
    bad = [
        row(date(2020, 1, 1) + timedelta(days=i), 99.5, 101.0, 100.0, 100.5)
        for i in range(MAX_ISOLATED_REPAIRS + 1)
    ]
    with pytest.raises(MarketChameleonParseError, match="two different price series"):
        parse_rows(clean(10) + bad, "XYZ")


def test_one_row_far_outside_its_range_is_refused() -> None:
    """A single row off by a split ratio is not a typo, however few there are."""
    with pytest.raises(MarketChameleonParseError, match="not a typo"):
        parse_rows([row(START, 50.0, 101.0, 100.0, 100.5)], "XYZ")
