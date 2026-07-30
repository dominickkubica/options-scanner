"""Implied volatility solving and, more importantly, refusing to solve.

Round trip is the core check: price an option at a known sigma, then recover that
sigma from the price. vollib provides an independent second opinion.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime

import pytest
from vollib.black_scholes.implied_volatility import implied_volatility as vollib_iv

from optscan.analytics.greeks import bsm_price
from optscan.analytics.iv import (
    MAX_SIGMA,
    VolReason,
    atm_strike,
    chain_implied_vols,
    contract_implied_vol,
    implied_vol,
    interpolate_atm_vol,
    price_bounds,
    screen_quote,
)
from optscan.models import OptionContract, Right

S, K, T, R = 100.0, 100.0, 1.0, 0.05
NOW = datetime(2026, 7, 30, 19, 45, tzinfo=UTC)
EXPIRY = date(2026, 8, 21)


def time_value(right: str, price: float, spot: float, time: float) -> float:
    """Price above the no arbitrage floor, which is what a volatility is implied from.

    Measured against the forward intrinsic, not the spot intrinsic. A 108 strike call
    on a 100 spot is worth about 8.10 with a week left, and that extra 0.10 over the
    8.00 spot intrinsic is carry on the strike, not time value. Confusing the two makes
    a contract with no volatility content look like it has some.
    """
    return price - price_bounds(right, spot, K, time, R)[0]


def contract(**overrides) -> OptionContract:
    defaults = dict(
        symbol="SPY",
        expiry=EXPIRY,
        strike=100.0,
        right=Right.PUT,
        bid=4.0,
        ask=4.2,
        fetched_at=NOW,
        source="test",
    )
    return OptionContract(**{**defaults, **overrides})


class TestPriceBounds:
    def test_call_bounds(self) -> None:
        """A call is worth at most the carried spot and at least the forward intrinsic."""
        lower, upper = price_bounds("C", 100.0, 90.0, 1.0, 0.05)
        assert lower == pytest.approx(100.0 - 90.0 * math.exp(-0.05), abs=1e-9)
        assert upper == pytest.approx(100.0)

    def test_put_bounds(self) -> None:
        lower, upper = price_bounds("P", 100.0, 110.0, 1.0, 0.05)
        assert upper == pytest.approx(110.0 * math.exp(-0.05), abs=1e-9)
        assert lower == pytest.approx(upper - 100.0, abs=1e-9)

    def test_out_of_the_money_lower_bound_is_zero(self) -> None:
        assert price_bounds("C", 100.0, 150.0, 1.0, 0.05)[0] == 0.0


class TestRoundTrip:
    @pytest.mark.parametrize("right", ["c", "p"])
    @pytest.mark.parametrize("sigma", [0.05, 0.12, 0.2, 0.45, 1.2, 3.0])
    @pytest.mark.parametrize("spot", [85.0, 100.0, 118.0])
    @pytest.mark.parametrize("time", [0.02, 0.25, 1.5])
    def test_recovers_the_sigma_it_was_priced_at(
        self, right: str, sigma: float, spot: float, time: float
    ) -> None:
        """Round trip, or an honest refusal when the contract carries no vol information.

        Some combinations here are worth a fraction of a cent, where a wide range of
        volatilities gives the same price to double precision. The solver must not
        return a confident number for those.
        """
        price = bsm_price(right, spot, K, time, R, sigma)
        result = implied_vol(right, price, spot, K, time, R)
        if not result:
            # Three different refusals, one meaning: the price sits on the no arbitrage
            # floor to within double precision, so there is no volatility to extract.
            assert result.reason in {
                VolReason.NOT_IDENTIFIABLE,
                VolReason.PRICE_TOO_SMALL,
                VolReason.BELOW_INTRINSIC,
            }
            assert time_value(right, price, spot, time) < 0.01, (
                "refused a contract that has real time value"
            )
            return
        assert result.sigma == pytest.approx(sigma, abs=1e-6)

    @pytest.mark.parametrize("right", ["c", "p"])
    @pytest.mark.parametrize("sigma", [0.08, 0.2, 0.55])
    @pytest.mark.parametrize("spot", [92.0, 100.0, 108.0])
    @pytest.mark.parametrize("time", [7 / 365, 30 / 365, 180 / 365])
    def test_every_tradeable_contract_round_trips(
        self, right: str, sigma: float, spot: float, time: float
    ) -> None:
        """The realistic grid: anything with real time value must solve.

        The filter is extrinsic value, not price. A deep in the money put worth 7.99
        against 8.00 of intrinsic carries as little volatility information as a far out
        of the money call worth a hundredth of a cent, and by put call parity it has
        the same near zero vega.
        """
        price = bsm_price(right, spot, K, time, R, sigma)
        if time_value(right, price, spot, time) < 0.01:
            pytest.skip("no meaningful time value, so nothing to imply a vol from")
        result = implied_vol(right, price, spot, K, time, R)
        assert result.ok, f"{result.reason} for sigma={sigma} spot={spot} t={time}"
        assert result.sigma == pytest.approx(sigma, abs=1e-6)

    @pytest.mark.parametrize("right", ["c", "p"])
    @pytest.mark.parametrize("sigma", [0.1, 0.3, 0.8])
    def test_agrees_with_vollib(self, right: str, sigma: float) -> None:
        price = bsm_price(right, S, K, T, R, sigma)
        ours = implied_vol(right, price, S, K, T, R)
        theirs = vollib_iv(price, S, K, T, R, right)
        assert ours.sigma == pytest.approx(theirs, abs=1e-6)

    def test_round_trip_with_a_dividend_yield(self) -> None:
        q = 0.025
        price = bsm_price("p", S, K, T, R, 0.3, q)
        result = implied_vol("p", price, S, K, T, R, q)
        assert result.sigma == pytest.approx(0.3, abs=1e-6)


class TestRefusals:
    def test_expired(self) -> None:
        result = implied_vol("C", 5.0, 105.0, 100.0, 0.0, R)
        assert not result
        assert result.reason is VolReason.EXPIRED

    def test_zero_price(self) -> None:
        assert implied_vol("C", 0.0, S, K, T, R).reason is VolReason.PRICE_TOO_SMALL

    def test_below_intrinsic(self) -> None:
        """A stale mark under intrinsic looks like free money and is a bad print."""
        result = implied_vol("C", 1.0, 130.0, 100.0, 1.0, R)
        assert not result
        assert result.reason is VolReason.BELOW_INTRINSIC

    def test_above_the_underlying(self) -> None:
        """A call cannot be worth more than the stock."""
        result = implied_vol("C", 150.0, 100.0, 100.0, 1.0, R)
        assert not result
        assert result.reason is VolReason.ABOVE_MAXIMUM

    def test_vol_beyond_the_bracket_is_reported_not_clamped(self) -> None:
        """Clamping at 500 percent would put a fictional number into the IV history."""
        price = bsm_price("C", S, K, T, R, MAX_SIGMA + 2.0)
        result = implied_vol("C", price, S, K, T, R)
        assert not result
        assert result.reason is VolReason.OUTSIDE_BRACKET

    def test_a_worthless_contract_has_no_identifiable_vol(self) -> None:
        """95 spot against a 100 strike with a week left at 5 percent vol.

        Worth about 1e-14 with a vega of 1e-13 dollars per vol point. Every volatility
        in the bracket reproduces that price to double precision, so any root the
        solver finds is an artifact of where the search landed, not a measurement.
        """
        price = bsm_price("C", 95.0, 100.0, 7 / 365, R, 0.05)
        assert 0 < price < 1e-10
        result = implied_vol("C", price, 95.0, 100.0, 7 / 365, R)
        assert not result
        assert result.reason is VolReason.NOT_IDENTIFIABLE

    def test_a_deep_in_the_money_put_is_refused_for_the_same_reason(self) -> None:
        """8.00 of intrinsic and no time value. Parity gives it the same vega as the
        far out of the money call, which is to say none."""
        price = bsm_price("P", 92.0, 100.0, 7 / 365, R, 0.08)
        result = implied_vol("P", price, 92.0, 100.0, 7 / 365, R)
        assert not result
        assert result.reason in {VolReason.NOT_IDENTIFIABLE, VolReason.BELOW_INTRINSIC}

    def test_the_identifiability_floor_is_a_parameter(self) -> None:
        price = bsm_price("C", 95.0, 100.0, 7 / 365, R, 0.15)
        assert implied_vol("C", price, 95.0, 100.0, 7 / 365, R).ok
        strict = implied_vol("C", price, 95.0, 100.0, 7 / 365, R, min_vega=1.0)
        assert strict.reason is VolReason.NOT_IDENTIFIABLE

    def test_a_refusal_still_reports_what_it_saw(self) -> None:
        result = implied_vol("C", 0.0, S, K, T, R)
        assert result.price_used == 0.0
        assert result.time_to_expiry == pytest.approx(T)


class TestScreenQuote:
    def test_accepts_a_normal_quote(self) -> None:
        assert screen_quote(contract(bid=4.0, ask=4.2)) is VolReason.OK

    def test_rejects_a_crossed_market(self) -> None:
        assert screen_quote(contract(bid=4.5, ask=4.0)) is VolReason.CROSSED

    def test_rejects_a_one_sided_market(self) -> None:
        assert screen_quote(contract(bid=0.0, ask=0.05)) is VolReason.NO_TWO_SIDED_MARKET
        assert screen_quote(contract(bid=None, ask=None)) is VolReason.NO_TWO_SIDED_MARKET

    def test_rejects_a_penny_quote(self) -> None:
        """Below a cent of mid there is no information, only rounding."""
        assert screen_quote(contract(bid=0.001, ask=0.002)) is VolReason.PRICE_TOO_SMALL

    def test_rejects_a_wide_spread(self) -> None:
        """0.50 by 1.50 is a mid of 1.00 and a spread of 100 percent of it."""
        assert screen_quote(contract(bid=0.5, ask=1.5)) is VolReason.SPREAD_TOO_WIDE

    def test_the_spread_threshold_is_a_parameter_not_a_constant(self) -> None:
        wide = contract(bid=0.5, ask=1.5)
        assert screen_quote(wide, max_spread_pct=0.25) is VolReason.SPREAD_TOO_WIDE
        assert screen_quote(wide, max_spread_pct=2.0) is VolReason.OK

    def test_crossed_is_reported_before_width(self) -> None:
        """Both are true for this quote, and crossed is the more useful diagnosis."""
        assert screen_quote(contract(bid=9.0, ask=1.0)) is VolReason.CROSSED


class TestContractImpliedVol:
    def test_solves_a_screened_contract(self) -> None:
        asof = datetime(2026, 8, 20, 20, 0, tzinfo=UTC)  # exactly one day to expiry
        price = bsm_price("P", 100.0, 100.0, 1 / 365, R, 0.25)
        c = contract(strike=100.0, bid=price - 0.005, ask=price + 0.005)
        result = contract_implied_vol(c, 100.0, asof, rate=R)
        assert result.ok
        assert result.sigma == pytest.approx(0.25, abs=1e-3)

    def test_a_rejected_quote_is_never_solved(self) -> None:
        c = contract(bid=0.0, ask=0.05)
        result = contract_implied_vol(c, 100.0, NOW, rate=R)
        assert result.sigma is None
        assert result.reason is VolReason.NO_TWO_SIDED_MARKET
        assert result.time_to_expiry is not None  # still reports what it knew

    def test_uses_mid_not_last(self) -> None:
        """A stale last against a live spot is a fabricated vol."""
        asof = datetime(2026, 8, 20, 20, 0, tzinfo=UTC)
        mid_price = bsm_price("P", 100.0, 100.0, 1 / 365, R, 0.25)
        c = contract(strike=100.0, bid=mid_price - 0.005, ask=mid_price + 0.005, last=99.0)
        result = contract_implied_vol(c, 100.0, asof, rate=R)
        assert result.price_used == pytest.approx(mid_price, abs=1e-3)


class TestChainImpliedVols:
    def test_keeps_rejections_in_the_result(self) -> None:
        """How much of a chain is unusable is itself a liquidity signal."""
        asof = datetime(2026, 8, 20, 20, 0, tzinfo=UTC)
        good_price = bsm_price("P", 100.0, 100.0, 1 / 365, R, 0.25)
        contracts = [
            contract(strike=100.0, bid=good_price - 0.005, ask=good_price + 0.005),
            contract(strike=95.0, bid=0.0, ask=0.05),
            contract(strike=105.0, bid=9.0, ask=1.0),
        ]
        results = chain_implied_vols(contracts, 100.0, asof, rate=R)
        assert len(results) == 3
        assert results[(EXPIRY, 100.0, Right.PUT)].ok
        assert results[(EXPIRY, 95.0, Right.PUT)].reason is VolReason.NO_TWO_SIDED_MARKET
        assert results[(EXPIRY, 105.0, Right.PUT)].reason is VolReason.CROSSED


class TestAtmHelpers:
    def test_nearest_strike(self) -> None:
        assert atm_strike([95.0, 100.0, 105.0], 101.0) == 100.0
        assert atm_strike([95.0, 100.0, 105.0], 103.0) == 105.0
        assert atm_strike([], 100.0) is None

    def test_exact_tie_goes_to_the_lower_strike(self) -> None:
        assert atm_strike([100.0, 105.0], 102.5) == 100.0

    def test_interpolates_between_bracketing_strikes(self) -> None:
        """Spot 102.5 halfway between 100 and 105 gives the midpoint of the two vols."""
        vols = {100.0: 0.20, 105.0: 0.24}
        assert interpolate_atm_vol(vols, 102.5) == pytest.approx(0.22)
        assert interpolate_atm_vol(vols, 101.0) == pytest.approx(0.208)

    def test_outside_the_ladder_falls_back_to_the_edge(self) -> None:
        vols = {100.0: 0.20, 105.0: 0.24}
        assert interpolate_atm_vol(vols, 50.0) == 0.20
        assert interpolate_atm_vol(vols, 500.0) == 0.24

    def test_ignores_unusable_vols(self) -> None:
        vols = {95.0: 0.0, 100.0: 0.20, 105.0: 0.24}
        assert interpolate_atm_vol(vols, 102.5) == pytest.approx(0.22)

    def test_nothing_usable(self) -> None:
        assert interpolate_atm_vol({}, 100.0) is None
        assert interpolate_atm_vol({100.0: 0.0}, 100.0) is None
