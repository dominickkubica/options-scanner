"""SQLite storage: migrations, watchlist, and the snapshot run manifest."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from optscan.storage import db

SESSION = date(2026, 7, 30)
CAPTURED = datetime(2026, 7, 30, 19, 45, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "test.sqlite")
    yield connection
    connection.close()


class TestMigrations:
    def test_creates_the_schema_and_sets_the_version(self, conn) -> None:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"watchlist", "snapshot_run"} <= tables

    def test_is_idempotent(self, conn) -> None:
        before = conn.execute("PRAGMA user_version").fetchone()[0]
        assert db.migrate(conn) == before
        assert db.migrate(conn) == before

    def test_creates_the_parent_directory(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "test.sqlite"
        connection = db.connect(nested)
        connection.close()
        assert nested.exists()


class TestWatchlist:
    def test_add_list_remove(self, conn) -> None:
        assert db.add_symbol(conn, "spy") == 1
        assert db.add_symbol(conn, "  qqq  ") == 1
        assert db.list_watchlist(conn) == ["QQQ", "SPY"]
        assert db.remove_symbol(conn, "spy") == 1
        assert db.list_watchlist(conn) == ["QQQ"]

    def test_adding_twice_is_not_an_error(self, conn) -> None:
        assert db.add_symbol(conn, "SPY") == 1
        assert db.add_symbol(conn, "SPY") == 0

    def test_removing_something_absent_is_not_an_error(self, conn) -> None:
        assert db.remove_symbol(conn, "NOPE") == 0

    def test_blank_symbol_is_rejected(self, conn) -> None:
        with pytest.raises(ValueError, match="must not be blank"):
            db.add_symbol(conn, "   ")

    def test_seed_only_applies_to_an_empty_watchlist(self, conn) -> None:
        """Once the user edits the list, config stops asserting itself."""
        assert db.seed_watchlist(conn, ["SPY", "QQQ"]) == 2
        assert db.seed_watchlist(conn, ["AAPL"]) == 0
        assert db.list_watchlist(conn) == ["QQQ", "SPY"]

    def test_seed_does_not_resurrect_a_deleted_symbol(self, conn) -> None:
        db.seed_watchlist(conn, ["SPY", "QQQ"])
        db.remove_symbol(conn, "SPY")
        db.seed_watchlist(conn, ["SPY", "QQQ"])
        assert db.list_watchlist(conn) == ["QQQ"]


class TestRunManifest:
    def test_records_a_success(self, conn, tmp_path: Path) -> None:
        run_id = db.record_run(
            conn,
            symbol="spy",
            session_date=SESSION,
            captured_at=CAPTURED,
            provider="fake",
            expiries=3,
            contracts=800,
            path=tmp_path / "x.parquet",
        )
        assert run_id > 0
        row = db.recent_runs(conn)[0]
        assert row["symbol"] == "SPY"
        assert row["session_date"] == "2026-07-30"
        assert row["contracts"] == 800
        assert row["error"] is None

    def test_records_a_failure_so_gaps_are_explainable(self, conn) -> None:
        db.record_run(
            conn,
            symbol="XYZ",
            session_date=SESSION,
            captured_at=CAPTURED,
            provider="fake",
            expiries=0,
            contracts=0,
            error="SymbolNotFound: nope",
        )
        assert db.recent_runs(conn)[0]["error"].startswith("SymbolNotFound")

    def test_has_run_ignores_failures(self, conn) -> None:
        """A failed attempt must not count as done, or the retry never happens."""
        db.record_run(
            conn,
            symbol="SPY",
            session_date=SESSION,
            captured_at=CAPTURED,
            provider="fake",
            expiries=0,
            contracts=0,
            error="boom",
        )
        assert db.has_run(conn, "SPY", SESSION) is False

        db.record_run(
            conn,
            symbol="SPY",
            session_date=SESSION,
            captured_at=CAPTURED,
            provider="fake",
            expiries=1,
            contracts=10,
        )
        assert db.has_run(conn, "SPY", SESSION) is True
        assert db.has_run(conn, "SPY", date(2026, 7, 29)) is False

    def test_recent_runs_are_newest_first(self, conn) -> None:
        for day in (date(2026, 7, 28), date(2026, 7, 29), date(2026, 7, 30)):
            db.record_run(
                conn,
                symbol="SPY",
                session_date=day,
                captured_at=datetime(day.year, day.month, day.day, 19, 45, tzinfo=UTC),
                provider="fake",
                expiries=1,
                contracts=1,
            )
        assert [row["session_date"] for row in db.recent_runs(conn, limit=2)] == [
            "2026-07-30",
            "2026-07-29",
        ]


def test_session_context_manager_commits(tmp_path: Path) -> None:
    path = tmp_path / "test.sqlite"
    with db.session(path) as conn:
        db.add_symbol(conn, "SPY")
    with db.session(path) as conn:
        assert db.list_watchlist(conn) == ["SPY"]
