"""Windows Task Scheduler registration for the recurring jobs.

Task Scheduler rather than an in process scheduler such as APScheduler. An in process
scheduler needs a process that is always running, and this runs on a machine that
sleeps, updates, and reboots. Task Scheduler survives all three.

The trade is that the schedule now lives outside the repo, in Windows. `optscan status`
and the snapshot_run table are what make that visible again.

## Why this registers through PowerShell rather than schtasks

`schtasks /Create` cannot set the two settings that decide whether a job actually runs,
and its defaults are wrong for both:

- **DisallowStartIfOnBatteries defaults to true.** An unplugged laptop skips the capture
  entirely. This machine is a desktop today, so the flag is inert here, but the failure
  is silent and the history it would cost cannot be rebuilt.
- **StartWhenAvailable defaults to false.** A machine asleep at 15:45 does not run the
  job late, it does not run it at all. The previous version of this module claimed in
  its own docstring that Task Scheduler "will start the job late rather than not at
  all". That was false as registered, and it is what this fixes.

`Register-ScheduledTask` sets both. Registering from hand written XML would too, but the
settings element is order sensitive and a schema mistake fails at install time on a
machine nobody is watching, which is the same class of problem being fixed.

## Running late is not free, and is still better

A deferred capture lands at a different time of day than 15:45, and IV rank compares
today against history sampled at a consistent time. So a late run is a slightly worse
observation. It is still much better than a hole: the capture records its real
`captured_at`, a reader can see it, and the snapshot job refuses outright once the
session is over. A missing session is invisible and permanent.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from optscan.config import REPO_ROOT, Settings
from optscan.console import Console
from optscan.logging import get_logger

log = get_logger("optscan.jobs.schedule")

TASK_NAME = "OptscanDailySnapshot"

#: How long a job may run before Task Scheduler kills it. Generous against a slow
#: vendor, short enough that a wedged process cannot still be holding the database
#: when tomorrow's run starts.
EXECUTION_TIME_LIMIT_HOURS = 1

#: Regular session open in market local time. Only the repeating job needs it, and a
#: half day simply stops producing work early rather than needing its own schedule.
MARKET_OPEN = time(hour=9, minute=30)
SESSION_MINUTES = 390

#: How long after recording the backup runs. Long enough that the day's capture and the
#: day's recorded candidates are both in the copy, which is the point of backing up
#: after the work rather than before it.
BACKUP_DELAY_MINUTES = 15

MINUTES_PER_HOUR = 60
MINUTES_PER_DAY = 24 * MINUTES_PER_HOUR


def shift(at: time, minutes: int) -> time:
    """A time of day moved by some minutes, wrapping at midnight."""
    total = (at.hour * MINUTES_PER_HOUR + at.minute + minutes) % MINUTES_PER_DAY
    return time(hour=total // MINUTES_PER_HOUR, minute=total % MINUTES_PER_HOUR)


@dataclass(slots=True)
class ScheduleResult:
    ok: bool
    message: str


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    """One recurring job, and the reason it runs when it does."""

    key: str
    task_name: str
    arguments: str
    at: time
    summary: str
    #: Minutes between repeats within a single day, for jobs that are not once daily.
    repeat_minutes: int | None = None
    repeat_for_minutes: int | None = None
    #: Whether a plain `--install` registers it. False means the job has a cost that
    #: should be a decision rather than a default, and `caution` says what it is.
    default_install: bool = True
    caution: str | None = None
    #: Whether the job only does real work on a trading day. The task fires every
    #: calendar day either way, because the jobs decline on their own and encoding the
    #: market calendar into Task Scheduler would be a second copy of it. This is for
    #: health: complaining that the capture has not run since Friday, on a Sunday, is
    #: noise, and a monitor that cries wolf gets ignored exactly when it is right.
    trading_days_only: bool = True


def python_executable() -> Path:
    """The interpreter the task should run. Resolves to the venv when running inside it."""
    return Path(sys.executable)


def machine_clock_time(settings: Settings, at: time | None = None, on: date | None = None) -> str:
    """A market local time translated into this machine's clock, as HH:MM.

    Task Scheduler thinks in machine time and these times are defined in market time. On
    a machine in New York these are the same string and this is a no op. On a machine
    anywhere else, skipping the conversion would silently capture at the wrong point in
    the session, which is the one thing an IV history cannot tolerate.

    Fixed to the offsets in effect on the given date, so the two DST changeovers move
    the real time by an hour until the tasks are reinstalled. Only matters when machine
    and market are in different zones.
    """
    market_time = datetime.combine(
        on or date.today(),
        at or settings.snapshot_time,
        tzinfo=ZoneInfo(settings.market_timezone),
    )
    return market_time.astimezone().strftime("%H:%M")


def jobs(settings: Settings) -> tuple[ScheduledJob, ...]:
    """Every job that wants a scheduled task, in the order they depend on each other.

    Not clock order: resolve runs first each morning but depends on recordings made on
    earlier days, so listing it after record is what makes the chain readable.
    """
    return (
        ScheduledJob(
            key="snapshot",
            task_name=TASK_NAME,
            arguments="-m optscan snapshot",
            at=settings.snapshot_time,
            summary=(
                "Capture the watchlist's chains. The IV history cannot be bought after "
                "the fact, so every skipped day is permanent."
            ),
        ),
        ScheduledJob(
            key="record",
            task_name="OptscanDailyRecord",
            arguments="-m optscan record",
            at=settings.record_time,
            summary=(
                "Log every candidate the screen surfaces, for the validation study. "
                "Runs after the capture because it scores the stored snapshot, and a "
                "candidate never logged when it was scored cannot be settled later."
            ),
        ),
        ScheduledJob(
            key="backup",
            task_name="OptscanDailyBackup",
            arguments="-m optscan backup",
            at=shift(settings.record_time, BACKUP_DELAY_MINUTES),
            summary=(
                "Copy the database and mirror the captures. Runs after the capture and "
                "the recording so the day's work is in the copy, not just yesterday's."
            ),
            # Backing up is worth doing on a Sunday. Nothing new was captured, but the
            # database still moved: positions, alerts, and any settling all write to it.
            trading_days_only=False,
        ),
        ScheduledJob(
            key="resolve",
            task_name="OptscanDailyResolve",
            arguments="-m optscan resolve",
            at=settings.resolve_time,
            summary=(
                "Settle logged candidates whose expiry has passed. Runs before the open "
                "so every expiry it can see is finished and its close is published."
            ),
            # Settling is arithmetic over dates that have already passed, so it works on
            # any calendar day and there is no reason to excuse it on a weekend.
            trading_days_only=False,
        ),
        ScheduledJob(
            key="manage",
            task_name="OptscanManagePositions",
            arguments="-m optscan manage",
            at=MARKET_OPEN,
            repeat_minutes=settings.manage_interval_minutes,
            repeat_for_minutes=SESSION_MINUTES,
            default_install=False,
            summary=(
                "Mark open positions, evaluate triggers, deliver new alerts. Safe to "
                "run repeatedly: an alert is delivered once per condition."
            ),
            caution=(
                "Not installed by default. At a "
                f"{settings.manage_interval_minutes} minute interval this hits the "
                "provider about 26 times a session, and yfinance publishes no rate "
                "limit and throttles silently. Being throttled at 15:45 costs a "
                "capture that cannot be rebuilt, so spending the budget on alerts "
                "nobody is watching is the wrong trade until the alerts go somewhere "
                "real. Install it explicitly once they do."
            ),
        ),
    )


def job_by_key(settings: Settings, key: str) -> ScheduledJob | None:
    return next((job for job in jobs(settings) if job.key == key), None)


def default_keys(settings: Settings) -> list[str]:
    return [job.key for job in jobs(settings) if job.default_install]


def _quote(value: str) -> str:
    """Single quote a PowerShell literal, doubling any quote inside it."""
    return "'" + value.replace("'", "''") + "'"


def register_script(settings: Settings, job: ScheduledJob) -> str:
    """The PowerShell that registers one job.

    Returned as text rather than run, so `optscan schedule` can print exactly what
    `--install` would do. Creating a scheduled task changes the machine, and that
    should be a decision, not a side effect of running a help command.
    """
    at = machine_clock_time(settings, job.at)
    action = (
        f"$action = New-ScheduledTaskAction -Execute {_quote(str(python_executable()))} "
        f"-Argument {_quote(job.arguments)} -WorkingDirectory {_quote(str(REPO_ROOT))}"
    )

    lines = [action, f"$trigger = New-ScheduledTaskTrigger -Daily -At {at}"]

    if job.repeat_minutes:
        # A daily trigger carries the repetition; the -Once trigger here is built only
        # to lift a correctly typed Repetition off it, which is the documented way to
        # get one onto a daily trigger from PowerShell.
        lines.append(
            f"$pattern = New-ScheduledTaskTrigger -Once -At {at} "
            f"-RepetitionInterval (New-TimeSpan -Minutes {job.repeat_minutes}) "
            f"-RepetitionDuration (New-TimeSpan -Minutes {job.repeat_for_minutes})"
        )
        lines.append("$trigger.Repetition = $pattern.Repetition")

    # The two settings this whole module exists to get right, plus a time limit so a
    # wedged run cannot still hold the database when the next one starts.
    lines.append(
        "$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable "
        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-MultipleInstances IgnoreNew "
        f"-ExecutionTimeLimit (New-TimeSpan -Hours {EXECUTION_TIME_LIMIT_HOURS})"
    )
    lines.append(
        f"Register-ScheduledTask -TaskName {_quote(job.task_name)} -Action $action "
        f"-Trigger $trigger -Settings $settings -Description {_quote(job.summary)} "
        "-Force | Out-Null"
    )
    return "; ".join(lines)


def install_task(settings: Settings, job: ScheduledJob) -> ScheduleResult:
    """Register one job with Windows. Overwrites an existing task of the same name.

    A daily job fires every calendar day. The jobs themselves refuse to act outside a
    trading session, so a weekend run is a no op that logs why, which is cheaper and
    more honest than encoding the market calendar into Task Scheduler.
    """
    if sys.platform != "win32":
        return ScheduleResult(
            ok=False,
            message=(
                f"Scheduled tasks are Windows only, this is {sys.platform}. Use cron "
                f'with: "{python_executable()}" {job.arguments}'
            ),
        )

    at = machine_clock_time(settings, job.at)
    log.info(
        "registering scheduled task",
        task=job.task_name,
        machine_time=at,
        market_time=job.at.strftime("%H:%M"),
        market_timezone=settings.market_timezone,
    )
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            register_script(settings, job),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout).strip()
        log.error("failed to register scheduled task", task=job.task_name, error=message)
        return ScheduleResult(ok=False, message=f"Could not create {job.task_name}: {message}")

    cadence = (
        f"every {job.repeat_minutes} minutes for {job.repeat_for_minutes // 60}h"
        f"{job.repeat_for_minutes % 60:02d}m from"
        if job.repeat_minutes
        else "daily at"
    )
    return ScheduleResult(
        ok=True,
        message=(
            f"Registered {job.task_name}: {cadence} {at} machine time, which is "
            f"{job.at.strftime('%H:%M')} in {settings.market_timezone}."
        ),
    )


def install_all(settings: Settings, keys: list[str] | None = None) -> list[ScheduleResult]:
    """Register the named jobs, or every job that installs by default."""
    wanted = keys if keys is not None else default_keys(settings)
    results: list[ScheduleResult] = []
    for key in wanted:
        job = job_by_key(settings, key)
        if job is None:
            known = ", ".join(item.key for item in jobs(settings))
            results.append(
                ScheduleResult(ok=False, message=f"No job named {key!r}. Known: {known}")
            )
            continue
        results.append(install_task(settings, job))
    return results


def task_states(task_names: list[str]) -> dict[str, str]:
    """What Windows currently thinks of each task. Missing keys are not registered.

    Read back rather than assumed. A task that exists is not the same as a task that is
    enabled, and the difference is invisible until a day of history is missing.

    One subprocess for every task rather than one each: this runs on the plain
    `optscan schedule` path, and five PowerShell starts to print five words is most of
    the command's runtime.
    """
    if sys.platform != "win32" or not task_names:
        return {}
    names = ",".join(_quote(name) for name in task_names)
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"Get-ScheduledTask -TaskName @({names}) -ErrorAction SilentlyContinue | "
            'ForEach-Object { "$($_.TaskName)=$($_.State)" }',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    states: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        name, _, state = line.strip().partition("=")
        if name and state:
            states[name] = state
    return states


def describe(settings: Settings, console: Console | None = None) -> list[str]:
    """One block per job, for `optscan schedule` with no --install."""
    paint = console or Console()
    everything = jobs(settings)
    states = task_states([job.task_name for job in everything])

    lines: list[str] = []
    for job in everything:
        at = machine_clock_time(settings, job.at)
        when = (
            f"every {job.repeat_minutes} min from {job.at.strftime('%H:%M')}"
            if job.repeat_minutes
            else f"daily {job.at.strftime('%H:%M')}"
        )
        raw = states.get(job.task_name)
        if raw == "Ready":
            state = paint.good(raw)
        elif raw is None:
            # Not an error for a job that is deliberately not installed, and the caution
            # line underneath already explains that one.
            state = (
                paint.dim("not registered")
                if not job.default_install
                else paint.bad("not registered")
            )
        else:
            state = paint.warn(raw)
        lines.append(f"  {job.key:<9} {when:<24} {at} local   {state}")
        lines.append(f"            {paint.dim(job.summary)}")
        if job.caution:
            lines.append(f"            {paint.dim(job.caution)}")
    return lines
