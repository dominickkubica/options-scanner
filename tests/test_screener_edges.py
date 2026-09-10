"""The paths the happy path does not reach.

Covered calls, the gap detectors that are off by default, the scoring penalties, and
the executability verdicts. These are the branches where a mistake is invisible until
someone turns a switch on.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from optscan.analytics.events import EventWindow
from optscan.analytics.ivrank import Confidence, IVRank
from optscan.analytics.returns import (
    NO_COMMISSIONS,
    Commissions,
    covered_call_profile,
    credit_spread_profile,
    iron_condor_profile,
    short_put_profile,
)
from optscan.models import ChainSnapshot, Right, Strategy
from optscan.screener.config import ScreenConfig
from optscan.screener.context import analyze_snapshot
from optscan.screener.gaps import (
    Executability,
    GapKind,
    assess_executability,
    find_gaps,
    find_vertical_mispricings,
    implied_forward,
)
from optscan.screener.scan import scan_analysis
from optscan.screener.scoring import score_event_risk, score_iv_rank, score_premium
from optscan.screener.strategies import GENERATORS

RATE = 0.043
NOW = datetime(2026, 7, 30, 19, 45, tzinfo=UTC)


@pytest.fixture
def analysis(frozen_snapshot: ChainSnapshot):
    return analyze_snapshot(frozen_snapshot, rate=RATE)


@pytest.fixture
def wide_config() -> ScreenConfig:
    return ScreenConfig.model_validate(
        {
            "filters": {
                "dte": {"min_dte": 0, "max_dte": 60},
                "premium": {"min_annualized_return": 0.0},
            }
        }
    )


class TestCommissions:
    def test_cost_scales_with_legs_and_contracts(self) -> None:
        """0.65 per contract, four legs, in and out: 4 * 0.65 * 2 = 5.20."""
        assert Commissions().cost(legs=4) == pytest.approx(5.20)
        assert Commissions().cost(legs=1) == pytest.approx(1.30)
        assert Commissions().cost(legs=2, contracts=3) == pytest.approx(7.80)

    def test_holding_to_expiration_pays_once(self) -> None:
        model = Commissions(assume_closing_trade=False)
        assert model.cost(legs=2) == pytest.approx(1.30)

    def test_a_per_trade_fee_is_added_once_per_side(self) -> None:
        model = Commissions(per_contract=0.50, per_trade=1.00)
        assert model.cost(legs=2) == pytest.approx((2 * 0.50 + 1.00) * 2)

    def test_the_no_commission_model_is_free(self) -> None:
        assert NO_COMMISSIONS.cost(legs=4, contracts=10) == 0.0

    def test_a_position_needs_at_least_one_leg(self) -> None:
        with pytest.raises(ValueError, match="at least one leg"):
            Commissions().cost(legs=0)

    def test_gross_profit_stays_visible(self) -> None:
        """The cut has to be inspectable, not just applied."""
        profile = short_put_profile(95.0, 2.00, 30, Commissions())
        assert profile.gross_max_profit == pytest.approx(200.0)
        assert profile.max_profit == pytest.approx(198.70)
        assert profile.commission == pytest.approx(1.30)

    def test_commissions_make_the_loss_worse_too(self) -> None:
        gross = credit_spread_profile(95.0, 90.0, 1.50, 45)
        net = credit_spread_profile(95.0, 90.0, 1.50, 45, Commissions())
        assert net.max_loss > gross.max_loss
        assert net.max_loss == pytest.approx(gross.max_loss + net.commission)

    def test_they_hit_narrow_spreads_hardest(self) -> None:
        """The whole reason for modelling them: a fixed fee is a large fraction of a
        small maximum profit and noise against a large one."""
        model = Commissions()
        narrow = credit_spread_profile(763.0, 762.0, 0.21, 22, model)
        wide = credit_spread_profile(770.0, 760.0, 2.10, 22, model)
        assert narrow.commission == wide.commission
        assert narrow.commission / narrow.gross_max_profit > 0.12
        assert wide.commission / wide.gross_max_profit < 0.02

    def test_a_condor_pays_for_four_legs(self) -> None:
        profile = iron_condor_profile(95.0, 90.0, 105.0, 110.0, 2.00, 45, Commissions())
        assert profile.commission == pytest.approx(5.20)

    def test_a_covered_call_pays_for_one(self) -> None:
        profile = covered_call_profile(105.0, 1.50, 100.0, 45, Commissions())
        assert profile.commission == pytest.approx(1.30)
        assert profile.max_profit == pytest.approx(650.0 - 1.30)


class TestCoveredCall:
    def test_generates_only_upside_strikes(self, analysis, wide_config) -> None:
        candidates = GENERATORS[Strategy.COVERED_CALL].generate(
            analysis, analysis.expiries[0], wide_config
        )
        assert candidates
        for candidate in candidates:
            assert candidate.legs[0].strike >= analysis.spot
            assert candidate.legs[0].right is Right.CALL

    def test_capital_is_the_shares_not_the_option(self, analysis, wide_config) -> None:
        candidate = GENERATORS[Strategy.COVERED_CALL].generate(
            analysis, analysis.expiries[0], wide_config
        )[0]
        assert candidate.profile.capital == pytest.approx(analysis.spot * 100)

    def test_the_cost_basis_assumption_is_stated_on_every_candidate(
        self, analysis, wide_config
    ) -> None:
        """The tool does not know your basis, so it says what it assumed."""
        candidate = GENERATORS[Strategy.COVERED_CALL].generate(
            analysis, analysis.expiries[0], wide_config
        )[0]
        assert any("cost basis" in note for note in candidate.notes)


class TestVerticalGapDetector:
    def test_produces_nothing_while_disabled(self, analysis) -> None:
        config = ScreenConfig().gaps
        assert config.vertical_mispricing_enabled is False
        assert find_vertical_mispricings(analysis, analysis.expiries[0], config) == []

    def test_turning_it_on_returns_at_the_money_spreads(self, analysis) -> None:
        """Documenting the behaviour rather than endorsing it.

        The measure runs smoothly with moneyness rather than scattering, so the
        outliers it reports are the near the money spreads, which is a fact about
        gamma. This test exists so that if someone ever fixes the baseline, the change
        in behaviour is visible.
        """
        config = ScreenConfig.model_validate({"gaps": {"vertical_mispricing_enabled": True}}).gaps
        found = find_vertical_mispricings(analysis, analysis.expiries[0], config)
        if not found:
            pytest.skip("fixture expiry has too few verticals for a baseline")
        assert all(gap.kind is GapKind.VERTICAL_MISPRICING for gap in found)
        nearest = min(found, key=lambda gap: abs(gap.strike - analysis.spot))
        assert abs(nearest.strike - analysis.spot) / analysis.spot < 0.05

    def test_a_raised_threshold_narrows_the_list(self, analysis) -> None:
        loose = ScreenConfig.model_validate(
            {"gaps": {"vertical_mispricing_enabled": True, "min_vertical_zscore": 2.0}}
        ).gaps
        strict = ScreenConfig.model_validate(
            {"gaps": {"vertical_mispricing_enabled": True, "min_vertical_zscore": 30.0}}
        ).gaps
        assert len(find_vertical_mispricings(analysis, analysis.expiries[0], strict)) <= len(
            find_vertical_mispricings(analysis, analysis.expiries[0], loose)
        )


class TestGapEdges:
    def test_gaps_can_be_turned_off_entirely(self, analysis) -> None:
        config = ScreenConfig.model_validate({"gaps": {"enabled": False}}).gaps
        assert find_gaps(analysis, config) == []

    def test_no_contract_is_not_tradeable(self, analysis) -> None:
        verdict, caveats = assess_executability(None, analysis, ScreenConfig().gaps)
        assert verdict is Executability.NO_TWO_SIDED_MARKET
        assert caveats

    def test_a_stale_quote_is_called_out(self, analysis) -> None:
        """A gap on a quote from three days ago is a stale quote, not a gap."""
        front = analysis.expiries[0]
        contract = next(
            c
            for c in front.chain.contracts
            if c.has_two_sided_market and c.last_trade_at is not None
        )
        old = contract.model_copy(update={"last_trade_at": analysis.asof - timedelta(days=3)})
        verdict, caveats = assess_executability(old, analysis, ScreenConfig().gaps)
        assert verdict is Executability.STALE_QUOTE
        assert "stale mark" in caveats[0]

    def test_a_wide_quote_is_called_out(self, analysis) -> None:
        """Freshly traded, so width is the only thing left to fail on. Staleness is
        checked first, which is the right order and makes the fixture matter."""
        front = analysis.expiries[0]
        contract = next(c for c in front.chain.contracts if c.has_two_sided_market)
        wide = contract.model_copy(
            update={"bid": 1.00, "ask": 3.00, "last_trade_at": analysis.asof}
        )
        verdict, _ = assess_executability(wide, analysis, ScreenConfig().gaps)
        assert verdict is Executability.WIDE_SPREAD

    def test_thin_open_interest_is_called_out(self, analysis) -> None:
        front = analysis.expiries[0]
        contract = next(
            c
            for c in front.chain.contracts
            if c.has_two_sided_market and (c.spread_pct_of_mid or 1.0) < 0.05
        )
        thin = contract.model_copy(update={"open_interest": 2, "last_trade_at": analysis.asof})
        verdict, _ = assess_executability(thin, analysis, ScreenConfig().gaps)
        assert verdict is Executability.THIN_INTEREST

    def test_the_forward_needs_enough_strikes(self, analysis) -> None:
        """With nothing quoted two sided there is no forward, and therefore no
        parity check rather than a parity check against a guess."""
        front = analysis.expiries[0]
        stripped = front.chain.model_copy(
            update={
                "contracts": tuple(
                    c.model_copy(update={"bid": None, "ask": None}) for c in front.chain.contracts
                )
            }
        )
        blanked = type(front)(
            **{
                **{name: getattr(front, name) for name in front.__slots__},
                "chain": stripped,
            }
        )
        assert implied_forward(analysis, blanked) is None


class TestScoringBranches:
    def test_iv_rank_is_discounted_by_its_confidence(self, analysis) -> None:
        """The same rank from three months is worth less than from two years."""

        def scored(confidence: Confidence) -> float | None:
            ranked = type(analysis)(
                **{
                    **{name: getattr(analysis, name) for name in analysis.__slots__},
                    "iv_rank": IVRank(
                        iv=0.2,
                        rank=0.8,
                        percentile=0.8,
                        observations=100,
                        span_days=150,
                        completeness=1.0,
                        confidence=confidence,
                    ),
                }
            )
            return score_iv_rank(ranked)

        assert scored(Confidence.HIGH) == pytest.approx(0.8)
        assert scored(Confidence.MEDIUM) == pytest.approx(0.64)
        assert scored(Confidence.LOW) == pytest.approx(0.40)
        assert scored(Confidence.INSUFFICIENT) is None

    def test_no_rank_at_all_drops_the_component(self, analysis) -> None:
        assert score_iv_rank(analysis) is None

    def test_earnings_is_the_heavy_penalty(self, analysis, wide_config) -> None:
        """Reaching the scorer at all means the earnings filter was turned off, so
        the position is marked down rather than excluded."""
        from tests.test_screener_config_filters import candidate

        with_earnings = type(analysis)(
            **{
                **{name: getattr(analysis, name) for name in analysis.__slots__},
                "events": EventWindow(earnings=date(2026, 8, 1)),
            }
        )
        clean = score_event_risk(candidate(), analysis, analysis.expiries[0])
        dirty = score_event_risk(candidate(), with_earnings, with_earnings.expiries[0])
        assert clean == 1.0
        assert dirty == pytest.approx(0.4)

    def test_a_strangle_scores_premium_without_a_capital_figure(self) -> None:
        """Undefined risk has no denominator, so it falls back to a weaker measure
        and is discounted for it."""
        from optscan.analytics.returns import ReturnProfile
        from tests.test_screener_config_filters import candidate

        # 22 days, not 45. At 45 the fixture annualizes to 5.5%, which is under the
        # premium ramp's floor, so both sides floored to 0.0 and the comparison stopped
        # comparing anything. A fixture that cannot reach the scale it is testing proves
        # nothing, in the same way a constant close cannot exercise a spread estimator.
        naked = candidate(
            profile=ReturnProfile(credit=5.0, max_profit=500.0, max_loss=None, capital=None, dte=22)
        )
        defined = candidate(credit=5.0, dte=22)
        assert score_premium(defined, ScreenConfig()) > 0.0, "fixture must reach the ramp"
        assert score_premium(naked, ScreenConfig()) < score_premium(defined, ScreenConfig())

    def test_a_thin_surface_warns_on_every_row(self, frozen_snapshot, wide_config) -> None:
        """A chain where most strikes cannot support a vol is worth saying so about."""
        result = scan_analysis(
            analyze_snapshot(frozen_snapshot, rate=RATE, max_spread_pct=0.005), wide_config
        )
        if not result.opportunities:
            pytest.skip("nothing survives that tight a spread filter")
        assert any("usable implied vol" in warning for warning in result.opportunities[0].warnings)
