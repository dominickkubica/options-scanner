"""The scheduled task registry.

Nothing here runs `Register-ScheduledTask`. The registration script is generated as
text and asserted on, because the two settings this module exists to set are exactly
the ones that are invisible once the task is installed and only matter on the day the
machine was asleep.
"""

from __future__ import annotations

from datetime import date, time
from pathlib import Path

import pytest

from optscan.config import Settings
from optscan.jobs.schedule import (
    ScheduledJob,
    default_keys,
    describe,
    install_all,
    job_by_key,
    jobs,
    machine_clock_time,
    register_script,
    shift,
    task_states,
)


@pytest.fixture
def settings(tmp_path: Path, clean_env: None) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path)


class TestShift:
    def test_moves_forward(self) -> None:
        assert shift(time(15, 45), 30) == time(16, 15)

    def test_wraps_at_midnight(self) -> None:
        """A capture time late enough that a delay crosses midnight must not raise."""
        assert shift(time(23, 50), 30) == time(0, 20)

    def test_zero_is_identity(self) -> None:
        assert shift(time(9, 30), 0) == time(9, 30)


class TestMachineClockTime:
    def test_new_york_market_time_converts_to_this_machine(self, settings: Settings) -> None:
        """The conversion is the point: Task Scheduler thinks in machine time."""
        result = machine_clock_time(settings, time(15, 45), on=date(2026, 8, 3))
        hour, minute = (int(part) for part in result.split(":"))
        assert 0 <= hour < 24
        assert minute == 45

    def test_defaults_to_the_capture_time(self, settings: Settings) -> None:
        assert machine_clock_time(settings, on=date(2026, 8, 3)) == machine_clock_time(
            settings, settings.snapshot_time, on=date(2026, 8, 3)
        )


class TestJobRegistry:
    def test_every_key_is_unique(self, settings: Settings) -> None:
        keys = [job.key for job in jobs(settings)]
        assert len(keys) == len(set(keys))

    def test_every_task_name_is_unique(self, settings: Settings) -> None:
        """Two jobs sharing a name would silently overwrite each other on install."""
        names = [job.task_name for job in jobs(settings)]
        assert len(names) == len(set(names))

    def test_record_runs_after_the_capture(self, settings: Settings) -> None:
        """Recording scores the stored snapshot, so ordering it first scores yesterday."""
        snapshot = job_by_key(settings, "snapshot")
        record = job_by_key(settings, "record")
        assert snapshot is not None and record is not None
        assert record.at > snapshot.at

    def test_backup_runs_after_the_recording(self, settings: Settings) -> None:
        """Backing up before the day's work copies yesterday and calls it today."""
        record = job_by_key(settings, "record")
        backup = job_by_key(settings, "backup")
        assert record is not None and backup is not None
        assert backup.at > record.at

    def test_resolve_runs_before_the_open(self, settings: Settings) -> None:
        """Settling on expiry day asks for a close the vendor has not published."""
        resolve = job_by_key(settings, "resolve")
        assert resolve is not None
        assert resolve.at < time(9, 30)

    def test_manage_is_not_installed_by_default_and_says_why(self, settings: Settings) -> None:
        manage = job_by_key(settings, "manage")
        assert manage is not None
        assert manage.default_install is False
        assert manage.caution is not None
        assert "throttle" in manage.caution

    def test_default_keys_are_the_ones_protecting_history(self, settings: Settings) -> None:
        assert default_keys(settings) == ["snapshot", "record", "backup", "resolve"]

    def test_unknown_key_returns_none(self, settings: Settings) -> None:
        assert job_by_key(settings, "nope") is None

    def test_manage_repeats_across_the_session(self, settings: Settings) -> None:
        manage = job_by_key(settings, "manage")
        assert manage is not None
        assert manage.repeat_minutes == settings.manage_interval_minutes
        assert manage.repeat_for_minutes == 390


class TestRegisterScript:
    def test_sets_the_two_settings_schtasks_gets_wrong(self, settings: Settings) -> None:
        """The whole reason this module stopped using schtasks.

        Without StartWhenAvailable a machine asleep at the capture time skips the run
        entirely rather than running it late, and the battery defaults stop an unplugged
        laptop from starting at all. Both failures are silent and cost history that
        cannot be rebuilt.
        """
        script = register_script(settings, job_by_key(settings, "snapshot"))
        assert "-StartWhenAvailable" in script
        assert "-AllowStartIfOnBatteries" in script
        assert "-DontStopIfGoingOnBatteries" in script

    def test_bounds_the_run_so_a_wedged_job_cannot_hold_the_database(
        self, settings: Settings
    ) -> None:
        script = register_script(settings, job_by_key(settings, "snapshot"))
        assert "-ExecutionTimeLimit" in script
        assert "-MultipleInstances IgnoreNew" in script

    def test_runs_the_current_interpreter(self, settings: Settings) -> None:
        """The venv is pinned to 3.12 and the system default is not."""
        import sys

        script = register_script(settings, job_by_key(settings, "snapshot"))
        assert str(Path(sys.executable)) in script

    def test_carries_the_job_arguments(self, settings: Settings) -> None:
        for key, expected in (
            ("snapshot", "-m optscan snapshot"),
            ("record", "-m optscan record"),
            ("resolve", "-m optscan resolve"),
            ("backup", "-m optscan backup"),
        ):
            assert expected in register_script(settings, job_by_key(settings, key))

    def test_only_the_repeating_job_gets_a_repetition(self, settings: Settings) -> None:
        assert "Repetition" not in register_script(settings, job_by_key(settings, "snapshot"))
        manage = register_script(settings, job_by_key(settings, "manage"))
        assert "$trigger.Repetition = $pattern.Repetition" in manage
        assert "-RepetitionInterval (New-TimeSpan -Minutes 15)" in manage
        assert "-RepetitionDuration (New-TimeSpan -Minutes 390)" in manage

    def test_quotes_are_escaped(self, settings: Settings) -> None:
        """A summary with an apostrophe must not end the PowerShell string early."""
        job = ScheduledJob(
            key="odd",
            task_name="OptscanOdd",
            arguments="-m optscan status",
            at=time(12, 0),
            summary="it's fine",
        )
        assert "'it''s fine'" in register_script(settings, job)


class TestInstallAll:
    def test_an_unknown_job_fails_without_touching_windows(self, settings: Settings) -> None:
        results = install_all(settings, ["nope"])
        assert len(results) == 1
        assert results[0].ok is False
        assert "No job named" in results[0].message
        assert "snapshot" in results[0].message


class TestDescribe:
    def test_lists_every_job_with_its_state(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from optscan.jobs import schedule

        monkeypatch.setattr(
            schedule, "task_states", lambda names: {"OptscanDailySnapshot": "Ready"}
        )
        text = "\n".join(describe(settings))
        assert "Ready" in text
        assert "not registered" in text
        for job in jobs(settings):
            assert job.key in text

    def test_no_names_means_no_subprocess(self) -> None:
        assert task_states([]) == {}
