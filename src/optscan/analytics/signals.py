"""Market conditions worth being told about, and the arithmetic of not crying wolf.

The alerts in `triggers.py` are about **positions you hold**: a short strike tested, a
delta breached, an expiry approaching. These are about **the market**, for every symbol
in the universe whether or not anything is open on it.

## The problem this module is mostly about

An alert that fires often is not an alert. It is a log line that trains its reader to
ignore the channel. So the question for every signal here is not "is this condition
interesting" but "how often does it hold anyway", and the answer has to be **measured
against the stored bars** before the signal ships. That is the standing check the
detectors in `levels.py` are held to, and it is why every threshold below is a keyword
argument rather than a literal. The rates quoted throughout were measured over 44,113
symbol-days: 294 symbols, one session in five across three years.

## Why a fixed threshold is the trap here

The obvious way to write the squeeze is "Bollinger width below four percent of price".
It is also wrong, and wrong in this project's most familiar way: four percent is a
squeeze for a utility and an ordinary Tuesday for a biotech, so a fixed cut fires
almost always on quiet symbols and almost never on loud ones. Across a 286 symbol
universe that detector is reporting **which symbol it is looking at**, not what that
symbol is doing.

So the squeeze here is the standard one: Bollinger bands entirely **inside** Keltner
channels. Both are widths for the same symbol over the same window, one from the
standard deviation of closes and one from the average true range, so the comparison
normalises itself and one threshold means the same thing on every symbol.

The measurement bears this out. Under a fixed 4% band width, the share of days a symbol
fires runs from 0% at the tenth percentile to 12% at the ninetieth: a tenth of the
universe would never alert and a tenth would alert constantly, whatever either was
actually doing. Under bands-inside-channels the same spread is 3% to 16%, around a
median of 9%.

The squeeze is also the one signal here that is a **state** rather than an event, and
states need an edge. See `evaluate`.

## Why the composite signals are the point

`price is near a level` fires constantly, because price is always near something. It is
included, at a tight tolerance, because it is genuinely what a person wants to know at
the moment it happens. But the signals worth a notification require two conditions to
hold at once: oversold **and** at a support level that survived a significance test.

**A conjunction is only rare if its parts are independent**, which is the trap to watch.
RSI and position within a Bollinger band are both functions of the same recent closes,
so "oversold and at the lower band" is one condition counted twice and is deliberately
not offered. Oversold near a *swing level* is momentum meeting structure, which are
different measurements — but only *approximately* independent, and the measurement says
so: across 44,113 symbol-days the composites fire 1.4x and 1.6x more often than
independence predicts, because the decline that carves a swing low is the same decline
that depresses RSI. A mild positive lift, not the tenfold one that would mean the two
halves were the same condition wearing different names. The conjunction still cuts the
rate roughly a hundredfold against either part, which is the effect being relied on.

## Only tested levels raise alerts

`Level.p_value` is None for the kinds where there is nothing to test: a round number has
no touch count, and a volume node's height is not a count of events. Value areas are in
that group too, which also settles a question this module would otherwise have to
answer badly, since `value_area_low` and `value_area_high` share one `LevelKind` and
cannot be told apart from the level alone. Support here means a swing low that beat
chance, resistance a swing high that did, and nothing else qualifies.

## What is deliberately absent

No signal says buy or sell, and none is scored for quality. They report that a condition
holds. Whether that is an opportunity depends on things this module cannot see, and a
"strong buy" label would be the tool making a recommendation it has never been validated
to make.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from optscan.analytics.levels import (
    Level,
    LevelKind,
    bollinger_bands,
    keltner_channels,
    levels_near,
    rsi,
)
from optscan.models import PriceBar

#: How close price must come to a level to count as approaching it, as a fraction of
#: price. Measured over 44,113 symbol-days: 0.25% fires on 0.67% of them, about two
#: alerts a day across a 294 symbol universe. 1% fires on 2.9% and 2% on 6.2%, which are
#: log lines rather than alerts.
DEFAULT_LEVEL_TOLERANCE = 0.0025

#: A level must have beaten chance by at least this much to be worth an alert. The same
#: default `build_levels` already applies, restated rather than imported so that
#: loosening one does not silently loosen the other.
DEFAULT_MAX_P_VALUE = 0.05

#: RSI bands. The conventional 30 and 70 rather than tuned values: these are the ones
#: every other platform draws and the ones the chart's own RSI pane shows, and a private
#: threshold would make the alert disagree with the line the user is looking at.
DEFAULT_OVERSOLD = 30.0
DEFAULT_OVERBOUGHT = 70.0

#: Volume against its own trailing average. Measured: 1.5x fires on 10.5% of symbol-days
#: and is a Tuesday, 2x on 4.0%, 3x on 1.2%. Two is the knee, and this signal is severity
#: 2, so it lands in the dashboard rather than chasing anyone by default.
DEFAULT_VOLUME_SURGE = 2.0
DEFAULT_VOLUME_PERIOD = 20

#: Bars needed before any of this is computable. Set by the longest window in use: a
#: 20 period Keltner needs 20 closes plus one prior close for the first true range.
MIN_BARS = 30


class SignalKind(StrEnum):
    """What held. Not what to do about it."""

    LEVEL_APPROACH = "level_approach"
    LEVEL_BREAK = "level_break"
    SQUEEZE = "squeeze"
    OVERSOLD_AT_SUPPORT = "oversold_at_support"
    OVERBOUGHT_AT_RESISTANCE = "overbought_at_resistance"
    VOLUME_SURGE = "volume_surge"


#: Ordering for a digest. A conjunction outranks a single condition because it is rarer,
#: not because it is more likely to be profitable, which is unknown and unmeasured.
SEVERITY: dict[SignalKind, int] = {
    SignalKind.OVERSOLD_AT_SUPPORT: 4,
    SignalKind.OVERBOUGHT_AT_RESISTANCE: 4,
    SignalKind.LEVEL_BREAK: 3,
    SignalKind.SQUEEZE: 2,
    SignalKind.VOLUME_SURGE: 2,
    SignalKind.LEVEL_APPROACH: 1,
}

#: Kinds that are a defended floor, and kinds that are a defended ceiling. Only swing
#: levels appear because only swing levels carry a p-value. See the module docstring.
SUPPORT_KINDS = (LevelKind.SWING_LOW,)
RESISTANCE_KINDS = (LevelKind.SWING_HIGH,)


@dataclass(frozen=True, slots=True)
class Signal:
    """One condition that holds for one symbol on one session."""

    symbol: str
    kind: SignalKind
    session: date
    message: str
    #: The measured quantity and the threshold it passed, so a reader can judge the call
    #: rather than take it. A signal showing only its name would be asking to be trusted.
    value: float | None = None
    threshold: float | None = None
    price: float | None = None

    @property
    def severity(self) -> int:
        return SEVERITY.get(self.kind, 1)

    @property
    def key(self) -> str:
        """Identity for once-per-condition delivery, stable within a session."""
        return f"{self.symbol}:{self.kind.value}:{self.session.isoformat()}"

    def as_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "kind": self.kind.value,
            "session": self.session.isoformat(),
            "severity": self.severity,
            "message": self.message,
            "value": self.value,
            "threshold": self.threshold,
            "price": self.price,
        }


def significant_levels(levels: Sequence[Level], max_p_value: float) -> list[Level]:
    """Levels that beat chance. Kinds with nothing to test are excluded, not failed."""
    return [level for level in levels if level.p_value is not None and level.p_value <= max_p_value]


def _describe(level: Level) -> str:
    return level.kind.value.replace("_", " ")


def _evidence(level: Level) -> str:
    """ "5 touches against 1.4 expected" rather than a bare count, which reads as more."""
    if level.expected_touches is None or level.p_value is None:
        return ""
    return (
        f" ({level.touches} touches against {level.expected_touches:.1f} expected, "
        f"p={level.p_value:.3f})"
    )


def volume_ratio(bars: Sequence[PriceBar], period: int = DEFAULT_VOLUME_PERIOD) -> float | None:
    """Latest volume against the average of the `period` bars before it.

    None when any bar in the window has unknown volume, rather than treating it as zero,
    which would inflate the ratio exactly on the days the data is patchy. Same
    null-is-not-zero rule the storage layer keeps.
    """
    if len(bars) < period + 1:
        return None
    window = bars[-(period + 1) : -1]
    if bars[-1].volume is None or any(bar.volume is None for bar in window):
        return None
    average = sum(bar.volume for bar in window) / period
    if average <= 0:
        return None
    return bars[-1].volume / average


def is_squeezed(bars: Sequence[PriceBar]) -> bool | None:
    """Are the Bollinger bands entirely inside the Keltner channels?

    None when either band is uncomputable, which is not the same as False: one says
    there is no squeeze, the other that nobody looked.
    """
    bands = bollinger_bands(bars)
    channels = keltner_channels(bars)
    if bands is None or channels is None:
        return None
    return bands.upper < channels.upper and bands.lower > channels.lower


def evaluate(
    symbol: str,
    bars: Sequence[PriceBar],
    levels: Sequence[Level] = (),
    *,
    tolerance: float = DEFAULT_LEVEL_TOLERANCE,
    max_p_value: float = DEFAULT_MAX_P_VALUE,
    oversold: float = DEFAULT_OVERSOLD,
    overbought: float = DEFAULT_OVERBOUGHT,
    volume_surge: float = DEFAULT_VOLUME_SURGE,
    volume_period: int = DEFAULT_VOLUME_PERIOD,
) -> list[Signal]:
    """Every condition holding on the most recent bar. Pure: no I/O, no config reads.

    Returns an empty list rather than raising when history is too short. A symbol with
    twenty bars is one nothing can be said about yet, which is not an error.

    `bars` must be in ascending time order, which is what every storage read in this
    project returns.
    """
    if len(bars) < MIN_BARS:
        return []

    latest = bars[-1]
    previous = bars[-2]
    session = latest.ts.date()
    price = latest.close
    if price <= 0:
        return []

    out: list[Signal] = []
    tested = significant_levels(levels, max_p_value)
    momentum = rsi(bars)

    # A break is a *close* through a level the previous close was the other side of. A
    # wick through and back is not a break, and counting it as one is most of how this
    # kind of alert becomes noise.
    broken: set[float] = set()
    for level in tested:
        crossed_up = previous.close < level.price <= price
        crossed_down = previous.close > level.price >= price
        if not (crossed_up or crossed_down):
            continue
        broken.add(level.price)
        out.append(
            Signal(
                symbol=symbol,
                kind=SignalKind.LEVEL_BREAK,
                session=session,
                message=(
                    f"closed {'above' if crossed_up else 'below'} a "
                    f"{_describe(level)} at {level.price:.2f}{_evidence(level)}"
                ),
                value=price,
                threshold=level.price,
                price=price,
            )
        )

    # levels_near takes an absolute width; the tolerance here is a fraction of price so
    # that one setting means the same thing on a $9 miner and a $900 index.
    #
    # A level that broke this session is excluded: price is necessarily near a line it
    # just closed through, so reporting both is the same event told twice, and the
    # weaker telling is the one that would arrive second.
    near = [
        level
        for level in levels_near(tested, price, price * tolerance)
        if level.price not in broken
    ]
    if near:
        closest = near[0]
        gap = abs(closest.price - price) / price
        out.append(
            Signal(
                symbol=symbol,
                kind=SignalKind.LEVEL_APPROACH,
                session=session,
                message=(
                    f"{price:.2f} is within {gap:.2%} of a {_describe(closest)} at "
                    f"{closest.price:.2f}{_evidence(closest)}"
                ),
                value=gap,
                threshold=tolerance,
                price=price,
            )
        )

        # Momentum meeting structure: two different measurements, which is what makes
        # the conjunction rare rather than one condition counted twice.
        if momentum is not None:
            if momentum <= oversold and closest.kind in SUPPORT_KINDS:
                out.append(
                    Signal(
                        symbol=symbol,
                        kind=SignalKind.OVERSOLD_AT_SUPPORT,
                        session=session,
                        message=(
                            f"RSI {momentum:.0f} with price at a {_describe(closest)} "
                            f"of {closest.price:.2f}{_evidence(closest)}"
                        ),
                        value=momentum,
                        threshold=oversold,
                        price=price,
                    )
                )
            elif momentum >= overbought and closest.kind in RESISTANCE_KINDS:
                out.append(
                    Signal(
                        symbol=symbol,
                        kind=SignalKind.OVERBOUGHT_AT_RESISTANCE,
                        session=session,
                        message=(
                            f"RSI {momentum:.0f} with price at a {_describe(closest)} "
                            f"of {closest.price:.2f}{_evidence(closest)}"
                        ),
                        value=momentum,
                        threshold=overbought,
                        price=price,
                    )
                )

    # Edge triggered: the day compression *begins*, not every day it persists. Measured
    # across the universe, a squeeze runs a median of 4 sessions and up to 41, so the
    # held state fires six times for every one thing that happened. Suppression cannot
    # fix that, because each of those days is a different day and legitimately a new row.
    if is_squeezed(bars) and is_squeezed(bars[:-1]) is False:
        bands = bollinger_bands(bars)
        out.append(
            Signal(
                symbol=symbol,
                kind=SignalKind.SQUEEZE,
                session=session,
                message=(
                    "the spread of closes has fallen inside this symbol's own daily "
                    f"range (Bollinger width {bands.width:.2%} of price, inside the "
                    "Keltner channels). Compression beginning, no direction implied."
                ),
                value=bands.width,
                threshold=None,
                price=price,
            )
        )

    surge = volume_ratio(bars, volume_period)
    if surge is not None and surge >= volume_surge:
        out.append(
            Signal(
                symbol=symbol,
                kind=SignalKind.VOLUME_SURGE,
                session=session,
                message=f"volume {surge:.1f}x its {volume_period} day average",
                value=surge,
                threshold=volume_surge,
                price=price,
            )
        )

    return out
