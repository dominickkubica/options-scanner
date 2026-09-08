"""The signal scan and its record: staleness, suppression, and delivery.

The staleness test is the one that matters most here. Every signal in this module is
defined on the most recent bar, so a symbol whose history stopped updating reports its
final session as though it were today, forever. On the first live run that produced
five of fifteen signals from eight delisted tickers, including a 15.6x "volume surge"
that was really the last day of trading before an acquisition closed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from optscan.alerts import Alert, AlertSink, raise_signals
from optscan.analytics.signals import Signal, SignalKind
from optscan.config import Settings
from optscan.jobs.signals import (
    MAX_STALE_DAYS,
    NO_SOURCE,
    STALE,
    TOO_SHORT,
    newest_session,
    run_scan,
    scan_symbol,
)
from optscan.models.vendor import VendorDailyBar
from optscan.storage import db
from optscan.storage import signals as store
from optscan.storage.vendor import import_daily_bars

SOURCE = "test"


class RecordingSink(AlertSink):
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[Alert] = []

    def send(self, alert: Alert) -> bool:
        self.sent.append(alert)
        return True


def series(
    symbol: str,
    last: date,
    count: int = 80,
    *,
    start: float = 100.0,
    step: float = 0.0,
) -> list[VendorDailyBar]:
    """`count` daily bars ending on `last`, one calendar day apart.

    Calendar days rather than sessions: nothing under test reads a market calendar, and
    a weekday generator would only make the fixture harder to reason about.
    """
    bars = []
    for index in range(count):
        day = last - timedelta(days=count - 1 - index)
        close = start + step * index
        bars.append(
            VendorDailyBar(
                source=SOURCE,
                symbol=symbol,
                session_date=day,
                open=close,
                high=close + 1.0,
                low=close - 1.0,
                close=close,
                volume=1_000_000,
            )
        )
    return bars


@pytest.fixture
def settings(tmp_settings: Settings) -> Settings:
    return tmp_settings


@pytest.fixture
def conn(settings: Settings):
    with db.session(settings.sqlite_path) as connection:
        yield connection


TODAY = date(2026, 9, 4)


# --------------------------------------------------------------------------------
# Staleness
# --------------------------------------------------------------------------------


def test_newest_session_is_the_latest_day_held_for_any_symbol(conn) -> None:
    import_daily_bars(conn, series("FRESH", TODAY))
    import_daily_bars(conn, series("OLD", TODAY - timedelta(days=400)))
    assert newest_session(conn) == TODAY


def test_a_symbol_that_stopped_updating_is_skipped(conn) -> None:
    """The delisted ticker case. Its last bar is real; it is just not news."""
    import_daily_bars(conn, series("FRESH", TODAY))
    import_daily_bars(conn, series("DEAD", TODAY - timedelta(days=400)))

    found, reason = scan_symbol(conn, "DEAD", asof=TODAY)
    assert found is None
    assert reason == STALE


def test_a_symbol_a_few_days_behind_is_still_evaluated(conn) -> None:
    """A halt or a vendor's missing day must not be treated as a delisting."""
    import_daily_bars(conn, series("LAGGY", TODAY - timedelta(days=MAX_STALE_DAYS)))
    found, reason = scan_symbol(conn, "LAGGY", asof=TODAY)
    assert reason is None
    assert found is not None


def test_staleness_is_measured_against_stored_history_not_the_wall_clock(conn) -> None:
    """Otherwise a scan run before the day's price sync declares everything stale.

    The whole universe is one day behind every morning until the sync lands, and a
    guard keyed on today's date would silently scan nothing at exactly that moment.
    """
    import_daily_bars(conn, series("A", TODAY - timedelta(days=60)))
    import_daily_bars(conn, series("B", TODAY - timedelta(days=60)))
    # Nothing in the database is recent, but everything agrees, so nothing is stale.
    for symbol in ("A", "B"):
        _, reason = scan_symbol(conn, symbol)
        assert reason is None


def test_the_report_names_the_stale_symbols_and_says_why(settings: Settings) -> None:
    with db.session(settings.sqlite_path) as conn:
        import_daily_bars(conn, series("FRESH", TODAY))
        import_daily_bars(conn, series("DEAD", TODAY - timedelta(days=400)))

    report = run_scan(settings, ["FRESH", "DEAD"], send_alerts=False)
    assert report.scanned == 1
    assert report.skipped_stale == [("DEAD", (TODAY - timedelta(days=400)).isoformat())]

    warning = report.warning()
    assert warning is not None
    assert "DEAD" in warning
    assert "stopped updating" in warning


# --------------------------------------------------------------------------------
# The other two skip reasons
# --------------------------------------------------------------------------------


def test_a_symbol_with_no_stored_source_is_reported_not_crashed(conn) -> None:
    found, reason = scan_symbol(conn, "NOSUCH")
    assert found is None
    assert reason == NO_SOURCE


def test_a_symbol_with_too_little_history_is_reported_separately(conn) -> None:
    """Distinct from stale: this one needs a sync, that one needs removing."""
    import_daily_bars(conn, series("NEW", TODAY, count=10))
    found, reason = scan_symbol(conn, "NEW", asof=TODAY)
    assert found is None
    assert reason == TOO_SHORT


# --------------------------------------------------------------------------------
# Suppression and delivery
# --------------------------------------------------------------------------------


def signal(symbol: str = "SPY", session: date = TODAY, severity_kind=None) -> Signal:
    return Signal(
        symbol=symbol,
        kind=severity_kind or SignalKind.LEVEL_BREAK,
        session=session,
        message="broke a level",
        price=100.0,
    )


def test_a_signal_is_delivered_once_per_session(conn) -> None:
    sink = RecordingSink()
    first = raise_signals(conn, [signal()], [sink])
    second = raise_signals(conn, [signal()], [sink])
    assert len(first) == 1
    assert second == []
    assert len(sink.sent) == 1


def test_the_same_condition_on_a_later_day_is_a_new_alert(conn) -> None:
    """A break in March and another in July are two events, not a repeat. This is the
    whole reason signals do not reuse the position alert table's key."""
    sink = RecordingSink()
    raise_signals(conn, [signal(session=TODAY)], [sink])
    later = raise_signals(conn, [signal(session=TODAY + timedelta(days=1))], [sink])
    assert len(later) == 1
    assert len(sink.sent) == 2


def test_a_signal_below_the_threshold_is_not_delivered(conn) -> None:
    sink = RecordingSink()
    quiet = signal(severity_kind=SignalKind.LEVEL_APPROACH)
    assert raise_signals(conn, [quiet], [sink], min_severity=3) == []
    assert sink.sent == []
    # And it was not recorded either, so raising the threshold later still lets it fire.
    assert not store.already_signalled(conn, "SPY", "level_approach", TODAY)


def test_a_signal_no_sink_accepted_is_not_recorded(conn) -> None:
    """A webhook outage must not consume the only notification a condition will send."""

    class DeadSink(AlertSink):
        name = "dead"

        def send(self, alert: Alert) -> bool:
            return False

    assert raise_signals(conn, [signal()], [DeadSink()]) == []
    assert not store.already_signalled(conn, "SPY", "level_break", TODAY)

    # The retry, once a sink works again, still fires.
    sink = RecordingSink()
    assert len(raise_signals(conn, [signal()], [sink])) == 1


def test_a_dry_run_does_not_consume_the_suppression(settings: Settings) -> None:
    """A preview that recorded would leave the real run silent, which is the worst
    possible failure for an alerting tool: it looks like it is working."""
    with db.session(settings.sqlite_path) as conn:
        import_daily_bars(conn, series("SPY", TODAY, count=80, step=1.0))

    run_scan(settings, ["SPY"], send_alerts=False)
    with db.session(settings.sqlite_path) as conn:
        assert store.recent_signals(conn) == []


# --------------------------------------------------------------------------------
# The record
# --------------------------------------------------------------------------------


def test_recent_signals_reads_back_what_was_delivered(conn) -> None:
    raise_signals(conn, [signal()], [RecordingSink()])
    rows = store.recent_signals(conn)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "SPY"
    assert rows[0]["kind"] == "level_break"
    assert rows[0]["session"] == TODAY.isoformat()
    assert rows[0]["message"] == "broke a level"


def test_recent_signals_respects_the_since_bound(conn) -> None:
    raise_signals(conn, [signal(session=date(2026, 1, 1))], [RecordingSink()])
    raise_signals(conn, [signal(session=TODAY)], [RecordingSink()])
    assert len(store.recent_signals(conn, since=date(2026, 6, 1))) == 1
    assert len(store.recent_signals(conn)) == 2


def test_signal_counts_group_by_kind(conn) -> None:
    raise_signals(
        conn,
        [
            signal(symbol="SPY"),
            signal(symbol="QQQ"),
            signal(symbol="IWM", severity_kind=SignalKind.OVERSOLD_AT_SUPPORT),
        ],
        [RecordingSink()],
    )
    assert store.signal_counts(conn) == {"level_break": 2, "oversold_at_support": 1}


def test_a_market_alert_carries_no_position_id(conn) -> None:
    """None rather than a sentinel zero, so a reader of alerts.jsonl can tell "no
    position" from "position 0" and no foreign key points at nothing."""
    sink = RecordingSink()
    raise_signals(conn, [signal()], [sink])
    alert = sink.sent[0]
    assert alert.position_id is None
    assert "position_id" not in alert.as_dict()
    assert alert.as_dict()["symbol"] == "SPY"


def test_delivery_is_timestamped_when_it_happened(conn) -> None:
    moment = datetime(2026, 9, 4, 21, 30, tzinfo=UTC)
    raise_signals(conn, [signal()], [RecordingSink()], now=moment)
    assert store.recent_signals(conn)[0]["fired_at"] == moment.isoformat()
