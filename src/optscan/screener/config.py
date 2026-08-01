"""Screen configuration.

Every threshold a user might tune lives here and is loaded from YAML. Nothing in
strategies/, filters.py, or scoring.py hardcodes a number: they take a config object
and read from it, so changing what the screen looks for never means changing code.

The defaults below are deliberately middle of the road for liquid US equity and ETF
options. They are a starting point to argue with, not a recommendation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_CONFIG_FILENAME = "screen.yaml"


class StrictModel(BaseModel):
    """Unknown keys are an error.

    A typo in a YAML key would otherwise silently leave a filter at its default, and
    the user would believe a threshold is in force when it is not.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class DteFilter(StrictModel):
    """Days to expiry window.

    The default 21 to 60 covers the range most premium selling research points at:
    far enough out that theta is not yet dominated by gamma, near enough that
    annualized return is worth the capital.
    """

    min_dte: int = Field(default=21, ge=0)
    max_dte: int = Field(default=60, ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.min_dte > self.max_dte:
            raise ValueError(f"min_dte {self.min_dte} is above max_dte {self.max_dte}")
        return self


class DeltaFilter(StrictModel):
    """Absolute delta band for the short leg.

    0.15 to 0.30 is the conventional short premium band. Below it the credit stops
    paying for the risk of being tested; above it the position is closer to a
    directional bet than a volatility one.
    """

    min_abs_delta: float = Field(default=0.15, ge=0.0, le=1.0)
    max_abs_delta: float = Field(default=0.30, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.min_abs_delta > self.max_abs_delta:
            raise ValueError("min_abs_delta is above max_abs_delta")
        return self


class LiquidityFilter(StrictModel):
    """Whether a quote can actually be traded."""

    min_open_interest: int = Field(default=100, ge=0)
    min_volume: int = Field(default=0, ge=0)
    max_spread_pct: float = Field(default=0.10, gt=0.0)
    min_liquidity_score: float = Field(default=0.35, ge=0.0, le=1.0)


class CostModel(StrictModel):
    """Broker commissions, applied to every candidate's economics.

    Modelled because leaving them out does not shift candidates equally: a fixed
    couple of dollars is an eighth of a narrow spread's maximum profit and noise
    against a cash secured put's. Omitting them systematically promotes the narrow
    trades, which is exactly what the first version of this screen did.
    """

    per_contract: float = Field(default=0.65, ge=0.0)
    per_trade: float = Field(default=0.0, ge=0.0)
    assume_closing_trade: bool = True


class PremiumFilter(StrictModel):
    """How much the position has to pay to be worth the capital and the risk."""

    min_credit: float = Field(default=0.10, gt=0.0)
    min_max_profit: float = Field(
        default=25.0,
        ge=0.0,
        description=(
            "Dollars of maximum profit, net of commissions. A percentage return says "
            "nothing about whether a trade is worth the ticket: a one wide spread can "
            "annualize at 400 percent and clear 18 dollars."
        ),
    )
    min_credit_to_width: float = Field(
        default=0.20,
        gt=0.0,
        description="Defined risk only. Credit as a fraction of the spread width.",
    )
    min_annualized_return: float = Field(default=0.10, ge=0.0)


class VolatilityFilter(StrictModel):
    """Volatility context required before selling.

    require_iv_rank is off by default and must stay that way until the snapshot
    history is deep enough to produce one. Turning it on with three weeks of history
    filters on a number that does not mean what its name says.
    """

    min_iv_rank: float | None = Field(default=None, ge=0.0, le=1.0)
    require_iv_rank: bool = False
    min_iv: float | None = Field(default=None, gt=0.0)


class EventFilter(StrictModel):
    """Scheduled events to stay clear of."""

    exclude_earnings: bool = True
    earnings_buffer_days: int = Field(default=0, ge=0)
    exclude_early_assignment_risk: bool = True


class Filters(StrictModel):
    """Everything a candidate must pass."""

    dte: DteFilter = DteFilter()
    delta: DeltaFilter = DeltaFilter()
    liquidity: LiquidityFilter = LiquidityFilter()
    premium: PremiumFilter = PremiumFilter()
    volatility: VolatilityFilter = VolatilityFilter()
    events: EventFilter = EventFilter()


class ScoringWeights(StrictModel):
    """Weights for the composite score. Normalized at use, so they need not sum to 1.

    Premium and liquidity lead deliberately. Premium is the reason to do the trade at
    all, and liquidity is what decides whether the premium is real or a screenshot.
    """

    premium: float = Field(default=0.30, ge=0.0)
    iv_rank: float = Field(default=0.20, ge=0.0)
    liquidity: float = Field(default=0.25, ge=0.0)
    probability: float = Field(default=0.15, ge=0.0)
    event_risk: float = Field(default=0.10, ge=0.0)

    @model_validator(mode="after")
    def _not_all_zero(self) -> Self:
        if self.total <= 0:
            raise ValueError("at least one scoring weight must be above zero")
        return self

    @property
    def total(self) -> float:
        return self.premium + self.iv_rank + self.liquidity + self.probability + self.event_risk

    def as_dict(self) -> dict[str, float]:
        return {
            "premium": self.premium,
            "iv_rank": self.iv_rank,
            "liquidity": self.liquidity,
            "probability": self.probability,
            "event_risk": self.event_risk,
        }


class ScoringNormalization(StrictModel):
    """The ramps that turn raw metrics into 0 to 1 component scores.

    These are the least defensible numbers in the project: they decide that a 25
    percent annualized return scores full marks and a 5 percent one scores zero, and
    nothing outside this file justifies those choices. They are exposed rather than
    buried so that Phase 8 can replace them with values that earned their place.
    """

    annualized_return_floor: float = Field(default=0.05, ge=0.0)
    annualized_return_ceiling: float = Field(default=0.25, gt=0.0)
    probability_floor: float = Field(default=0.50, ge=0.0, le=1.0)
    probability_ceiling: float = Field(default=0.90, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.annualized_return_floor >= self.annualized_return_ceiling:
            raise ValueError("annualized_return_floor must be below its ceiling")
        if self.probability_floor >= self.probability_ceiling:
            raise ValueError("probability_floor must be below its ceiling")
        return self


class StrategyConfig(StrictModel):
    """Which strategies to generate, and their shape parameters."""

    enabled: tuple[str, ...] = (
        "cash_secured_put",
        "put_credit_spread",
        "call_credit_spread",
        "iron_condor",
    )
    spread_widths: tuple[float, ...] = Field(
        default=(1.0, 2.5, 5.0, 10.0),
        description="Candidate widths in strike points. The nearest listed strike is used.",
    )
    max_candidates_per_expiry: int = Field(default=200, gt=0)


class GapsConfig(StrictModel):
    """Thresholds for the mispricing hunt.

    Every one of these is a "how far from normal is abnormal" question, and none of
    them has a principled answer yet. They are starting points chosen to flag roughly
    the top one percent of a liquid chain, and Phase 8 is where they get calibrated
    against whether the flags meant anything.
    """

    enabled: bool = True
    min_skew_residual: float = Field(
        default=0.03,
        gt=0.0,
        description="Vol points a strike must deviate from the fitted smile, as a floor.",
    )
    min_skew_zscore: float = Field(
        default=3.0,
        gt=0.0,
        description=(
            "Deviations off the fit's own residual scale. A fixed number of vol points "
            "cannot be right for both a 12 vol index and a 90 vol single name, so both "
            "this and the floor above must be cleared."
        ),
    )
    min_term_inversion: float = Field(default=0.03, gt=0.0)
    vertical_mispricing_enabled: bool = Field(
        default=False,
        description=(
            "Off by default and honestly so. See find_vertical_mispricings for why: "
            "the measure it uses varies smoothly with moneyness, so without a "
            "historical baseline there is no way to separate an anomaly from the "
            "normal shape of the variance risk premium."
        ),
    )
    min_vertical_zscore: float = Field(
        default=3.0,
        gt=0.0,
        description=(
            "Only used when vertical_mispricing_enabled is on. Robust deviations above "
            "the rest of the same expiry and width."
        ),
    )
    min_parity_violation: float = Field(
        default=0.10,
        gt=0.0,
        description="Dollars of put call parity breach after crossing both spreads.",
    )
    max_quote_age_hours: float = Field(
        default=4.0,
        gt=0.0,
        description="A gap on a stale quote is a stale quote, not a gap.",
    )


class ManagementConfig(StrictModel):
    """When a held position wants looking at. Phase 7.

    Every number here is a convention rather than a finding. Nothing in this project
    has been validated against outcomes, which is what Phase 8 is for, so these are
    starting points a user is expected to change rather than settings that were tuned.
    """

    profit_target: float = Field(
        default=0.50,
        gt=0.0,
        le=1.0,
        description=(
            "Share of maximum profit at which to consider closing. The one management "
            "rule with a real argument behind it: the last of a credit takes longest "
            "to collect and carries the same tail risk the whole time."
        ),
    )
    dte_threshold: int = Field(
        default=21,
        ge=0,
        description="Days to expiry at which gamma starts to dominate the position.",
    )
    delta_breach: float = Field(
        default=0.30,
        gt=0.0,
        le=1.0,
        description=(
            "Absolute delta on a short leg that counts as a breach. Measured per leg, "
            "since a condor's net delta can read flat while one side is badly tested."
        ),
    )
    alert_min_severity: int = Field(
        default=3,
        ge=0,
        description=(
            "Severity at or above which a trigger is worth interrupting somebody for. "
            "Below it the trigger still shows in the dashboard, it just does not chase."
        ),
    )
    mark_convention: str = Field(
        default="closing",
        description=(
            "closing or mid. Closing marks a short leg at the ask, which is what it "
            "costs to actually get out. Mid overstates a short book by half the spread "
            "on every leg, which matters most in the range where a profit target fires."
        ),
    )

    @field_validator("mark_convention")
    @classmethod
    def _known_convention(cls, value: str) -> str:
        allowed = {"closing", "mid"}
        if value not in allowed:
            raise ValueError(f"mark_convention must be one of {sorted(allowed)}, got {value!r}")
        return value


class ScreenConfig(StrictModel):
    """The whole screen, as loaded from YAML."""

    filters: Filters = Filters()
    costs: CostModel = CostModel()
    weights: ScoringWeights = ScoringWeights()
    normalization: ScoringNormalization = ScoringNormalization()
    strategies: StrategyConfig = StrategyConfig()
    gaps: GapsConfig = GapsConfig()
    management: ManagementConfig = ManagementConfig()
    max_results: int = Field(default=50, gt=0)

    @classmethod
    def load(cls, path: Path | None) -> ScreenConfig:
        """Load from YAML, or return defaults when there is no file.

        A missing file is fine and means defaults. A malformed one is not: it fails
        loudly rather than silently reverting to defaults the user did not choose.
        """
        if path is None or not path.exists():
            return cls()

        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ValueError(f"{path} must contain a YAML mapping, got {type(raw).__name__}")
        return cls.model_validate(raw)

    def to_yaml(self) -> str:
        """Serialize the whole effective config, defaults included.

        Used by `optscan config` so a user can see every knob and its current value
        rather than guessing what is available.
        """
        return yaml.safe_dump(_plain(self.model_dump(mode="json")), sort_keys=False, indent=2)


def _plain(value: Any) -> Any:
    """Tuples become lists so the YAML comes out readable."""
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    return value
