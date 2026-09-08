"""Running PowerShell, with the flags that make it safe to run unattended.

Four call sites had their own copy of this invocation. The duplication mattered less
than the flags: `-NoProfile` and `-NonInteractive` are what stop a user's profile from
running arbitrary setup before the command and what stop the process hanging forever on
a prompt nobody can see, and a fifth caller written from memory would very likely omit
one of them.

`check=False` everywhere on purpose. Every caller here inspects the return code and
turns a failure into a message a person can act on: "could not create the shortcut",
"could not register the task". A raised CalledProcessError would replace those with a
traceback and a command line.
"""

from __future__ import annotations

import subprocess
import sys

from optscan.logging import get_logger

log = get_logger("optscan.jobs.powershell")

#: The flags every invocation needs. No profile so a user's own PowerShell setup cannot
#: change what runs; non interactive so a prompt fails fast instead of hanging a
#: scheduled task.
FLAGS = ("-NoProfile", "-NonInteractive", "-Command")


def available() -> bool:
    """Whether PowerShell is worth trying at all."""
    return sys.platform == "win32"


def run(script: str) -> subprocess.CompletedProcess[str]:
    """Run a script and hand back the completed process for the caller to judge."""
    return subprocess.run(
        ["powershell", *FLAGS, script],
        capture_output=True,
        text=True,
        check=False,
    )


def output(script: str, default: str = "") -> str:
    """Stdout of a script, or `default` when it failed or printed nothing."""
    completed = run(script)
    if completed.returncode != 0:
        log.debug("powershell command failed", code=completed.returncode)
        return default
    return completed.stdout.strip() or default


def failure_message(completed: subprocess.CompletedProcess[str]) -> str:
    """The error a failed run should be reported with.

    stderr when there is one, stdout otherwise: PowerShell writes some failures to
    stdout, and reporting an empty string tells the reader nothing at all.
    """
    return (completed.stderr or completed.stdout).strip()
