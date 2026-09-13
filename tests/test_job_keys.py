"""Every scheduled job is logged under its own key (BUG-0005).

`optscan health` reads the job_run log by job key, and the key used to be the argparse
verb. Two jobs broke that without a sound: the universe capture runs `snapshot
--universe` and filed its success as the watchlist capture's, so a missed watchlist run
read as fine; and `prices sync` was never logged at all, so health called a working job
missing every day. This parses every scheduled job's real command line, so a new flag or
subcommand that reopens the gap fails here rather than in a health report.
"""

from __future__ import annotations

import shlex

from optscan.cli import TRACKED_JOBS, build_parser, job_key
from optscan.config import Settings
from optscan.jobs.schedule import jobs


def _parsed(arguments: str):
    argv = shlex.split(arguments)
    assert argv[:2] == ["-m", "optscan"], arguments
    return build_parser().parse_args(argv[2:])


def test_every_scheduled_command_line_logs_under_its_own_job_key(tmp_settings: Settings) -> None:
    for job in jobs(tmp_settings):
        assert job_key(_parsed(job.arguments)) == job.key, job.arguments


def test_the_tracked_set_is_exactly_the_scheduled_jobs(tmp_settings: Settings) -> None:
    assert {job.key for job in jobs(tmp_settings)} == set(TRACKED_JOBS)


def test_the_watchlist_and_universe_captures_are_told_apart() -> None:
    """The dangerous half: a universe run must never stand in for the watchlist run."""
    parser = build_parser()
    assert job_key(parser.parse_args(["snapshot"])) == "snapshot"
    assert job_key(parser.parse_args(["snapshot", "--universe"])) == "capture"
    assert job_key(parser.parse_args(["snapshot", "--universe", "tech"])) == "capture"


def test_a_command_that_is_not_a_job_has_no_key() -> None:
    parser = build_parser()
    assert job_key(parser.parse_args(["prices", "groups"])) is None
    assert job_key(parser.parse_args(["status"])) is None
