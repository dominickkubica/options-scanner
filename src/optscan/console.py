"""Colour for the terminal, and the rules about when not to use it.

Colour here is not decoration. Every command in this tool prints a table where one or
two rows are the point and the rest are context, and the whole design of the project is
about making a bad state visible rather than plausible. A red word does that faster than
a column somebody has to read.

## The rules

**Never when the output is not a terminal.** Escape codes in a file or a pipe are
corruption. `optscan scan > today.txt` has to produce text.

**Never when NO_COLOR is set.** The de facto standard, honoured by enough tools that
somebody who sets it means it. Read in `config.py`, which is the only module allowed to
touch the environment.

**Colour is never the only carrier.** Every state that has a colour also has a word.
About one man in twelve cannot distinguish red from green, the terminal may be themed to
anything, and a log pasted into a chat window arrives as plain text. `late` in yellow
still reads as `late`.

## Windows

Windows Terminal handles ANSI. The older conhost.exe does not unless the mode is set,
and unset it prints the escape codes literally, which is worse than no colour. So the
flag is set explicitly and, if that fails, colour is off.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from functools import cache
from typing import TextIO

RESET = "\033[0m"

#: Names rather than codes at the call site, so the palette can change in one place.
CODES: dict[str, str] = {
    "good": "\033[32m",
    "bad": "\033[31m",
    "warn": "\033[33m",
    "info": "\033[36m",
    "dim": "\033[90m",
    "bold": "\033[1m",
}

#: STD_OUTPUT_HANDLE, and the console mode flag that makes ANSI work.
_STD_OUTPUT_HANDLE = -11
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004


@cache
def _enable_windows_vt() -> bool:
    """Turn on virtual terminal processing for this console. True if colour is safe.

    Cached because it is a syscall and otherwise every painted word would make one.
    """
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(_STD_OUTPUT_HANDLE)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(
            kernel32.SetConsoleMode(handle, mode.value | _ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        )
    except (OSError, AttributeError, ImportError):
        return False


def should_colour(mode: str, stream: TextIO | None = None) -> bool:
    """Whether to emit escape codes, given the configured mode and the destination."""
    if mode == "never":
        return False
    target = stream if stream is not None else sys.stdout
    if mode == "always":
        return True
    try:
        if not target.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    return _enable_windows_vt()


@dataclass(frozen=True, slots=True)
class Console:
    """Paints text, or does not, and nothing downstream has to know which."""

    enabled: bool = False

    @classmethod
    def for_stream(cls, mode: str, stream: TextIO | None = None) -> Console:
        return cls(enabled=should_colour(mode, stream))

    def paint(self, text: str, style: str) -> str:
        code = CODES.get(style)
        if not self.enabled or code is None or not text:
            return text
        return f"{code}{text}{RESET}"

    def good(self, text: str) -> str:
        return self.paint(text, "good")

    def bad(self, text: str) -> str:
        return self.paint(text, "bad")

    def warn(self, text: str) -> str:
        return self.paint(text, "warn")

    def info(self, text: str) -> str:
        return self.paint(text, "info")

    def dim(self, text: str) -> str:
        return self.paint(text, "dim")

    def bold(self, text: str) -> str:
        return self.paint(text, "bold")


def width(text: str) -> int:
    """Printable length, ignoring escape codes.

    Needed because `f"{painted:<10}"` pads to the length of the escape sequence rather
    than of the word, so a coloured column is a misaligned column. Anything laying out a
    table pads with this and then paints, or pads by this number.
    """
    result = 0
    index = 0
    while index < len(text):
        if text[index] == "\033":
            end = text.find("m", index)
            if end == -1:
                break
            index = end + 1
            continue
        result += 1
        index += 1
    return result


def pad(text: str, target: int) -> str:
    """Left justify to a printable width, so colour does not break a column."""
    return text + " " * max(0, target - width(text))
