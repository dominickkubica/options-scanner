"""The Desktop launcher and its shortcut.

The generated batch file embeds two PowerShell one liners built by f-string, and a
brace that fails to collapse produces a script that Windows still runs and that fails
at exactly one branch. That happened, so the syntax of both fragments is pinned here.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from optscan.config import Settings
from optscan.jobs.launcher import (
    SHORTCUT_NAME,
    browse_host,
    dashboard_url,
    icon_bytes,
    launcher_script,
    shortcut_script,
    write_icon,
)


@pytest.fixture
def settings(tmp_path: Path, clean_env: None) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path)


class TestBrowseHost:
    def test_loopback_is_kept(self, settings: Settings) -> None:
        assert browse_host(settings) == "127.0.0.1"

    def test_a_wildcard_bind_becomes_loopback(self, tmp_path: Path, clean_env: None) -> None:
        """0.0.0.0 is a valid thing to bind and not a valid thing to visit."""
        wild = Settings(_env_file=None, data_dir=tmp_path, api_host="0.0.0.0")
        assert browse_host(wild) == "127.0.0.1"

    def test_a_real_host_is_left_alone(self, tmp_path: Path, clean_env: None) -> None:
        named = Settings(_env_file=None, data_dir=tmp_path, api_host="192.168.1.50")
        assert browse_host(named) == "192.168.1.50"

    def test_the_url_carries_the_configured_port(self, tmp_path: Path, clean_env: None) -> None:
        moved = Settings(_env_file=None, data_dir=tmp_path, api_port=8123)
        assert dashboard_url(moved) == "http://127.0.0.1:8123"


class TestLauncherScript:
    def test_it_uses_the_venv_interpreter(self, settings: Settings) -> None:
        """The system python is 3.13 and this project does not run on it."""
        script = launcher_script(settings)
        assert "venv\\Scripts\\python.exe" in script
        assert "-m optscan serve" in script

    def test_the_configured_port_reaches_every_line(self, tmp_path: Path, clean_env: None) -> None:
        moved = Settings(_env_file=None, data_dir=tmp_path, api_port=8123)
        script = launcher_script(moved)
        assert ":8000" not in script
        assert script.count(":8123") >= 3

    def test_it_checks_health_before_starting_a_second_server(self, settings: Settings) -> None:
        script = launcher_script(settings)
        assert "/api/health" in script
        assert "if not errorlevel 1" in script

    def test_no_stray_double_braces_survive_the_f_strings(self, settings: Settings) -> None:
        """The bug this file exists for. `}}` in the output is a PowerShell syntax error."""
        script = launcher_script(settings)
        assert "}}" not in script
        assert "{{" not in script

    def test_the_braces_balance(self, settings: Settings) -> None:
        script = launcher_script(settings)
        assert script.count("{") == script.count("}")


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell parses the generated script")
class TestGeneratedPowerShellParses:
    """Hand the fragments to PowerShell's own parser rather than trusting the shape.

    Parsing only. Nothing is executed, so no request is made and no server is started.
    """

    def _parse(self, fragment: str) -> subprocess.CompletedProcess[str]:
        command = (
            "$errors = $null; "
            "[void][System.Management.Automation.Language.Parser]::ParseInput("
            "$env:OPTSCAN_FRAGMENT, [ref]$null, [ref]$errors); "
            "if ($errors.Count) { $errors[0].Message; exit 1 } else { exit 0 }"
        )
        return subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            check=False,
            env={**dict(__import__("os").environ), "OPTSCAN_FRAGMENT": fragment},
        )

    def _fragments(self, script: str) -> list[str]:
        return re.findall(r'-Command "(.+)"', script)

    def test_both_powershell_fragments_parse(self, settings: Settings) -> None:
        fragments = self._fragments(launcher_script(settings))
        assert len(fragments) == 2, "expected the health probe and the browser waiter"
        for fragment in fragments:
            completed = self._parse(fragment)
            assert completed.returncode == 0, f"{fragment}\n{completed.stdout}"


class TestShortcutScript:
    def test_it_points_at_the_launcher_and_starts_minimized(self, tmp_path: Path) -> None:
        script = shortcut_script(
            tmp_path / "optscan-dashboard.cmd",
            tmp_path / "optscan dashboard.lnk",
            tmp_path / "optscan.ico",
        )
        assert "optscan-dashboard.cmd" in script
        assert "WindowStyle = 7" in script
        assert "IconLocation" in script

    def test_the_shortcut_is_named_for_a_human(self) -> None:
        assert SHORTCUT_NAME.endswith(".lnk")
        assert " " in SHORTCUT_NAME


class TestIcon:
    def test_it_is_a_well_formed_ico_header(self) -> None:
        data = icon_bytes()
        # reserved 0, type 1 (icon), one image
        assert data[:6] == b"\x00\x00\x01\x00\x01\x00"

    def test_the_declared_length_matches_the_payload(self) -> None:
        data = icon_bytes()
        declared = int.from_bytes(data[14:18], "little")
        offset = int.from_bytes(data[18:22], "little")
        assert offset == 22
        assert len(data) == offset + declared

    def test_it_writes_and_creates_its_directory(self, tmp_path: Path) -> None:
        path = write_icon(tmp_path / "nested" / "optscan.ico")
        assert path.exists()
        assert path.read_bytes() == icon_bytes()

    @pytest.mark.skipif(sys.platform != "win32", reason="uses the Windows icon parser")
    def test_windows_can_parse_it(self, tmp_path: Path) -> None:
        """The only opinion that matters: an .ico Windows rejects shows as a blank page."""
        path = write_icon(tmp_path / "optscan.ico")
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Add-Type -AssemblyName System.Drawing; "
                f"$i = New-Object System.Drawing.Icon('{path}'); "
                '"$($i.Width)x$($i.Height)"; $i.Dispose()',
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert "32x32" in completed.stdout
