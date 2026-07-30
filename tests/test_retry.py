"""Retry policy.

Backoff values are hand computed: base 2.0 doubling gives 2, 4, 8, 16, capped at
max_delay. Jitter is injected as a fixed value so the assertions stay exact.
"""

from __future__ import annotations

import pytest

from optscan.providers.errors import ProviderUnavailable, RateLimited, SymbolNotFound
from optscan.providers.retry import compute_delay, with_retry


class TestComputeDelay:
    def test_doubles_from_the_base(self) -> None:
        assert compute_delay(1, base_delay=2.0, max_delay=60.0) == 2.0
        assert compute_delay(2, base_delay=2.0, max_delay=60.0) == 4.0
        assert compute_delay(3, base_delay=2.0, max_delay=60.0) == 8.0
        assert compute_delay(4, base_delay=2.0, max_delay=60.0) == 16.0

    def test_respects_the_cap(self) -> None:
        assert compute_delay(10, base_delay=2.0, max_delay=30.0) == 30.0

    def test_jitter_only_lengthens(self) -> None:
        assert compute_delay(1, base_delay=2.0, max_delay=60.0, jitter=0.25) == 2.5

    def test_attempt_is_one_based(self) -> None:
        with pytest.raises(ValueError, match="1 based"):
            compute_delay(0, base_delay=2.0, max_delay=60.0)


class TestWithRetry:
    def _run(self, operation, **kwargs):
        slept: list[float] = []
        result = with_retry(
            operation,
            attempts=kwargs.pop("attempts", 3),
            base_delay=kwargs.pop("base_delay", 2.0),
            sleep=slept.append,
            rng=lambda: 0.0,
            **kwargs,
        )
        return result, slept

    def test_returns_immediately_when_it_works(self) -> None:
        result, slept = self._run(lambda: "ok")
        assert result == "ok"
        assert slept == []

    def test_retries_a_retryable_error_then_succeeds(self) -> None:
        calls = {"n": 0}

        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise ProviderUnavailable("timeout")
            return "ok"

        result, slept = self._run(flaky)
        assert result == "ok"
        assert calls["n"] == 3
        assert slept == [2.0, 4.0]

    def test_does_not_retry_a_permanent_error(self) -> None:
        calls = {"n": 0}

        def missing():
            calls["n"] += 1
            raise SymbolNotFound("no such ticker")

        with pytest.raises(SymbolNotFound):
            self._run(missing)
        assert calls["n"] == 1

    def test_gives_up_after_the_last_attempt(self) -> None:
        calls = {"n": 0}

        def always_down():
            calls["n"] += 1
            raise ProviderUnavailable("down")

        with pytest.raises(ProviderUnavailable):
            self._run(always_down, attempts=3)
        assert calls["n"] == 3

    def test_vendor_retry_after_beats_our_backoff(self) -> None:
        calls = {"n": 0}

        def throttled() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RateLimited("slow down", retry_after_seconds=45.0)
            return "ok"

        result, slept = self._run(throttled)
        assert result == "ok"
        assert slept == [45.0]  # not the 2.0 our backoff would have chosen

    def test_unexpected_exceptions_are_not_retried(self) -> None:
        """A bug in an adapter should surface, not be papered over by three attempts."""
        calls = {"n": 0}

        def broken():
            calls["n"] += 1
            raise KeyError("bug")

        with pytest.raises(KeyError):
            self._run(broken)
        assert calls["n"] == 1

    def test_attempts_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            with_retry(lambda: None, attempts=0, base_delay=1.0)
