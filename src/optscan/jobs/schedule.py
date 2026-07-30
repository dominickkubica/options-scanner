"""Windows Task Scheduler registration for the daily snapshot.

Task Scheduler rather than an in process scheduler such as APScheduler. An in process
scheduler needs a process that is always running, and this runs on a laptop that
sleeps, updates, and reboots. Task Scheduler survives all three and will start the job
late rather than not at all.

The trade is that the schedule now lives outside the repo, in Windows. `optscan status`
and the snapshot_run table are what make that visible again.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from optscan.config import Settings
from optscan.logging import get_logger

log = get_logger("optscan.jobs.schedule")

TASK_NAME = "OptscanDailySnapshot"


@dataclass(slots=True)
class ScheduleResult:
    ok: bool
    message: str


def python_executable() -> Path:
    """The interpreter the task should run. Resolves to the venv when running inside it."""
    return Path(sys.executable)


def machine_clock_time(settings: Settings, on: date | None = None) -> str:
    """snapshot_time_local translated into this machine's local clock, as HH:MM.

    Task Scheduler thinks in machine time and the capture time is defined in market
    time. On a machine in New York these are the same string and this is a no op. On a
    machine anywhere else, skipping the conversion would silently capture at the wrong
    point in the session, which is the one thing an IV history cannot tolerate.

    Fixed to the offsets in effect on the given date, so the two DST changeovers move
    the real capture time by an hour until the task is reinstalled. Only matters when
    machine and market are in different zones.
    """
    market_time = datetime.combine(
        on or date.today(),
        settings.snapshot_time,
        tzinfo=ZoneInfo(settings.market_timezone),
    )
    return market_time.astimezone().strftime("%H:%M")


def task_command(settings: Settings, task_name: str = TASK_NAME) -> str:
    """The schtasks command line that registers the daily job.

    Printed rather than run by default. Creating a scheduled task changes the machine,
    and that should be a decision, not a side effect of running a help command.
    """
    python = python_executable()
    action = f'"{python}" -m optscan snapshot'
    return (
        f'schtasks /Create /TN "{task_name}" /TR "{action}" '
        f"/SC DAILY /ST {machine_clock_time(settings)} /F"
    )


def install_task(settings: Settings, task_name: str = TASK_NAME) -> ScheduleResult:
    """Register the task with Windows. Overwrites an existing task of the same name.

    The task fires every calendar day. The job itself refuses to capture on a market
    holiday, so a weekend run is a no op that logs why, which is cheaper and more
    honest than encoding the market calendar into Task Scheduler.
    """
    if sys.platform != "win32":
        return ScheduleResult(
            ok=False,
            message=f"schtasks is Windows only, this is {sys.platform}. Use cron with: "
            f'"{python_executable()}" -m optscan snapshot',
        )

    python = python_executable()
    at = machine_clock_time(settings)
    args = [
        "schtasks",
        "/Create",
        "/TN",
        task_name,
        "/TR",
        f'"{python}" -m optscan snapshot',
        "/SC",
        "DAILY",
        "/ST",
        at,
        "/F",
    ]
    log.info(
        "registering scheduled task",
        task=task_name,
        machine_time=at,
        market_time=settings.snapshot_time_local,
        market_timezone=settings.market_timezone,
    )
    completed = subprocess.run(args, capture_output=True, text=True, check=False)

    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout).strip()
        log.error("failed to register scheduled task", task=task_name, error=message)
        return ScheduleResult(ok=False, message=f"Could not create the task: {message}")

    return ScheduleResult(
        ok=True,
        message=(
            f"Registered {task_name}: daily at {at} machine time, which is "
            f"{settings.snapshot_time_local} in {settings.market_timezone}. "
            f'Remove it with: schtasks /Delete /TN "{task_name}" /F'
        ),
    )
