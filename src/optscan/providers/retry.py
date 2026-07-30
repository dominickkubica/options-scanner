"""Retry with exponential backoff, for provider calls only.

Deliberately hand written rather than pulled from a library: the decision of what is
worth retrying is domain logic, not plumbing. Only errors that declare themselves
retryable are retried, and a vendor supplied Retry-After always wins over our backoff.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

from optscan.logging import get_logger
from optscan.providers.errors import ProviderError, RateLimited

log = get_logger("optscan.providers.retry")


def compute_delay(
    attempt: int,
    *,
    base_delay: float,
    max_delay: float,
    jitter: float = 0.0,
) -> float:
    """Delay before the given attempt. attempt is 1 based, so attempt 1 waits base_delay.

    jitter is a fraction of the computed delay, added not subtracted, so backoff never
    gets shorter than intended.
    """
    if attempt < 1:
        raise ValueError("attempt is 1 based")
    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
    return delay * (1.0 + jitter)


def with_retry[T](
    operation: Callable[[], T],
    *,
    attempts: int,
    base_delay: float,
    max_delay: float = 60.0,
    description: str = "provider call",
    sleep: Callable[[float], None] = time.sleep,
    rng: Callable[[], float] = random.random,
) -> T:
    """Call operation, retrying retryable ProviderErrors up to `attempts` times total.

    Anything that is not a ProviderError propagates immediately: an unexpected
    exception is a bug in the adapter, and retrying a bug just makes it slower to find.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    last_error: ProviderError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except ProviderError as error:
            last_error = error
            if not error.retryable or attempt == attempts:
                raise
            delay = compute_delay(
                attempt,
                base_delay=base_delay,
                max_delay=max_delay,
                jitter=rng() * 0.25,
            )
            if isinstance(error, RateLimited) and error.retry_after_seconds is not None:
                delay = max(delay, error.retry_after_seconds)
            log.warning(
                "provider call failed, retrying",
                description=description,
                attempt=attempt,
                of=attempts,
                delay_seconds=round(delay, 2),
                error=str(error),
                error_type=type(error).__name__,
            )
            sleep(delay)

    # Unreachable: the loop either returns or raises. Kept so the type checker and a
    # future reader both know there is no silent None path.
    raise last_error if last_error else RuntimeError("retry loop ended without a result")
