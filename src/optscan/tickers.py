"""What a ticker may look like, checked once wherever a symbol enters the system.

A symbol becomes a path component (`symbol=SPY` under the snapshot root) and, until
2026-09-12, became glob pattern text as well. Unchecked, `SPY/../..` in the scan's
`symbols` query walked above the snapshot root and read any parquet file the server
could reach (SEC-0001), and a symbol typed into `optscan watchlist add` could make the
capture job create directories and write files anywhere (SEC-0003). The only check that
existed sat in the dashboard's pin route, so the CLI and the scan query bypassed it.

So the rule lives here, once: every writer and every lookup goes through
`normalize_ticker`, and something that is not a ticker is refused by name rather than
used.
"""

from __future__ import annotations

import re

#: Letters and digits, with the punctuation real listings use: BRK.B, BF-B, and the
#: caret of an index such as ^VIX. No separators, no whitespace, no leading dot, so no
#: value that passes can be more than one path component or a relative step.
TICKER = re.compile(r"^[A-Z0-9^][A-Z0-9.\-]{0,15}$")


def is_ticker(text: str | None) -> bool:
    return bool(text) and TICKER.fullmatch(text.strip().upper()) is not None


def normalize_ticker(text: str | None) -> str:
    """The ticker upper-cased and trimmed, or a ValueError naming what was refused."""
    value = (text or "").strip().upper()
    if not TICKER.fullmatch(value):
        raise ValueError(f"{text!r} is not a ticker")
    return value
