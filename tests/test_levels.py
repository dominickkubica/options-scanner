"""Price levels, tested against the real captured SPY bars.

The hand-verified values are computed on tiny synthetic bar sets where the arithmetic
can be checked by eye. The real fixture is used for the question that actually decides
whether this module is worth shipping: how much does each detector fire, and does it
fire more than chance explains.

That second class of test is the point of the file. Support and resistance is the
easiest thing here to build so that it flags everything, and a test suite that only
checked the arithmetic would pass happily while the module drew a hairball.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from optscan.analytics.levels import (
    DEFAULT_MAX_P_VALUE,
    BollingerBands,
    LevelKind,
    atr,
    bollinger_bands,
    bracketing_levels,
    build_levels,
    candidate_levels,
    cluster_pivots,
    levels_near,
    moving_average,
    occupancy_share,
    realized_volatility,
    round_numbers,
    support_resistance,
    swing_pivots,
    touch_p_value,
    true_ranges,
    volume_levels,
    volume_profile,
)
from optscan.models import PriceBar

FIXTURE = Path(__file__).parent / "fixtures" / "spy_daily_bars.json"

BASE = datetime(2026, 1, 5, tzinfo=UTC)


def bar(
    close: float,
    *,
    high: float | None = None,
    low: float | None = None,
    open_: float | None = None,
    volume: int = 1_000,
    day: int = 0,
) -> PriceBar:
    """A bar with sensible defaults, so a test only states what it cares about."""
    high = close if high is None else high
    low = close if low is None else low
    return PriceBar(
        symbol="TEST",
        ts=BASE + timedelta(days=day),
        open=close if open_ is None else open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        fetched_at=BASE,
        source="test",
    )


def ramp(prices: list[float], spread: float = 1.0) -> list[PriceBar]:
    """A series where each bar's range straddles its close by `spread`."""
    return [
        bar(price, high=price + spread, low=price - spread, day=index)
        for index, price in enumerate(prices)
    ]


@pytest.fixture(scope="session")
def spy_bars() -> list[PriceBar]:
    """The real capture: 1100 SPY sessions, 2022 to 2026."""
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [
        PriceBar(
            symbol=payload["symbol"],
            ts=datetime.fromisoformat(row["ts"]),
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            volume=row["volume"],
            fetched_at=datetime.fromisoformat(payload["captured_at"]),
            source=payload["source"],
        )
        for row in payload["bars"]
    ]


class TestIndicators:
    def test_true_range_takes_the_gap_into_account(self) -> None:
        """A quiet bar that gapped has a small range and a large true range, and only
        the second describes the risk that was taken."""
        bars = [bar(100.0, high=101.0, low=99.0), bar(110.0, high=110.5, low=109.5, day=1)]
        # Today's range is 1.0; today's high against yesterday's close is 10.5.
        assert true_ranges(bars) == [pytest.approx(10.5)]

    def test_atr_matches_a_hand_computed_wilder_average(self) -> None:
        """Five bars each with a true range of exactly 2.0, period 3.

        Seed is the mean of the first three, which is 2.0, and every update feeds in
        another 2.0, so the answer stays 2.0 no matter how many bars follow.
        """
        bars = [
            bar(100.0 + index, high=101.0 + index, low=99.0 + index, day=index)
            for index in range(6)
        ]
        ranges = true_ranges(bars)
        assert ranges == [pytest.approx(2.0)] * 5
        assert atr(bars, period=3) == pytest.approx(2.0)

    def test_atr_refuses_rather_than_averaging_what_it_has(self) -> None:
        """A 14 day ATR from four bars is not a 14 day ATR, and every threshold in the
        module is a multiple of it."""
        assert atr(ramp([100.0, 101.0, 102.0]), period=14) is None

    def test_moving_average_is_the_mean_of_the_last_n_closes(self) -> None:
        bars = ramp([10.0, 20.0, 30.0, 40.0])
        assert moving_average(bars, 2) == pytest.approx(35.0)
        assert moving_average(bars, 4) == pytest.approx(25.0)
        assert moving_average(bars, 5) is None

    def test_bollinger_uses_the_population_deviation(self) -> None:
        """Closes 1..5 have a population deviation of sqrt(2) and a sample deviation of
        sqrt(2.5). Charting packages compute the first, and the difference moves the
        band visibly."""
        bands = bollinger_bands(ramp([1.0, 2.0, 3.0, 4.0, 5.0]), period=5, deviations=1.0)
        assert isinstance(bands, BollingerBands)
        assert bands.middle == pytest.approx(3.0)
        assert bands.upper == pytest.approx(3.0 + 2.0**0.5)
        assert bands.lower == pytest.approx(3.0 - 2.0**0.5)

    def test_realized_vol_of_a_constant_series_is_zero(self) -> None:
        assert realized_volatility(ramp([100.0] * 40), window=30) == pytest.approx(0.0)

    def test_realized_vol_annualizes_by_the_root_of_trading_days(self) -> None:
        """A series alternating +1 percent and -1 percent in log terms has a daily
        deviation of almost exactly 0.01, so annualized it is 0.01 * sqrt(252)."""
        import math

        prices = [100.0 * math.exp(0.01 if index % 2 else -0.01) for index in range(41)]
        levels = [100.0]
        for index in range(40):
            levels.append(levels[-1] * math.exp(0.01 if index % 2 else -0.01))
        value = realized_volatility(ramp(levels), window=40)
        assert value == pytest.approx(0.01 * math.sqrt(252), rel=0.05)
        assert prices  # the alternating construction is the point, kept for the reader

    def test_realized_vol_refuses_a_window_it_cannot_fill(self) -> None:
        assert realized_volatility(ramp([100.0] * 5), window=30) is None


class TestSwingPivots:
    def test_a_clear_peak_is_found(self) -> None:
        prices = [10.0, 11.0, 12.0, 13.0, 14.0, 20.0, 14.0, 13.0, 12.0, 11.0, 10.0]
        pivots = swing_pivots(ramp(prices, spread=0.1), lookback=5)
        highs = [pivot for pivot in pivots if pivot.high]
        assert len(highs) == 1
        assert highs[0].price == pytest.approx(20.1)

    def test_a_flat_series_has_no_pivots_at_all(self) -> None:
        """The degenerate case that has to be right. With ties counted as extremes,
        every bar of a flat stretch is both a swing high and a swing low, and the
        module manufactures a wall of levels out of a market doing nothing."""
        assert swing_pivots(ramp([100.0] * 30, spread=0.5), lookback=5) == []

    def test_prominence_is_the_depth_of_the_swing_not_the_height_of_the_bar(self) -> None:
        """Topographic prominence: the drop to the higher of the two surrounding
        troughs. Both sides of this window bottom at 10, so a peak of 20 has a
        prominence of 10 rather than the 6 you would get by measuring to the
        neighbouring bar."""
        prices = [10.0, 11.0, 12.0, 13.0, 14.0, 20.0, 14.0, 13.0, 12.0, 11.0, 10.0]
        pivots = swing_pivots(ramp(prices, spread=0.0), lookback=5)
        peak = next(pivot for pivot in pivots if pivot.high)
        assert peak.prominence == pytest.approx(10.0)


class TestClustering:
    def test_nearby_pivots_become_one_level_with_a_touch_count(self) -> None:
        prices = [100.0, 100.4, 100.2]
        pivots = [
            swing_pivots(
                ramp([90.0, 95.0, 98.0, 99.0, 99.5, price, 99.5, 99.0, 98.0, 95.0, 90.0], 0.0), 5
            )[0]
            for price in prices
        ]
        levels = cluster_pivots(pivots, tolerance=1.0, min_touches=2)
        assert len(levels) == 1
        assert levels[0].touches == 3
        assert levels[0].price == pytest.approx(100.2, abs=0.05)

    def test_a_single_touch_is_not_a_level(self) -> None:
        """One pivot is a place price turned once, which is not what support means."""
        pivots = swing_pivots(
            ramp([90.0, 95.0, 98.0, 99.0, 99.5, 120.0, 99.5, 99.0, 98.0, 95.0, 90.0], 0.0), 5
        )
        assert cluster_pivots(pivots, tolerance=1.0, min_touches=2) == []


class TestSignificance:
    """The baseline that stops touch count being read as evidence when it is density."""

    def test_occupancy_is_the_share_of_sessions_overlapping_a_band(self) -> None:
        bars = ramp([100.0, 100.0, 200.0, 200.0], spread=1.0)
        # Two of four bars span 99 to 101, and each covers the band completely.
        assert occupancy_share(bars, 99.0, 101.0) == pytest.approx(0.5)

    def test_occupancy_counts_a_straddling_bar_partially(self) -> None:
        """A bar half inside the band contributes half, so a level on the edge of a
        congestion zone is not credited with the whole zone."""
        bars = [bar(100.0, high=102.0, low=98.0)]
        assert occupancy_share(bars, 98.0, 100.0) == pytest.approx(0.5)

    def test_a_touch_count_matching_expectation_is_not_significant(self) -> None:
        """The whole point. Two touches where two were expected is not evidence."""
        assert touch_p_value(2, expected=2.0) > 0.5

    def test_a_touch_count_far_above_expectation_is_significant(self) -> None:
        assert touch_p_value(7, expected=1.5) < 0.01

    def test_p_value_is_hand_checkable_against_the_poisson_tail(self) -> None:
        """P(X >= 1 | lambda = 1) = 1 - e^-1 = 0.6321."""
        import math

        assert touch_p_value(1, expected=1.0) == pytest.approx(1.0 - math.exp(-1.0), abs=1e-6)


class TestVolumeProfile:
    def test_volume_lands_in_the_bins_the_bar_actually_spanned(self) -> None:
        bars = [
            bar(10.0, high=11.0, low=9.0, volume=100),
            bar(20.0, high=21.0, low=19.0, volume=900),
        ]
        profile = volume_profile(bars, bins=10)
        assert profile is not None
        # The busier bar is up near 20, so the point of control has to be up there.
        assert profile.point_of_control > 15.0

    def test_the_value_area_holds_the_requested_share_of_volume(self) -> None:
        bars = [
            bar(
                100.0 + index % 5,
                high=101.0 + index % 5,
                low=99.0 + index % 5,
                volume=1000,
                day=index,
            )
            for index in range(50)
        ]
        profile = volume_profile(bars, bins=20, value_area_share=0.7)
        assert profile is not None
        assert profile.value_area_low < profile.point_of_control < profile.value_area_high

    def test_no_volume_means_no_profile_rather_than_an_empty_one(self) -> None:
        bars = [bar(100.0, high=101.0, low=99.0, volume=0, day=index) for index in range(10)]
        assert volume_profile(bars) is None

    def test_nodes_must_clear_a_share_of_the_busiest_bin(self) -> None:
        """Every bump in a sixty bin histogram is a local maximum. Most are binning
        noise, and without the share test they all get drawn.

        Three volume humps, the tallest at 100 and two smaller ones at 120 and 140. A
        loose share threshold draws all three; a strict one keeps only the tallest.
        """
        bars: list[PriceBar] = []
        day = 0
        for centre, volume in ((100.0, 5_000), (120.0, 1_200), (140.0, 1_000)):
            for _ in range(10):
                bars.append(
                    bar(centre, high=centre + 1.0, low=centre - 1.0, volume=volume, day=day)
                )
                day += 1

        profile = volume_profile(bars, bins=60)
        strict = volume_levels(profile, min_share=0.9)
        loose = volume_levels(profile, min_share=0.1)
        assert len(strict) < len(loose)


class TestRoundNumbers:
    def test_the_step_scales_with_the_price(self) -> None:
        """One rule has to work on a 30 dollar name and a 740 dollar index."""
        cheap = round_numbers(28.0, 32.0, spot=30.0)
        rich = round_numbers(700.0, 760.0, spot=740.0)
        assert cheap and rich
        cheap_step = cheap[1].price - cheap[0].price
        rich_step = rich[1].price - rich[0].price
        assert cheap_step < rich_step

    def test_lines_are_actually_round(self) -> None:
        for level in round_numbers(700.0, 760.0, spot=740.0):
            assert level.price % 10 == pytest.approx(0.0)

    def test_they_carry_a_constant_strength_because_nothing_was_measured(self) -> None:
        """A round number has no touch count. Inventing a score would make it look
        comparable to a level that earned one."""
        lines = round_numbers(700.0, 760.0, spot=740.0)
        assert len({level.strength for level in lines}) == 1
        assert all(level.p_value is None for level in lines)


class TestAgainstRealBars:
    """The standing check: count what fires, on real data, before believing any of it."""

    def test_the_fixture_is_the_real_capture(self, spy_bars: list[PriceBar]) -> None:
        assert len(spy_bars) == 1100
        assert spy_bars[0].ts.date().isoformat() == "2022-03-14"
        assert all(bar.source == "yfinance" for bar in spy_bars)

    def test_raw_pivots_fire_on_roughly_one_bar_in_nine(self, spy_bars: list[PriceBar]) -> None:
        """Documented because it is the trap. Drawn unfiltered this is a hairball, and
        any future change that makes it worse should fail here."""
        raw = swing_pivots(spy_bars, lookback=5)
        assert 100 <= len(raw) <= 150

    def test_prominence_in_atr_barely_discriminates(self, spy_bars: list[PriceBar]) -> None:
        """The measured reason its default is zero. An eleven bar window's depth is
        about one ATR by construction, so thresholding it in ATR selects almost
        nothing while looking like a significance filter."""
        average_range = atr(spy_bars)
        assert average_range is not None
        loose = swing_pivots(spy_bars, 5, min_prominence=0.0)
        mild = swing_pivots(spy_bars, 5, min_prominence=0.5 * average_range)
        assert len(mild) >= 0.95 * len(loose)

    def test_clustering_alone_publishes_far_too_many_levels(self, spy_bars: list[PriceBar]) -> None:
        """31 levels on four years is a hairball, and it is what touch count alone
        gives you. This is the number the significance test has to cut down."""
        candidates = candidate_levels(spy_bars)
        assert 20 <= len(candidates) <= 45

    def test_the_significance_test_removes_most_of_them(self, spy_bars: list[PriceBar]) -> None:
        """The load bearing assertion of this file. Most clustered levels are prices
        where the pivot count is exactly what time spent there predicts."""
        candidates = candidate_levels(spy_bars)
        survivors = support_resistance(spy_bars)
        assert len(survivors) < len(candidates) / 4
        assert all(level.p_value <= DEFAULT_MAX_P_VALUE for level in survivors)

    def test_survivors_beat_their_own_expected_touch_count(self, spy_bars: list[PriceBar]) -> None:
        for level in support_resistance(spy_bars):
            assert level.expected_touches is not None
            assert level.touches > level.expected_touches

    def test_a_long_window_volume_profile_finds_the_trend(self, spy_bars: list[PriceBar]) -> None:
        """The constant offset trap in its purest form, pinned so it stays visible.

        Over four years in which SPY went from 400 to 740, the point of control lands
        down where the advance began. That is a fact about the path, not a price buyers
        defend, and it is why build_levels trims the profile window.
        """
        spot = spy_bars[-1].close
        long_profile = volume_profile(spy_bars)
        short_profile = volume_profile(spy_bars[-120:])

        assert long_profile is not None and short_profile is not None
        assert long_profile.point_of_control < spot * 0.75
        assert short_profile.point_of_control > spot * 0.9

    def test_build_levels_draws_a_readable_number_of_lines(self, spy_bars: list[PriceBar]) -> None:
        """A chart is unreadable past roughly twenty horizontal lines, and that is the
        real constraint this whole module is filtering toward."""
        levels = build_levels(spy_bars)
        assert len(levels.all()) <= 20
        assert levels.sessions == 1100
        assert levels.swing_candidates > len(levels.swing)

    def test_build_levels_says_how_many_candidates_it_rejected(
        self, spy_bars: list[PriceBar]
    ) -> None:
        """An empty or short level list has to read as a finding, not as a broken job."""
        levels = build_levels(spy_bars)
        joined = " ".join(levels.notes)
        assert "swing candidates" in joined
        assert str(levels.swing_candidates) in joined

    def test_indicators_are_plausible_on_real_data(self, spy_bars: list[PriceBar]) -> None:
        levels = build_levels(spy_bars)
        spot = spy_bars[-1].close

        assert levels.atr is not None
        assert 0.003 < levels.atr / spot < 0.05
        assert levels.realized_vol is not None
        assert 0.03 < levels.realized_vol < 1.0
        assert set(levels.moving_averages) == {20, 50, 200}
        assert levels.bollinger is not None
        assert levels.bollinger.lower < levels.bollinger.middle < levels.bollinger.upper


class TestLookups:
    def test_levels_near_returns_nearest_first(self) -> None:
        lines = round_numbers(600.0, 800.0, spot=740.0)
        near = levels_near(lines, 731.0, tolerance=60.0)
        assert len(near) >= 2
        distances = [abs(level.price - 731.0) for level in near]
        assert distances == sorted(distances)

    def test_bracketing_returns_none_where_there_is_nothing(self) -> None:
        """A strike above every level has no resistance between it and wherever price
        went last time, and that is worth seeing rather than filling in."""
        lines = round_numbers(700.0, 760.0, spot=740.0)
        below, above = bracketing_levels(lines, 10_000.0)
        assert below is not None
        assert above is None

    def test_bracketing_finds_the_pair_around_a_price(self) -> None:
        lines = round_numbers(700.0, 760.0, spot=740.0)
        below, above = bracketing_levels(lines, 725.0)
        assert below is not None and above is not None
        assert below < 725.0 < above


class TestKinds:
    def test_a_cluster_of_mostly_lows_is_labelled_support(self) -> None:
        prices = [110.0, 105.0, 102.0, 101.0, 100.5, 90.0, 100.5, 101.0, 102.0, 105.0, 110.0]
        pivots = swing_pivots(ramp(prices, 0.0), 5)
        lows = [pivot for pivot in pivots if not pivot.high]
        levels = cluster_pivots(lows * 2, tolerance=1.0, min_touches=2)
        assert levels
        assert levels[0].kind is LevelKind.SWING_LOW
