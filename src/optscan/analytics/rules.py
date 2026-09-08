"""Entry rules: the conditions a backtest is allowed to enter on.

## Why these are series and `levels.py` is scalar

The live path asks "does this hold right now", so `levels.py` returns one number for the
most recent bar. A backtest asks the same question of every bar, and calling the scalar
form once per bar is quadratic: 2,500 sessions across 294 symbols is about four billion
operations to answer a question that costs a single pass.

So the indicators here are rolling. That makes two implementations of the same
arithmetic, which is a real hazard rather than a tidy separation of concerns: the day
they disagree, the chart draws one line, the alert fires on a second, and the backtest
measures a third. `tests/test_rules.py` pins every one of them against its `levels.py`
counterpart on the same bars, which is the same check already run between `levels.py`
and the frontend's `compute.js`.

## The rule about lookahead

A rule returns the indices of bars whose **closing information** satisfies it. It never
looks past index `i` when deciding about bar `i`. The fill happens one bar later and
that is `backtest.simulate`'s job, not this module's, but the two halves only add up if
this half is strict.

The level rules are where that is easiest to get wrong, because levels are expensive and
the temptation is to build them once from the whole history and reuse them. That would
mean a 2019 trade entering on a level derived from 2024. Levels are therefore rebuilt on
a cadence from **only the bars before the point being evaluated**, which is also what a
person does: nobody redraws support every morning.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

from optscan.analytics import levels as scalar
from optscan.analytics.signals import SignalKind
from optscan.models import PriceBar

#: How often the level rules rebuild support and resistance, in bars. Monthly. Rebuilding
#: every bar is 20x the cost for a picture that barely moves, and rebuilding never is
#: lookahead.
DEFAULT_LEVEL_CADENCE = 21

#: Bars of history a level rebuild is given. Two years, matching `jobs/signals.py`: a
#: level nobody has defended since 2016 is not a level anybody is watching.
DEFAULT_LEVEL_WINDOW = 504

#: Bars before the first rule may fire, so every indicator has a full window.
WARMUP = 60

#: Bars a level rebuild needs before it is worth attempting. Below this the swing test
#: has too few pivots to reject anything and would publish no levels anyway.
MIN_LEVEL_HISTORY = 60


# --------------------------------------------------------------------------------
# Rolling indicators. Each mirrors a scalar in levels.py; the tests pin them together.
# --------------------------------------------------------------------------------


def _wilder(values: Sequence[float], period: int) -> list[float | None]:
    """Wilder's recursive smoothing, aligned so index i is the value after values[i].

    Not a rolling mean. Substituting one is the common error that makes an RSI disagree
    with every other platform by a point or two, which is exactly the size of
    disagreement nobody investigates.
    """
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    average = sum(values[:period]) / period
    out[period - 1] = average
    for index in range(period, len(values)):
        average = (average * (period - 1) + values[index]) / period
        out[index] = average
    return out


def rsi_series(bars: Sequence[PriceBar], period: int = 14) -> list[float | None]:
    """RSI at every bar, None until the window is full."""
    out: list[float | None] = [None] * len(bars)
    if len(bars) < period + 1:
        return out

    gains, losses = [], []
    for index in range(1, len(bars)):
        change = bars[index].close - bars[index - 1].close
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    average_gain = _wilder(gains, period)
    average_loss = _wilder(losses, period)
    for index in range(len(gains)):
        gain, loss = average_gain[index], average_loss[index]
        if gain is None or loss is None:
            continue
        # No losses in the window is not a divide by zero, it is an RSI of 100: the
        # definition's limit, and the state a strong trend actually reaches.
        value = 100.0 if loss == 0 else 100.0 - 100.0 / (1 + gain / loss)
        out[index + 1] = value
    return out


def moving_average_series(bars: Sequence[PriceBar], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    if period < 1 or len(bars) < period:
        return out
    total = sum(bar.close for bar in bars[:period])
    out[period - 1] = total / period
    for index in range(period, len(bars)):
        total += bars[index].close - bars[index - period].close
        out[index] = total / period
    return out


def _rolling_stdev(bars: Sequence[PriceBar], period: int) -> list[tuple[float, float] | None]:
    """(mean, population stdev) of closes at every bar."""
    out: list[tuple[float, float] | None] = [None] * len(bars)
    total = 0.0
    total_squares = 0.0
    for index, bar in enumerate(bars):
        total += bar.close
        total_squares += bar.close * bar.close
        if index >= period:
            dropped = bars[index - period].close
            total -= dropped
            total_squares -= dropped * dropped
        if index >= period - 1:
            mean = total / period
            # Clamped: floating point can leave a tiny negative variance on a flat
            # window, and sqrt of that is a domain error.
            variance = max(total_squares / period - mean * mean, 0.0)
            out[index] = (mean, math.sqrt(variance))
    return out


def atr_series(bars: Sequence[PriceBar], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    if len(bars) < period + 1:
        return out
    ranges = []
    for index in range(1, len(bars)):
        previous_close = bars[index - 1].close
        ranges.append(
            max(
                bars[index].high - bars[index].low,
                abs(bars[index].high - previous_close),
                abs(bars[index].low - previous_close),
            )
        )
    smoothed = _wilder(ranges, period)
    for index, value in enumerate(smoothed):
        if value is not None:
            out[index + 1] = value
    return out


def ema_series(bars: Sequence[PriceBar], period: int) -> list[float | None]:
    """Seeded with the simple average of the first window, matching `levels.ema`."""
    out: list[float | None] = [None] * len(bars)
    if period < 1 or len(bars) < period:
        return out
    k = 2.0 / (period + 1)
    value = sum(bar.close for bar in bars[:period]) / period
    out[period - 1] = value
    for index in range(period, len(bars)):
        value = bars[index].close * k + value * (1 - k)
        out[index] = value
    return out


def squeeze_series(
    bars: Sequence[PriceBar],
    period: int = 20,
    deviations: float = 2.0,
    multiple: float = 1.5,
) -> list[bool | None]:
    """Are the Bollinger bands inside the Keltner channels at each bar?

    None where either band is uncomputable, which is not the same as False: one says
    there is no squeeze, the other that nobody could look.
    """
    bands = _rolling_stdev(bars, period)
    centre = ema_series(bars, period)
    width = atr_series(bars, period)

    out: list[bool | None] = [None] * len(bars)
    for index in range(len(bars)):
        stats = bands[index]
        middle = centre[index]
        spread = width[index]
        if stats is None or middle is None or spread is None:
            continue
        mean, sd = stats
        out[index] = (mean + deviations * sd) < (middle + multiple * spread) and (
            mean - deviations * sd
        ) > (middle - multiple * spread)
    return out


def volume_ratio_series(bars: Sequence[PriceBar], period: int = 20) -> list[float | None]:
    """Each bar's volume against the average of the `period` before it.

    None when any bar in the window has unknown volume rather than treating it as zero,
    which would inflate the ratio exactly on the days the data is patchy.
    """
    out: list[float | None] = [None] * len(bars)
    for index in range(period, len(bars)):
        window = bars[index - period : index]
        if bars[index].volume is None or any(b.volume is None for b in window):
            continue
        average = sum(b.volume for b in window) / period
        if average > 0:
            out[index] = bars[index].volume / average
    return out


# --------------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------------

Rule = Callable[[Sequence[PriceBar]], list[int]]


def _rising_cross(values: Sequence[bool | None]) -> list[int]:
    """Indices where a boolean series turns on. The edge, not the state.

    Same argument as `signals.evaluate`: a squeeze runs a median of four sessions, so a
    rule keyed on the state enters four times for one thing happening, and every one of
    those entries overlaps the last.
    """
    out = []
    for index in range(1, len(values)):
        if values[index] and values[index - 1] is False:
            out.append(index)
    return out


def rsi_below(bars: Sequence[PriceBar], threshold: float = 30.0, period: int = 14):
    series = rsi_series(bars, period)
    return [
        index
        for index in range(WARMUP, len(bars))
        if series[index] is not None and series[index] <= threshold
    ]


def rsi_above(bars: Sequence[PriceBar], threshold: float = 70.0, period: int = 14):
    series = rsi_series(bars, period)
    return [
        index
        for index in range(WARMUP, len(bars))
        if series[index] is not None and series[index] >= threshold
    ]


def squeeze_starts(bars: Sequence[PriceBar], period: int = 20):
    return [i for i in _rising_cross(squeeze_series(bars, period)) if i >= WARMUP]


def volume_surge(bars: Sequence[PriceBar], multiple: float = 2.0, period: int = 20):
    series = volume_ratio_series(bars, period)
    return [
        index
        for index in range(WARMUP, len(bars))
        if series[index] is not None and series[index] >= multiple
    ]


def above_ma(bars: Sequence[PriceBar], period: int = 200):
    """Crossing above, not being above. Being above is a regime, not an entry."""
    series = moving_average_series(bars, period)
    out = []
    for index in range(max(WARMUP, period), len(bars)):
        now, before = series[index], series[index - 1]
        if now is None or before is None:
            continue
        if bars[index].close > now and bars[index - 1].close <= before:
            out.append(index)
    return out


def below_ma(bars: Sequence[PriceBar], period: int = 200):
    series = moving_average_series(bars, period)
    out = []
    for index in range(max(WARMUP, period), len(bars)):
        now, before = series[index], series[index - 1]
        if now is None or before is None:
            continue
        if bars[index].close < now and bars[index - 1].close >= before:
            out.append(index)
    return out


def ibs_series(bars: Sequence[PriceBar]) -> list[float | None]:
    """Internal bar strength: where the close sits inside the day's own range.

    (close - low) / (high - low), so zero means the day closed on its low and one means
    it closed on its high. Documented since the 1990s as a mean reversion signal on
    equity indices and ETFs, with closes in the bottom fifth of the range tending to be
    followed by strength the next session.

    A bar with no range has no position inside it. Returned as None rather than 0.5,
    because a limit-up or halted day is not a neutral close.
    """
    out: list[float | None] = []
    for bar in bars:
        span = bar.high - bar.low
        out.append((bar.close - bar.low) / span if span > 0 else None)
    return out


def ibs_below(bars: Sequence[PriceBar], threshold: float = 0.2):
    """Closed in the weakest part of its own daily range."""
    series = ibs_series(bars)
    return [
        index
        for index in range(WARMUP, len(bars))
        if series[index] is not None and series[index] <= threshold
    ]


def ibs_above(bars: Sequence[PriceBar], threshold: float = 0.8):
    """Closed in the strongest part of its own daily range."""
    series = ibs_series(bars)
    return [
        index
        for index in range(WARMUP, len(bars))
        if series[index] is not None and series[index] >= threshold
    ]


def down_days(bars: Sequence[PriceBar], count: int = 3):
    """`count` consecutive lower closes. The oldest short-term reversal rule there is.

    The edge is the streak's completion, not its continuation, so this fires on the day
    the streak reaches `count` and on every day it extends. Both are entries in the same
    condition; overlap suppression in the simulator stops that becoming many positions.
    """
    if count < 1:
        raise ValueError("count must be at least one")
    out = []
    for index in range(WARMUP, len(bars)):
        window = bars[index - count : index + 1]
        if len(window) == count + 1 and all(
            window[step + 1].close < window[step].close for step in range(count)
        ):
            out.append(index)
    return out


def up_days(bars: Sequence[PriceBar], count: int = 3):
    """`count` consecutive higher closes."""
    if count < 1:
        raise ValueError("count must be at least one")
    out = []
    for index in range(WARMUP, len(bars)):
        window = bars[index - count : index + 1]
        if len(window) == count + 1 and all(
            window[step + 1].close > window[step].close for step in range(count)
        ):
            out.append(index)
    return out


def gap_down(bars: Sequence[PriceBar], size: float = 0.02):
    """Opened at least `size` below the previous close, and closed below it too.

    Requiring the close as well as the open is what separates a gap that stuck from one
    that filled during the session. Without it the rule fires on days that already
    recovered, which is a different event wearing the same name.
    """
    out = []
    for index in range(WARMUP, len(bars)):
        previous = bars[index - 1].close
        if previous <= 0:
            continue
        if bars[index].open <= previous * (1 - size) and bars[index].close < previous:
            out.append(index)
    return out


def near_52w_high(bars: Sequence[PriceBar], within: float = 0.02, window: int = 252):
    """Within `within` of the highest close of the last `window` sessions.

    The momentum side of the grid, and a deliberate counterweight: everything else here
    is mean reversion, and a search containing only one idea can only confirm it.
    """
    out = []
    for index in range(max(WARMUP, window), len(bars)):
        peak = max(bar.close for bar in bars[index - window : index + 1])
        if peak > 0 and bars[index].close >= peak * (1 - within):
            out.append(index)
    return out


def turn_of_month(bars: Sequence[PriceBar], days: int = 3):
    """The last session of a month and the first `days` of the next.

    A calendar rule with no market input at all, included as a second control: it should
    beat its null only if the turn-of-month effect is real in this sample, and a search
    that ranks it highly is telling you something about the search rather than the month.
    """
    out = []
    for index in range(WARMUP, len(bars)):
        today = bars[index].ts.date()
        previous = bars[index - 1].ts.date()
        # Last session of a month: the next bar is in a different month.
        month_end = index + 1 < len(bars) and bars[index + 1].ts.date().month != today.month
        month_start = today.month != previous.month
        if month_end or (month_start and today.day <= days):
            out.append(index)
    return out


def level_rule(
    bars: Sequence[PriceBar],
    kind: str = SignalKind.LEVEL_BREAK.value,
    *,
    cadence: int = DEFAULT_LEVEL_CADENCE,
    window: int = DEFAULT_LEVEL_WINDOW,
    max_p_value: float = 0.05,
    tolerance: float = 0.0025,
) -> list[int]:
    """Entries from the level signals, with levels rebuilt on a cadence.

    Levels are rebuilt every `cadence` bars from the preceding `window` bars only, so a
    2019 entry can never see a level carved in 2024. Between rebuilds the last set is
    carried forward, which is both cheaper and closer to what a person does.
    """
    from optscan.analytics.levels import build_levels  # noqa: PLC0415
    from optscan.analytics.signals import significant_levels  # noqa: PLC0415

    wanted = SignalKind(kind)
    out: list[int] = []
    tested: list = []

    for index in range(WARMUP, len(bars)):
        if (index - WARMUP) % cadence == 0:
            # Strictly the bars before this one. `bars[:index]` excludes index itself,
            # which is the whole no-lookahead guarantee for these rules.
            history = bars[max(0, index - window) : index]
            if len(history) >= MIN_LEVEL_HISTORY:
                tested = significant_levels(build_levels(history).all(), max_p_value)
        if not tested:
            continue

        price = bars[index].close
        previous = bars[index - 1].close
        if price <= 0:
            continue

        if wanted is SignalKind.LEVEL_BREAK:
            if any(
                (previous < level.price <= price) or (previous > level.price >= price)
                for level in tested
            ):
                out.append(index)
        elif wanted is SignalKind.LEVEL_APPROACH and any(
            abs(level.price - price) / price <= tolerance for level in tested
        ):
            out.append(index)

    return out


#: Every rule a strategy may name, with the parameters it accepts. Kept as data so the
#: CLI, the API and the UI all offer exactly the same set and none of them can drift.
REGISTRY: dict[str, dict[str, object]] = {
    "every_bar": {
        "fn": lambda bars: list(range(WARMUP, len(bars))),
        "params": {},
        "label": "Every bar (control)",
        "about": "Enters constantly. Not a strategy: the baseline a real rule must beat.",
    },
    "rsi_below": {
        "fn": rsi_below,
        "params": {"threshold": 30.0, "period": 14},
        "label": "RSI below threshold",
        "about": "Oversold by the conventional definition.",
    },
    "rsi_above": {
        "fn": rsi_above,
        "params": {"threshold": 70.0, "period": 14},
        "label": "RSI above threshold",
        "about": "Overbought by the conventional definition.",
    },
    "squeeze": {
        "fn": squeeze_starts,
        "params": {"period": 20},
        "label": "Squeeze begins",
        "about": "Bollinger bands close inside the Keltner channels. Edge triggered.",
    },
    "volume_surge": {
        "fn": volume_surge,
        "params": {"multiple": 2.0, "period": 20},
        "label": "Volume surge",
        "about": "Volume at a multiple of its own trailing average.",
    },
    "above_ma": {
        "fn": above_ma,
        "params": {"period": 200},
        "label": "Crosses above moving average",
        "about": "The cross, not the state: being above a average is a regime.",
    },
    "below_ma": {
        "fn": below_ma,
        "params": {"period": 200},
        "label": "Crosses below moving average",
        "about": "The cross, not the state.",
    },
    "ibs_below": {
        "fn": ibs_below,
        "params": {"threshold": 0.2},
        "label": "Closed weak within its range (low IBS)",
        "about": (
            "Internal bar strength: the close's position inside the day's own high-low "
            "range. Documented mean reversion on indices and ETFs since the 1990s."
        ),
    },
    "ibs_above": {
        "fn": ibs_above,
        "params": {"threshold": 0.8},
        "label": "Closed strong within its range (high IBS)",
        "about": "The other side of the same indicator.",
    },
    "down_days": {
        "fn": down_days,
        "params": {"count": 3},
        "label": "Consecutive lower closes",
        "about": "The oldest short-term reversal rule there is.",
    },
    "up_days": {
        "fn": up_days,
        "params": {"count": 3},
        "label": "Consecutive higher closes",
        "about": "The mirror of the streak rule.",
    },
    "gap_down": {
        "fn": gap_down,
        "params": {"size": 0.02},
        "label": "Gapped down and stayed down",
        "about": "Opened below the prior close by a margin and closed below it too.",
    },
    "near_52w_high": {
        "fn": near_52w_high,
        "params": {"within": 0.02, "window": 252},
        "label": "Near its 52 week high",
        "about": "The momentum counterweight in a grid otherwise full of reversion.",
    },
    "turn_of_month": {
        "fn": turn_of_month,
        "params": {"days": 3},
        "label": "Turn of the month",
        "about": (
            "A pure calendar rule with no market input. A second control: if this ranks "
            "highly, read it as information about the search."
        ),
    },
    "level_break": {
        "fn": lambda bars, **kw: level_rule(bars, SignalKind.LEVEL_BREAK.value, **kw),
        "params": {"max_p_value": 0.05},
        "label": "Breaks a tested level",
        "about": "Closes through a swing level that beat its own significance test.",
    },
    "level_approach": {
        "fn": lambda bars, **kw: level_rule(bars, SignalKind.LEVEL_APPROACH.value, **kw),
        "params": {"tolerance": 0.0025, "max_p_value": 0.05},
        "label": "Approaches a tested level",
        "about": "Comes within a tolerance of a swing level that beat chance.",
    },
}


def resolve(name: str, params: dict | None = None) -> Rule:
    """A named rule with its parameters bound, or a ValueError naming what exists."""
    entry = REGISTRY.get(name)
    if entry is None:
        raise ValueError(f"unknown entry rule {name!r}. Available: {', '.join(sorted(REGISTRY))}")
    merged = dict(entry["params"])
    for key, value in (params or {}).items():
        if key not in merged:
            raise ValueError(f"{name} takes {sorted(merged) or 'no parameters'}, not {key!r}")
        merged[key] = value

    function = entry["fn"]
    return lambda bars: function(bars, **merged)


def describe() -> list[dict[str, object]]:
    """The registry as plain data, for the API and the CLI's help."""
    return [
        {
            "name": name,
            "label": entry["label"],
            "about": entry["about"],
            "params": entry["params"],
        }
        for name, entry in sorted(REGISTRY.items())
    ]


#: Re-exported so callers can pin the rolling forms against the scalar ones without
#: importing two modules. See the module docstring on why both exist.
SCALAR = scalar
