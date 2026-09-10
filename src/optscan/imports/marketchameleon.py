"""Market Chameleon daily history export.

The file a Market Chameleon account produces under Stock Info > Historical Data >
Download Now. A CSV with these columns, verified against real QQQ and AAPL exports
each covering 2014-01-02 to 2026-09-04:

    Date, Open, High, Low, Close, Adj Close, Change, Pct Change, Volume, Day VWAP,
    IV30, IV30 Change, IV30 Pct Change, Call Option Volume, Put Option Volume,
    Call Open Interest, Put Open Interest

Everything below was measured on those two files. Nothing is taken from documentation,
because the site publishes none for the format.

## The one genuinely dangerous thing about this format

**There is no symbol column.** Not in a header, not in a comment, nowhere in the file.
The ticker exists only in the filename, HistoricalPrices_QQQ.csv, and a renamed or
mis-saved file will import twelve years of the wrong instrument under the right name
with nothing anywhere to contradict it. Every number downstream would be wrong and
every number would look fine.

So the symbol is a required argument to parse_file. symbol_from_filename offers a
guess from the name and the caller decides whether to accept it; the callers in this
project print the guess and record the filename on every stored row. That is as much
as can be done from inside the file, which is nothing, so it is done from outside.

## What was measured, and what it changed

**IV30 is quoted in vol points and this project stores decimals.** 17.19 in the file
is 0.1719 here. The conversion is one division and there is a test pinning it, because
a hundredfold volatility error is glaring in a payoff diagram and completely invisible
in a rank, and a rank is where this number is going.

**Dates are M/D/YYYY, and this was checked rather than assumed.** Across 3,188 QQQ
rows the first field is never above 12 and the second is above 12 on 1,926 of them, so
the reading is forced. No row falls on a weekend. Month first is used with no
fallback: a European reading of this file would silently keep the rows that parse both
ways and drop the rest, which is a subset that correlates with the day of the month.

**Rows arrive newest first.** They are sorted ascending on the way out, because
everything downstream, realized volatility above all, assumes chronological order and
will happily compute a number from a reversed series without complaining.

**Change is dividend adjusted and Close is not.** They agree on 3,135 of 3,187
consecutive QQQ pairs and disagree on exactly 52, which is exactly the number of
ex-dividend days in 12.7 years of a quarterly payer. That reconciliation is what
confirms both the row ordering and that Adj Close is a real adjustment rather than a
copy of the close.

**Coverage varies inside one file, so absent is not zero.** QQQ carries option open
interest only from 2018-01-30, has no Day VWAP before 2014-01-27, and has three
isolated sessions with no IV30 at all: 2014-07-02, 2014-07-08 and 2015-06-23. Every
one of those becomes None. A zero would be a real published number and would drag any
mean or rank computed over it.

## The rule this module will not bend

**An unexpected column layout is an error, not a best effort read.** The header is
checked against the one that was verified, and a file that does not match refuses to
parse rather than filling what it recognises and leaving the rest None. A vendor
silently adding a column is exactly how a value ends up read from the field beside it.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path

from optscan.logging import get_logger
from optscan.models.vendor import VendorDailyBar

log = get_logger("optscan.imports.marketchameleon")

#: The vendor name stored on every row. An IV history is per vendor and never pooled,
#: so this string is what keeps a Market Chameleon series from being concatenated with
#: the ATM vols this project solves for itself out of stored chains.
SOURCE = "marketchameleon"

#: The header verified against real exports. A file whose header differs is refused.
EXPECTED_HEADER: tuple[str, ...] = (
    "Date",
    "Open",
    "High",
    "Low",
    "Close",
    "Adj Close",
    "Change",
    "Pct Change",
    "Volume",
    "Day VWAP",
    "IV30",
    "IV30 Change",
    "IV30 Pct Change",
    "Call Option Volume",
    "Put Option Volume",
    "Call Open Interest",
    "Put Open Interest",
)

#: Vol points to a decimal. The file says 17.19 and this project means 0.1719.
VOL_POINTS = 100.0

#: HistoricalPrices_QQQ.csv. The only place the ticker appears.
_FILENAME = re.compile(r"^HistoricalPrices_([A-Za-z][A-Za-z0-9.\-]*)$")


class MarketChameleonParseError(ValueError):
    """A file that could not be read. Fatal, and names the row and the reason.

    Deliberately fatal for the same reason the broker importer is: a partially read
    history is worse than none, because a rank computed over a series with a hole in
    it looks exactly like a rank computed over a whole one.
    """


def symbol_from_filename(path: Path) -> str | None:
    """The ticker Market Chameleon put in the filename, or None if it is not there.

    A guess, and treated as one. The file itself cannot confirm it.
    """
    match = _FILENAME.match(path.stem)
    return match.group(1).upper() if match else None


def _number(raw: str | None) -> float | None:
    """A float, or None for a blank. Never 0.0 for a blank."""
    text = (raw or "").strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError as error:
        raise MarketChameleonParseError(f"not a number: {raw!r}") from error


def _required(raw: str | None, column: str) -> float:
    value = _number(raw)
    if value is None:
        raise MarketChameleonParseError(f"{column} is blank, and a bar needs it")
    return value


def _integer(raw: str | None) -> int | None:
    value = _number(raw)
    return None if value is None else round(value)


def _session_date(raw: str | None) -> date:
    try:
        return datetime.strptime((raw or "").strip(), "%m/%d/%Y").date()
    except ValueError as error:
        raise MarketChameleonParseError(
            f"date {raw!r} is not M/D/YYYY. Verified against real exports as month "
            "first; no other reading is attempted, because a fallback would keep the "
            "ambiguous rows and drop the rest."
        ) from error


#: How far outside its own high/low an open or close may sit and still be treated as a
#: rounding artifact rather than a broken row, as a fraction of price.
#:
#: Real exports contain these. Measured over two twelve year files, 6,382 sessions:
#:
#:     AMZN 2019-03-18   open 85.6195 against a low of 85.6315   0.014%
#:     SPY  2018-03-29   open 259.83  against a low of 259.8389  0.003%
#:
#: One row each. The vendor reports the open and the range from different aggregations
#: and rounds them independently, so the open lands a hair below the low.
#:
#: Deliberately narrow. The failure this must not wave through is a split: an
#: unadjusted open beside an adjusted range is off by the split ratio, which on AMZN's
#: 20:1 would be 1,900% and nothing near this bound. A tenth of a percent separates
#: "the vendor rounded twice" from "these two numbers describe different shares".
RANGE_TOLERANCE = 0.001


def _reconcile_range(
    open_: float, high: float, low: float, close: float, session: date
) -> tuple[float, float]:
    """Widen high and low so the range contains the open and the close.

    Rejecting the row would be the wrong response and so would trusting it blindly. An
    opening print is a trade: if the stock printed 85.6195 at the open then the low was
    at most 85.6195, whatever the low column says. Taking the min and the max is the
    only reconstruction consistent with every number in the row.

    Beyond the tolerance nothing is repaired, because at that point the two figures are
    not disagreeing about rounding, they are describing different things, and quietly
    stretching a range to cover a split would bury the error in the data rather than
    surface it.
    """
    span = max(abs(high), abs(low), 1e-9)
    breach = max(low - min(open_, close), max(open_, close) - high, 0.0)
    if breach == 0.0:
        return high, low
    if breach / span > RANGE_TOLERANCE:
        raise MarketChameleonParseError(
            f"session {session} has an open/close {breach:.4f} outside its own "
            f"{low}-{high} range, which is {breach / span:.2%} of price and far past "
            f"the {RANGE_TOLERANCE:.1%} rounding tolerance. That is not a rounding "
            "artifact; it usually means the row mixes adjusted and unadjusted prices."
        )
    log.warning(
        "widened a session range to contain its own open and close",
        session=str(session),
        breach=round(breach, 6),
        fraction=f"{breach / span:.4%}",
    )
    return max(high, open_, close), min(low, open_, close)


def parse_rows(rows: Iterable[dict[str, str]], symbol: str) -> list[VendorDailyBar]:
    """Parse already-read rows. Sorted ascending by session date."""
    bars: list[VendorDailyBar] = []
    seen: dict[date, int] = {}

    for number, row in enumerate(rows, start=2):  # line 1 is the header
        try:
            session = _session_date(row.get("Date"))
            if session in seen:
                raise MarketChameleonParseError(
                    f"session {session} appears twice, on lines {seen[session]} and "
                    f"{number}. A daily series with a duplicated day would double that "
                    "day's weight in every rank computed over it."
                )
            seen[session] = number

            iv30 = _number(row.get("IV30"))
            open_ = _required(row.get("Open"), "Open")
            close_ = _required(row.get("Close"), "Close")
            high, low = _reconcile_range(
                open_,
                _required(row.get("High"), "High"),
                _required(row.get("Low"), "Low"),
                close_,
                session,
            )
            bars.append(
                VendorDailyBar(
                    source=SOURCE,
                    symbol=symbol,
                    session_date=session,
                    open=open_,
                    high=high,
                    low=low,
                    close=close_,
                    adj_close=_number(row.get("Adj Close")),
                    volume=_integer(row.get("Volume")),
                    vwap=_number(row.get("Day VWAP")),
                    iv30=None if iv30 is None else iv30 / VOL_POINTS,
                    call_volume=_integer(row.get("Call Option Volume")),
                    put_volume=_integer(row.get("Put Option Volume")),
                    call_open_interest=_integer(row.get("Call Open Interest")),
                    put_open_interest=_integer(row.get("Put Open Interest")),
                )
            )
        except MarketChameleonParseError as error:
            raise MarketChameleonParseError(f"line {number}: {error}") from error

    bars.sort(key=lambda bar: bar.session_date)
    return bars


def parse_file(path: Path, symbol: str) -> list[VendorDailyBar]:
    """Read one export. symbol is required and is never inferred silently.

    The file carries no ticker anywhere, so nothing in here can check the symbol
    against its contents. The caller is responsible for it, and every caller in this
    project shows the user which ticker it is about to file the rows under.
    """
    # utf-8-sig: the export is written with a byte order mark, which otherwise turns
    # the first header into a name no lookup will match.
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        header = tuple(reader.fieldnames or ())
        if header != EXPECTED_HEADER:
            missing = [c for c in EXPECTED_HEADER if c not in header]
            extra = [c for c in header if c not in EXPECTED_HEADER]
            raise MarketChameleonParseError(
                f"{path.name} does not have the verified Market Chameleon layout. "
                f"missing={missing or 'none'} unexpected={extra or 'none'}. "
                "Refusing rather than reading what is recognisable: a column added "
                "beside another is how a value gets read from the wrong field."
            )
        bars = parse_rows(reader, symbol)

    if not bars:
        raise MarketChameleonParseError(f"{path.name} has a valid header and no rows")

    with_iv = sum(1 for bar in bars if bar.iv30 is not None)
    log.info(
        "parsed vendor history",
        file=path.name,
        symbol=symbol,
        rows=len(bars),
        with_iv30=with_iv,
        first=str(bars[0].session_date),
        last=str(bars[-1].session_date),
    )
    return bars
