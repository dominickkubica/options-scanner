"""Rate limiter tests. The clock is injected, so nothing here waits on real time."""

from __future__ import annotations

import pytest

from optscan.providers.ratelimit import (
    MILLISECOND_EPOCH_THRESHOLD,
    RateLimiter,
    epoch_to_seconds,
)


class FakeClock:
    """A monotonic clock the test drives, and a sleep that advances it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def build(per_minute: int = 60) -> tuple[RateLimiter, FakeClock]:
    clock = FakeClock()
    return RateLimiter(per_minute, clock=clock, sleep=clock.sleep), clock


def test_a_full_bucket_never_waits() -> None:
    limiter, clock = build(60)
    for _ in range(60):
        assert limiter.acquire() == 0.0
    assert clock.slept == []


def test_the_request_after_the_budget_is_spent_waits_for_one_refill() -> None:
    """60 a minute refills one token a second, so the 61st request waits about a second."""
    limiter, clock = build(60)
    for _ in range(60):
        limiter.acquire()

    waited = limiter.acquire()

    assert waited == pytest.approx(1.0)
    assert clock.slept == [pytest.approx(1.0)]


def test_tokens_accrue_while_nothing_is_asking() -> None:
    limiter, clock = build(60)
    for _ in range(60):
        limiter.acquire()
    assert limiter.available == pytest.approx(0.0)

    clock.now += 10.0

    assert limiter.available == pytest.approx(10.0)


def test_the_bucket_never_refills_past_its_capacity() -> None:
    """Otherwise an idle hour would buy an hour's worth of burst in one second."""
    limiter, clock = build(60)
    clock.now += 3600.0
    assert limiter.available == pytest.approx(60.0)


def test_the_vendor_counter_clamps_downward() -> None:
    limiter, _ = build(60)
    limiter.observe_vendor_counter(5)
    assert limiter.available == pytest.approx(5.0)


def test_the_vendor_counter_is_never_permission_to_speed_up() -> None:
    """A vendor count above ours usually means their window is about to roll."""
    limiter, _ = build(60)
    for _ in range(50):
        limiter.acquire()
    assert limiter.available == pytest.approx(10.0)

    limiter.observe_vendor_counter(60)

    assert limiter.available == pytest.approx(10.0)


def test_a_missing_vendor_counter_changes_nothing() -> None:
    limiter, _ = build(60)
    limiter.observe_vendor_counter(None)
    assert limiter.available == pytest.approx(60.0)


def test_a_vendor_counter_of_zero_empties_the_bucket() -> None:
    limiter, clock = build(60)
    limiter.observe_vendor_counter(0)

    assert limiter.available == pytest.approx(0.0)
    limiter.acquire()
    assert clock.slept == [pytest.approx(1.0)]


def test_a_limit_below_one_a_minute_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        RateLimiter(0)


def test_second_and_millisecond_epochs_are_told_apart_by_magnitude() -> None:
    """Tradier names X-Ratelimit-Expiry but never states its unit."""
    seconds = 1785513600.0
    assert epoch_to_seconds(seconds) == pytest.approx(seconds)
    assert epoch_to_seconds(seconds * 1000) == pytest.approx(seconds)


def test_the_threshold_sits_between_a_plausible_date_and_its_millisecond_form() -> None:
    """A century of seconds is well under it; a year of milliseconds is well over."""
    year_2100_seconds = 4102444800.0
    assert year_2100_seconds < MILLISECOND_EPOCH_THRESHOLD
    assert MILLISECOND_EPOCH_THRESHOLD < 1785513600000.0
