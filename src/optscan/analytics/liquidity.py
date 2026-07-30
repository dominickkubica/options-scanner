"""Liquidity scoring.

A wonderful looking premium on a contract nobody trades is not an opportunity, it is
a quote. This is the filter that keeps the screener from surfacing 40 of them.

Four components, weighted by how much each one actually costs you:

- Spread as a fraction of mid, weighted highest. It is the only component you pay
  directly and immediately, on entry and again on exit.
- Open interest. Whether a market exists at all at this strike.
- Volume. Whether it traded today, which says more about a market maker's willingness
  to quote it now than open interest built up over months does.
- Trade staleness. A last trade from three days ago against a live spot means the
  displayed marks are decorative.

All thresholds are arguments. The defaults below suit liquid US equity and ETF
options and are too generous for anything thinner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from optscan.models import OptionContract

#: Open interest: nothing below poor, full marks at good.
DEFAULT_OI_POOR = 50.0
DEFAULT_OI_GOOD = 1000.0

#: Volume traded today.
DEFAULT_VOLUME_POOR = 0.0
DEFAULT_VOLUME_GOOD = 200.0

#: Spread as a fraction of mid. Lower is better, so these run the other way.
DEFAULT_SPREAD_GOOD = 0.02
DEFAULT_SPREAD_POOR = 0.25

#: Hours since the last print.
DEFAULT_STALENESS_GOOD_HOURS = 1.0
DEFAULT_STALENESS_POOR_HOURS = 48.0

#: Score at or above which the default screen calls a contract tradeable. A judgment
#: call, and the first thing to tune if the screener surfaces things you cannot fill.
DEFAULT_TRADEABLE_THRESHOLD = 0.35

DEFAULT_WEIGHTS = {
    "spread": 0.40,
    "open_interest": 0.30,
    "volume": 0.20,
    "staleness": 0.10,
}


@dataclass(frozen=True, slots=True)
class LiquidityScore:
    """A 0 to 1 score and every input that produced it.

    The components are kept so the UI can show why something scored badly. A score
    with no explanation is a number people learn to ignore.
    """

    score: float
    components: dict[str, float] = field(default_factory=dict)
    open_interest: int | None = None
    volume: int | None = None
    spread_pct: float | None = None
    trade_age_hours: float | None = None
    notes: tuple[str, ...] = ()

    @property
    def tradeable(self) -> bool:
        """A blunt yes or no for the default screen."""
        return self.score >= DEFAULT_TRADEABLE_THRESHOLD

    def worst_component(self) -> str | None:
        """Which component is dragging the score down."""
        if not self.components:
            return None
        return min(self.components, key=lambda key: self.components[key])


def ramp(value: float | None, poor: float, good: float) -> float | None:
    """Linear 0 to 1 between two thresholds, clamped. Works in either direction.

    Returns None for a missing input, which is different from a zero score: unknown
    open interest is not the same as no open interest, and averaging the two together
    would quietly invent data.
    """
    if value is None:
        return None
    if good == poor:
        raise ValueError("poor and good thresholds must differ")
    fraction = (value - poor) / (good - poor)
    return min(max(fraction, 0.0), 1.0)


def score_liquidity(
    contract: OptionContract,
    *,
    asof: datetime | None = None,
    oi_poor: float = DEFAULT_OI_POOR,
    oi_good: float = DEFAULT_OI_GOOD,
    volume_poor: float = DEFAULT_VOLUME_POOR,
    volume_good: float = DEFAULT_VOLUME_GOOD,
    spread_good: float = DEFAULT_SPREAD_GOOD,
    spread_poor: float = DEFAULT_SPREAD_POOR,
    staleness_good_hours: float = DEFAULT_STALENESS_GOOD_HOURS,
    staleness_poor_hours: float = DEFAULT_STALENESS_POOR_HOURS,
    weights: dict[str, float] | None = None,
) -> LiquidityScore:
    """Score one contract's tradeability.

    Missing components are dropped and the remaining weights renormalized, rather than
    scored as zero. A contract with no reported volume is not necessarily illiquid,
    and treating absent data as bad data would systematically punish whichever fields
    the current vendor happens not to populate.
    """
    weights = weights or DEFAULT_WEIGHTS
    notes: list[str] = []

    spread_pct = contract.spread_pct_of_mid
    trade_age_hours = None
    if contract.last_trade_at is not None:
        age_seconds = contract.quote_age_seconds(asof)
        trade_age_hours = age_seconds / 3600.0 if age_seconds is not None else None

    components: dict[str, float] = {}

    oi_score = ramp(contract.open_interest, oi_poor, oi_good)
    if oi_score is not None:
        components["open_interest"] = oi_score
    else:
        notes.append("no open interest reported")

    volume_score = ramp(contract.volume, volume_poor, volume_good)
    if volume_score is not None:
        components["volume"] = volume_score
    else:
        notes.append("no volume reported")

    spread_score = ramp(spread_pct, spread_poor, spread_good)
    if spread_score is not None:
        components["spread"] = spread_score
    else:
        notes.append("no two sided market, so no spread to measure")

    staleness_score = ramp(trade_age_hours, staleness_poor_hours, staleness_good_hours)
    if staleness_score is not None:
        components["staleness"] = staleness_score
    else:
        notes.append("never traded, so staleness is unknown")

    if not components:
        return LiquidityScore(
            score=0.0,
            components={},
            open_interest=contract.open_interest,
            volume=contract.volume,
            spread_pct=spread_pct,
            trade_age_hours=trade_age_hours,
            notes=("nothing measurable about this quote",),
        )

    total_weight = sum(weights.get(name, 0.0) for name in components)
    if total_weight <= 0:
        raise ValueError("weights do not cover any of the available components")

    score = sum(value * weights.get(name, 0.0) for name, value in components.items())
    score /= total_weight

    return LiquidityScore(
        score=score,
        components=components,
        open_interest=contract.open_interest,
        volume=contract.volume,
        spread_pct=spread_pct,
        trade_age_hours=trade_age_hours,
        notes=tuple(notes),
    )
