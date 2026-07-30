"""IV rank and percentile, and the confidence rules that qualify them.

The confidence logic gets as much attention as the arithmetic here, because the
arithmetic is trivial and the qualification is what stops a three week history from
being read as a signal.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from optscan.analytics.ivrank import (
    LOW_CONFIDENCE_OBSERVATIONS,
    MEDIUM_CONFIDENCE_OBSERVATIONS,
    MIN_OBSERVATIONS,
    Confidence,
    assess_confidence,
    completeness_ratio,
    iv_rank,
    iv_rank_from_series,
)


def flat_history(value: float, count: int) -> list[float]:
    return [value] * count


def spread_history(low: float, high: float, count: int) -> list[float]:
    """Evenly spaced vols from low to high."""
    if count == 1:
        return [low]
    step = (high - low) / (count - 1)
    return [low + step * i for i in range(count)]


class TestRankArithmetic:
    def test_midpoint_of_the_range_is_rank_fifty(self) -> None:
        """History spans 0.10 to 0.30, today is 0.20, so rank is exactly 0.5."""
        result = iv_rank(0.20, spread_history(0.10, 0.30, 100))
        assert result.rank == pytest.approx(0.5)
        assert result.low == pytest.approx(0.10)
        assert result.high == pytest.approx(0.30)

    def test_at_the_high_is_rank_one(self) -> None:
        assert iv_rank(0.30, spread_history(0.10, 0.30, 100)).rank == pytest.approx(1.0)

    def test_above_the_high_is_clamped(self) -> None:
        """A new high is rank 1, not rank 1.4."""
        assert iv_rank(0.45, spread_history(0.10, 0.30, 100)).rank == 1.0

    def test_below_the_low_is_clamped(self) -> None:
        assert iv_rank(0.02, spread_history(0.10, 0.30, 100)).rank == 0.0

    def test_percentile_counts_days_below(self) -> None:
        """25 of 100 observations sit below 0.15 on a 0.10 to 0.30 ladder."""
        result = iv_rank(0.15, spread_history(0.10, 0.30, 100))
        assert result.percentile == pytest.approx(0.25, abs=0.01)

    def test_rank_and_percentile_disagree_when_an_outlier_is_present(self) -> None:
        """The reason both are reported.

        Ninety days near 0.20 and one spike to 0.90. Today at 0.25 is above almost
        every observation, so the percentile is high, while the range is dominated by
        the spike, so the rank is low. Only reporting rank would hide that vol is
        near the top of where it normally sits.
        """
        history = [*flat_history(0.20, 90), 0.90]
        result = iv_rank(0.25, history)
        assert result.percentile > 0.98
        assert result.rank is not None and result.rank < 0.1

    def test_a_flat_history_has_a_percentile_but_no_rank(self) -> None:
        """Zero range makes a rank undefined. A percentile is still meaningful."""
        result = iv_rank(0.25, flat_history(0.20, 60))
        assert result.rank is None
        assert result.percentile == 1.0

    def test_median_is_reported(self) -> None:
        result = iv_rank(0.20, spread_history(0.10, 0.30, 101))
        assert result.median == pytest.approx(0.20, abs=1e-9)

    def test_zero_and_negative_vols_are_dropped_from_history(self) -> None:
        history = [*flat_history(0.20, 60), 0.0, -1.0]
        assert iv_rank(0.25, history).observations == 60

    def test_current_iv_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            iv_rank(0.0, spread_history(0.10, 0.30, 60))


class TestConfidence:
    def test_too_little_history_publishes_nothing(self) -> None:
        """Not a low confidence number. No number."""
        result = iv_rank(0.25, flat_history(0.20, MIN_OBSERVATIONS - 1))
        assert result.confidence is Confidence.INSUFFICIENT
        assert result.rank is None
        assert result.percentile is None
        assert not result.usable

    def test_the_thresholds(self) -> None:
        assert assess_confidence(MIN_OBSERVATIONS - 1, 1.0) is Confidence.INSUFFICIENT
        assert assess_confidence(MIN_OBSERVATIONS, 1.0) is Confidence.LOW
        assert assess_confidence(LOW_CONFIDENCE_OBSERVATIONS, 1.0) is Confidence.MEDIUM
        assert assess_confidence(MEDIUM_CONFIDENCE_OBSERVATIONS, 1.0) is Confidence.HIGH

    def test_gaps_downgrade_confidence(self) -> None:
        """A year of observations with holes is not a year of coverage.

        The days the snapshot job fails are not random days: vendors struggle when
        markets are busy, so the missing observations are biased toward high vol.
        """
        assert assess_confidence(MEDIUM_CONFIDENCE_OBSERVATIONS, 1.0) is Confidence.HIGH
        assert assess_confidence(MEDIUM_CONFIDENCE_OBSERVATIONS, 0.5) is Confidence.MEDIUM
        assert assess_confidence(LOW_CONFIDENCE_OBSERVATIONS, 0.5) is Confidence.LOW

    def test_low_cannot_be_downgraded_further(self) -> None:
        assert assess_confidence(MIN_OBSERVATIONS, 0.1) is Confidence.LOW

    def test_caveat_text_exists_where_it_should(self) -> None:
        assert iv_rank(0.25, flat_history(0.2, 10)).caveat() is not None
        assert iv_rank(0.25, spread_history(0.1, 0.3, 30)).caveat() is not None
        assert iv_rank(0.25, spread_history(0.1, 0.3, 100)).caveat() is not None
        full = iv_rank(0.25, spread_history(0.1, 0.3, 250), span_days=360)
        assert full.confidence is Confidence.HIGH
        assert full.caveat() is None


class TestCompleteness:
    def test_a_full_window(self) -> None:
        """252 observations over 365 calendar days is complete coverage."""
        assert completeness_ratio(252, 365) == pytest.approx(1.0, abs=0.01)

    def test_half_a_window(self) -> None:
        assert completeness_ratio(126, 365) == pytest.approx(0.5, abs=0.01)

    def test_capped_at_one(self) -> None:
        """Recaptures on the same day are not extra coverage."""
        assert completeness_ratio(500, 365) == 1.0

    def test_no_span(self) -> None:
        assert completeness_ratio(0, 0) == 0.0
        assert completeness_ratio(5, 0) == 1.0


class TestDatedSeries:
    def _series(self, days: int, value: float = 0.20, end: date | None = None):
        end = end or date(2026, 7, 30)
        return [(end - timedelta(days=i), value + i * 0.001) for i in range(days)]

    def test_uses_only_the_lookback_window(self) -> None:
        history = self._series(500)
        result = iv_rank_from_series(0.25, history, lookback_days=365)
        assert result.observations <= 366
        assert result.span_days <= 365

    def test_span_reflects_the_dates_not_the_count(self) -> None:
        """Twenty five observations scattered over a year read as sparse, not as
        twenty five consecutive days."""
        end = date(2026, 7, 30)
        sparse = [(end - timedelta(days=i * 14), 0.20 + i * 0.002) for i in range(25)]
        result = iv_rank_from_series(0.25, sparse, asof=end)
        assert result.span_days > 300
        assert result.completeness < 0.2
        assert result.confidence is Confidence.LOW

    def test_dense_recent_history_reads_as_complete(self) -> None:
        end = date(2026, 7, 30)
        dense = [(end - timedelta(days=i), 0.20 + i * 0.001) for i in range(70)]
        result = iv_rank_from_series(0.25, dense, asof=end)
        assert result.completeness > 0.9
        assert result.confidence is Confidence.MEDIUM

    def test_empty_history(self) -> None:
        result = iv_rank_from_series(0.25, [])
        assert result.confidence is Confidence.INSUFFICIENT
        assert result.observations == 0

    def test_future_dates_are_ignored(self) -> None:
        end = date(2026, 7, 30)
        history = [(end + timedelta(days=5), 0.5), *self._series(60, end=end)]
        result = iv_rank_from_series(0.25, history, asof=end)
        assert result.observations == 60
