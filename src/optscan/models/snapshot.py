"""The unit the daily job captures and storage persists.

One snapshot is everything known about one underlying at one moment: the quote plus
every chain that was captured. This is the record that IV rank is built from in
Phase 2, so it is deliberately conservative about what it will accept.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Self

from pydantic import Field, model_validator

from optscan.models.base import Record
from optscan.models.market import OptionChain, Quote, Symbol


class ChainSnapshot(Record):
    """A full capture for one symbol, tagged with the trading date it belongs to.

    session_date is the trading day the capture represents, not the wall clock date
    of the process. A job that runs at 21:00 UTC on a US market day is capturing that
    day's session, and a job that runs on a weekend is capturing nothing at all.
    """

    symbol: Symbol
    session_date: date
    quote: Quote
    chains: tuple[OptionChain, ...] = ()
    partial: bool = Field(
        default=False,
        description="True when at least one expiry failed to fetch. Never silently dropped.",
    )
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _chains_belong_to_this_symbol(self) -> Self:
        if self.quote.symbol != self.symbol:
            raise ValueError(f"quote is for {self.quote.symbol}, snapshot is for {self.symbol}")
        for chain in self.chains:
            if chain.symbol != self.symbol:
                raise ValueError(f"chain is for {chain.symbol}, snapshot is for {self.symbol}")
        expiries = [chain.expiry for chain in self.chains]
        if len(expiries) != len(set(expiries)):
            raise ValueError("snapshot contains the same expiry twice")
        return self

    @property
    def expiries(self) -> tuple[date, ...]:
        return tuple(sorted(chain.expiry for chain in self.chains))

    @property
    def contract_count(self) -> int:
        return sum(len(chain.contracts) for chain in self.chains)

    def contracts(self) -> Iterator:
        """Every contract across every expiry, in chain order."""
        for chain in self.chains:
            yield from chain.contracts
