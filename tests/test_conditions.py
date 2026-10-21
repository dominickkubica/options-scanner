"""The daily condition record, which exists because gamma cannot be backtested.

No vendor this project can reach publishes historical per-strike open interest: the
option tape stores trades, and volume is not open interest. So the walls, the pin, max
pain and the zero-gamma level have no past to be tested against and can only be written
down going forward. These tests cover the writing down.

Offline like the rest of the suite. The fake provider serves the real captured SPY
chain, shifted forward so its expiries are still ahead of today, because every gamma in
the profile is a function of time to expiry.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta

import pytest

from optscan.api.deps import clear_caches
from optscan.config import Settings
from optscan.jobs.conditions import run_record_conditions
from optscan.models import ChainSnapshot
from optscan.storage import db, write_snapshot
from optscan.storage.conditions import DailyCondition, coverage, history, record
from tests.conftest import FakeProvider

SESSION = date(2026, 9, 4)


@pytest.fixture(autouse=True)
def _clear() -> Iterator[None]:
    clear_caches()
    yield
    clear_caches()


def condition(**overrides) -> DailyCondition:
    base = {
        "session_date": SESSION,
        "symbol": "SPY",
        "spot": 700.0,
        "spot_source": "chain underlying",
        "regime": "dampened",
        "zero_gamma": 690.0,
        "call_wall": 720.0,
        "call_wall_oi": 5_000,
        "put_wall": 650.0,
        "max_pain": 695.0,
        "rsi14": 55.0,
        "total_oi": 100_000,
    }
    return DailyCondition(**{**base, **overrides})


class TestTheRecord:
    def test_a_session_is_stored_once(self, tmp_settings: Settings) -> None:
        tmp_settings.ensure_dirs()
        with db.session(tmp_settings.sqlite_path) as conn:
            assert record(conn, condition()) is True
            assert record(conn, condition(spot=999.0)) is False

            held = history(conn)
        assert len(held) == 1
        assert held[0].spot == 700.0

    def test_a_second_run_does_not_overwrite_the_first_reading(
        self, tmp_settings: Settings
    ) -> None:
        """A 15:45 reading and a 20:00 one are different samples of a moving thing.

        Replacing the first with the second would pool two sampling times into one
        series, which is the same pollution the IV history has exactly one writer to
        avoid.
        """
        tmp_settings.ensure_dirs()
        with db.session(tmp_settings.sqlite_path) as conn:
            record(conn, condition(regime="dampened"))
            record(conn, condition(regime="volatile"))

            held = history(conn)
        assert held[0].regime == "dampened"

    def test_different_symbols_and_days_are_different_rows(self, tmp_settings: Settings) -> None:
        tmp_settings.ensure_dirs()
        with db.session(tmp_settings.sqlite_path) as conn:
            record(conn, condition())
            record(conn, condition(symbol="QQQ"))
            record(conn, condition(session_date=SESSION + timedelta(days=1)))

            assert len(history(conn)) == 3
            assert len(history(conn, "SPY")) == 2
            assert len(history(conn, since=SESSION + timedelta(days=1))) == 1

    def test_nulls_survive_the_round_trip(self, tmp_settings: Settings) -> None:
        """A chain with no flip and a symbol with no IV rank are real states, and a
        zero in either column would be a different and false claim."""
        tmp_settings.ensure_dirs()
        with db.session(tmp_settings.sqlite_path) as conn:
            record(conn, condition(zero_gamma=None, iv_rank=None, max_pain=None))
            held = history(conn)[0]

        assert held.zero_gamma is None
        assert held.iv_rank is None
        assert held.max_pain is None

    def test_coverage_says_how_little_there_is(self, tmp_settings: Settings) -> None:
        """The honest answer for the first several months is "not yet", and the
        coverage view is what says so."""
        tmp_settings.ensure_dirs()
        with db.session(tmp_settings.sqlite_path) as conn:
            record(conn, condition())
            record(conn, condition(session_date=SESSION + timedelta(days=1), zero_gamma=None))

            rows = coverage(conn)
        assert len(rows) == 1
        assert rows[0]["sessions"] == 2
        assert rows[0]["with_flip"] == 1


class TestDerivedReadings:
    def test_which_side_of_the_flip_spot_sits_on(self) -> None:
        assert condition(spot=700.0, zero_gamma=690.0).above_zero_gamma is True
        assert condition(spot=680.0, zero_gamma=690.0).above_zero_gamma is False

    def test_no_flip_is_not_a_side(self) -> None:
        """None means the search never crossed inside the band, which is not the same
        as spot being below a level that does not exist."""
        assert condition(zero_gamma=None).above_zero_gamma is None

    def test_distance_is_signed_and_relative(self) -> None:
        row = condition(spot=700.0)

        assert row.distance_to(721.0) == pytest.approx(0.03)
        assert row.distance_to(679.0) == pytest.approx(-0.03)
        assert row.distance_to(None) is None


class TestTheJob:
    def test_it_records_the_watchlist(
        self, tmp_settings: Settings, future_snapshot: ChainSnapshot
    ) -> None:
        tmp_settings.ensure_dirs()
        write_snapshot(future_snapshot, tmp_settings.snapshot_path)
        with db.session(tmp_settings.sqlite_path) as conn:
            db.seed_watchlist(conn, [future_snapshot.symbol])

        provider = FakeProvider(future_snapshot)
        result = run_record_conditions(
            tmp_settings,
            symbols=[future_snapshot.symbol],
            provider_factory=lambda: provider,
        )

        assert result.recorded == 1
        with db.session(tmp_settings.sqlite_path) as conn:
            held = history(conn)
        assert held[0].symbol == future_snapshot.symbol
        assert held[0].spot > 0
        assert held[0].total_oi

    def test_a_second_run_in_one_session_adds_nothing(
        self, tmp_settings: Settings, future_snapshot: ChainSnapshot
    ) -> None:
        tmp_settings.ensure_dirs()
        write_snapshot(future_snapshot, tmp_settings.snapshot_path)
        provider = FakeProvider(future_snapshot)

        run_record_conditions(
            tmp_settings, symbols=[future_snapshot.symbol], provider_factory=lambda: provider
        )
        again = run_record_conditions(
            tmp_settings, symbols=[future_snapshot.symbol], provider_factory=lambda: provider
        )

        assert again.recorded == 0
        assert again.already_held == 1

    def test_a_failed_chain_still_leaves_a_row(
        self, tmp_settings: Settings, future_snapshot: ChainSnapshot
    ) -> None:
        """Half a row is a usable observation; a missing row is a hole in a series that
        nobody can fill in later, which is the whole point of recording forward."""
        tmp_settings.ensure_dirs()
        write_snapshot(future_snapshot, tmp_settings.snapshot_path)
        broken = FakeProvider(future_snapshot, fail_expiries=set(future_snapshot.expiries))

        result = run_record_conditions(
            tmp_settings, symbols=[future_snapshot.symbol], provider_factory=lambda: broken
        )

        assert result.recorded == 1
        with db.session(tmp_settings.sqlite_path) as conn:
            held = history(conn)[0]
        assert held.zero_gamma is None
        assert held.spot > 0

    def test_a_symbol_with_nothing_at_all_is_skipped_with_a_reason(
        self, tmp_settings: Settings, future_snapshot: ChainSnapshot
    ) -> None:
        tmp_settings.ensure_dirs()
        empty = FakeProvider(future_snapshot, fail_symbols={"NOPE"})

        result = run_record_conditions(
            tmp_settings, symbols=["NOPE"], provider_factory=lambda: empty
        )

        assert result.recorded == 0
        assert result.notes
