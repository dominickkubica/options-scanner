"""Request budgeting for vendors that publish a limit.

Separate from retry.py because the two solve opposite problems. Retry reacts after a
call has already failed; this stops the call being made. A rate limiter that only
existed as a 429 handler would still burn the budget it was meant to protect, and on
a per minute limit that means a minute of dead time for every burst.

Two sources of truth, and the vendor's wins:

- A local token bucket, sized from the documented limit. It is what makes the first
  request of a burst wait rather than fail.
- The vendor's own counter, read off the response headers. Our bucket cannot know
  about requests made by another process holding the same token, or about a window
  that started before this one did, so when the vendor says fewer remain than we
  think, we believe the vendor.

The clock is injected so the tests are exact rather than slow. Nothing here sleeps in
a test.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from optscan.logging import get_logger

log = get_logger("optscan.providers.ratelimit")

SECONDS_PER_MINUTE = 60.0

#: Above this many milliseconds an epoch value cannot be seconds: it would be a date
#: in the year 5138. Vendors are inconsistent about the unit and Tradier's docs do not
#: state one for X-Ratelimit-Expiry, so it is detected rather than assumed.
MILLISECOND_EPOCH_THRESHOLD = 1e11


class RateLimiter:
    """Token bucket sized from a documented per minute limit.

    Thread safe, because the live poller and a request thread can both be inside a
    provider at once, and two threads that each think they have the last token is
    exactly the case a limiter exists to prevent.
    """

    def __init__(
        self,
        per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if per_minute < 1:
            raise ValueError("per_minute must be at least 1")
        self._capacity = float(per_minute)
        self._refill_per_second = per_minute / SECONDS_PER_MINUTE
        self._tokens = float(per_minute)
        self._clock = clock
        self._sleep = sleep
        self._updated_at = clock()
        self._lock = threading.Lock()

    @property
    def available(self) -> float:
        """Tokens available right now, refilled to the current instant."""
        with self._lock:
            self._refill()
            return self._tokens

    def _refill(self) -> None:
        """Add the tokens that accrued since the last look. Caller holds the lock."""
        now = self._clock()
        elapsed = now - self._updated_at
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_per_second)
            self._updated_at = now

    def acquire(self) -> float:
        """Take one token, waiting if there is none. Returns the seconds waited.

        The wait is a real sleep on the calling thread. That is the intended behaviour
        for a polling loop and for a request handler alike: slowing down is the point,
        and a caller that would rather fail than wait should check `available` first.
        """
        with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return 0.0
            deficit = 1.0 - self._tokens
            wait = deficit / self._refill_per_second

        self._sleep(wait)

        with self._lock:
            self._refill()
            # Take the token even if the refill landed a hair short. Recomputing the
            # wait here would let a thread that keeps losing the race starve, and the
            # overdraft is at most one request against a per minute window.
            self._tokens = max(0.0, self._tokens - 1.0)
        return wait

    def observe_vendor_counter(self, available: int | None) -> None:
        """Clamp the bucket down to what the vendor says is left.

        Only ever downward. A vendor counter that reads higher than our bucket is not
        permission to speed up: it usually means their window is about to roll, and
        spending against the new window before it opens is how a burst turns into a
        429 on the first call of the next minute.
        """
        if available is None:
            return
        with self._lock:
            self._refill()
            if available < self._tokens:
                log.debug(
                    "vendor counter is lower than the local bucket",
                    vendor_available=available,
                    local_available=round(self._tokens, 2),
                )
                self._tokens = float(max(available, 0))


def epoch_to_seconds(value: float) -> float:
    """Normalize an epoch timestamp that may be in seconds or milliseconds.

    Vendors disagree and Tradier's rate limit documentation names the header without
    giving a unit. Anything past the threshold cannot be a plausible date in seconds,
    so the unit is inferred from magnitude instead of assumed. Being wrong in the safe
    direction matters here: treating milliseconds as seconds would put the window
    expiry thousands of years out and disable the backoff entirely.
    """
    return value / 1000.0 if abs(value) >= MILLISECOND_EPOCH_THRESHOLD else value
