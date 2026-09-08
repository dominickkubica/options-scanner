"""Market signals, tested for arithmetic on synthetic bars and for rate on real ones.

The second class of test is the point of the file, and it is the same argument
`test_levels.py` makes: a signal module is trivial to build so that it flags everything,
and a suite that only checked the arithmetic would pass happily while the tool sent a
hundred notifications a day and got muted. So the real SPY capture is used to assert
that each detector stays rare.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from optscan.analytics.levels import Level, LevelKind, build_levels
from optscan.analytics.signals import (
    MIN_BARS,
    SUPPORT_KINDS,
    Signal,
    SignalKind,
    evaluate,
    is_squeezed,
    significant_levels,
    volume_ratio,
)
from optscan.models import PriceBar

FIXTURE = Path(__file__).parent / "fixtures" / "spy_daily_bars.json"
BASE = datetime(2026, 1, 5, tzinfo=UTC)


def bar(
    close: float,
    *,
    high: float | None = None,
    low: float | None = None,
    volume: int | None = 1_000,
    day: int = 0,
) -> PriceBar:
    return PriceBar(
        symbol="TEST",
        ts=BASE + timedelta(days=day),
        open=close,
        high=close if high is None else high,
        low=close if low is None else low,
        close=close,
        volume=volume,
        fetched_at=BASE,
        source="test",
    )


@pytest.fixture(scope="session")
def spy_bars() -> list[PriceBar]:
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
            fetched_at=BASE,
            source="fixture",
        )
        for row in payload["bars"]
    ]


# --------------------------------------------------------------------------------
# volume_ratio: the null-is-not-zero rule
# --------------------------------------------------------------------------------


def test_volume_ratio_is_the_multiple_of_the_trailing_average() -> None:
    bars = [bar(100, volume=1_000, day=index) for index in range(20)]
    bars.append(bar(100, volume=3_000, day=20))
    assert volume_ratio(bars) == pytest.approx(3.0)


def test_volume_ratio_is_unknown_when_a_bar_in_the_window_has_no_volume() -> None:
    """A missing volume must not be read as a quiet day, which would inflate the ratio.

    This is the case that matters: treating None as zero drops the average and makes
    the surge detector fire hardest exactly when the data is worst.
    """
    bars = [bar(100, volume=1_000, day=index) for index in range(20)]
    bars[5] = bar(100, volume=None, day=5)
    bars.append(bar(100, volume=1_100, day=20))
    assert volume_ratio(bars) is None


def test_volume_ratio_is_unknown_without_a_full_window() -> None:
    assert volume_ratio([bar(100, day=index) for index in range(10)]) is None


# --------------------------------------------------------------------------------
# is_squeezed
# --------------------------------------------------------------------------------


def test_squeeze_when_closes_are_flat_but_the_daily_range_is_wide() -> None:
    """Zero close-to-close spread inside a two point true range is the squeeze."""
    bars = [bar(100.0, high=101.0, low=99.0, day=index) for index in range(40)]
    assert is_squeezed(bars) is True


def test_no_squeeze_when_closes_travel_further_than_the_daily_range() -> None:
    bars = [
        bar(100.0 + index, high=100.0 + index + 0.01, low=100.0 + index - 0.01, day=index)
        for index in range(40)
    ]
    assert is_squeezed(bars) is False


def test_squeeze_is_unknown_rather_than_false_without_history() -> None:
    """None and False are different answers: nobody looked, versus no squeeze."""
    assert is_squeezed([bar(100, day=index) for index in range(5)]) is None


# --------------------------------------------------------------------------------
# significant_levels
# --------------------------------------------------------------------------------


def test_levels_without_a_p_value_are_excluded_not_failed() -> None:
    """Round numbers, volume nodes and value areas have nothing to have survived.

    Value areas matter most here: they are the kind most likely to be mistaken for
    support, and `value_area_low` and `value_area_high` share one LevelKind, so a level
    alert could not tell a floor from a ceiling even if it wanted to.
    """
    levels = [
        Level(price=100.0, kind=LevelKind.SWING_LOW, strength=0.9, p_value=0.01),
        Level(price=101.0, kind=LevelKind.SWING_HIGH, strength=0.5, p_value=0.40),
        Level(price=102.0, kind=LevelKind.VALUE_AREA, strength=0.6),
        Level(price=103.0, kind=LevelKind.ROUND_NUMBER, strength=0.35),
        Level(price=104.0, kind=LevelKind.VOLUME_NODE, strength=0.8),
    ]
    kept = significant_levels(levels, 0.05)
    assert [level.price for level in kept] == [100.0]


def test_only_swing_kinds_can_be_support() -> None:
    """Guards the invariant the composites rely on: the kinds that can raise a support
    alert are exactly the kinds that carry a p-value."""
    assert SUPPORT_KINDS == (LevelKind.SWING_LOW,)


# --------------------------------------------------------------------------------
# evaluate
# --------------------------------------------------------------------------------


def test_evaluate_says_nothing_without_enough_history() -> None:
    bars = [bar(100, day=index) for index in range(MIN_BARS - 1)]
    assert evaluate("TEST", bars) == []


def test_level_break_needs_a_close_through_not_a_wick_through() -> None:
    """A wick through a level and back is not a break, and counting it as one is most
    of how this kind of alert turns into noise."""
    level = Level(price=105.0, kind=LevelKind.SWING_HIGH, strength=0.9, p_value=0.01)
    bars = [bar(100.0, day=index) for index in range(MIN_BARS)]

    # High pierces 105, close does not.
    wick = [*bars, bar(104.0, high=106.0, low=103.0, day=MIN_BARS)]
    kinds = {signal.kind for signal in evaluate("TEST", wick, [level])}
    assert SignalKind.LEVEL_BREAK not in kinds

    # Close is through it.
    through = [*bars, bar(106.0, high=106.5, low=103.0, day=MIN_BARS)]
    breaks = [
        signal
        for signal in evaluate("TEST", through, [level])
        if signal.kind is SignalKind.LEVEL_BREAK
    ]
    assert len(breaks) == 1
    assert "above" in breaks[0].message


def test_level_break_ignores_a_level_that_did_not_beat_chance() -> None:
    weak = Level(price=105.0, kind=LevelKind.SWING_HIGH, strength=0.1, p_value=0.40)
    bars = [bar(100.0, day=index) for index in range(MIN_BARS)]
    bars.append(bar(106.0, day=MIN_BARS))
    kinds = {signal.kind for signal in evaluate("TEST", bars, [weak])}
    assert SignalKind.LEVEL_BREAK not in kinds


def test_oversold_at_support_needs_both_halves() -> None:
    """Near a swing low with an ordinary RSI raises the approach, not the composite."""
    support = Level(price=100.0, kind=LevelKind.SWING_LOW, strength=0.9, p_value=0.01)
    flat = [bar(100.0, day=index) for index in range(MIN_BARS + 1)]
    kinds = {signal.kind for signal in evaluate("TEST", flat, [support])}
    assert SignalKind.LEVEL_APPROACH in kinds
    assert SignalKind.OVERSOLD_AT_SUPPORT not in kinds


def test_oversold_at_support_fires_on_a_sustained_decline_into_a_swing_low() -> None:
    """Price falls to just above a swing low, oversold, without closing through it."""
    prices = [140.0 - index for index in range(40)]  # RSI pinned near zero
    bars = [bar(price, day=index) for index, price in enumerate(prices)]
    support = Level(
        price=bars[-1].close * 0.999, kind=LevelKind.SWING_LOW, strength=0.9, p_value=0.01
    )
    signals = {s.kind: s for s in evaluate("TEST", bars, [support])}
    assert SignalKind.OVERSOLD_AT_SUPPORT in signals
    assert signals[SignalKind.OVERSOLD_AT_SUPPORT].value < 30.0


def test_a_level_that_broke_does_not_also_report_as_approached() -> None:
    """Price is necessarily near a line it just closed through, so reporting both is
    the same event told twice."""
    prices = [140.0 - index for index in range(40)]
    bars = [bar(price, day=index) for index, price in enumerate(prices)]
    # Sits between the last two closes, so this session closed through it.
    level = Level(
        price=(bars[-1].close + bars[-2].close) / 2,
        kind=LevelKind.SWING_LOW,
        strength=0.9,
        p_value=0.01,
    )
    kinds = {signal.kind for signal in evaluate("TEST", bars, [level])}
    assert SignalKind.LEVEL_BREAK in kinds
    assert SignalKind.LEVEL_APPROACH not in kinds
    # And the composite goes with it: closing through support is a breakdown, not a
    # bounce, and calling it "oversold at support" would invert what happened.
    assert SignalKind.OVERSOLD_AT_SUPPORT not in kinds


def test_an_untouched_level_still_reports_as_approached_alongside_a_break() -> None:
    """Suppression is per level, not per symbol: a second level nearby still counts."""
    prices = [140.0 - index for index in range(40)]
    bars = [bar(price, day=index) for index, price in enumerate(prices)]
    crossed = Level(
        price=(bars[-1].close + bars[-2].close) / 2,
        kind=LevelKind.SWING_LOW,
        strength=0.9,
        p_value=0.01,
    )
    intact = Level(
        price=bars[-1].close * 0.999,
        kind=LevelKind.SWING_LOW,
        strength=0.9,
        p_value=0.01,
    )
    kinds = {signal.kind for signal in evaluate("TEST", bars, [crossed, intact])}
    assert SignalKind.LEVEL_BREAK in kinds
    assert SignalKind.LEVEL_APPROACH in kinds


def test_a_swing_high_never_raises_the_oversold_composite() -> None:
    """The support and resistance predicates are disjoint, so a level cannot be both."""
    prices = [140.0 - index for index in range(40)]
    bars = [bar(price, day=index) for index, price in enumerate(prices)]
    resistance = Level(price=bars[-1].close, kind=LevelKind.SWING_HIGH, strength=0.9, p_value=0.01)
    kinds = {signal.kind for signal in evaluate("TEST", bars, [resistance])}
    assert SignalKind.OVERSOLD_AT_SUPPORT not in kinds
    assert SignalKind.OVERBOUGHT_AT_RESISTANCE not in kinds


def test_tolerance_is_a_fraction_so_one_setting_suits_every_price() -> None:
    """A $9 miner and a $900 index must mean the same thing by "near"."""
    for price in (9.0, 900.0):
        bars = [bar(price, day=index) for index in range(MIN_BARS + 1)]
        near = Level(price=price * 1.002, kind=LevelKind.SWING_HIGH, strength=0.9, p_value=0.01)
        far = Level(price=price * 1.020, kind=LevelKind.SWING_HIGH, strength=0.9, p_value=0.01)
        assert any(s.kind is SignalKind.LEVEL_APPROACH for s in evaluate("T", bars, [near]))
        assert not any(s.kind is SignalKind.LEVEL_APPROACH for s in evaluate("T", bars, [far]))


def test_signal_key_is_stable_and_carries_the_session() -> None:
    """Suppression is per symbol, kind and day: a break in March and one in July are
    two events, not a repeat."""
    signal = Signal(
        symbol="SPY",
        kind=SignalKind.LEVEL_BREAK,
        session=BASE.date(),
        message="x",
    )
    assert signal.key == "SPY:level_break:2026-01-05"


def test_message_states_the_evidence_not_just_the_count() -> None:
    """ "5 touches against 1.4 expected" rather than a bare 5, which reads as more."""
    level = Level(
        price=100.0,
        kind=LevelKind.SWING_LOW,
        strength=0.9,
        touches=5,
        p_value=0.01,
        expected_touches=1.4,
    )
    bars = [bar(100.0, day=index) for index in range(MIN_BARS + 1)]
    approach = next(
        s for s in evaluate("TEST", bars, [level]) if s.kind is SignalKind.LEVEL_APPROACH
    )
    assert "5 touches against 1.4 expected" in approach.message
    assert "p=0.010" in approach.message


# --------------------------------------------------------------------------------
# The rate tests. These are the ones that decide whether this ships.
# --------------------------------------------------------------------------------


def _walk(bars: list[PriceBar], window: int = 400, stride: int = 10) -> list[list[Signal]]:
    """Evaluate on a trailing window at every `stride`th session, with no lookahead."""
    out = []
    for end in range(window, len(bars), stride):
        recent = bars[end - window : end + 1]
        levels = build_levels(recent)
        out.append(evaluate("SPY", recent, levels.all()))
    return out


@pytest.fixture(scope="module")
def spy_signals(spy_bars: list[PriceBar]) -> list[list[Signal]]:
    return _walk(spy_bars)


def test_the_detectors_stay_rare_on_real_bars(spy_signals: list[list[Signal]]) -> None:
    """No kind may fire on more than a fifth of sessions.

    The threshold is deliberately loose, because the failure this catches is not a
    poorly tuned cut but a broken detector: anything firing on most days is describing
    the market rather than finding anything in it, and would arrive as a hundred
    notifications a day across the real universe.
    """
    days = len(spy_signals)
    assert days > 50, "not enough sampled sessions for a rate to mean anything"

    for kind in SignalKind:
        hits = sum(1 for signals in spy_signals if any(s.kind is kind for s in signals))
        assert hits / days <= 0.20, (
            f"{kind.value} fired on {hits}/{days} sessions "
            f"({hits / days:.0%}), which is a log line rather than an alert"
        )


def test_the_composites_are_rarer_than_their_parts(
    spy_signals: list[list[Signal]],
) -> None:
    """The whole argument for a conjunction is that it is rarer than either half."""
    days = len(spy_signals)
    approach = sum(
        1 for signals in spy_signals if any(s.kind is SignalKind.LEVEL_APPROACH for s in signals)
    )
    composites = sum(
        1
        for signals in spy_signals
        if any(
            s.kind in (SignalKind.OVERSOLD_AT_SUPPORT, SignalKind.OVERBOUGHT_AT_RESISTANCE)
            for s in signals
        )
    )
    assert composites <= approach
    assert composites / days <= 0.05


def test_a_composite_always_arrives_with_its_approach(
    spy_signals: list[list[Signal]],
) -> None:
    """A composite is a refinement of the approach, so it cannot fire without one.

    Guards against the two drifting apart if either predicate is edited later.
    """
    for signals in spy_signals:
        kinds = {s.kind for s in signals}
        if kinds & {SignalKind.OVERSOLD_AT_SUPPORT, SignalKind.OVERBOUGHT_AT_RESISTANCE}:
            assert SignalKind.LEVEL_APPROACH in kinds


def test_every_signal_reports_what_it_measured(spy_signals: list[list[Signal]]) -> None:
    """A signal showing only its name would be asking to be trusted rather than read."""
    for signals in spy_signals:
        for signal in signals:
            assert signal.message
            assert signal.price is not None
            if signal.kind is not SignalKind.SQUEEZE:
                assert signal.threshold is not None
