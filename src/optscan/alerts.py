"""Delivering a triggered alert somewhere a person will see it.

Two things this module is careful about, and neither is the delivery.

**An alert fires once per condition, not once per poll.** The suppression lives in the
database rather than in memory, so it survives a restart. This is the difference
between a tool that is trusted and one that is muted, and a muted alerting tool is
worse than no alerting tool because it is still believed to be working.

**A sink that fails must not lose the alert quietly.** Every send returns whether it
worked, a failure is logged with the reason, and the fired-at record is only written
when at least one sink accepted it. Otherwise a webhook outage would silently mark
everything as delivered and the condition would never fire again.

## Why the default sink writes to a file and a log

The obvious sinks all need an account: a Telegram bot token, a Discord webhook, an SMTP
login. Those are supported through `WebhookSink`, and none of them can be the default,
because a default that cannot run until somebody registers somewhere is a feature that
does not work out of the box. So the default is a JSONL file next to the database plus
a structured log line, both of which work on a fresh clone with no configuration and
no network.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from optscan.analytics.signals import Signal
from optscan.analytics.triggers import Trigger
from optscan.logging import get_logger

log = get_logger("optscan.alerts")

DEFAULT_ALERT_FILENAME = "alerts.jsonl"

#: Severity at or above which a trigger is worth interrupting somebody for. Below it
#: the trigger still shows in the dashboard, it just does not chase them.
DEFAULT_MIN_SEVERITY = 3

#: Anything at or above this status is a rejection rather than a delivery.
HTTP_BAD_REQUEST = 400


#: What a sink needs from the thing being announced. `Trigger` and `Signal` both satisfy
#: it structurally, which is the whole reason the sinks did not have to be duplicated:
#: a webhook that can post "your short strike was tested" can post "SPY broke a level"
#: without knowing there are two kinds of thing.
Notice = Trigger | Signal


@dataclass(frozen=True, slots=True)
class Alert:
    """One notification, ready to deliver.

    `position_id` is None for a market signal, which belongs to a symbol rather than to
    anything held. It is a real absence rather than a sentinel zero: a reader of
    alerts.jsonl can tell "no position" from "position 0", and a sentinel would have
    meant a foreign key pointing at nothing.
    """

    symbol: str
    trigger: Notice
    raised_at: datetime
    position_id: int | None = None

    @property
    def kind(self) -> str:
        return str(self.trigger.kind)

    @property
    def title(self) -> str:
        return f"{self.symbol}: {self.trigger.kind.value.replace('_', ' ')}"

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "symbol": self.symbol,
            "kind": self.kind,
            "severity": self.trigger.severity,
            "message": self.trigger.message,
            "value": self.trigger.value,
            "threshold": self.trigger.threshold,
            "raised_at": self.raised_at.isoformat(),
        }
        # Omitted rather than null for a market signal, so the position alerts already
        # written to alerts.jsonl keep exactly the shape they had.
        if self.position_id is not None:
            payload["position_id"] = self.position_id
        return payload


class AlertSink(ABC):
    """Somewhere an alert can be delivered."""

    name: str = "sink"

    @abstractmethod
    def send(self, alert: Alert) -> bool:
        """Deliver one alert. Returns whether it was accepted.

        Must not raise. A sink that throws would abort the whole management run, and
        the run is more important than any one notification.
        """


class LogSink(AlertSink):
    """A structured log line. Always available, needs nothing."""

    name = "log"

    def send(self, alert: Alert) -> bool:
        log.warning(
            "position alert" if alert.position_id is not None else "market alert",
            position_id=alert.position_id,
            symbol=alert.symbol,
            kind=alert.kind,
            severity=alert.trigger.severity,
            message=alert.trigger.message,
        )
        return True


class FileSink(AlertSink):
    """Append one JSON object per line, next to the database.

    JSONL rather than a log file because it is meant to be read back: it is the record
    of what this tool told you and when, which is the thing Phase 8 will want when it
    asks whether any of these triggers were worth acting on.
    """

    name = "file"

    def __init__(self, path: Path) -> None:
        self.path = path

    def send(self, alert: Alert) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(alert.as_dict(), separators=(",", ":")) + "\n")
        except OSError as error:
            log.error("alert file write failed", path=str(self.path), error=str(error))
            return False
        return True


class WebhookSink(AlertSink):
    """POST the alert as JSON to a URL. Works with Discord, Slack, and anything else
    that accepts a JSON body.

    Unconfigured and untested against a real endpoint: this project has no webhook to
    try, and claiming otherwise would be the kind of unverified assertion it refuses
    elsewhere. The shape is deliberately plain rather than tailored to one vendor's
    payload format, so pointing it at Discord will need their `content` key.
    """

    name = "webhook"

    def __init__(self, url: str, timeout: float = 5.0) -> None:
        self.url = url
        self.timeout = timeout

    def send(self, alert: Alert) -> bool:
        # Imported here rather than at module scope so that a person who never
        # configures a webhook does not pay for the import, and so this module stays
        # usable if the HTTP client is ever moved behind the provider boundary.
        import httpx  # noqa: TID251, PLC0415

        try:
            response = httpx.post(self.url, json=alert.as_dict(), timeout=self.timeout)
        except httpx.HTTPError as error:
            log.error("alert webhook failed", url=self.url, error=str(error))
            return False
        if response.status_code >= HTTP_BAD_REQUEST:
            log.error("alert webhook rejected", url=self.url, status=response.status_code)
            return False
        return True


def default_sinks(data_path: Path) -> list[AlertSink]:
    """What runs with no configuration: a log line and a JSONL file."""
    return [LogSink(), FileSink(data_path / DEFAULT_ALERT_FILENAME)]


def deliver(
    alert: Alert,
    sinks: Sequence[AlertSink],
) -> bool:
    """Send to every sink. Returns True if at least one accepted it.

    Every sink is tried even after one fails, because they are alternatives rather than
    a chain: the point of configuring two is that one of them working is enough.
    """
    accepted = False
    for sink in sinks:
        try:
            accepted = sink.send(alert) or accepted
        except Exception as error:  # a sink must not take the run down with it
            log.exception("alert sink raised", sink=sink.name, error=str(error))
    return accepted


def raise_alerts(
    conn,
    position_id: int,
    symbol: str,
    triggers: Sequence[Trigger],
    sinks: Sequence[AlertSink],
    *,
    min_severity: int = DEFAULT_MIN_SEVERITY,
    now: datetime | None = None,
) -> list[Alert]:
    """Deliver the triggers worth interrupting somebody for, once each.

    Returns the alerts that were actually delivered. Anything already recorded for this
    position and kind is skipped silently, which is the normal case on every poll after
    the first.
    """
    from optscan.storage import positions as store  # noqa: PLC0415

    raised: list[Alert] = []
    moment = now or datetime.now(UTC)

    for trigger in triggers:
        if trigger.severity < min_severity:
            continue
        if store.already_alerted(conn, position_id, str(trigger.kind)):
            continue

        alert = Alert(
            position_id=position_id,
            symbol=symbol,
            trigger=trigger,
            raised_at=moment,
        )
        if not deliver(alert, sinks):
            # Not recorded, so it will be retried on the next run. A webhook outage
            # must not consume the only notification this condition will ever send.
            log.error("no sink accepted the alert", position_id=position_id, kind=alert.kind)
            continue

        store.record_alert(conn, position_id, str(trigger.kind), trigger.message)
        raised.append(alert)

    return raised


def raise_signals(
    conn,
    signals: Sequence[Signal],
    sinks: Sequence[AlertSink],
    *,
    min_severity: int = DEFAULT_MIN_SEVERITY,
    now: datetime | None = None,
) -> list[Alert]:
    """Deliver the market signals worth interrupting somebody for, once each per session.

    The same shape as `raise_alerts` and the same two rules: suppression is recorded in
    the database rather than in memory so it survives a restart, and the record is only
    written once a sink has accepted, so a webhook outage does not silently consume the
    only notification a condition will ever send.

    What differs is the key. A position trigger fires once for the life of the position;
    a market signal fires once per symbol, kind and session, because the same break next
    month is a second event rather than a repeat.
    """
    from optscan.storage import signals as store  # noqa: PLC0415

    raised: list[Alert] = []
    moment = now or datetime.now(UTC)

    for signal in signals:
        if signal.severity < min_severity:
            continue
        if store.already_signalled(conn, signal.symbol, signal.kind.value, signal.session):
            continue

        alert = Alert(symbol=signal.symbol, trigger=signal, raised_at=moment)
        if not deliver(alert, sinks):
            log.error("no sink accepted the signal", symbol=signal.symbol, kind=alert.kind)
            continue

        store.record_signal(conn, signal, now=moment)
        raised.append(alert)

    return raised
