"""The premium study end to end, on two synthetic stored sessions.

Spot sits at 100 and every contract prints its exact model price every minute, so the
study's plumbing can be checked without a market: which positions open, that a 1 DTE
position needs the next session to reach expiry, and that paying more of the spread
costs money.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest

from optscan.analytics.premium import Structure, expiry_seconds
from optscan.analytics.vector_bsm import bsm_price
from optscan.config import Settings
from optscan.jobs.premium import (
    BASELINE,
    StudySpec,
    cell_rows,
    reconcile,
    reconcile_lines,
    report_lines,
    run_study,
)
from optscan.models import PriceBar
from optscan.storage.option_tape import tape_root, write_session

RATE = 0.043
SIGMA = 1.0
MONDAY = date(2025, 4, 7)
TUESDAY = date(2025, 4, 8)
STRIKES = np.arange(90.0, 111.0)


def store_session(settings: Settings, session: date, expiries: list[date]) -> None:
    opened = datetime(session.year, session.month, session.day, 13, 30, tzinfo=UTC)
    stamps = [opened + timedelta(minutes=i) for i in range(390)]
    minutes = [
        PriceBar(
            symbol="QQQ",
            ts=stamp,
            open=100.0,
            high=100.0,
            low=100.0,
            close=100.0,
            volume=1000,
            fetched_at=stamp,
            source="fake",
        )
        for stamp in stamps
    ]
    tape: dict[str, list[dict]] = {}
    for expiry in expiries:
        for right in ("P", "C"):
            for strike in STRIKES:
                tape[f"QQQ{expiry:%y%m%d}{right}{int(strike * 1000):08d}"] = []
        for stamp in stamps:
            years = (expiry_seconds(expiry) - stamp.timestamp()) / (365 * 24 * 3600)
            for right in ("P", "C"):
                prices = bsm_price(right == "C", 100.0, STRIKES, years, RATE, SIGMA)
                for strike, price in zip(STRIKES, prices, strict=True):
                    tape[f"QQQ{expiry:%y%m%d}{right}{int(strike * 1000):08d}"].append(
                        {
                            "expiry": expiry,
                            "right": right,
                            "strike": float(strike),
                            "ts": stamp,
                            "close": float(price),
                            "vwap": float(price),
                            "volume": 1,
                            "trades": 1,
                        }
                    )
    write_session(tape_root(settings.data_path), "QQQ", session, minutes, tape, manifest={})


@pytest.fixture
def stored(tmp_settings: Settings) -> Settings:
    store_session(tmp_settings, MONDAY, [MONDAY, TUESDAY])
    store_session(tmp_settings, TUESDAY, [TUESDAY])
    return tmp_settings


def test_positions_open_on_every_session_and_a_1dte_needs_the_next(stored: Settings) -> None:
    spec = StudySpec(symbols=("QQQ",), ks=(0.0, 1.0))
    result = run_study(stored, spec)

    for structure in Structure:
        assert result.opened[(0, structure)] == 2, "one 0DTE position a session"
        assert result.opened[(1, structure)] == 1, "Monday's 1DTE; Tuesday has no Wednesday"
    assert result.coverage["QQQ"] == (MONDAY, TUESDAY, 2)


def test_paying_the_spread_costs_money(stored: Settings) -> None:
    result = run_study(stored, StudySpec(symbols=("QQQ",), ks=(0.0, 1.0), dtes=(0,)))
    free = result.trades[(0, Structure.IRON_CONDOR, BASELINE.label, 0.0)]
    paid = result.trades[(0, Structure.IRON_CONDOR, BASELINE.label, 1.0)]
    assert len(free) == len(paid) == 2
    assert sum(t.pnl for t in paid) < sum(t.pnl for t in free)


def test_a_flat_market_decays_a_short_premium_position_into_profit(stored: Settings) -> None:
    """Nothing moves, so the credit decays toward zero by the close: with no spread
    paid, flattening five minutes before the close keeps most of it."""
    result = run_study(stored, StudySpec(symbols=("QQQ",), ks=(0.0,), dtes=(0,)))
    for trade in result.trades[(0, Structure.PUT_SPREAD, BASELINE.label, 0.0)]:
        assert trade.exit_debit < trade.credit
        assert trade.pnl > 0


def test_the_report_renders_and_says_what_it_cannot_tell(stored: Settings) -> None:
    result = run_study(stored, StudySpec(symbols=("QQQ",), ks=(0.0, 0.5)))
    rows = cell_rows(result)
    assert rows and all(not row.stats.reportable for row in rows), "two sessions is too few"
    text = "\n".join(report_lines(result, rows))
    assert "Short premium study" in text
    assert "What this cannot tell you" in text
    assert "too few sessions" in text


def test_an_unsupported_dte_is_refused(stored: Settings) -> None:
    with pytest.raises(ValueError, match="not supported"):
        run_study(stored, StudySpec(symbols=("QQQ",), dtes=(7,)))


def test_reconciling_an_empty_ledger_says_so(stored: Settings) -> None:
    lines = reconcile_lines(reconcile(stored))
    assert lines[0].startswith("Replayed 0")
    assert "Nothing to compare" in lines[1]
