"""Shared model base.

Every record that came from outside this process carries when it was fetched and
which provider produced it. The provider matters as much as the timestamp: an IV
history that silently mixes vendors is corrupt in a way that is very hard to see later.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict


def _require_utc(value: datetime) -> datetime:
    """Reject naive timestamps outright rather than guessing a timezone."""
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone aware, got a naive datetime")
    return value.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(_require_utc)]


class Record(BaseModel):
    """Immutable, strictly validated, and always attributable to a source and a time."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )

    fetched_at: UtcDatetime
    source: str

    def age_seconds(self, now: datetime | None = None) -> float:
        """Seconds since this record was fetched. Callers decide what counts as stale."""
        reference = now or datetime.now(UTC)
        if reference.tzinfo is None:
            raise ValueError("now must be timezone aware")
        return (reference - self.fetched_at).total_seconds()
