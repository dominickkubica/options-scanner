"""Open interest levels and gamma exposure, on chains small enough to check by hand.

Synthetic chains are the right fixture here, unlike the level detectors, which use real
captures because a synthetic series agrees with whatever a detector happens to do. There
is no detector in this module: every number is a closed form or a minimum over a handful
of strikes, so a chain built to have a known answer is exactly what tests it.

The gamma underneath GEX is checked against vollib rather than against our own
`bsm_greeks`, so a sign or a scale error cannot agree with itself.
"""

from __future__ import annotations

import math

import pytest
from vollib.black_scholes.greeks.analytical import gamma as vollib_gamma

from optscan.analytics.gex import (
    CONTRACT_SIZE,
    GexProfile,
    OiRow,
    build_profile,
    max_pain,
    net_gex_by_strike,
    pin_candidate,
    row_gex,
    total_gex_at,
    walls,
    zero_gamma,
)
from optscan.models import Right

RATE = 0.04
IV = 0.20
YEARS = 30.0 / 365.0
SPOT = 100.0


def call(strike: float, oi: int, *, iv: float | None = IV, years: float = YEARS) -> OiRow:
    return OiRow(strike=strike, right=Right.CALL, open_interest=oi, iv=iv, years=years)


def put(strike: float, oi: int, *, iv: float | None = IV, years: float = YEARS) -> OiRow:
    return OiRow(strike=strike, right=Right.PUT, open_interest=oi, iv=iv, years=years)


def ladder(call_oi: dict[float, int], put_oi: dict[float, int]) -> list[OiRow]:
    return [call(k, oi) for k, oi in call_oi.items()] + [put(k, oi) for k, oi in put_oi.items()]


# --------------------------------------------------------------------------------
# Gamma exposure
# --------------------------------------------------------------------------------


def test_call_gex_matches_the_formula_with_an_independent_gamma() -> None:
    """OI x gamma x 100 x spot^2 x 0.01, with gamma from vollib rather than from us."""
    oi = 1_000
    gamma = vollib_gamma("c", SPOT, 100.0, YEARS, RATE, IV)
    expected = oi * gamma * CONTRACT_SIZE * SPOT * SPOT * 0.01

    assert row_gex(call(100.0, oi), SPOT, RATE) == pytest.approx(expected, rel=1e-6)


def test_puts_carry_the_same_magnitude_with_the_opposite_sign() -> None:
    """The dealer convention is the only thing separating the two sides.

    Gamma does not depend on the right, so a call and a put on the same strike with the
    same open interest must cancel exactly. If they ever stop cancelling, the sign rule
    has been applied somewhere it should not have been.
    """
    one_call = row_gex(call(100.0, 500), SPOT, RATE)
    one_put = row_gex(put(100.0, 500), SPOT, RATE)

    assert one_call is not None and one_put is not None
    assert one_put == pytest.approx(-one_call)
    assert total_gex_at([call(100.0, 500), put(100.0, 500)], SPOT, RATE) == pytest.approx(0.0)


def test_gex_is_per_one_percent_move_not_per_dollar() -> None:
    """The 0.01 in the formula is the whole difference between two conventions.

    A dollar-move convention would be a hundred times larger, and a reader comparing
    this to a published GEX figure would have no way to tell which one they had.
    """
    gamma = vollib_gamma("c", SPOT, 100.0, YEARS, RATE, IV)
    per_dollar = 1_000 * gamma * CONTRACT_SIZE * SPOT
    exposure = row_gex(call(100.0, 1_000), SPOT, RATE)

    assert exposure == pytest.approx(per_dollar * SPOT * 0.01, rel=1e-6)


def test_a_strike_with_no_implied_vol_reports_none_rather_than_zero() -> None:
    """None means "no gamma could be solved". Zero would mean "this strike is flat"."""
    assert row_gex(call(100.0, 1_000, iv=None), SPOT, RATE) is None
    assert row_gex(call(100.0, 1_000, iv=float("nan")), SPOT, RATE) is None
    assert row_gex(call(100.0, 1_000, iv=0.0), SPOT, RATE) is None


def test_open_interest_of_zero_is_flat_not_unknown() -> None:
    """The vendor said zero. That is a fact about the strike, not a missing number."""
    assert row_gex(call(100.0, 0), SPOT, RATE) == 0.0
    assert row_gex(call(100.0, 0, iv=None), SPOT, RATE) == 0.0


def test_an_expired_row_cannot_be_priced() -> None:
    """Gamma at zero time is not zero, it is undefined, and BSM returns a degenerate 0."""
    assert row_gex(call(100.0, 1_000, years=0.0), SPOT, RATE) is None


def test_unpriceable_strikes_keep_their_open_interest() -> None:
    """A strike with no vol still counts toward the walls, which need no model."""
    profiles = net_gex_by_strike([call(105.0, 400, iv=None), put(95.0, 700)], SPOT, RATE)
    unpriced = next(item for item in profiles if item.strike == 105.0)

    assert unpriced.call_oi == 400
    assert unpriced.net_gex is None
    assert unpriced.call_gex is None


def test_one_side_priced_nets_the_side_that_priced() -> None:
    """Losing the whole strike would lose open interest the walls are counted from."""
    profiles = net_gex_by_strike([call(100.0, 300, iv=None), put(100.0, 300)], SPOT, RATE)
    row = profiles[0]

    assert row.call_gex is None
    assert row.put_gex is not None
    assert row.net_gex == pytest.approx(row.put_gex)
    assert row.total_oi == 600


def test_strike_profiles_come_back_in_ascending_strike_order() -> None:
    profiles = net_gex_by_strike(ladder({105.0: 10, 95.0: 10}, {90.0: 10}), SPOT, RATE)

    assert [item.strike for item in profiles] == [90.0, 95.0, 105.0]


def test_total_gex_is_none_when_the_whole_chain_is_unpriceable() -> None:
    """No volatilities anywhere is a different fact from no gamma anywhere."""
    assert total_gex_at([call(100.0, 500, iv=None)], SPOT, RATE) is None


# --------------------------------------------------------------------------------
# Max pain
# --------------------------------------------------------------------------------


def test_max_pain_is_the_cheapest_settlement_not_the_biggest_strike() -> None:
    """Hand computed, and deliberately not the strike with the most open interest.

    calls 100 @ 100, puts 300 @ 110:
      settle at 100 -> calls pay 0, puts pay (110-100)*300 = 3000
      settle at 110 -> calls pay (110-100)*100 = 1000, puts pay 0
    So 110 is where least value is paid out, even though the heaviest open interest
    sits there too. A version that just took the busiest strike would pass this by
    luck, so the next test breaks that tie.
    """
    assert max_pain([call(100.0, 100), put(110.0, 300)]) == 110.0


def test_max_pain_is_not_the_strike_with_the_most_open_interest() -> None:
    """The busiest strike here is 90, and settling there would pay out the most.

    calls 10 @ 90 (heaviest), puts 5 @ 110:
      settle at 90  -> calls 0, puts (110-90)*5 = 100
      settle at 110 -> calls (110-90)*10 = 200, puts 0
    """
    assert max_pain([call(90.0, 10), put(110.0, 5)]) == 90.0


def test_max_pain_ignores_missing_volatility() -> None:
    """It is a count of contracts, so a chain with no vols at all still has one."""
    rows = [call(100.0, 100, iv=None), put(110.0, 300, iv=None)]

    assert max_pain(rows) == 110.0


def test_max_pain_is_none_without_open_interest() -> None:
    assert max_pain([call(100.0, 0), put(110.0, 0)]) is None
    assert max_pain([]) is None


# --------------------------------------------------------------------------------
# Zero gamma
# --------------------------------------------------------------------------------


def put_heavy_below_call_heavy_above() -> list[OiRow]:
    """Puts stacked under spot and calls stacked over it, so the flip sits between.

    Every strike's gamma peaks near that strike, so a hypothetical spot near 103 is
    dominated by the calls and one near 97 by the puts. Between them the sum passes
    through zero, which is the level the search is for.
    """
    return [put(97.0, 5_000), call(103.0, 5_000)]


def test_zero_gamma_sits_between_the_put_wall_and_the_call_wall() -> None:
    flip = zero_gamma(put_heavy_below_call_heavy_above(), SPOT, RATE)

    assert flip is not None
    assert 97.0 < flip < 103.0


def test_gex_is_negative_below_the_flip_and_positive_above_it() -> None:
    """The sign either side is the whole meaning of the level."""
    rows = put_heavy_below_call_heavy_above()
    flip = zero_gamma(rows, SPOT, RATE)
    assert flip is not None

    below = total_gex_at(rows, flip * 0.995, RATE)
    above = total_gex_at(rows, flip * 1.005, RATE)

    assert below is not None and above is not None
    assert below < 0.0 < above


def test_the_flip_is_where_total_gex_is_actually_zero() -> None:
    """Checked against the function the search is built on, not against a constant.

    The residual is compared to the size of the exposure a half percent away, so this
    says "flat relative to the curve's own scale" rather than picking a dollar figure
    that would need changing whenever the fixture does.
    """
    rows = put_heavy_below_call_heavy_above()
    flip = zero_gamma(rows, SPOT, RATE)
    assert flip is not None

    residual = total_gex_at(rows, flip, RATE)
    nearby = total_gex_at(rows, flip * 1.005, RATE)

    assert residual is not None and nearby is not None
    assert abs(residual) < abs(nearby) * 0.05


def test_no_flip_inside_the_band_returns_none() -> None:
    """All calls, so gamma exposure is positive everywhere and there is nothing to mark.

    Returning an edge of the band would put a line on the chart at a price where
    nothing happens, which is worse than leaving it off.
    """
    rows = [call(95.0, 1_000), call(100.0, 1_000), call(105.0, 1_000)]

    assert zero_gamma(rows, SPOT, RATE) is None


def test_zero_gamma_is_none_when_nothing_can_be_priced() -> None:
    rows = [call(100.0, 1_000, iv=None), put(100.0, 1_000, iv=None)]

    assert zero_gamma(rows, SPOT, RATE) is None


def test_a_wider_band_finds_a_flip_the_narrow_one_misses() -> None:
    """The band is a window, not a verdict, and the note says so when nothing crosses.

    A deep put wall at 70 has almost no gamma at spot, so inside 3% the small call
    position owns the sign and nothing crosses. Twenty-five percent down, the put's own
    gamma peak is in range and the sum turns over at about 85.
    """
    rows = [put(70.0, 50_000), call(101.0, 5_000)]

    assert zero_gamma(rows, SPOT, RATE, band=0.03) is None
    assert zero_gamma(rows, SPOT, RATE, band=0.25) == pytest.approx(85.4, abs=0.5)


# --------------------------------------------------------------------------------
# Walls and the pin
# --------------------------------------------------------------------------------


def test_walls_are_the_heaviest_strike_on_each_side_of_spot() -> None:
    rows = ladder({103.0: 900, 107.0: 400}, {98.0: 1_500, 92.0: 600})
    call_wall, put_wall = walls(rows, SPOT)

    assert call_wall is not None and put_wall is not None
    assert call_wall.strike == 103.0
    assert call_wall.open_interest == 900
    assert put_wall.strike == 98.0
    assert put_wall.distance_pct == pytest.approx(-0.02)


def test_a_call_below_spot_is_not_a_call_wall() -> None:
    """Only strikes above spot can be a call wall, whatever the open interest says."""
    rows = ladder({95.0: 10_000, 105.0: 50}, {})
    call_wall, _ = walls(rows, SPOT)

    assert call_wall is not None
    assert call_wall.strike == 105.0


def test_walls_are_none_when_one_side_is_empty() -> None:
    call_wall, put_wall = walls([call(105.0, 100)], SPOT)

    assert call_wall is not None
    assert put_wall is None


def test_ties_go_to_the_strike_nearer_spot() -> None:
    """Equally held, the closer one is the one price reaches first."""
    call_wall, _ = walls(ladder({102.0: 700, 108.0: 700}, {}), SPOT)

    assert call_wall is not None
    assert call_wall.strike == 102.0


def test_the_pin_is_the_busiest_strike_inside_one_percent() -> None:
    """99.5 holds less than 105 and wins anyway, because 105 is outside the band."""
    rows = ladder({105.0: 9_000, 99.5: 400}, {99.5: 300})
    pin = pin_candidate(rows, SPOT)

    assert pin is not None
    assert pin.strike == 99.5
    assert pin.open_interest == 700


def test_the_pin_is_none_when_the_band_is_empty() -> None:
    assert pin_candidate(ladder({110.0: 5_000}, {90.0: 5_000}), SPOT) is None


# --------------------------------------------------------------------------------
# The whole profile
# --------------------------------------------------------------------------------


def full_chain() -> list[OiRow]:
    """A plausible ladder: puts heavier below, calls heavier above, vols everywhere.

    Seven strikes, which is deliberately over `MIN_STRIKES_WITH_OI`: this fixture is
    also what the "no thinness warning" test leans on, and a fixture sitting one strike
    under the threshold would make that test pass for the wrong reason.
    """
    calls = {94.0: 100, 96.0: 200, 98.0: 400, 100.0: 900, 102.0: 2_500, 104.0: 1_200, 106.0: 600}
    puts = {94.0: 900, 96.0: 2_800, 98.0: 1_400, 100.0: 800, 102.0: 300, 104.0: 100, 106.0: 50}
    return ladder(calls, puts)


def test_the_profile_reports_every_level_together() -> None:
    profile = build_profile(full_chain(), SPOT, RATE)

    assert isinstance(profile, GexProfile)
    assert profile.call_wall is not None and profile.call_wall.strike == 102.0
    assert profile.put_wall is not None and profile.put_wall.strike == 96.0
    assert profile.max_pain is not None
    assert profile.total_gex is not None
    assert profile.priced_oi == profile.total_oi
    strikes = [item.strike for item in profile.strikes]
    assert strikes == [94.0, 96.0, 98.0, 100.0, 102.0, 104.0, 106.0]


def test_the_regime_reads_from_which_side_of_the_flip_spot_is_on() -> None:
    rows = put_heavy_below_call_heavy_above()

    above = build_profile(rows, 102.5, RATE)
    below = build_profile(rows, 97.5, RATE)

    assert above.zero_gamma is not None
    assert above.regime == "dampened"
    assert below.regime == "volatile"


def test_the_regime_falls_back_to_the_sign_when_nothing_crosses() -> None:
    """Without a flip the sign of total GEX is the more direct measurement anyway."""
    calls_only = build_profile([call(95.0, 1_000), call(105.0, 1_000)], SPOT, RATE)

    assert calls_only.zero_gamma is None
    assert calls_only.regime == "dampened"
    assert any("no zero-gamma level" in note for note in calls_only.notes)


def test_an_unpriceable_chain_is_unknown_rather_than_volatile() -> None:
    """Negative and unknown are not the same regime, and one of them is a trade."""
    profile = build_profile([call(105.0, 100, iv=None), put(95.0, 100, iv=None)], SPOT, RATE)

    assert profile.regime == "unknown"
    assert profile.total_gex is None
    assert profile.call_wall is not None
    assert any("No strike could be priced" in note for note in profile.notes)


def test_a_thin_chain_says_so() -> None:
    profile = build_profile([call(105.0, 50), put(95.0, 40)], SPOT, RATE)

    assert any("handful of prints" in note for note in profile.notes)


def test_a_full_chain_carries_no_thinness_warning() -> None:
    profile = build_profile(full_chain(), SPOT, RATE)

    assert not any("handful of prints" in note for note in profile.notes)


def test_mostly_unpriced_open_interest_says_so() -> None:
    """The walls survive it and the gamma curve does not, and the note separates them."""
    rows = [
        call(102.0, 9_000, iv=None),
        put(98.0, 9_000, iv=None),
        call(104.0, 100),
        put(96.0, 100),
        call(106.0, 100),
        put(94.0, 100),
    ]
    profile = build_profile(rows, SPOT, RATE)

    assert any("of the open interest" in note for note in profile.notes)
    assert profile.priced_oi == 400
    assert profile.total_oi == 18_400
    assert profile.call_wall is not None and profile.call_wall.strike == 102.0


def test_an_empty_chain_is_empty_rather_than_an_error() -> None:
    profile = build_profile([], SPOT, RATE)

    assert profile.strikes == ()
    assert profile.max_pain is None
    assert profile.zero_gamma is None
    assert profile.regime == "unknown"
    assert any("No open interest" in note for note in profile.notes)


def test_a_chain_of_nothing_but_zero_open_interest_is_treated_as_empty() -> None:
    """Every listed strike with 0 OI is the normal state of a far-dated wing."""
    profile = build_profile([call(105.0, 0), put(95.0, 0)], SPOT, RATE)

    assert profile.total_oi == 0
    assert any("No open interest" in note for note in profile.notes)


@pytest.mark.parametrize("spot", [0.0, -5.0, float("nan")])
def test_a_spot_that_is_not_a_price_is_refused(spot: float) -> None:
    """Every level here is measured against spot, so a bad one poisons all of them."""
    with pytest.raises(ValueError, match="positive"):
        build_profile(full_chain(), spot, RATE)


def test_nan_strikes_are_dropped_rather_than_ranked() -> None:
    """A NaN compares false against everything and would quietly win a max()."""
    rows = [*full_chain(), call(float("nan"), 99_999), put(float("nan"), 99_999)]
    profile = build_profile(rows, SPOT, RATE)

    assert all(math.isfinite(item.strike) for item in profile.strikes)
    assert profile.call_wall is not None and profile.call_wall.strike == 102.0


def test_two_expiries_at_one_strike_are_priced_on_their_own_clocks() -> None:
    """Folding their open interest together first would price both at one expiry.

    The far row has more time, so its gamma at the money is lower; summing the open
    interest and pricing it once at either expiry cannot reproduce the total.
    """
    near = call(100.0, 1_000, years=7.0 / 365.0)
    far = call(100.0, 1_000, years=180.0 / 365.0)

    separate = total_gex_at([near, far], SPOT, RATE)
    folded = row_gex(call(100.0, 2_000, years=7.0 / 365.0), SPOT, RATE)

    assert separate is not None and folded is not None
    assert separate != pytest.approx(folded, rel=0.01)
    assert separate == pytest.approx(
        (row_gex(near, SPOT, RATE) or 0.0) + (row_gex(far, SPOT, RATE) or 0.0)
    )


def test_a_wall_far_from_spot_is_reported_with_a_caveat() -> None:
    """The definition has no distance limit, so the heaviest strike is often a wing.

    Found on the first live run: the SPX call wall came back 70% above spot and SPY's
    put wall 32% below. Both are real counts and neither is a level, so the number
    stands and the note says what it is.
    """
    rows = ladder({170.0: 50_000, 102.0: 900}, {96.0: 1_200})
    profile = build_profile(rows, SPOT, RATE)

    assert profile.call_wall is not None
    assert profile.call_wall.strike == 170.0
    assert any("off the chart" in note for note in profile.notes)


def test_a_wall_close_to_spot_carries_no_caveat() -> None:
    profile = build_profile(full_chain(), SPOT, RATE)

    assert not any("off the chart" in note for note in profile.notes)
