"""Terminal colour, and the rules about when not to use it.

The rules are the point rather than the palette. Escape codes in a redirected file are
corruption, and a coloured column that pads to the length of its escape sequence is a
misaligned column.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from optscan.config import Settings
from optscan.console import CODES, RESET, Console, pad, should_colour, width


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


class TestShouldColour:
    def test_never_means_never_even_on_a_terminal(self) -> None:
        assert should_colour("never", _Tty()) is False

    def test_always_means_always_even_into_a_pipe(self) -> None:
        """For a CI log or a pager that renders codes the guess is wrong, so it can be
        overridden."""
        assert should_colour("always", _Pipe()) is True

    def test_auto_is_off_when_the_destination_is_not_a_terminal(self) -> None:
        """`optscan scan > today.txt` has to produce text, not escape codes."""
        assert should_colour("auto", _Pipe()) is False

    def test_auto_survives_a_stream_that_cannot_answer(self) -> None:
        class Awkward:
            def isatty(self):
                raise ValueError("closed")

        assert should_colour("auto", Awkward()) is False  # type: ignore[arg-type]

    def test_auto_survives_a_stream_with_no_isatty_at_all(self) -> None:
        assert should_colour("auto", object()) is False  # type: ignore[arg-type]


class TestConsole:
    def test_disabled_returns_the_text_untouched(self) -> None:
        console = Console(enabled=False)
        assert console.good("ok") == "ok"
        assert console.bad("fail") == "fail"

    def test_enabled_wraps_and_resets(self) -> None:
        console = Console(enabled=True)
        assert console.good("ok") == f"{CODES['good']}ok{RESET}"

    def test_every_style_has_a_code(self) -> None:
        console = Console(enabled=True)
        for name in ("good", "bad", "warn", "info", "dim", "bold"):
            assert getattr(console, name)("x") != "x"

    def test_an_unknown_style_is_left_alone_rather_than_raising(self) -> None:
        """A typo in a style name should cost the colour, not the command."""
        assert Console(enabled=True).paint("x", "chartreuse") == "x"

    def test_empty_text_is_not_wrapped(self) -> None:
        assert Console(enabled=True).good("") == ""

    def test_for_stream_reads_the_mode(self) -> None:
        assert Console.for_stream("never", _Tty()).enabled is False
        assert Console.for_stream("always", _Pipe()).enabled is True

    def test_the_default_console_is_plain(self) -> None:
        """So a caller that forgets to build one prints text rather than codes."""
        assert Console().enabled is False


class TestWidth:
    def test_plain_text_is_its_own_length(self) -> None:
        assert width("late") == 4

    def test_escape_codes_do_not_count(self) -> None:
        assert width(Console(enabled=True).warn("late")) == 4

    def test_an_unterminated_escape_does_not_hang(self) -> None:
        assert width("\033[31") == 0


class TestPad:
    def test_plain_text_pads_to_the_target(self) -> None:
        assert pad("ok", 5) == "ok   "

    def test_coloured_text_pads_by_printable_width(self) -> None:
        """The bug this exists for: f-string padding counts the escape sequence."""
        painted = Console(enabled=True).good("ok")
        result = pad(painted, 5)
        assert result.endswith("   ")
        assert width(result) == 5

    def test_text_longer_than_the_target_is_not_truncated(self) -> None:
        assert pad("overlong", 3) == "overlong"


@pytest.fixture
def no_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear NO_COLOR from the ambient environment.

    Not hypothetical: the environment these tests were written in sets NO_COLOR=1, so
    without this the default-mode test passes or fails depending on who runs it, which
    is worse than not having it.
    """
    monkeypatch.delenv("NO_COLOR", raising=False)


class TestColorSetting:
    def test_auto_is_the_default(self, tmp_path: Path, clean_env: None, no_no_color: None) -> None:
        assert Settings(_env_file=None, data_dir=tmp_path).color_mode == "auto"

    def test_no_color_turns_auto_into_never(
        self, tmp_path: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cross tool convention, honoured because somebody who sets it means it."""
        monkeypatch.setenv("NO_COLOR", "1")
        assert Settings(_env_file=None, data_dir=tmp_path).color_mode == "never"

    def test_an_explicit_always_beats_no_color(
        self, tmp_path: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NO_COLOR", "1")
        settings = Settings(_env_file=None, data_dir=tmp_path, color="always")
        assert settings.color_mode == "always"

    def test_an_empty_no_color_is_not_set(
        self, tmp_path: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NO_COLOR", "")
        assert Settings(_env_file=None, data_dir=tmp_path).color_mode == "auto"

    def test_never_stays_never(self, tmp_path: Path, clean_env: None, no_no_color: None) -> None:
        assert Settings(_env_file=None, data_dir=tmp_path, color="never").color_mode == "never"
