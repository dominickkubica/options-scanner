"""Whether the recurring jobs are actually running.

The project's central claim is that a missed day is permanent. Until this existed there
was nothing that would tell you a day had been missed: `optscan schedule` reported all
four tasks as "Ready", which is true of a task that has failed every morning for a week,
and `optscan status` listed captures only, so a broken `record` was invisible.

## Two sources, because they answer different questions

**Windows** knows whether it started a process and what that process returned. It does
not know whether any work happened.

**The `job_run` log** knows whether the work happened. It does not know about a job that
died before Python got far enough to write a row, which is exactly what a bad config or
a broken install looks like.

Neither is sufficient. A job that runs daily, finds nothing to do and exits 0 is a green
tick in Task Scheduler and a hole in the history. A job that fails on import never writes
a log row at all, and looks identical to one that was never scheduled. So both are read,
and where they disagree the disagreement is the finding.

## Not crying wolf

A monitor that is wrong on weekends gets ignored on Mondays. So `expected_by` walks the
market calendar for the jobs that only work on a trading day, and a job whose first fire
has not arrived yet reports as waiting rather than as missing.

The counterpart matters as much: when something *is* late, the number of missed sessions
is stated, because "late" is a shrug and "3 sessions missed" is a decision.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from optscan.config import Settings
from optscan.jobs.schedule import ScheduledJob, jobs
from optscan.logging import get_logger
from optscan.market_calendar import is_trading_day
from optscan.storage import db
from optscan.storage import jobs as job_log

log = get_logger("optscan.jobs.health")

#: How far back `expected_by` will walk looking for a trading day. Ten calendar days
#: covers every real market closure including the long holiday stretches.
CALENDAR_LOOKBACK_DAYS = 10

#: Windows reports this when a task has been registered but has never run. It is not a
#: failure, and treating it as one would make every fresh install look broken.
TASK_HAS_NEVER_RUN = 267011

#: Fields in one line of the PowerShell query below.
TASK_FIELDS = 5

#: Windows returns a sentinel date for "never ran", and which one depends on the API:
#: 1899-12-30 in places, and 1999-11-30 from Get-ScheduledTaskInfo, which is what this
#: reads. Verified against a freshly registered task rather than assumed, because an
#: unrecognised sentinel parses fine and reports a task that has never run as one that
#: ran successfully in the last century.
NEVER_RAN_BEFORE_YEAR = 2000


class Verdict(StrEnum):
    """Ordered worst to best. The report sorts on this, so the bad news is at the top."""

    FAILED = "failed"
    MISSING = "missing"
    LATE = "late"
    STALLED = "stalled"
    DISABLED = "disabled"
    WAITING = "waiting"
    OK = "ok"

    @property
    def tone(self) -> str:
        """Which console style says this. Named, so colour is never the only signal."""
        return {
            Verdict.FAILED: "bad",
            Verdict.MISSING: "bad",
            Verdict.LATE: "warn",
            Verdict.STALLED: "warn",
            Verdict.DISABLED: "warn",
            Verdict.WAITING: "dim",
            Verdict.OK: "good",
        }[self]


SEVERITY = {verdict: rank for rank, verdict in enumerate(Verdict)}


@dataclass(frozen=True, slots=True)
class JobHealth:
    job: ScheduledJob
    verdict: Verdict
    detail: str
    registered: bool
    last_success: datetime | None = None
    last_attempt: datetime | None = None
    expected_by: date | None = None
    missed: int = 0

    @property
    def key(self) -> str:
        return self.job.key


def expected_by(job: ScheduledJob, settings: Settings, now: datetime) -> date:
    """The most recent day this job should already have finished.

    Two steps and they must happen in this order. First drop to yesterday if today's
    scheduled time has not arrived, because a capture due at 15:45 is not late at noon.
    Then walk back to a trading day if the job only works on one. Doing it the other way
    round would call a Monday morning late against Monday itself.
    """
    market_now = now.astimezone(ZoneInfo(settings.market_timezone))
    day = market_now.date()
    if market_now.time() < job.at:
        day -= timedelta(days=1)

    if not job.trading_days_only:
        return day

    for _ in range(CALENDAR_LOOKBACK_DAYS):
        if is_trading_day(day, settings.market_calendar):
            return day
        day -= timedelta(days=1)
    return day


def expected_at(job: ScheduledJob, settings: Settings, now: datetime) -> datetime:
    """The moment of the most recent scheduled firing, in market time.

    The date alone is too coarse to decide whether the run log was around to witness a
    run. A job due at 08:00 fires hours before a log that starts at midday on the same
    date, and comparing only the dates calls that a missed run on its first day.
    """
    day = expected_by(job, settings, now)
    return datetime.combine(day, job.at, tzinfo=ZoneInfo(settings.market_timezone))


def sessions_between(start: date, end: date, settings: Settings, *, trading_only: bool) -> int:
    """How many scheduled opportunities fall in (start, end].

    The number that turns "late" into a decision. Counted over the market calendar for
    the session jobs, so a Tuesday after a long weekend does not report four misses.
    """
    if end <= start:
        return 0
    count = 0
    day = start + timedelta(days=1)
    while day <= end:
        if not trading_only or is_trading_day(day, settings.market_calendar):
            count += 1
        day += timedelta(days=1)
    return count


@dataclass(frozen=True, slots=True)
class TaskInfo:
    state: str | None = None
    last_run: datetime | None = None
    last_result: int | None = None
    next_run: datetime | None = None

    @property
    def registered(self) -> bool:
        return self.state is not None


def task_info(task_names: list[str]) -> dict[str, TaskInfo]:
    """What Windows knows about each task. One subprocess, not one per task."""
    if sys.platform != "win32" or not task_names:
        return {}

    quoted = ",".join("'" + name.replace("'", "''") + "'" for name in task_names)
    command = (
        f"Get-ScheduledTask -TaskName @({quoted}) -ErrorAction SilentlyContinue | "
        "ForEach-Object { $i = $_ | Get-ScheduledTaskInfo; "
        "\"$($_.TaskName)|$($_.State)|$($i.LastRunTime.ToString('o'))|"
        "$($i.LastTaskResult)|$($i.NextRunTime.ToString('o'))\" }"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )

    found: dict[str, TaskInfo] = {}
    for line in completed.stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) != TASK_FIELDS or not parts[0]:
            continue
        name, state, last_run, last_result, next_run = parts
        found[name] = TaskInfo(
            state=state or None,
            last_run=_parse_task_time(last_run),
            last_result=_parse_int(last_result),
            next_run=_parse_task_time(next_run),
        )
    return found


def _parse_int(text: str) -> int | None:
    try:
        return int(text)
    except ValueError:
        return None


def _parse_task_time(text: str) -> datetime | None:
    """Windows reports 1899-12-30 for "never". That is not a time, so it becomes None."""
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    if moment.year < NEVER_RAN_BEFORE_YEAR:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _verdict_for(
    job: ScheduledJob,
    info: TaskInfo,
    latest: job_log.JobRun | None,
    success: job_log.JobRun | None,
    settings: Settings,
    now: datetime,
    log_started: datetime | None = None,
) -> JobHealth:
    due = expected_by(job, settings, now)
    # Before the log existed, a missing row is missing evidence rather than a missed
    # run. Everything that would otherwise be reported as a hole is held back until the
    # job has had a scheduled opportunity that this database was around to witness.
    unwitnessed = log_started is not None and expected_at(job, settings, now) < log_started

    def build(verdict: Verdict, detail: str, missed: int = 0) -> JobHealth:
        return JobHealth(
            job=job,
            verdict=verdict,
            detail=detail,
            registered=info.registered,
            last_success=success.started_at if success else None,
            last_attempt=latest.started_at if latest else None,
            expected_by=due,
            missed=missed,
        )

    if not info.registered:
        if not job.default_install:
            return build(Verdict.WAITING, "Not installed, on purpose. See optscan schedule.")
        return build(Verdict.MISSING, "Windows has no such task. Run optscan schedule --install.")

    if info.state and info.state.lower() == "disabled":
        return build(Verdict.DISABLED, "Registered but disabled, so it will not fire.")

    # A run that started and never closed. Checked before failure, because a killed job
    # never got to record one and would otherwise read as though it had never run.
    if latest and latest.stalled(now):
        started = latest.started_at.astimezone().strftime("%Y-%m-%d %H:%M")
        return build(
            Verdict.STALLED,
            f"Started {started} and never finished. It was killed, or the machine went away.",
        )

    if latest and latest.ok is False:
        detail = latest.detail or "no reason recorded"
        return build(Verdict.FAILED, f"Last run failed: {detail}")

    # Windows saw a non-zero exit and we have no record of the work. That is a job dying
    # before it could log anything, which is what a bad config or a broken install does.
    # Only counted for a run the log was around to witness: every run before that logged
    # nothing because there was nowhere to log it.
    if (
        success is None
        and info.last_result not in (None, 0, TASK_HAS_NEVER_RUN)
        and info.last_run is not None
        and (log_started is None or info.last_run >= log_started)
    ):
        when = info.last_run.astimezone().strftime("%Y-%m-%d %H:%M")
        return build(
            Verdict.FAILED,
            f"Windows ran it at {when} and it exited {info.last_result}, and it logged "
            "nothing. It is failing before it starts: try running it by hand.",
        )

    if success is None:
        if info.last_run is None:
            when = info.next_run.astimezone().strftime("%Y-%m-%d %H:%M") if info.next_run else "?"
            return build(Verdict.WAITING, f"Registered, has not fired yet. First run {when}.")
        if unwitnessed:
            started = log_started.date() if log_started else "recently"
            return build(
                Verdict.WAITING,
                f"Run logging only started {started}, so there is nothing to judge yet. "
                "The next scheduled run records one.",
            )
        return build(Verdict.MISSING, "Windows has run it, but no run has ever completed here.")

    last_session = success.started_at.astimezone(ZoneInfo(settings.market_timezone)).date()
    if last_session < due:
        missed = sessions_between(last_session, due, settings, trading_only=job.trading_days_only)
        unit = "session" if job.trading_days_only else "day"
        plural = "" if missed == 1 else "s"
        return build(
            Verdict.LATE,
            f"Last success {last_session}, expected by {due}. "
            f"{missed} {unit}{plural} missed, and they cannot be filled in later.",
            missed=missed,
        )

    when = success.started_at.astimezone().strftime("%Y-%m-%d %H:%M")
    return build(Verdict.OK, f"Last success {when}.")


def check(settings: Settings, *, now: datetime | None = None) -> list[JobHealth]:
    """Health for every job, worst first."""
    moment = now or datetime.now(UTC)
    everything = jobs(settings)
    info = task_info([job.task_name for job in everything])

    results: list[JobHealth] = []
    with db.session(settings.sqlite_path) as conn:
        log_started = job_log.log_started_at(conn)
        for job in everything:
            results.append(
                _verdict_for(
                    job,
                    info.get(job.task_name, TaskInfo()),
                    job_log.latest(conn, job.key),
                    job_log.latest_success(conn, job.key),
                    settings,
                    moment,
                    log_started,
                )
            )

    results.sort(key=lambda item: (SEVERITY[item.verdict], item.job.key))
    return results


def worst(results: list[JobHealth]) -> Verdict:
    """The single verdict for the whole set, for a one line summary elsewhere."""
    if not results:
        return Verdict.OK
    return min(results, key=lambda item: SEVERITY[item.verdict]).verdict


def needs_attention(results: list[JobHealth]) -> list[JobHealth]:
    return [
        item
        for item in results
        if item.verdict in (Verdict.FAILED, Verdict.MISSING, Verdict.LATE, Verdict.STALLED)
    ]
