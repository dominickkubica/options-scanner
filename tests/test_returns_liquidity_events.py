"""Return metrics, liquidity scoring, and event risk.

The return numbers are all hand computable and the arithmetic is stated in each test,
because the margin model is an assumption and an assumption nobody can check is just
a number.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from optscan.analytics.events import (
    EventWindow,
    assess_events,
    early_assignment_risk,
    excludes_earnings,
)
from optscan.analytics.liquidity import (
    DEFAULT_WEIGHTS,
    ramp,
    score_liquidity,
)
from optscan.analytics.returns import (
    cash_secured_put_capital,
    covered_call_profile,
    credit_spread_profile,
    credit_to_width,
    iron_condor_profile,
    naked_call_capital,
    short_put_profile,
    vertical_spread_capital,
)
from optscan.models import OptionContract, Right

NOW = datetime(2026, 7, 30, 20, 0, tzinfo=UTC)
TODAY = date(2026, 7, 30)


def contract(**overrides) -> OptionContract:
    defaults = dict(
        symbol="SPY",
        expiry=date(2026, 8, 21),
        strike=730.0,
        right=Right.PUT,
        bid=4.00,
        ask=4.10,
        volume=500,
        open_interest=2500,
        last_trade_at=NOW,
        fetched_at=NOW,
        source="test",
    )
    return OptionContract(**{**defaults, **overrides})


class TestCapital:
    def test_cash_secured_put(self) -> None:
        """95 strike costs 9500 to assign, less 200 of credit taken in: 9300."""
        assert cash_secured_put_capital(95.0, 2.00) == pytest.approx(9300.0)

    def test_vertical_spread(self) -> None:
        """5 wide, 1.50 credit: 350 at risk, which is what the broker holds."""
        assert vertical_spread_capital(5.0, 1.50) == pytest.approx(350.0)

    def test_credit_at_or_above_width_is_rejected(self) -> None:
        """Impossible in a real market and would report an infinite return."""
        with pytest.raises(ValueError, match="not below the width"):
            vertical_spread_capital(5.0, 5.0)
        with pytest.raises(ValueError, match="not below the width"):
            vertical_spread_capital(5.0, 6.0)

    def test_naked_calls_have_no_honest_denominator(self) -> None:
        assert naked_call_capital() is None


class TestShortPut:
    def test_the_full_profile(self) -> None:
        """95 strike, 2.00 credit, 30 days.

        max profit 200, capital 9300, so ROC is 200/9300 = 2.15 percent,
        annualized 2.15% * 365/30 = 26.2 percent.
        max loss is the strike less the credit, times 100: 9300, because the
        underlying can go to zero.
        """
        profile = short_put_profile(95.0, 2.00, 30)
        assert profile.max_profit == pytest.approx(200.0)
        assert profile.capital == pytest.approx(9300.0)
        assert profile.max_loss == pytest.approx(9300.0)
        assert profile.return_on_capital == pytest.approx(0.021505, abs=1e-6)
        assert profile.annualized_return == pytest.approx(0.26164, abs=1e-4)

    def test_annualizing_is_simple_scaling_not_compounding(self) -> None:
        """Doubling the days must halve the annualized return exactly."""
        thirty = short_put_profile(95.0, 2.00, 30).annualized_return
        sixty = short_put_profile(95.0, 2.00, 60).annualized_return
        assert thirty == pytest.approx(sixty * 2)

    def test_zero_dte_has_no_annualized_return(self) -> None:
        assert short_put_profile(95.0, 2.00, 0).annualized_return is None

    def test_a_debit_is_not_a_short_premium_position(self) -> None:
        with pytest.raises(ValueError, match="credit must be positive"):
            short_put_profile(95.0, -1.0, 30)


class TestCoveredCall:
    def test_profit_includes_the_appreciation_to_the_strike(self) -> None:
        """Basis 100, strike 105, credit 1.50.

        Called away means 500 of stock gain plus 150 of premium: 650, not 150.
        Capital is the shares at basis: 10000. ROC 6.5 percent over 45 days.
        """
        profile = covered_call_profile(105.0, 1.50, 100.0, 45)
        assert profile.max_profit == pytest.approx(650.0)
        assert profile.capital == pytest.approx(10_000.0)
        assert profile.return_on_capital == pytest.approx(0.065)

    def test_a_strike_below_basis_has_no_appreciation(self) -> None:
        profile = covered_call_profile(95.0, 1.50, 100.0, 45)
        assert profile.max_profit == pytest.approx(150.0)


class TestCreditSpread:
    def test_the_full_profile(self) -> None:
        """95/90 put spread for 1.50: 150 max profit, 350 max loss and capital.

        Risk reward is 150/350 = 0.43, which is normal for short premium and only
        means something against the win rate.
        """
        profile = credit_spread_profile(95.0, 90.0, 1.50, 45)
        assert profile.max_profit == pytest.approx(150.0)
        assert profile.max_loss == pytest.approx(350.0)
        assert profile.capital == pytest.approx(350.0)
        assert profile.risk_reward == pytest.approx(0.42857, abs=1e-5)

    def test_direction_does_not_matter_to_the_arithmetic(self) -> None:
        put_side = credit_spread_profile(95.0, 90.0, 1.50, 45)
        call_side = credit_spread_profile(105.0, 110.0, 1.50, 45)
        assert put_side.capital == call_side.capital

    def test_credit_to_width(self) -> None:
        assert credit_to_width(1.50, 5.0) == pytest.approx(0.30)
        with pytest.raises(ValueError, match="width must be positive"):
            credit_to_width(1.5, 0.0)


class TestIronCondor:
    def test_capital_is_the_wider_wing_only(self) -> None:
        """Both sides cannot finish in the money, so only the wider one is at risk.

        Puts 95/90 is 5 wide, calls 105/115 is 10 wide, credit 2.00.
        Capital is (10 - 2) * 100 = 800, not the sum of both wings.
        """
        profile = iron_condor_profile(95.0, 90.0, 105.0, 115.0, 2.00, 45)
        assert profile.capital == pytest.approx(800.0)
        assert profile.max_profit == pytest.approx(200.0)
        assert profile.max_loss == pytest.approx(800.0)

    def test_balanced_wings(self) -> None:
        profile = iron_condor_profile(95.0, 90.0, 105.0, 110.0, 2.00, 45)
        assert profile.capital == pytest.approx(300.0)


class TestRamp:
    def test_linear_between_thresholds(self) -> None:
        assert ramp(500.0, 0.0, 1000.0) == pytest.approx(0.5)
        assert ramp(0.0, 0.0, 1000.0) == 0.0
        assert ramp(1000.0, 0.0, 1000.0) == 1.0

    def test_clamped_outside(self) -> None:
        assert ramp(5000.0, 0.0, 1000.0) == 1.0
        assert ramp(-10.0, 0.0, 1000.0) == 0.0

    def test_runs_backwards_for_lower_is_better(self) -> None:
        """Spread percent: 0.02 is good, 0.25 is poor."""
        assert ramp(0.02, 0.25, 0.02) == 1.0
        assert ramp(0.25, 0.25, 0.02) == 0.0
        assert ramp(0.135, 0.25, 0.02) == pytest.approx(0.5, abs=0.01)

    def test_missing_is_none_not_zero(self) -> None:
        """Unknown open interest is not the same as no open interest."""
        assert ramp(None, 0.0, 1000.0) is None

    def test_identical_thresholds_are_a_bug(self) -> None:
        with pytest.raises(ValueError, match="must differ"):
            ramp(5.0, 1.0, 1.0)


class TestLiquidityScore:
    def test_a_liquid_contract_scores_well(self) -> None:
        """4.00 by 4.10 is a 2.4 percent spread, 2500 OI, 500 volume, traded now."""
        score = score_liquidity(contract(), asof=NOW)
        assert score.score > 0.85
        assert score.tradeable

    def test_an_illiquid_contract_scores_badly(self) -> None:
        score = score_liquidity(
            contract(bid=0.50, ask=1.50, volume=0, open_interest=3),
            asof=NOW,
        )
        assert score.score < 0.2
        assert not score.tradeable

    def test_the_worst_component_is_identifiable(self) -> None:
        """A score with no explanation is a number people learn to ignore."""
        score = score_liquidity(
            contract(bid=0.50, ask=1.50, volume=500, open_interest=2500),
            asof=NOW,
        )
        assert score.worst_component() == "spread"

    def test_spread_dominates_the_weighting(self) -> None:
        """It is the only component you pay directly, twice."""
        assert DEFAULT_WEIGHTS["spread"] > DEFAULT_WEIGHTS["open_interest"]
        wide = score_liquidity(contract(bid=0.50, ask=1.50), asof=NOW)
        no_volume = score_liquidity(contract(volume=0), asof=NOW)
        assert wide.score < no_volume.score

    def test_missing_components_are_dropped_not_zeroed(self) -> None:
        """Otherwise the score punishes whichever field this vendor leaves empty."""
        full = score_liquidity(contract(), asof=NOW)
        partial = score_liquidity(contract(volume=None), asof=NOW)
        assert "volume" not in partial.components
        assert partial.score == pytest.approx(full.score, abs=0.05)
        assert any("volume" in note for note in partial.notes)

    def test_a_stale_last_trade_costs_points(self) -> None:
        fresh = score_liquidity(contract(), asof=NOW)
        stale = score_liquidity(
            contract(last_trade_at=datetime(2026, 7, 27, 20, 0, tzinfo=UTC)), asof=NOW
        )
        assert stale.score < fresh.score
        assert stale.components["staleness"] == 0.0

    def test_a_contract_with_nothing_measurable(self) -> None:
        empty = contract(bid=None, ask=None, volume=None, open_interest=None, last_trade_at=None)
        score = score_liquidity(empty, asof=NOW)
        assert score.score == 0.0
        assert score.components == {}
        assert score.notes

    def test_thresholds_are_parameters(self) -> None:
        thin = contract(open_interest=100, volume=20, bid=4.0, ask=4.2)
        strict = score_liquidity(thin, asof=NOW, oi_good=10_000, volume_good=5_000)
        lenient = score_liquidity(thin, asof=NOW, oi_good=100, volume_good=20)
        assert lenient.score > strict.score


class TestEvents:
    def test_earnings_inside_the_expiry_is_flagged(self) -> None:
        events = EventWindow(earnings=date(2026, 8, 10))
        risk = assess_events(events, date(2026, 8, 21), TODAY)
        assert risk.has_earnings
        assert risk.days_to_earnings == 11
        assert not risk.clean
        assert any("priced, not mispriced" in reason for reason in risk.reasons)

    def test_earnings_after_the_expiry_is_not(self) -> None:
        events = EventWindow(earnings=date(2026, 9, 10))
        risk = assess_events(events, date(2026, 8, 21), TODAY)
        assert not risk.has_earnings
        assert risk.clean

    def test_no_known_earnings_date(self) -> None:
        risk = assess_events(EventWindow(), date(2026, 8, 21), TODAY)
        assert not risk.has_earnings
        assert risk.clean

    def test_exclusion_buffer_extends_past_expiry(self) -> None:
        events = EventWindow(earnings=date(2026, 8, 24))
        expiry = date(2026, 8, 21)
        assert excludes_earnings(events, expiry, TODAY)
        assert not excludes_earnings(events, expiry, TODAY, buffer_days=7)


class TestEarlyAssignment:
    def test_short_call_with_less_extrinsic_than_the_dividend(self) -> None:
        """The classic case: 0.05 of extrinsic against a 0.25 dividend.

        Exercising early forfeits 0.05 of time value and collects 0.25, so it is
        rational and somebody will do it.
        """
        assert early_assignment_risk("C", 0.05, 0.25, 1) is True

    def test_more_extrinsic_than_the_dividend_is_safe(self) -> None:
        """Moneyness alone does not decide it, the extrinsic does."""
        assert early_assignment_risk("C", 0.80, 0.25, 1) is False

    def test_outside_the_window(self) -> None:
        assert early_assignment_risk("C", 0.05, 0.25, 30) is False
        assert early_assignment_risk("C", 0.05, 0.25, None) is False

    def test_no_dividend(self) -> None:
        assert early_assignment_risk("C", 0.05, None, 1) is False
        assert early_assignment_risk("C", 0.05, 0.0, 1) is False

    def test_puts_are_not_a_dividend_case(self) -> None:
        """Early exercise of a put is driven by rates, not dividends, so it is not
        an event flag."""
        assert early_assignment_risk("P", 0.05, 0.25, 1) is False

    def test_assessed_end_to_end(self) -> None:
        events = EventWindow(ex_dividend=date(2026, 8, 3), dividend_amount=0.25)
        risk = assess_events(
            events,
            date(2026, 8, 21),
            TODAY,
            right="C",
            extrinsic_value=0.05,
        )
        assert risk.has_ex_dividend
        assert risk.early_assignment_risk
        assert not risk.clean
        assert any("early assignment is rational" in reason for reason in risk.reasons)
