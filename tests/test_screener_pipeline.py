"""Strategies, scoring, gaps, and the whole pipeline, against the frozen SPY chain.

The roadmap asks for snapshot tests on a frozen fixture so that a scoring change shows
up as a diff rather than as a surprise. That is what TestRankingIsStable does: it
pins the top ranked candidate's identity and score, so any change to weights, ramps,
or filters has to be an explicit decision.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from optscan.analytics.events import EventWindow
from optscan.models import ChainSnapshot, Right, Strategy
from optscan.screener.config import ScreenConfig
from optscan.screener.context import analyze_snapshot
from optscan.screener.gaps import (
    Executability,
    GapKind,
    assess_executability,
    find_gaps,
    find_skew_anomalies,
    find_term_inversions,
    fit_smile,
    implied_forward,
)
from optscan.screener.scan import scan_analysis, scan_snapshot, scan_snapshots
from optscan.screener.scoring import composite, ramp, score_premium, score_probability
from optscan.screener.strategies import GENERATORS, generators_for
from optscan.screener.strategies.singles import CashSecuredPut

RATE = 0.043


@pytest.fixture
def analysis(frozen_snapshot: ChainSnapshot):
    return analyze_snapshot(frozen_snapshot, rate=RATE)


@pytest.fixture
def wide_config() -> ScreenConfig:
    """Loose enough that the two week fixture produces candidates at all.

    The frozen chain is 4 and 8 days out, well inside the default 21 day floor, so the
    default config correctly finds nothing in it.
    """
    return ScreenConfig.model_validate(
        {
            "filters": {
                "dte": {"min_dte": 0, "max_dte": 60},
                "premium": {"min_annualized_return": 0.0},
            }
        }
    )


class TestContext:
    def test_solves_the_whole_snapshot(self, analysis) -> None:
        assert analysis.symbol == "SPY"
        assert len(analysis.expiries) == 2
        assert analysis.spot > 0
        assert all(expiry.atm_iv is not None for expiry in analysis.expiries)

    def test_solve_rate_is_reported(self, analysis) -> None:
        """How much of a chain is unusable is a liquidity signal, not an error."""
        front = analysis.expiries[0]
        assert 0.0 < front.solve_rate < 1.0
        assert front.solved < front.total

    def test_term_structure_is_built(self, analysis) -> None:
        assert len(analysis.term.points) == 2
        assert analysis.term.slope is not None

    def test_greeks_come_from_each_strike_own_vol(self, analysis) -> None:
        front = analysis.expiries[0]
        strike = front.nearest_strike(analysis.spot, Right.PUT)
        greeks = front.greeks(strike, Right.PUT, analysis.spot, RATE)
        assert greeks is not None
        assert -1.0 < greeks.delta < 0.0

    def test_a_snapshot_with_no_price_is_refused(self, frozen_snapshot: ChainSnapshot) -> None:
        """Every greek downstream is a function of spot, so a guess is worse than nothing."""
        broken = frozen_snapshot.model_copy(
            update={
                "quote": frozen_snapshot.quote.model_copy(
                    update={"last": None, "bid": None, "ask": None}
                )
            }
        )
        with pytest.raises(ValueError, match="no usable underlying price"):
            analyze_snapshot(broken, rate=RATE)


class TestStrategies:
    def test_registry_covers_every_strategy(self) -> None:
        assert set(GENERATORS) == set(Strategy)

    def test_unknown_strategy_name_is_an_error(self) -> None:
        """A typo in config must not silently disable a strategy."""
        with pytest.raises(ValueError, match="unknown strategy"):
            generators_for(("cash_secured_puts",))

    def test_cash_secured_puts_are_all_out_of_the_money(
        self, analysis, wide_config: ScreenConfig
    ) -> None:
        candidates = CashSecuredPut().generate(analysis, analysis.expiries[0], wide_config)
        assert candidates
        for candidate in candidates:
            assert candidate.legs[0].strike <= analysis.spot
            assert candidate.legs[0].is_short
            assert candidate.credit > 0

    def test_verticals_have_a_long_wing_at_the_configured_width(
        self, analysis, wide_config: ScreenConfig
    ) -> None:
        generator = GENERATORS[Strategy.PUT_CREDIT_SPREAD]
        candidates = generator.generate(analysis, analysis.expiries[0], wide_config)
        assert candidates
        for candidate in candidates[:20]:
            short, long_leg = candidate.legs
            assert short.is_short and not long_leg.is_short
            assert long_leg.strike < short.strike
            assert candidate.width == pytest.approx(short.strike - long_leg.strike)
            assert candidate.credit < candidate.width

    def test_iron_condors_have_four_legs_and_a_balanced_shape(
        self, analysis, wide_config: ScreenConfig
    ) -> None:
        generator = GENERATORS[Strategy.IRON_CONDOR]
        candidates = generator.generate(analysis, analysis.expiries[0], wide_config)
        if not candidates:
            pytest.skip("no condor fits the fixture's strike ladder")
        candidate = candidates[0]
        assert len(candidate.legs) == 4
        assert sum(1 for leg in candidate.legs if leg.is_short) == 2

    def test_strangles_report_no_capital_rather_than_a_made_up_one(
        self, analysis, wide_config: ScreenConfig
    ) -> None:
        """Undefined risk has no honest denominator, so it gets none."""
        generator = GENERATORS[Strategy.SHORT_STRANGLE]
        candidates = generator.generate(analysis, analysis.expiries[0], wide_config)
        if not candidates:
            pytest.skip("no strangle fits the fixture's delta band")
        candidate = candidates[0]
        assert candidate.profile.max_loss is None
        assert candidate.profile.capital is None
        assert candidate.profile.return_on_capital is None

    def test_liquidity_takes_the_worst_leg(self, analysis, wide_config: ScreenConfig) -> None:
        """A position is only as fillable as its hardest leg."""
        generator = GENERATORS[Strategy.PUT_CREDIT_SPREAD]
        for candidate in generator.generate(analysis, analysis.expiries[0], wide_config)[:30]:
            per_leg = [
                analysis.liquidity_for(leg.expiry, leg.strike, leg.right) for leg in candidate.legs
            ]
            present = [value for value in per_leg if value is not None]
            if present and candidate.liquidity_score is not None:
                assert candidate.liquidity_score == pytest.approx(min(present))


class TestScoring:
    def test_ramp(self) -> None:
        assert ramp(0.15, 0.05, 0.25) == pytest.approx(0.5)
        assert ramp(0.01, 0.05, 0.25) == 0.0
        assert ramp(0.99, 0.05, 0.25) == 1.0
        assert ramp(None, 0.05, 0.25) is None

    def test_premium_scores_on_annualized_return(self, wide_config: ScreenConfig) -> None:
        from tests.test_screener_config_filters import candidate

        weak = candidate(credit=0.50, dte=45)
        strong = candidate(credit=20.0, dte=45)
        assert score_premium(strong, wide_config) > score_premium(weak, wide_config)

    def test_probability_ramps_between_the_configured_bounds(self) -> None:
        from tests.test_screener_config_filters import candidate

        config = ScreenConfig()
        assert score_probability(candidate(probability_of_profit=0.50), config) == 0.0
        assert score_probability(candidate(probability_of_profit=0.90), config) == 1.0
        assert score_probability(candidate(probability_of_profit=0.70), config) == pytest.approx(
            0.5
        )

    def test_a_missing_component_renormalizes_rather_than_scoring_zero(self) -> None:
        """Before the IV history is deep enough, every symbol would otherwise be
        dragged toward the bottom by a component nobody can compute yet."""
        from optscan.models import ScoreComponents

        now = datetime(2026, 7, 30, 19, 45, tzinfo=UTC)
        with_rank = ScoreComponents(
            premium=0.8,
            iv_rank=0.8,
            liquidity=0.8,
            probability=0.8,
            event_risk=0.8,
            fetched_at=now,
            source="test",
        )
        without = with_rank.model_copy(update={"iv_rank": None})
        config = ScreenConfig()
        assert composite(with_rank, config) == pytest.approx(0.8)
        assert composite(without, config) == pytest.approx(0.8)

    def test_scored_opportunities_carry_their_components(
        self, analysis, wide_config: ScreenConfig
    ) -> None:
        result = scan_analysis(analysis, wide_config)
        assert result.opportunities
        top = result.opportunities[0]
        assert 0.0 <= top.score <= 1.0
        assert top.components.premium is not None
        assert top.source == "yfinance"
        assert top.fetched_at == analysis.asof

    def test_thin_history_produces_a_caveat_on_every_row(self, frozen_snapshot) -> None:
        """One session of history cannot support an IV rank, and the row says so."""
        history = [(date(2026, 7, 29), 0.13), (date(2026, 7, 30), 0.14)]
        result = scan_snapshot(
            frozen_snapshot,
            ScreenConfig.model_validate(
                {"filters": {"dte": {"min_dte": 0}, "premium": {"min_annualized_return": 0.0}}}
            ),
            rate=RATE,
            iv_history=history,
        )
        assert result.opportunities
        top = result.opportunities[0]
        assert top.iv_rank is None
        assert any("observations" in warning for warning in top.warnings)


class TestGaps:
    def test_fit_smile_recovers_a_known_quadratic(self) -> None:
        """y = 0.2 - 0.5x + 2x^2, sampled exactly, must come back exactly."""
        xs = [-0.10, -0.05, 0.0, 0.05, 0.10, 0.15]
        ys = [0.2 - 0.5 * x + 2 * x * x for x in xs]
        a, b, c = fit_smile(xs, ys)
        assert a == pytest.approx(0.2, abs=1e-9)
        assert b == pytest.approx(-0.5, abs=1e-9)
        assert c == pytest.approx(2.0, abs=1e-9)

    def test_fit_needs_enough_points(self) -> None:
        assert fit_smile([0.0, 0.1], [0.2, 0.3]) is None

    def test_implied_forward_sits_above_spot_for_a_positive_rate(self, analysis) -> None:
        """The forward is what carries the dividend and rate assumptions, and solving
        for it is what makes the parity check independent of both."""
        forward = implied_forward(analysis, analysis.expiries[0])
        assert forward is not None
        assert abs(forward - analysis.spot) < analysis.spot * 0.02

    def test_parity_does_not_fire_across_a_whole_healthy_chain(self, analysis) -> None:
        """The bug this test exists for: measuring against an assumed carry instead
        of the implied forward flagged 1043 strikes on this chain, every one of them
        the same dividend yield offset."""
        gaps = find_gaps(analysis, ScreenConfig().gaps)
        parity = [gap for gap in gaps if gap.kind is GapKind.PARITY_VIOLATION]
        assert len(parity) < 20

    def test_skew_anomalies_are_a_short_list_not_the_whole_chain(self, analysis) -> None:
        found = find_skew_anomalies(analysis, analysis.expiries[0], ScreenConfig().gaps)
        assert len(found) < analysis.expiries[0].total * 0.10

    def test_vertical_mispricing_is_off_by_default(self, analysis) -> None:
        """It has no baseline. See find_vertical_mispricings for the whole story."""
        gaps = find_gaps(analysis, ScreenConfig().gaps)
        assert not [gap for gap in gaps if gap.kind is GapKind.VERTICAL_MISPRICING]

    def test_term_inversion_is_not_reported_when_earnings_explain_it(self, analysis) -> None:
        """An inversion before a known report is the market working, not a gap."""
        inverted = analysis.__class__(
            **{
                **{field: getattr(analysis, field) for field in analysis.__slots__},
                "events": EventWindow(earnings=date(2026, 8, 1)),
            }
        )
        found = find_term_inversions(inverted, ScreenConfig().gaps)
        assert not any(gap.kind is GapKind.TERM_INVERSION for gap in found)

    def test_executability_rejects_a_one_sided_quote(self, analysis) -> None:
        front = analysis.expiries[0]
        contract = next(c for c in front.chain.contracts if not c.has_two_sided_market)
        verdict, caveats = assess_executability(contract, analysis, ScreenConfig().gaps)
        assert verdict is Executability.NO_TWO_SIDED_MARKET
        assert caveats

    def test_every_gap_carries_caveats(self, analysis) -> None:
        """The module exists to talk you out of things."""
        for gap in find_gaps(analysis, ScreenConfig().gaps)[:20]:
            assert gap.description
            if not gap.actionable:
                assert gap.executability is not Executability.LOOKS_TRADEABLE


class TestPipeline:
    def test_default_config_finds_nothing_in_a_two_week_fixture(self, frozen_snapshot) -> None:
        """Both expiries are inside the 21 day floor, so an empty table is correct.

        And the tally has to say why, because an unexplained empty table is how a
        screener loses its user.
        """
        result = scan_snapshot(frozen_snapshot, ScreenConfig(), rate=RATE)
        assert result.opportunities == []
        assert result.tally.counts
        assert "dte_too_short" in result.tally.summary()

    def test_widening_the_window_finds_candidates(self, frozen_snapshot, wide_config) -> None:
        result = scan_snapshot(frozen_snapshot, wide_config, rate=RATE)
        assert result.opportunities
        assert result.tally.passed > 0

    def test_results_are_ranked_by_score(self, frozen_snapshot, wide_config) -> None:
        result = scan_snapshot(frozen_snapshot, wide_config, rate=RATE)
        scores = [opportunity.score for opportunity in result.opportunities]
        assert scores == sorted(scores, reverse=True)

    def test_quote_age_comes_back_with_the_result(self, frozen_snapshot, wide_config) -> None:
        """A scan of a three day old snapshot is legitimate as long as nobody
        mistakes it for live."""
        now = datetime(2026, 8, 2, 19, 45, tzinfo=UTC)
        result = scan_snapshot(frozen_snapshot, wide_config, rate=RATE, now=now)
        assert result.stale_symbols["SPY"] > 3 * 24 * 3600 * 0.9

    def test_one_bad_symbol_does_not_lose_the_scan(self, frozen_snapshot, wide_config) -> None:
        broken = frozen_snapshot.model_copy(
            update={
                "symbol": "BAD",
                "quote": frozen_snapshot.quote.model_copy(
                    update={"symbol": "BAD", "last": None, "bid": None, "ask": None}
                ),
                "chains": tuple(
                    chain.model_copy(
                        update={
                            "symbol": "BAD",
                            "contracts": tuple(
                                c.model_copy(update={"symbol": "BAD"}) for c in chain.contracts
                            ),
                        }
                    )
                    for chain in frozen_snapshot.chains
                ),
            }
        )
        result = scan_snapshots([broken, frozen_snapshot], wide_config, rate=RATE)
        assert "BAD" in result.symbols_failed
        assert result.opportunities

    def test_max_results_is_honored(self, frozen_snapshot, wide_config) -> None:
        capped = wide_config.model_copy(update={"max_results": 5})
        result = scan_snapshots([frozen_snapshot], capped, rate=RATE)
        assert len(result.opportunities) == 5


class TestRankingIsStable:
    """Snapshot test: the roadmap's requirement that scoring changes show in a diff.

    These values come from the frozen 30 July 2026 SPY capture. If a weight, a ramp,
    or a filter changes, this test fails and the new numbers have to be written down
    deliberately rather than discovered later.
    """

    def test_top_candidate_is_pinned(self, frozen_snapshot, wide_config) -> None:
        result = scan_snapshot(frozen_snapshot, wide_config, rate=RATE)
        top = result.opportunities[0]

        assert top.symbol == "SPY"
        assert top.strategy is Strategy.CALL_CREDIT_SPREAD
        assert top.expiry == date(2026, 8, 7)
        assert [leg.strike for leg in top.legs] == [751.0, 756.0]
        assert top.score == pytest.approx(0.94570, abs=5e-5)

    def test_the_top_candidate_is_worth_the_ticket(self, frozen_snapshot, wide_config) -> None:
        """The min_max_profit floor exists because a one wide spread can annualize at
        400 percent and clear 18 dollars. The top row must be a real trade."""
        top = scan_snapshot(frozen_snapshot, wide_config, rate=RATE).opportunities[0]
        assert top.max_profit == pytest.approx(109.9, abs=0.05)
        assert top.capital == pytest.approx(387.5, abs=0.05)
        assert top.commission == pytest.approx(2.60, abs=0.01)

    def test_the_ordering_is_a_strict_ranking(self, frozen_snapshot, wide_config) -> None:
        """Second place is meaningfully behind first, so the ordering is not a
        coin flip between saturated scores."""
        result = scan_snapshot(frozen_snapshot, wide_config, rate=RATE)
        assert result.opportunities[1].score == pytest.approx(0.94237, abs=5e-5)
        assert result.opportunities[0].score > result.opportunities[1].score

    def test_candidate_count_is_pinned(self, frozen_snapshot, wide_config) -> None:
        result = scan_snapshot(frozen_snapshot, wide_config, rate=RATE)
        assert result.tally.considered == 524
        assert result.tally.passed == 74
        assert len(result.opportunities) == 74
