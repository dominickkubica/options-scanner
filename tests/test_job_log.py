"""The job run log.

Two properties matter more than the rest and both are about failure: a run that dies
must leave a row that says so, and this layer must never be able to take down the job
it is recording.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from optscan.storage import db
from optscan.storage import jobs as job_log


@pytest.fixture
def conn(tmp_path: Path):
    with db.session(tmp_path / "test.sqlite") as connection:
        yield connection


NOW = datetime(2026, 8, 3, 20, 45, tzinfo=UTC)


class TestStartAndFinish:
    def test_a_started_run_is_open(self, conn) -> None:
        job_log.start(conn, "snapshot", now=NOW)

        run = job_log.latest(conn, "snapshot")
        assert run is not None
        assert run.unfinished is True
        assert run.ok is None

    def test_finishing_closes_it(self, conn) -> None:
        run_id = job_log.start(conn, "snapshot", now=NOW)
        job_log.finish(conn, run_id, ok=True, now=NOW + timedelta(seconds=30))

        run = job_log.latest(conn, "snapshot")
        assert run is not None
        assert run.unfinished is False
        assert run.ok is True
        assert run.finished_at == NOW + timedelta(seconds=30)

    def test_a_failure_keeps_its_reason(self, conn) -> None:
        run_id = job_log.start(conn, "record", now=NOW)
        job_log.finish(conn, run_id, ok=False, detail="no snapshot to score", now=NOW)

        run = job_log.latest(conn, "record")
        assert run is not None
        assert run.ok is False
        assert run.detail == "no snapshot to score"

    def test_jobs_do_not_see_each_other(self, conn) -> None:
        job_log.start(conn, "snapshot", now=NOW)
        job_log.start(conn, "record", now=NOW)

        assert job_log.latest(conn, "snapshot") is not None
        assert job_log.latest(conn, "backup") is None

    def test_nothing_logged_is_none(self, conn) -> None:
        assert job_log.latest(conn, "snapshot") is None
        assert job_log.latest_success(conn, "snapshot") is None


class TestLatestSuccess:
    def test_a_later_failure_does_not_erase_the_last_success(self, conn) -> None:
        """The two answer different questions: what happened, and how long the hole is."""
        first = job_log.start(conn, "snapshot", now=NOW)
        job_log.finish(conn, first, ok=True, now=NOW)
        second = job_log.start(conn, "snapshot", now=NOW + timedelta(days=1))
        job_log.finish(conn, second, ok=False, detail="vendor down", now=NOW + timedelta(days=1))

        assert job_log.latest(conn, "snapshot").ok is False
        assert job_log.latest_success(conn, "snapshot").started_at == NOW

    def test_an_unfinished_run_is_not_a_success(self, conn) -> None:
        job_log.start(conn, "snapshot", now=NOW)
        assert job_log.latest_success(conn, "snapshot") is None


class TestStalled:
    def test_a_finished_run_is_never_stalled(self, conn) -> None:
        run_id = job_log.start(conn, "snapshot", now=NOW)
        job_log.finish(conn, run_id, ok=True, now=NOW)

        assert job_log.latest(conn, "snapshot").stalled(NOW + timedelta(days=2)) is False

    def test_an_open_run_is_not_stalled_immediately(self, conn) -> None:
        """It might simply still be going."""
        job_log.start(conn, "snapshot", now=NOW)
        assert job_log.latest(conn, "snapshot").stalled(NOW + timedelta(minutes=5)) is False

    def test_an_open_run_past_the_time_limit_is_stalled(self, conn) -> None:
        """Windows would have killed it by now, so nothing is still running."""
        job_log.start(conn, "snapshot", now=NOW)
        assert job_log.latest(conn, "snapshot").stalled(NOW + timedelta(hours=3)) is True


class TestRecent:
    def test_newest_first(self, conn) -> None:
        job_log.start(conn, "snapshot", now=NOW)
        job_log.start(conn, "record", now=NOW + timedelta(minutes=30))

        assert [run.job for run in job_log.recent(conn)] == ["record", "snapshot"]

    def test_filtered_by_job(self, conn) -> None:
        job_log.start(conn, "snapshot", now=NOW)
        job_log.start(conn, "record", now=NOW)

        assert [run.job for run in job_log.recent(conn, "record")] == ["record"]

    def test_limited(self, conn) -> None:
        for index in range(5):
            job_log.start(conn, "snapshot", now=NOW + timedelta(minutes=index))
        assert len(job_log.recent(conn, limit=2)) == 2


class TestLogStartedAt:
    def test_the_migration_records_when_logging_began(self, conn) -> None:
        """The floor under every "it has not run" claim."""
        started = job_log.log_started_at(conn)
        assert started is not None
        assert started.tzinfo is not None

    def test_missing_meta_is_none_rather_than_a_crash(self, conn) -> None:
        conn.execute("DELETE FROM meta")
        assert job_log.log_started_at(conn) is None

    def test_an_unparseable_value_is_none(self, conn) -> None:
        conn.execute("UPDATE meta SET value = 'not a date' WHERE key = 'job_log_started_at'")
        assert job_log.log_started_at(conn) is None


class TestTrack:
    def test_a_clean_block_records_a_success(self, tmp_path: Path) -> None:
        path = tmp_path / "test.sqlite"
        with job_log.track(path, "backup"):
            pass

        with db.session(path) as conn:
            run = job_log.latest(conn, "backup")
        assert run is not None
        assert run.ok is True
        assert run.unfinished is False

    def test_an_exception_is_recorded_and_re_raised(self, tmp_path: Path) -> None:
        """The caller still fails. The reason is in the log rather than only on a
        console nobody was watching at 15:45."""
        path = tmp_path / "test.sqlite"

        with pytest.raises(RuntimeError, match="vendor exploded"):
            with job_log.track(path, "snapshot"):
                raise RuntimeError("vendor exploded")

        with db.session(path) as conn:
            run = job_log.latest(conn, "snapshot")
        assert run is not None
        assert run.ok is False
        assert "vendor exploded" in (run.detail or "")

    def test_a_keyboard_interrupt_is_recorded_too(self, tmp_path: Path) -> None:
        """BaseException, not Exception: a job cancelled halfway still left a hole."""
        path = tmp_path / "test.sqlite"

        with pytest.raises(KeyboardInterrupt):
            with job_log.track(path, "record"):
                raise KeyboardInterrupt

        with db.session(path) as conn:
            assert job_log.latest(conn, "record").ok is False

    def test_the_block_can_mark_itself_failed_without_raising(self, tmp_path: Path) -> None:
        """A non-zero exit code is a failure too, and it is the more common shape."""
        path = tmp_path / "test.sqlite"

        with job_log.track(path, "resolve") as run:
            run.failed("the command exited 1")

        with db.session(path) as conn:
            recorded = job_log.latest(conn, "resolve")
        assert recorded.ok is False
        assert recorded.detail == "the command exited 1"

    def test_an_unusable_database_does_not_stop_the_job(self, tmp_path: Path) -> None:
        """The whole reason this layer swallows its own errors. Bookkeeping must never
        convert a survivable outage into a permanent one."""
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("this is a file")

        ran = False
        with job_log.track(blocker / "nested" / "test.sqlite", "snapshot"):
            ran = True

        assert ran is True

    def test_an_unusable_database_still_lets_the_exception_through(self, tmp_path: Path) -> None:
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("this is a file")

        with pytest.raises(RuntimeError):
            with job_log.track(blocker / "nested" / "test.sqlite", "snapshot"):
                raise RuntimeError("still fails")

    def test_a_failure_to_close_does_not_stop_the_job(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "test.sqlite"

        def explode(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(job_log, "finish", explode)

        ran = False
        with job_log.track(path, "backup"):
            ran = True

        assert ran is True
