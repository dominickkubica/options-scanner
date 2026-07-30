"""Scheduled corporate events.

A record like any other: it carries when it was fetched and who said so. Event dates
move, and a stale earnings date is worse than no earnings date because it produces a
confident "no event before expiry" that is wrong.
"""

from __future__ import annotations

from datetime import date

from pydantic import Field

from optscan.models.base import Record
from optscan.models.market import Symbol


class SymbolEvents(Record):
    """What is scheduled for one underlying.

    estimated marks an earnings date the vendor inferred rather than one the company
    confirmed. Vendors routinely publish a guess for a quarter that has not been
    announced yet, and treating that as fact is how a position ends up held through
    a report it was screened to avoid.
    """

    symbol: Symbol
    earnings_date: date | None = None
    earnings_estimated: bool = False
    ex_dividend_date: date | None = None
    dividend_amount: float | None = Field(default=None, ge=0.0)

    @property
    def has_any(self) -> bool:
        return self.earnings_date is not None or self.ex_dividend_date is not None
