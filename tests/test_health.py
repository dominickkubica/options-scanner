"""Whether the recurring jobs are running, and whether the answer can be trusted.

Most of these are about not crying wolf. A monitor that reports a missed capture every
Saturday, or reports every job as broken on the day it is installed, gets ignored before
it has ever been right, and then it is silent in the one week that matters.

The dates here are real. 2026-08-01 is a Saturday, 2026-08-02 a Sunday, 2026-07-31 a
Friday, and 2026-04-03 is Good Friday, which the NYSE is shut for.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from optscan.config import Settings
from optscan.jobs.health import (
    NEVER_RAN_BEFORE_YEAR,
    JobHealth,
    TaskInfo,
    Verdict,
    _parse_task_time,
    _verdict_for,
    expected_at,
    expected_by,
    needs_attention,
    sessions_between,
    worst,
)
from optscan.jobs.schedule import ScheduledJob, job_by_key
from optscan.storage import jobs as job_log

ET = "America/New_York"


@pytest.fixture
def settings(tmp_path: Path, clean_env: None) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path)


def at_et(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    from zoneinfo import ZoneInfo

    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(ET))


def a_job(**kwargs) -> ScheduledJob:
    base = {
        "key": "test",
        "task_name": "OptscanTest",
        "arguments": "-m optscan status",
        "at": time(15, 45),
        "summary": "a job",
    }
    return ScheduledJob(**{**base, **kwargs})


def a_run(started: datetime, *, ok: bool | None = True, finished: bool = True) -> job_log.JobRun:
    return job_log.JobRun(
        job="test",
        started_at=started,
        finished_at=started + timedelta(minutes=1) if finished else None,
        ok=ok,
        detail=None if ok else "it broke",
    )


class TestExpectedBy:
    def test_after_the_time_today_is_expected(self, settings: Settings) -> None:
        job = a_job(at=time(15, 45))
        assert expected_by(job, settings, at_et(2026, 7, 31, 16, 0)) == date(2026, 7, 31)

    def test_before_the_time_yesterday_is_expected(self, settings: Settings) -> None:
        """A capture due at 15:45 is not late at noon."""
        job = a_job(at=time(15, 45))
        assert expected_by(job, settings, at_et(2026, 7, 31, 12, 0)) == date(2026, 7, 30)

    def test_a_session_job_on_a_sunday_looks_back_to_friday(self, settings: Settings) -> None:
        """The headline anti-noise case. Nothing was missed over a weekend."""
        job = a_job(at=time(15, 45), trading_days_only=True)
        assert expected_by(job, settings, at_et(2026, 8, 2, 20, 0)) == date(2026, 7, 31)

    def test_a_session_job_on_a_saturday_looks_back_to_friday(self, settings: Settings) -> None:
        job = a_job(trading_days_only=True)
        assert expected_by(job, settings, at_et(2026, 8, 1, 20, 0)) == date(2026, 7, 31)

    def test_a_calendar_job_on_a_sunday_expects_sunday(self, settings: Settings) -> None:
        """Backing up is worth doing on a Sunday, so a missed Sunday is a real miss."""
        job = a_job(at=time(8, 0), trading_days_only=False)
        assert expected_by(job, settings, at_et(2026, 8, 2, 20, 0)) == date(2026, 8, 2)

    def test_a_market_holiday_is_skipped(self, settings: Settings) -> None:
        """Good Friday 2026 is 3 April, so the Thursday is the last session."""
        job = a_job(trading_days_only=True)
        assert expected_by(job, settings, at_et(2026, 4, 3, 20, 0)) == date(2026, 4, 2)

    def test_monday_morning_is_measured_against_friday_not_monday(self, settings: Settings) -> None:
        """The ordering bug: dropping to a trading day first would call Monday late
        against a Monday whose capture time has not arrived."""
        job = a_job(at=time(15, 45), trading_days_only=True)
        assert expected_by(job, settings, at_et(2026, 8, 3, 9, 0)) == date(2026, 7, 31)


class TestExpectedAt:
    def test_it_carries_the_scheduled_time_of_day(self, settings: Settings) -> None:
        job = a_job(at=time(8, 0), trading_days_only=False)
        moment = expected_at(job, settings, at_et(2026, 8, 2, 20, 0))
        assert moment.date() == date(2026, 8, 2)
        assert moment.hour == 8

    def test_it_is_earlier_than_a_log_that_started_that_afternoon(self, settings: Settings) -> None:
        """Day granularity would call these equal and blame the job for a run that
        happened hours before anything was recording."""
        job = a_job(at=time(8, 0), trading_days_only=False)
        moment = expected_at(job, settings, at_et(2026, 8, 2, 20, 0))
        assert moment < at_et(2026, 8, 2, 14, 0)


class TestSessionsBetween:
    def test_a_weekend_counts_as_no_missed_sessions(self, settings: Settings) -> None:
        count = sessions_between(date(2026, 7, 31), date(2026, 8, 2), settings, trading_only=True)
        assert count == 0

    def test_a_weekend_counts_as_two_missed_days_for_a_calendar_job(
        self, settings: Settings
    ) -> None:
        count = sessions_between(date(2026, 7, 31), date(2026, 8, 2), settings, trading_only=False)
        assert count == 2

    def test_consecutive_sessions_count(self, settings: Settings) -> None:
        count = sessions_between(date(2026, 7, 29), date(2026, 7, 31), settings, trading_only=True)
        assert count == 2

    def test_nothing_in_the_future(self, settings: Settings) -> None:
        assert (
            sessions_between(date(2026, 8, 5), date(2026, 8, 1), settings, trading_only=True) == 0
        )


class TestParseTaskTime:
    def test_the_never_ran_sentinel_becomes_none(self) -> None:
        """Get-ScheduledTaskInfo reports 1999-11-30 for a task that has never run.
        Parsing it as a real time reports a fresh task as one that ran last century."""
        assert _parse_task_time("1999-11-30T00:00:00.0000000-08:00") is None

    def test_the_other_windows_sentinel_becomes_none(self) -> None:
        assert _parse_task_time("1899-12-30T00:00:00.0000000Z") is None

    def test_the_cutoff_is_before_this_tool_existed(self) -> None:
        assert NEVER_RAN_BEFORE_YEAR <= 2026

    def test_a_real_time_survives(self) -> None:
        moment = _parse_task_time("2026-08-01T12:45:01.0000000-07:00")
        assert moment is not None
        assert moment.year == 2026

    def test_nonsense_is_none(self) -> None:
        assert _parse_task_time("not a time") is None


def verdict_for(job, info, latest, success, settings, now, log_started=None) -> JobHealth:
    return _verdict_for(job, info, latest, success, settings, now, log_started)


class TestVerdicts:
    def test_an_unregistered_default_job_is_missing(self, settings: Settings) -> None:
        result = verdict_for(a_job(), TaskInfo(), None, None, settings, at_et(2026, 8, 3, 20))
        assert result.verdict is Verdict.MISSING
        assert "optscan schedule --install" in result.detail

    def test_an_unregistered_opt_in_job_is_fine(self, settings: Settings) -> None:
        """manage is deliberately not installed, so reporting it as broken is noise."""
        job = a_job(default_install=False)
        result = verdict_for(job, TaskInfo(), None, None, settings, at_et(2026, 8, 3, 20))
        assert result.verdict is Verdict.WAITING

    def test_a_disabled_task_is_called_out(self, settings: Settings) -> None:
        result = verdict_for(
            a_job(), TaskInfo(state="Disabled"), None, None, settings, at_et(2026, 8, 3, 20)
        )
        assert result.verdict is Verdict.DISABLED

    def test_a_run_that_never_finished_is_stalled(self, settings: Settings) -> None:
        now = at_et(2026, 7, 31, 20)
        open_run = a_run(now - timedelta(hours=4), ok=None, finished=False)
        result = verdict_for(a_job(), TaskInfo(state="Ready"), open_run, None, settings, now)
        assert result.verdict is Verdict.STALLED
        assert "never finished" in result.detail

    def test_a_recent_open_run_is_not_stalled(self, settings: Settings) -> None:
        """It might still be going, and calling that a failure would be a false alarm."""
        now = at_et(2026, 7, 31, 16)
        open_run = a_run(now - timedelta(minutes=2), ok=None, finished=False)
        result = verdict_for(a_job(), TaskInfo(state="Ready"), open_run, None, settings, now)
        assert result.verdict is not Verdict.STALLED

    def test_a_failed_run_reports_its_reason(self, settings: Settings) -> None:
        now = at_et(2026, 7, 31, 20)
        failed = a_run(now - timedelta(minutes=5), ok=False)
        result = verdict_for(a_job(), TaskInfo(state="Ready"), failed, None, settings, now)
        assert result.verdict is Verdict.FAILED
        assert "it broke" in result.detail

    def test_a_green_exit_code_with_no_logged_work_is_not_a_pass(self, settings: Settings) -> None:
        """The case the whole two source design exists for. Windows is satisfied and
        the work never happened."""
        now = at_et(2026, 8, 3, 20)
        info = TaskInfo(state="Ready", last_run=at_et(2026, 8, 3, 15, 46), last_result=0)
        result = verdict_for(a_job(), info, None, None, settings, now)
        assert result.verdict is Verdict.MISSING

    def test_a_non_zero_exit_with_no_logged_work_says_it_died_early(
        self, settings: Settings
    ) -> None:
        now = at_et(2026, 8, 3, 20)
        info = TaskInfo(state="Ready", last_run=at_et(2026, 8, 3, 15, 46), last_result=1)
        result = verdict_for(a_job(), info, None, None, settings, now)
        assert result.verdict is Verdict.FAILED
        assert "failing before it starts" in result.detail

    def test_a_registered_task_that_has_not_fired_yet_is_waiting(self, settings: Settings) -> None:
        now = at_et(2026, 8, 3, 9)
        info = TaskInfo(state="Ready", last_run=None, next_run=at_et(2026, 8, 3, 15, 45))
        result = verdict_for(a_job(), info, None, None, settings, now)
        assert result.verdict is Verdict.WAITING
        assert "has not fired yet" in result.detail

    def test_a_run_before_the_log_existed_is_not_a_missed_run(self, settings: Settings) -> None:
        """The day this shipped, every job had run for weeks with nothing recording it.
        Reporting all of them as broken is how a monitor loses its reader on day one."""
        now = at_et(2026, 8, 3, 20)
        info = TaskInfo(state="Ready", last_run=at_et(2026, 8, 3, 15, 46), last_result=0)
        log_started = at_et(2026, 8, 3, 18, 0)
        result = verdict_for(a_job(), info, None, None, settings, now, log_started)
        assert result.verdict is Verdict.WAITING
        assert "Run logging only started" in result.detail

    def test_a_non_zero_exit_before_the_log_existed_is_not_blamed(self, settings: Settings) -> None:
        now = at_et(2026, 8, 3, 20)
        info = TaskInfo(state="Ready", last_run=at_et(2026, 8, 3, 15, 46), last_result=1)
        log_started = at_et(2026, 8, 3, 18, 0)
        result = verdict_for(a_job(), info, None, None, settings, now, log_started)
        assert result.verdict is not Verdict.FAILED

    def test_a_missed_run_after_the_log_existed_is_reported(self, settings: Settings) -> None:
        """The floor must not become a permanent excuse."""
        now = at_et(2026, 8, 5, 20)
        info = TaskInfo(state="Ready", last_run=at_et(2026, 8, 5, 15, 46), last_result=0)
        log_started = at_et(2026, 8, 3, 10, 0)
        result = verdict_for(a_job(), info, None, None, settings, now, log_started)
        assert result.verdict is Verdict.MISSING

    def test_a_recent_success_is_ok(self, settings: Settings) -> None:
        now = at_et(2026, 7, 31, 20)
        success = a_run(at_et(2026, 7, 31, 15, 46))
        result = verdict_for(a_job(), TaskInfo(state="Ready"), success, success, settings, now)
        assert result.verdict is Verdict.OK

    def test_a_weekend_does_not_make_friday_late(self, settings: Settings) -> None:
        """Ran on Friday, checked on Sunday. Nothing was missed."""
        now = at_et(2026, 8, 2, 20)
        success = a_run(at_et(2026, 7, 31, 15, 46))
        job = a_job(trading_days_only=True)
        result = verdict_for(job, TaskInfo(state="Ready"), success, success, settings, now)
        assert result.verdict is Verdict.OK

    def test_a_stale_success_is_late_with_a_count(self, settings: Settings) -> None:
        """ "late" is a shrug. "2 sessions missed" is a decision."""
        now = at_et(2026, 8, 5, 20)
        success = a_run(at_et(2026, 7, 31, 15, 46))
        job = a_job(trading_days_only=True)
        result = verdict_for(job, TaskInfo(state="Ready"), success, success, settings, now)
        assert result.verdict is Verdict.LATE
        assert result.missed == 3
        assert "3 sessions missed" in result.detail

    def test_one_missed_session_is_singular(self, settings: Settings) -> None:
        now = at_et(2026, 8, 4, 20)
        success = a_run(at_et(2026, 8, 3, 15, 46))
        job = a_job(trading_days_only=True)
        result = verdict_for(job, TaskInfo(state="Ready"), success, success, settings, now)
        assert "1 session missed" in result.detail


class TestOrdering:
    def test_worst_picks_the_most_severe(self) -> None:
        job = a_job()
        results = [
            JobHealth(job=job, verdict=Verdict.OK, detail="", registered=True),
            JobHealth(job=job, verdict=Verdict.LATE, detail="", registered=True),
            JobHealth(job=job, verdict=Verdict.FAILED, detail="", registered=True),
        ]
        assert worst(results) is Verdict.FAILED

    def test_worst_of_nothing_is_ok(self) -> None:
        assert worst([]) is Verdict.OK

    def test_needs_attention_excludes_waiting_and_ok(self) -> None:
        job = a_job()
        results = [
            JobHealth(job=job, verdict=Verdict.OK, detail="", registered=True),
            JobHealth(job=job, verdict=Verdict.WAITING, detail="", registered=True),
            JobHealth(job=job, verdict=Verdict.DISABLED, detail="", registered=True),
            JobHealth(job=job, verdict=Verdict.LATE, detail="", registered=True),
        ]
        assert [item.verdict for item in needs_attention(results)] == [Verdict.LATE]

    def test_every_verdict_has_a_tone(self) -> None:
        for verdict in Verdict:
            assert verdict.tone in {"good", "bad", "warn", "dim"}


class TestRealJobsAreClassified:
    def test_the_session_jobs_are_marked_as_such(self, settings: Settings) -> None:
        for key in ("snapshot", "record"):
            assert job_by_key(settings, key).trading_days_only is True

    def test_the_calendar_jobs_are_marked_as_such(self, settings: Settings) -> None:
        """Backup and resolve do real work on a Sunday, so a missed Sunday is real."""
        for key in ("backup", "resolve"):
            assert job_by_key(settings, key).trading_days_only is False
