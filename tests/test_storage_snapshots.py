"""Parquet snapshot storage, exercised against the frozen real chain.

This is the Phase 1 exit criterion in test form: write a capture to disk, read it
back into a DataFrame, and get the same numbers out.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from optscan.models import ChainSnapshot
from optscan.storage import snapshots


def test_fixture_is_a_real_chain(frozen_snapshot: ChainSnapshot) -> None:
    """Guards against the fixture being quietly replaced by something trivial."""
    assert frozen_snapshot.symbol == "SPY"
    assert len(frozen_snapshot.chains) == 2
    assert frozen_snapshot.contract_count > 100
    assert frozen_snapshot.quote.price is not None


class TestWrite:
    def test_path_layout_is_partitioned(
        self, frozen_snapshot: ChainSnapshot, tmp_path: Path
    ) -> None:
        path = snapshots.write_snapshot(frozen_snapshot, tmp_path)
        assert path.parent.name == f"session_date={frozen_snapshot.session_date.isoformat()}"
        assert path.parent.parent.name == "symbol=SPY"
        assert path.suffix == ".parquet"
        assert path.stat().st_size > 0

    def test_filename_is_the_capture_time(self, frozen_snapshot: ChainSnapshot) -> None:
        captured = datetime(2026, 7, 30, 19, 45, 3, tzinfo=UTC)
        assert snapshots.snapshot_filename(captured) == "20260730T194503Z.parquet"

    def test_two_captures_in_one_day_do_not_collide(
        self, frozen_snapshot: ChainSnapshot, tmp_path: Path
    ) -> None:
        first = snapshots.write_snapshot(frozen_snapshot, tmp_path)
        later = frozen_snapshot.model_copy(
            update={"fetched_at": frozen_snapshot.fetched_at.replace(hour=20, minute=1, second=2)}
        )
        second = snapshots.write_snapshot(later, tmp_path)
        assert first != second
        assert len(snapshots.snapshot_files(tmp_path)) == 2

    def test_two_captures_in_the_same_second_do_not_overwrite(
        self, frozen_snapshot: ChainSnapshot, tmp_path: Path
    ) -> None:
        """Filenames have one second resolution, so the collision path must be real."""
        first = snapshots.write_snapshot(frozen_snapshot, tmp_path)
        second = snapshots.write_snapshot(frozen_snapshot, tmp_path)
        assert first != second
        assert second.stem.endswith("-1")
        assert len(snapshots.snapshot_files(tmp_path)) == 2

    def test_refuses_to_write_an_empty_snapshot(
        self, frozen_snapshot: ChainSnapshot, tmp_path: Path
    ) -> None:
        """An empty file in the history is indistinguishable from a real zero."""
        empty = frozen_snapshot.model_copy(update={"chains": ()})
        with pytest.raises(ValueError, match="refusing to write an empty snapshot"):
            snapshots.write_snapshot(empty, tmp_path)


class TestRoundTrip:
    def test_reload_matches_the_source(
        self, frozen_snapshot: ChainSnapshot, tmp_path: Path
    ) -> None:
        snapshots.write_snapshot(frozen_snapshot, tmp_path)
        frame = snapshots.read_snapshots(tmp_path)

        assert len(frame) == frozen_snapshot.contract_count
        assert set(frame["symbol"]) == {"SPY"}
        assert set(frame["right"]) <= {"C", "P"}
        assert sorted(set(frame["expiry"].dt.date)) == list(frozen_snapshot.expiries)

        source = next(iter(frozen_snapshot.contracts()))
        row = frame[
            (frame["strike"] == source.strike)
            & (frame["right"] == str(source.right))
            & (frame["expiry"].dt.date == source.expiry)
        ].iloc[0]
        assert row["contract_symbol"] == source.contract_symbol
        assert row["open_interest"] == source.open_interest
        if source.bid is not None:
            assert row["bid"] == pytest.approx(source.bid)

    def test_provenance_survives_the_round_trip(
        self, frozen_snapshot: ChainSnapshot, tmp_path: Path
    ) -> None:
        """A stored row with no source or capture time is unusable years later."""
        snapshots.write_snapshot(frozen_snapshot, tmp_path)
        frame = snapshots.read_snapshots(tmp_path)
        assert set(frame["source"]) == {frozen_snapshot.source}
        assert frame["captured_at"].notna().all()
        assert set(frame["session_date"].dt.date) == {frozen_snapshot.session_date}

    def test_timestamps_come_back_in_utc_not_machine_time(
        self, frozen_snapshot: ChainSnapshot, tmp_path: Path
    ) -> None:
        """Otherwise the same file reads as a different clock time on another machine."""
        snapshots.write_snapshot(frozen_snapshot, tmp_path)
        frame = snapshots.read_snapshots(tmp_path)
        assert str(frame["captured_at"].dt.tz) == "UTC"
        assert frame["captured_at"].iloc[0].to_pydatetime() == frozen_snapshot.fetched_at.replace(
            microsecond=frozen_snapshot.fetched_at.microsecond
        )

    def test_filters_by_symbol_and_date(
        self, frozen_snapshot: ChainSnapshot, tmp_path: Path
    ) -> None:
        snapshots.write_snapshot(frozen_snapshot, tmp_path)
        other = frozen_snapshot.model_copy(
            update={
                "symbol": "QQQ",
                "quote": frozen_snapshot.quote.model_copy(update={"symbol": "QQQ"}),
                "chains": tuple(
                    chain.model_copy(
                        update={
                            "symbol": "QQQ",
                            "contracts": tuple(
                                c.model_copy(update={"symbol": "QQQ"}) for c in chain.contracts
                            ),
                        }
                    )
                    for chain in frozen_snapshot.chains
                ),
            }
        )
        snapshots.write_snapshot(other, tmp_path)

        assert set(snapshots.read_snapshots(tmp_path)["symbol"]) == {"SPY", "QQQ"}
        assert set(snapshots.read_snapshots(tmp_path, symbol="SPY")["symbol"]) == {"SPY"}
        assert snapshots.read_snapshots(tmp_path, start=date(2099, 1, 1)).empty

    def test_empty_root_returns_an_empty_frame_with_columns(self, tmp_path: Path) -> None:
        """The first run of a fresh install must not be a special case for callers."""
        frame = snapshots.read_snapshots(tmp_path / "nothing-here")
        assert frame.empty
        assert "vendor_iv" in frame.columns
        assert "session_date" in frame.columns


def test_none_stays_none_and_zero_stays_zero(
    frozen_snapshot: ChainSnapshot, tmp_path: Path
) -> None:
    """The distinction the models protect must survive parquet as well."""
    snapshots.write_snapshot(frozen_snapshot, tmp_path)
    frame = snapshots.read_snapshots(tmp_path)

    zero_bids = frame[frame["bid"] == 0.0]
    assert len(zero_bids) > 0, "a real SPY chain always has some zero bid contracts"
    assert not zero_bids["bid"].isna().any()
