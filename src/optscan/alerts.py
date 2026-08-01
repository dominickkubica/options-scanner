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

from optscan.analytics.triggers import Trigger
from optscan.logging import get_logger

log = get_logger("optscan.alerts")

DEFAULT_ALERT_FILENAME = "alerts.jsonl"

#: Severity at or above which a trigger is worth interrupting somebody for. Below it
#: the trigger still shows in the dashboard, it just does not chase them.
DEFAULT_MIN_SEVERITY = 3

#: Anything at or above this status is a rejection rather than a delivery.
HTTP_BAD_REQUEST = 400


@dataclass(frozen=True, slots=True)
class Alert:
    """One notification, ready to deliver."""

    position_id: int
    symbol: str
    trigger: Trigger
    raised_at: datetime

    @property
    def kind(self) -> str:
        return str(self.trigger.kind)

    @property
    def title(self) -> str:
        return f"{self.symbol}: {self.trigger.kind.value.replace('_', ' ')}"

    def as_dict(self) -> dict[str, object]:
        return {
            "position_id": self.position_id,
            "symbol": self.symbol,
            "kind": self.kind,
            "severity": self.trigger.severity,
            "message": self.trigger.message,
            "value": self.trigger.value,
            "threshold": self.trigger.threshold,
            "raised_at": self.raised_at.isoformat(),
        }


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
            "position alert",
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
