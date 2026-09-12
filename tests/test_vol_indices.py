"""Cboe volatility indices as the IV history for SPY, QQQ, IWM and GLD.

What these hold: the index is its own series and is chosen whole, never mixed; among
series long enough for a full rank the current one wins, so a frozen export stops
describing today; and an index close never lands in the price table.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from optscan.jobs.vol_indices import (
    BACKFILL_DAYS,
    TOP_UP_DAYS,
    VOL_INDICES,
    index_history,
    sync_vol_indices,
)
from optscan.screener.history import (
    FRESH_DAYS,
    SUFFICIENT_OBSERVATIONS,
    IvHistory,
    choose_iv_history,
    vendor_iv_history,
)
from optscan.storage import db, vol_index

END = date(2026, 9, 11)


def closes(count: int, end: date = END, level: float = 15.0) -> list[tuple[date, float]]:
    return [(end - timedelta(days=count - 1 - i), level + (i % 7)) for i in range(count)]


def test_the_etfs_with_a_published_index_and_only_those() -> None:
    """IWM's RVX is out: Yahoo returns nothing for ^RVX."""
    assert set(VOL_INDICES) == {"SPY", "QQQ", "GLD"}


def test_an_index_history_is_in_decimals_with_its_own_latest_as_today(tmp_settings) -> None:
    with db.session(tmp_settings.sqlite_path) as conn:
        vol_index.save_closes(conn, "^VXN", closes(300))
        history = index_history(conn, "QQQ", until=END)
        assert index_history(conn, "AAPL", until=END).points == []
    assert history.downloaded
    assert history.source == "Cboe VXN"
    assert history.current_date == END
    assert history.current == pytest.approx((15.0 + (299 % 7)) / 100)
    assert all(value < 1 for _, value in history.points), "vol points were not converted"
    assert (END, history.current) not in history.points


def test_an_index_close_never_lands_in_the_price_table(tmp_settings) -> None:
    """A VIX row filed as SPY would be read as SPY's price by everything downstream."""
    with db.session(tmp_settings.sqlite_path) as conn:
        vol_index.save_closes(conn, "^VIX", closes(10))
        assert conn.execute("SELECT COUNT(*) FROM vendor_daily").fetchone()[0] == 0


def test_a_current_index_beats_a_frozen_export_of_the_same_length() -> None:
    """QQQ on 2026-09-12: the export's latest was eight days old, VXN's yesterday's."""
    frozen = vendor_iv_history(closes(400, end=END - timedelta(days=8)), "marketchameleon")
    current = vendor_iv_history(closes(300), "Cboe VXN")
    chosen = choose_iv_history(IvHistory(), frozen, current, asof=END + timedelta(days=1))
    assert chosen.source == "Cboe VXN"
    assert chosen.stale_days == 1


def test_a_stale_year_still_beats_two_fresh_weeks(tmp_settings) -> None:
    """AAPL has no index. A stale rank from a year of data beats no rank at all, and
    the note says how stale it is rather than letting it pass as today's."""
    own = IvHistory(points=closes(13), source="yfinance")
    frozen = vendor_iv_history(closes(400, end=END - timedelta(days=8)), "marketchameleon")
    chosen = choose_iv_history(own, frozen, IvHistory(), asof=END)
    assert chosen.source == "marketchameleon"
    assert chosen.stale_days == 8
    assert chosen.stale_days > FRESH_DAYS
    assert "8 days older than this capture" in (chosen.note() or "")


def test_below_a_year_length_still_decides() -> None:
    short = vendor_iv_history(closes(SUFFICIENT_OBSERVATIONS - 50), "Cboe GVZ")
    longer = vendor_iv_history(
        closes(SUFFICIENT_OBSERVATIONS - 10, end=END - timedelta(days=30)), "x"
    )
    assert choose_iv_history(IvHistory(), longer, short, asof=END).source == "x"


def test_the_sync_backfills_once_then_tops_up(tmp_settings) -> None:
    asked: list[tuple[str, int]] = []

    def fake(index: str, days: int):
        asked.append((index, days))
        return closes(20)

    first = sync_vol_indices(tmp_settings, fetch=fake)
    assert set(first.stored) == set(VOL_INDICES.values())
    assert {days for _, days in asked} == {BACKFILL_DAYS}

    asked.clear()
    sync_vol_indices(tmp_settings, fetch=fake)
    assert {days for _, days in asked} == {TOP_UP_DAYS}


def test_one_failed_index_does_not_stop_the_others(tmp_settings) -> None:
    def flaky(index: str, days: int):
        if index == "^GVZ":
            raise RuntimeError("yahoo said no")
        return closes(5)

    report = sync_vol_indices(tmp_settings, fetch=flaky)
    assert "^GVZ" in report.failed
    assert len(report.stored) == len(set(VOL_INDICES.values())) - 1
    assert "Failed" in report.summary()
