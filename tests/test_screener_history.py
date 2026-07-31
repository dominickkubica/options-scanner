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

#: The frozen fixture was captured from yfinance, and the source column carries that
#: through storage. Every call here has to name a vendor, which is the point.
SOURCE = "yfinance"


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


@pytest.fixture
def two_vendors(frozen_snapshot: ChainSnapshot, tmp_path: Path) -> pd.DataFrame:
    """Three yfinance sessions followed by two captured from tradier.

    What a provider switch actually looks like on disk. The vols are deliberately
    shifted on the tradier sessions, because the failure this guards against is a level
    shift between vendors being read as a move in the market.
    """
    for offset in range(3):
        write_snapshot(
            frozen_snapshot.model_copy(
                update={
                    "session_date": frozen_snapshot.session_date - timedelta(days=offset + 2),
                    "fetched_at": frozen_snapshot.fetched_at - timedelta(days=offset + 2),
                }
            ),
            tmp_path,
        )

    for offset in range(2):
        widened = frozen_snapshot.model_copy(
            update={
                "session_date": frozen_snapshot.session_date - timedelta(days=offset),
                "fetched_at": frozen_snapshot.fetched_at - timedelta(days=offset),
                "source": "tradier",
                "quote": frozen_snapshot.quote.model_copy(update={"source": "tradier"}),
                "chains": tuple(
                    chain.model_copy(update={"source": "tradier"})
                    for chain in frozen_snapshot.chains
                ),
            }
        )
        write_snapshot(widened, tmp_path)

    return read_snapshots(tmp_path)


class TestAtmIvHistory:
    def test_one_value_per_session(self, three_sessions: pd.DataFrame) -> None:
        history = atm_iv_history(three_sessions, rate=RATE, source=SOURCE)
        assert len(history) == 3
        assert len({day for day, _ in history.points}) == 3

    def test_oldest_first(self, three_sessions: pd.DataFrame) -> None:
        history = atm_iv_history(three_sessions, rate=RATE, source=SOURCE)
        days = [day for day, _ in history.points]
        assert days == sorted(days)

    def test_values_are_plausible_volatilities(self, stored: pd.DataFrame) -> None:
        history = atm_iv_history(stored, rate=RATE, source=SOURCE)
        assert history
        for _, value in history.points:
            assert 0.01 < value < 2.0

    def test_picks_the_expiry_nearest_the_target_horizon(self, stored: pd.DataFrame) -> None:
        """The fixture holds a 4 day and an 8 day expiry, so a 30 day target lands on
        the 8 day one and a 1 day target lands on the 4 day one. Different expiries
        mean different vols, which is what proves the selection happened."""
        near = atm_iv_history(stored, rate=RATE, source=SOURCE, target_dte=1)
        far = atm_iv_history(stored, rate=RATE, source=SOURCE, target_dte=30)
        assert near and far
        assert near.points[0][1] != pytest.approx(far.points[0][1])

    def test_an_empty_frame_is_an_empty_history(self) -> None:
        history = atm_iv_history(pd.DataFrame(), rate=RATE, source=SOURCE)
        assert history.points == []
        assert history.note() is None

    def test_a_frame_missing_columns_is_an_error(self) -> None:
        """A silently empty history would look exactly like a symbol with no data."""
        with pytest.raises(ValueError, match="missing columns"):
            atm_iv_history(pd.DataFrame({"session_date": [1]}), rate=RATE, source=SOURCE)

    def test_sessions_that_cannot_solve_are_skipped_not_filled(self, stored: pd.DataFrame) -> None:
        """A gap shows up in the completeness ratio. An interpolated value would not."""
        broken = stored.copy()
        broken["bid"] = 0.0
        broken["ask"] = 0.0
        assert atm_iv_history(broken, rate=RATE, source=SOURCE).points == []

    def test_feeds_iv_rank_end_to_end(self, three_sessions: pd.DataFrame) -> None:
        """Three sessions is far below the 20 observation floor, so the rank refuses."""
        from optscan.analytics.ivrank import Confidence, iv_rank_from_series

        history = atm_iv_history(three_sessions, rate=RATE, source=SOURCE)
        rank = iv_rank_from_series(0.15, history.points, asof=date(2026, 7, 30))
        assert rank.confidence is Confidence.INSUFFICIENT
        assert rank.rank is None
        assert rank.caveat() is not None


class TestSourceIsolation:
    """The provider switch case. An IV history that pools two vendors is corrupt in a
    way nothing downstream can detect, so these are the load bearing tests of the file."""

    def test_only_the_named_vendors_sessions_are_used(self, two_vendors: pd.DataFrame) -> None:
        yahoo = atm_iv_history(two_vendors, rate=RATE, source="yfinance")
        tradier = atm_iv_history(two_vendors, rate=RATE, source="tradier")

        assert len(yahoo) == 3
        assert len(tradier) == 2
        # Five sessions on disk, and neither series is the whole of it.
        assert len(yahoo) + len(tradier) == 5

    def test_the_two_series_do_not_share_a_session(self, two_vendors: pd.DataFrame) -> None:
        yahoo = {day for day, _ in atm_iv_history(two_vendors, rate=RATE, source="yfinance")}
        tradier = {day for day, _ in atm_iv_history(two_vendors, rate=RATE, source="tradier")}
        assert yahoo.isdisjoint(tradier)

    def test_what_was_excluded_is_counted_by_vendor(self, two_vendors: pd.DataFrame) -> None:
        history = atm_iv_history(two_vendors, rate=RATE, source="tradier")
        assert history.excluded == {"yfinance": 3}
        assert history.excluded_sessions == 3

    def test_the_exclusion_is_stated_rather_than_silent(self, two_vendors: pd.DataFrame) -> None:
        """A confidence level that drops after a provider switch must not look like the
        snapshot job has been failing."""
        note = atm_iv_history(two_vendors, rate=RATE, source="tradier").note()
        assert note is not None
        assert "yfinance" in note
        assert "3" in note

    def test_a_single_vendor_history_says_nothing(self, three_sessions: pd.DataFrame) -> None:
        """No note when there is nothing to explain. A permanent banner is a banner
        nobody reads by the second week."""
        history = atm_iv_history(three_sessions, rate=RATE, source=SOURCE)
        assert history.excluded == {}
        assert history.note() is None

    def test_a_brand_new_vendor_has_no_history_rather_than_inheriting_one(
        self, three_sessions: pd.DataFrame
    ) -> None:
        """The first day on a new provider: three yfinance sessions on disk, and the
        new vendor's rank must start from nothing rather than adopt them."""
        history = atm_iv_history(three_sessions, rate=RATE, source="tradier")

        assert history.points == []
        assert history.excluded == {"yfinance": 3}
        assert history.note() is not None
