"""Screen config loading, and the filter layer.

The config tests care most about what happens when a user gets it wrong, since a
silently ignored threshold is worse than a crash.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from optscan.analytics.events import EventWindow
from optscan.analytics.ivrank import Confidence, IVRank
from optscan.analytics.returns import short_put_profile
from optscan.models import Action, Leg, Right, Strategy
from optscan.screener.config import ScreenConfig
from optscan.screener.filters import (
    Rejection,
    RejectionTally,
    check_delta,
    check_dte,
    check_events,
    check_liquidity,
    check_premium,
    check_volatility,
    evaluate,
)
from optscan.screener.strategies.base import Candidate

NOW = datetime(2026, 7, 30, 19, 45, tzinfo=UTC)
EXPIRY = date(2026, 8, 21)


def leg(**overrides) -> Leg:
    defaults = dict(
        action=Action.SELL,
        right=Right.PUT,
        strike=730.0,
        expiry=EXPIRY,
        bid=4.00,
        ask=4.10,
        mid=4.05,
        iv=0.18,
        delta=-0.22,
        open_interest=2500,
        volume=400,
        fetched_at=NOW,
        source="test",
    )
    return Leg(**{**defaults, **overrides})


def candidate(**overrides) -> Candidate:
    legs = overrides.pop("legs", (leg(),))
    # 5.00 on a 730 strike over 22 days annualizes to 11.4 percent, which clears
    # the default 10 percent floor. Deliberately not borderline.
    credit = overrides.pop("credit", 5.00)
    dte = overrides.pop("dte", 22)
    defaults = dict(
        strategy=Strategy.CASH_SECURED_PUT,
        legs=legs,
        credit=credit,
        profile=short_put_profile(legs[0].strike, credit, dte),
        short_delta=legs[0].delta,
        short_iv=legs[0].iv,
        probability_of_profit=0.80,
        probability_of_touch=0.40,
        liquidity_score=0.90,
    )
    return Candidate(**{**defaults, **overrides})


class FakeAnalysis:
    """Just enough of SymbolAnalysis for the filters under test."""

    def __init__(self, *, iv_rank=None, events=None, atm_iv=0.18):
        self.symbol = "SPY"
        self.spot = 740.53
        self.session_date = date(2026, 7, 30)
        self.iv_rank = iv_rank
        self.events = events or EventWindow()
        self.expiries = [FakeExpiry(atm_iv)]


class FakeExpiry:
    def __init__(self, atm_iv: float):
        self.atm_iv = atm_iv
        self.expiry = EXPIRY
        self.dte = 22
        self.time = 22 / 365
        self.solve_rate = 0.9


class TestConfigLoading:
    def test_defaults_when_there_is_no_file(self, tmp_path: Path) -> None:
        config = ScreenConfig.load(tmp_path / "absent.yaml")
        assert config.filters.dte.min_dte == 21
        assert config.max_results == 50

    def test_none_path_is_defaults(self) -> None:
        assert ScreenConfig.load(None).filters.delta.max_abs_delta == 0.30

    def test_partial_yaml_overrides_only_what_it_names(self, tmp_path: Path) -> None:
        path = tmp_path / "screen.yaml"
        path.write_text(
            yaml.safe_dump({"filters": {"dte": {"min_dte": 30, "max_dte": 45}}}),
            encoding="utf-8",
        )
        config = ScreenConfig.load(path)
        assert config.filters.dte.min_dte == 30
        assert config.filters.delta.max_abs_delta == 0.30  # untouched

    def test_a_typo_in_a_key_is_an_error(self, tmp_path: Path) -> None:
        """Otherwise the user believes a threshold is in force when it is not."""
        path = tmp_path / "screen.yaml"
        path.write_text(yaml.safe_dump({"filters": {"dte": {"min_dtee": 30}}}), encoding="utf-8")
        with pytest.raises(ValidationError):
            ScreenConfig.load(path)

    def test_a_non_mapping_file_is_an_error(self, tmp_path: Path) -> None:
        path = tmp_path / "screen.yaml"
        path.write_text(yaml.safe_dump([1, 2, 3]), encoding="utf-8")
        with pytest.raises(ValueError, match="must contain a YAML mapping"):
            ScreenConfig.load(path)

    def test_an_empty_file_is_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "screen.yaml"
        path.write_text("", encoding="utf-8")
        assert ScreenConfig.load(path).max_results == 50

    def test_round_trips_through_yaml(self) -> None:
        """`optscan config` output must be loadable back in."""
        config = ScreenConfig()
        reloaded = ScreenConfig.model_validate(yaml.safe_load(config.to_yaml()))
        assert reloaded == config

    def test_inverted_ranges_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="above max_dte"):
            ScreenConfig.model_validate({"filters": {"dte": {"min_dte": 60, "max_dte": 30}}})
        with pytest.raises(ValidationError, match="min_abs_delta is above"):
            ScreenConfig.model_validate(
                {"filters": {"delta": {"min_abs_delta": 0.5, "max_abs_delta": 0.2}}}
            )

    def test_all_zero_weights_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="at least one scoring weight"):
            ScreenConfig.model_validate(
                {
                    "weights": {
                        "premium": 0,
                        "iv_rank": 0,
                        "liquidity": 0,
                        "probability": 0,
                        "event_risk": 0,
                    }
                }
            )

    def test_vertical_gap_detection_is_off_by_default(self) -> None:
        """It has no working baseline. See find_vertical_mispricings."""
        assert ScreenConfig().gaps.vertical_mispricing_enabled is False


class TestFilters:
    def test_dte_window(self) -> None:
        config = ScreenConfig()
        assert check_dte(candidate(dte=22), config).passed
        assert check_dte(candidate(dte=5), config).reason is Rejection.DTE_TOO_SHORT
        assert check_dte(candidate(dte=200), config).reason is Rejection.DTE_TOO_LONG

    def test_delta_band(self) -> None:
        config = ScreenConfig()
        assert check_delta(candidate(), config).passed
        assert (
            check_delta(candidate(legs=(leg(delta=-0.02),)), config).reason
            is Rejection.DELTA_TOO_LOW
        )
        assert (
            check_delta(candidate(legs=(leg(delta=-0.60),)), config).reason
            is Rejection.DELTA_TOO_HIGH
        )

    def test_a_leg_with_no_delta_cannot_be_judged(self) -> None:
        assert (
            check_delta(candidate(legs=(leg(delta=None),)), ScreenConfig()).reason
            is Rejection.NO_DELTA
        )

    def test_multi_leg_delta_uses_the_nearest_short_strike(self) -> None:
        """A condor is near delta neutral and its shorts are still 20 delta each."""
        legs = (leg(delta=-0.20), leg(right=Right.CALL, strike=760.0, delta=0.45))
        assert check_delta(candidate(legs=legs), ScreenConfig()).reason is Rejection.DELTA_TOO_HIGH

    def test_premium_floor(self) -> None:
        config = ScreenConfig()
        assert check_premium(candidate(), config).passed
        assert check_premium(candidate(credit=0.01), config).reason is Rejection.CREDIT_TOO_SMALL

    def test_credit_to_width_only_applies_to_defined_risk(self) -> None:
        """A single leg has no width, so the ratio cannot and should not apply."""
        config = ScreenConfig()
        thin = candidate(credit=0.30, width=5.0)
        assert check_premium(thin, config).reason is Rejection.CREDIT_TO_WIDTH_TOO_LOW
        assert check_premium(candidate(width=None), config).passed

    def test_annualized_return_floor(self) -> None:
        """0.15 on a 730 strike over 60 days is 0.12 percent annualized."""
        config = ScreenConfig()
        assert check_premium(candidate(credit=0.15, dte=60), config).reason is (
            Rejection.RETURN_TOO_LOW
        )

    def test_liquidity_checks_every_leg_not_just_the_short(self) -> None:
        config = ScreenConfig()
        legs = (leg(), leg(action=Action.BUY, strike=725.0, open_interest=3))
        assert (
            check_liquidity(candidate(legs=legs), config).reason is Rejection.OPEN_INTEREST_TOO_LOW
        )

    def test_wide_spread_is_rejected(self) -> None:
        wide = candidate(legs=(leg(bid=3.0, ask=5.0, mid=4.0),))
        assert check_liquidity(wide, ScreenConfig()).reason is Rejection.SPREAD_TOO_WIDE

    def test_liquidity_score_floor(self) -> None:
        assert (
            check_liquidity(candidate(liquidity_score=0.05), ScreenConfig()).reason
            is Rejection.LIQUIDITY_TOO_LOW
        )


class TestVolatilityFilter:
    def test_no_rules_means_no_gate(self) -> None:
        assert check_volatility(FakeAnalysis(), ScreenConfig()).passed

    def test_thin_history_passes_when_the_rank_is_not_required(self) -> None:
        """The default. Before the history is deep enough, an IV rank gate would
        pass or reject everything depending on which side of the line it landed."""
        config = ScreenConfig.model_validate({"filters": {"volatility": {"min_iv_rank": 0.5}}})
        thin = IVRank(
            iv=0.2,
            rank=None,
            percentile=None,
            observations=3,
            span_days=3,
            completeness=1.0,
            confidence=Confidence.INSUFFICIENT,
        )
        assert check_volatility(FakeAnalysis(iv_rank=thin), config).passed

    def test_requiring_a_rank_rejects_when_there_is_none(self) -> None:
        config = ScreenConfig.model_validate({"filters": {"volatility": {"require_iv_rank": True}}})
        assert check_volatility(FakeAnalysis(), config).reason is Rejection.IV_RANK_UNAVAILABLE

    def test_a_usable_rank_below_the_floor_rejects(self) -> None:
        config = ScreenConfig.model_validate({"filters": {"volatility": {"min_iv_rank": 0.5}}})
        low = IVRank(
            iv=0.2,
            rank=0.2,
            percentile=0.3,
            observations=200,
            span_days=300,
            completeness=1.0,
            confidence=Confidence.HIGH,
        )
        assert (
            check_volatility(FakeAnalysis(iv_rank=low), config).reason is Rejection.IV_RANK_TOO_LOW
        )

    def test_absolute_iv_floor(self) -> None:
        config = ScreenConfig.model_validate({"filters": {"volatility": {"min_iv": 0.30}}})
        assert check_volatility(FakeAnalysis(atm_iv=0.12), config).reason is Rejection.IV_TOO_LOW
        assert check_volatility(FakeAnalysis(atm_iv=0.45), config).passed


class TestEventFilter:
    def test_earnings_before_expiry_is_excluded_by_default(self) -> None:
        analysis = FakeAnalysis(events=EventWindow(earnings=date(2026, 8, 10)))
        result = check_events(candidate(), analysis, analysis.expiries[0], ScreenConfig())
        assert result.reason is Rejection.EARNINGS_BEFORE_EXPIRY

    def test_earnings_after_expiry_is_fine(self) -> None:
        analysis = FakeAnalysis(events=EventWindow(earnings=date(2026, 12, 10)))
        assert check_events(candidate(), analysis, analysis.expiries[0], ScreenConfig()).passed

    def test_the_exclusion_can_be_turned_off(self) -> None:
        config = ScreenConfig.model_validate({"filters": {"events": {"exclude_earnings": False}}})
        analysis = FakeAnalysis(events=EventWindow(earnings=date(2026, 8, 10)))
        assert check_events(candidate(), analysis, analysis.expiries[0], config).passed

    def test_short_call_with_assignment_risk_is_excluded(self) -> None:
        """0.05 of extrinsic against a 0.25 dividend two days out."""
        analysis = FakeAnalysis(
            events=EventWindow(ex_dividend=date(2026, 8, 1), dividend_amount=0.25)
        )
        short_call = leg(right=Right.CALL, strike=740.0, mid=0.55, delta=0.5)
        result = check_events(
            candidate(legs=(short_call,)), analysis, analysis.expiries[0], ScreenConfig()
        )
        assert result.reason is Rejection.EARLY_ASSIGNMENT_RISK


class TestEvaluateAndTally:
    def test_evaluate_reports_the_first_and_most_decisive_failure(self) -> None:
        analysis = FakeAnalysis()
        bad = candidate(dte=2, legs=(leg(delta=-0.9),))
        result = evaluate(bad, analysis, analysis.expiries[0], ScreenConfig())
        assert result.reason is Rejection.DTE_TOO_SHORT

    def test_a_good_candidate_passes_everything(self) -> None:
        analysis = FakeAnalysis()
        assert evaluate(candidate(), analysis, analysis.expiries[0], ScreenConfig()).passed

    def test_tally_explains_an_empty_table(self) -> None:
        """The reason a screener keeps a user's trust when it finds nothing."""
        analysis = FakeAnalysis()
        tally = RejectionTally.empty()
        config = ScreenConfig()
        for dte in (2, 3, 4):
            tally.record(evaluate(candidate(dte=dte), analysis, analysis.expiries[0], config))
        tally.record(evaluate(candidate(), analysis, analysis.expiries[0], config))

        assert tally.considered == 4
        assert tally.passed == 1
        assert tally.counts[Rejection.DTE_TOO_SHORT] == 3
        assert "dte_too_short 3" in tally.summary()

    def test_tallies_merge(self) -> None:
        first = RejectionTally.empty()
        first.counts[Rejection.DTE_TOO_SHORT] = 2
        first.considered, first.passed = 5, 3
        second = RejectionTally.empty()
        second.counts[Rejection.DTE_TOO_SHORT] = 1
        second.considered, second.passed = 4, 3

        first.merge(second)
        assert first.counts[Rejection.DTE_TOO_SHORT] == 3
        assert first.considered == 9
        assert first.passed == 6
