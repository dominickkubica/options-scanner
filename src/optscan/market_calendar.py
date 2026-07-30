"""Market hours awareness.

Sits outside providers/ and analytics/ on purpose: it does no network I/O and it is
not options math. It answers one question the snapshot job cannot get wrong, which is
whether right now belongs to a trading session and which session that is.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas as pd
import pandas_market_calendars as mcal


class SessionState(StrEnum):
    """Where a moment in time sits relative to the regular session."""

    PRE = "pre"
    OPEN = "open"
    POST = "post"
    CLOSED = "closed"  # not a trading day at all


@lru_cache(maxsize=8)
def _calendar(name: str) -> mcal.MarketCalendar:
    return mcal.get_calendar(name)


@lru_cache(maxsize=512)
def _session_bounds(name: str, day: date) -> tuple[datetime, datetime] | None:
    """UTC open and close for one calendar day, or None if it is not a trading day.

    Half days come out of the calendar with their real early close, which is why this
    goes through pandas_market_calendars instead of hardcoding 09:30 to 16:00.
    """
    schedule = _calendar(name).schedule(start_date=day, end_date=day)
    if schedule.empty:
        return None
    row = schedule.iloc[0]
    open_utc = pd.Timestamp(row["market_open"]).tz_convert("UTC").to_pydatetime()
    close_utc = pd.Timestamp(row["market_close"]).tz_convert("UTC").to_pydatetime()
    return open_utc, close_utc


def is_trading_day(day: date, calendar: str = "NYSE") -> bool:
    """True if the market holds a regular session on this date."""
    return _session_bounds(calendar, day) is not None


def session_bounds(day: date, calendar: str = "NYSE") -> tuple[datetime, datetime] | None:
    """UTC (open, close) for the session on this date, or None if there is none."""
    return _session_bounds(calendar, day)


def previous_trading_day(day: date, calendar: str = "NYSE", *, lookback: int = 10) -> date:
    """The most recent trading day strictly before `day`.

    lookback caps the search so a bad calendar name cannot spin forever. Ten days
    covers every real market closure including the long holiday stretches.
    """
    for offset in range(1, lookback + 1):
        candidate = day - timedelta(days=offset)
        if is_trading_day(candidate, calendar):
            return candidate
    raise ValueError(f"no trading day found within {lookback} days before {day} on {calendar}")


def market_local_date(moment: datetime, timezone: str = "America/New_York") -> date:
    """The calendar date `moment` falls on in market local time.

    Necessary because a job running at 20:45 UTC is on the same session as 15:45 in
    New York, but a naive UTC date would already have rolled over in the other direction
    for anything after 19:00 local.
    """
    if moment.tzinfo is None:
        raise ValueError("moment must be timezone aware")
    return moment.astimezone(ZoneInfo(timezone)).date()


def session_state(
    moment: datetime,
    calendar: str = "NYSE",
    timezone: str = "America/New_York",
) -> SessionState:
    """Classify a moment against the regular session for its market local date."""
    if moment.tzinfo is None:
        raise ValueError("moment must be timezone aware")
    bounds = _session_bounds(calendar, market_local_date(moment, timezone))
    if bounds is None:
        return SessionState.CLOSED
    open_utc, close_utc = bounds
    moment_utc = moment.astimezone(UTC)
    if moment_utc < open_utc:
        return SessionState.PRE
    if moment_utc <= close_utc:
        return SessionState.OPEN
    return SessionState.POST


def session_date_for(
    moment: datetime,
    calendar: str = "NYSE",
    timezone: str = "America/New_York",
) -> date | None:
    """The trading session a capture at `moment` belongs to, or None if there is none.

    A capture during or after the session belongs to that day. A capture before the
    open belongs to nothing yet: it would record yesterday's close as if it were today,
    which is exactly the kind of quiet corruption an IV history cannot survive.
    """
    state = session_state(moment, calendar, timezone)
    if state in (SessionState.OPEN, SessionState.POST):
        return market_local_date(moment, timezone)
    return None


def minutes_from_close(
    moment: datetime,
    calendar: str = "NYSE",
    timezone: str = "America/New_York",
) -> float | None:
    """Signed minutes between `moment` and the session close. Negative is before close."""
    bounds = _session_bounds(calendar, market_local_date(moment, timezone))
    if bounds is None:
        return None
    _, close_utc = bounds
    return (moment.astimezone(UTC) - close_utc).total_seconds() / 60.0
