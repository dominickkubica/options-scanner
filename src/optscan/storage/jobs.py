"""The job run log: what actually ran, and how it ended.

Every scheduled job writes here, whether Task Scheduler started it or a person did. The
distinction does not matter for the question being asked, which is whether the work has
happened recently: a hole filled by hand is filled.

## Recording must never be able to break the job

This is a bookkeeping layer wrapped around the jobs that build history which cannot be
rebuilt. A failure to write a log row is worth a warning and nothing more. Every entry
point here swallows its own errors, and `track` in particular is written so that the
wrapped job runs even if the database cannot be opened at all.

An observability layer that can take down the thing it observes is worse than no
observability layer, because it converts a survivable outage into a permanent one.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from optscan.logging import get_logger
from optscan.storage import db

log = get_logger("optscan.storage.jobs")

#: Longest a run may sit unfinished before it is called stalled rather than running.
#: Matches the scheduled task execution time limit, so anything Windows would have
#: killed is past this too.
STALL_AFTER_HOURS = 1


@dataclass(frozen=True, slots=True)
class JobRun:
    job: str
    started_at: datetime
    finished_at: datetime | None
    ok: bool | None
    detail: str | None

    @property
    def unfinished(self) -> bool:
        return self.finished_at is None

    def stalled(self, now: datetime) -> bool:
        """Started, never closed, and long enough ago that it is not still going."""
        if self.finished_at is not None:
            return False
        return (now - self.started_at).total_seconds() > STALL_AFTER_HOURS * 3600


def _row_to_run(row: sqlite3.Row) -> JobRun:
    return JobRun(
        job=row["job"],
        started_at=datetime.fromisoformat(row["started_at"]),
        finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
        ok=None if row["ok"] is None else bool(row["ok"]),
        detail=row["detail"],
    )


def start(conn: sqlite3.Connection, job: str, *, now: datetime | None = None) -> int:
    """Open a run. Returns its id."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    cursor = conn.execute(
        "INSERT INTO job_run (job, started_at) VALUES (?, ?)",
        (job, moment.isoformat()),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def finish(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    ok: bool,
    detail: str | None = None,
    now: datetime | None = None,
) -> None:
    """Close a run."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    conn.execute(
        "UPDATE job_run SET finished_at = ?, ok = ?, detail = ? WHERE id = ?",
        (moment.isoformat(), int(ok), detail, run_id),
    )
    conn.commit()


def latest(conn: sqlite3.Connection, job: str) -> JobRun | None:
    """The most recent run of one job, finished or not."""
    row = conn.execute(
        "SELECT * FROM job_run WHERE job = ? ORDER BY started_at DESC, id DESC LIMIT 1",
        (job,),
    ).fetchone()
    return _row_to_run(row) if row else None


def latest_success(conn: sqlite3.Connection, job: str) -> JobRun | None:
    """The most recent run of one job that finished successfully.

    Separate from `latest` because they answer different questions. The last run says
    what happened; the last *success* says how long the hole is.
    """
    row = conn.execute(
        "SELECT * FROM job_run WHERE job = ? AND ok = 1 ORDER BY started_at DESC, id DESC LIMIT 1",
        (job,),
    ).fetchone()
    return _row_to_run(row) if row else None


def log_started_at(conn: sqlite3.Connection) -> datetime | None:
    """When this database began recording job runs.

    The floor under every "it has not run" claim. Before this moment the log simply did
    not exist, so the absence of a row is not evidence that the job did not run, and
    reporting it as such would be a guess dressed as a finding.
    """
    row = conn.execute("SELECT value FROM meta WHERE key = 'job_log_started_at'").fetchone()
    if not row:
        return None
    try:
        moment = datetime.fromisoformat(row["value"])
    except (ValueError, TypeError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def recent(conn: sqlite3.Connection, job: str | None = None, limit: int = 20) -> list[JobRun]:
    """Recent runs, newest first, optionally for one job."""
    if job:
        rows = conn.execute(
            "SELECT * FROM job_run WHERE job = ? ORDER BY started_at DESC, id DESC LIMIT ?",
            (job, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM job_run ORDER BY started_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_run(row) for row in rows]


@dataclass(slots=True)
class Tracker:
    """Handle for the block inside `track`, so a job can fail without raising.

    A command that returns a non-zero exit code has failed just as surely as one that
    raised, and it is the more common shape. Without this it would close its run as a
    success, which is the precise thing this whole table exists to stop.
    """

    ok: bool = True
    detail: str | None = None

    def failed(self, detail: str) -> None:
        self.ok = False
        self.detail = detail


@contextmanager
def track(sqlite_path: Path, job: str) -> Iterator[Tracker]:
    """Record one run of `job` around the block that performs it.

    Swallows its own failures on purpose. If the log cannot be written the job still
    runs, because the job is the thing that matters and this is the paperwork.

    An exception from the block is recorded as a failure and then re-raised: the caller
    still fails, and the reason is in the log rather than only on a console nobody was
    watching at 15:45.
    """
    run_id: int | None = None
    conn: sqlite3.Connection | None = None
    try:
        conn = db.connect(sqlite_path)
        run_id = start(conn, job)
    except (sqlite3.Error, OSError) as error:
        log.warning("could not open a job run record", job=job, error=str(error))
        if conn is not None:
            conn.close()
            conn = None

    tracker = Tracker()
    try:
        yield tracker
    except BaseException as error:
        _close(conn, run_id, job, ok=False, detail=f"{type(error).__name__}: {error}")
        raise
    else:
        _close(conn, run_id, job, ok=tracker.ok, detail=tracker.detail)


def _close(
    conn: sqlite3.Connection | None,
    run_id: int | None,
    job: str,
    *,
    ok: bool,
    detail: str | None,
) -> None:
    if conn is None or run_id is None:
        return
    try:
        finish(conn, run_id, ok=ok, detail=detail)
    except (sqlite3.Error, OSError) as error:
        log.warning("could not close a job run record", job=job, error=str(error))
    finally:
        conn.close()
