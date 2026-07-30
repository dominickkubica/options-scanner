"""Rebuilding an ATM implied vol history from stored snapshots.

Driven by the frozen chain written out through the real storage layer and read back,
so the column names and dtypes under test are the ones the snapshot job actually
produces rather than ones invented for the test.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from optscan.models import ChainSnapshot
from optscan.screener.history import atm_iv_history
from optscan.storage import read_snapshots, write_snapshot

RATE = 0.043


@pytest.fixture
def stored(frozen_snapshot: ChainSnapshot, tmp_path: Path) -> pd.DataFrame:
    write_snapshot(frozen_snapshot, tmp_path)
    return read_snapshots(tmp_path)


@pytest.fixture
def three_sessions(frozen_snapshot: ChainSnapshot, tmp_path: Path) -> pd.DataFrame:
    """The same chain restamped onto three consecutive sessions."""
    for offset in range(3):
        shifted = frozen_snapshot.model_copy(
            update={
                "session_date": frozen_snapshot.session_date - timedelta(days=offset),
                "fetched_at": frozen_snapshot.fetched_at - timedelta(days=offset),
            }
        )
        write_snapshot(shifted, tmp_path)
    return read_snapshots(tmp_path)


class TestAtmIvHistory:
    def test_one_value_per_session(self, three_sessions: pd.DataFrame) -> None:
        history = atm_iv_history(three_sessions, rate=RATE)
        assert len(history) == 3
        assert len({day for day, _ in history}) == 3

    def test_oldest_first(self, three_sessions: pd.DataFrame) -> None:
        days = [day for day, _ in atm_iv_history(three_sessions, rate=RATE)]
        assert days == sorted(days)

    def test_values_are_plausible_volatilities(self, stored: pd.DataFrame) -> None:
        history = atm_iv_history(stored, rate=RATE)
        assert history
        for _, value in history:
            assert 0.01 < value < 2.0

    def test_picks_the_expiry_nearest_the_target_horizon(self, stored: pd.DataFrame) -> None:
        """The fixture holds a 4 day and an 8 day expiry, so a 30 day target lands on
        the 8 day one and a 1 day target lands on the 4 day one. Different expiries
        mean different vols, which is what proves the selection happened."""
        near = atm_iv_history(stored, rate=RATE, target_dte=1)
        far = atm_iv_history(stored, rate=RATE, target_dte=30)
        assert near and far
        assert near[0][1] != pytest.approx(far[0][1])

    def test_an_empty_frame_is_an_empty_history(self) -> None:
        assert atm_iv_history(pd.DataFrame(), rate=RATE) == []

    def test_a_frame_missing_columns_is_an_error(self) -> None:
        """A silently empty history would look exactly like a symbol with no data."""
        with pytest.raises(ValueError, match="missing columns"):
            atm_iv_history(pd.DataFrame({"session_date": [1]}), rate=RATE)

    def test_sessions_that_cannot_solve_are_skipped_not_filled(self, stored: pd.DataFrame) -> None:
        """A gap shows up in the completeness ratio. An interpolated value would not."""
        broken = stored.copy()
        broken["bid"] = 0.0
        broken["ask"] = 0.0
        assert atm_iv_history(broken, rate=RATE) == []

    def test_feeds_iv_rank_end_to_end(self, three_sessions: pd.DataFrame) -> None:
        """Three sessions is far below the 20 observation floor, so the rank refuses."""
        from optscan.analytics.ivrank import Confidence, iv_rank_from_series

        history = atm_iv_history(three_sessions, rate=RATE)
        rank = iv_rank_from_series(0.15, history, asof=date(2026, 7, 30))
        assert rank.confidence is Confidence.INSUFFICIENT
        assert rank.rank is None
        assert rank.caveat() is not None
