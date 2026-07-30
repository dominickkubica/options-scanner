"""Market calendar.

Expected values are real NYSE facts for 2026, checked by hand:
- 1 January is New Year's Day.
- 4 July 2026 falls on a Saturday, so Independence Day is observed Friday 3 July.
- Thanksgiving is 26 November, and the day after is a 13:00 early close.
- 30 July 2026 is an ordinary Thursday: 09:30 to 16:00 EDT, which is 13:30 to 20:00 UTC.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from optscan.market_calendar import (
    SessionState,
    is_trading_day,
    market_local_date,
    minutes_from_close,
    previous_trading_day,
    session_bounds,
    session_date_for,
    session_state,
)


@pytest.mark.parametrize(
    ("day", "trading"),
    [
        (date(2026, 7, 30), True),  # ordinary Thursday
        (date(2026, 8, 1), False),  # Saturday
        (date(2026, 8, 2), False),  # Sunday
        (date(2026, 1, 1), False),  # New Year's Day
        (date(2026, 7, 3), False),  # Independence Day observed
        (date(2026, 11, 26), False),  # Thanksgiving
        (date(2026, 11, 27), True),  # early close, but open
        (date(2026, 12, 25), False),  # Christmas
    ],
)
def test_trading_days(day: date, trading: bool) -> None:
    assert is_trading_day(day) is trading


def test_regular_session_bounds_are_utc() -> None:
    bounds = session_bounds(date(2026, 7, 30))
    assert bounds is not None
    open_utc, close_utc = bounds
    assert open_utc == datetime(2026, 7, 30, 13, 30, tzinfo=UTC)  # 09:30 EDT
    assert close_utc == datetime(2026, 7, 30, 20, 0, tzinfo=UTC)  # 16:00 EDT


def test_half_day_close_is_not_assumed_to_be_four_pm() -> None:
    """The day after Thanksgiving closes at 13:00 ET, which is 18:00 UTC in winter."""
    bounds = session_bounds(date(2026, 11, 27))
    assert bounds is not None
    assert bounds[1] == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)


def test_holiday_has_no_bounds() -> None:
    assert session_bounds(date(2026, 12, 25)) is None


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 7, 30, 12, 0, tzinfo=UTC), SessionState.PRE),  # 08:00 ET
        (datetime(2026, 7, 30, 13, 30, tzinfo=UTC), SessionState.OPEN),  # the bell
        (datetime(2026, 7, 30, 18, 0, tzinfo=UTC), SessionState.OPEN),
        (datetime(2026, 7, 30, 20, 0, tzinfo=UTC), SessionState.OPEN),  # the close itself
        (datetime(2026, 7, 30, 21, 30, tzinfo=UTC), SessionState.POST),
        (datetime(2026, 8, 1, 17, 0, tzinfo=UTC), SessionState.CLOSED),  # Saturday
    ],
)
def test_session_state(moment: datetime, expected: SessionState) -> None:
    assert session_state(moment) is expected


def test_session_date_is_none_before_the_open() -> None:
    """A pre open capture would record yesterday's close under today's date."""
    assert session_date_for(datetime(2026, 7, 30, 12, 0, tzinfo=UTC)) is None


def test_session_date_covers_open_and_after_close() -> None:
    assert session_date_for(datetime(2026, 7, 30, 18, 0, tzinfo=UTC)) == date(2026, 7, 30)
    assert session_date_for(datetime(2026, 7, 30, 22, 0, tzinfo=UTC)) == date(2026, 7, 30)


def test_market_local_date_does_not_roll_over_with_utc() -> None:
    """23:30 UTC on 30 July is still 19:30 on 30 July in New York."""
    moment = datetime(2026, 7, 30, 23, 30, tzinfo=UTC)
    assert moment.date() == date(2026, 7, 30)
    assert market_local_date(moment) == date(2026, 7, 30)
    # And 01:00 UTC on the 31st is 21:00 on the 30th in New York.
    assert market_local_date(datetime(2026, 7, 31, 1, 0, tzinfo=UTC)) == date(2026, 7, 30)


def test_minutes_from_close_is_signed() -> None:
    assert minutes_from_close(datetime(2026, 7, 30, 19, 45, tzinfo=UTC)) == pytest.approx(-15.0)
    assert minutes_from_close(datetime(2026, 7, 30, 20, 30, tzinfo=UTC)) == pytest.approx(30.0)
    assert minutes_from_close(datetime(2026, 8, 1, 20, 30, tzinfo=UTC)) is None


def test_previous_trading_day_skips_the_holiday_weekend() -> None:
    """Monday 6 July 2026 looks back past the weekend and the observed holiday."""
    assert previous_trading_day(date(2026, 7, 6)) == date(2026, 7, 2)


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValueError, match="timezone aware"):
        session_state(datetime(2026, 7, 30, 18, 0))
