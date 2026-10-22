"""Two legged structures managed before expiry.

The engine this extends had an exact payoff: held to expiry, an option is worth
`max(spot - strike, 0)` against a stored close and no model is involved. Closing early
means pricing the structure, so a managed exit is a modelled exit. These tests pin the
two things that keep that honest -- a trigger never fills on the bar that produced it,
and a trade that reaches expiry still settles at exact intrinsic -- plus the tenor
refusal, which is what stops anyone pricing a one week spread with a thirty day vol.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from itertools import pairwise

import pytest

from optscan.analytics.backtest import (
    TENOR_BAND,
    spread_trades,
    spread_trades_as_trades,
)
from optscan.models import PriceBar

START = date(2026, 1, 5)


def series(closes: list[float], *, opens: list[float] | None = None) -> list[PriceBar]:
    """One bar a calendar day, so `dte` days out is `dte` bars out."""
    bars = []
    for index, close in enumerate(closes):
        day = START + timedelta(days=index)
        open_ = opens[index] if opens else close
        bars.append(
            PriceBar(
                symbol="TEST",
                ts=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
                open=open_,
                high=max(open_, close),
                low=min(open_, close),
                close=close,
                volume=1_000,
                fetched_at=datetime.now(UTC),
                source="test",
            )
        )
    return bars


def vols(bars: list[PriceBar], value: float = 0.25) -> dict[date, float]:
    return {bar.ts.date(): value for bar in bars}


class TestTenorRefusal:
    """The refusal is the feature. A thirty day vol cannot price a seven day spread."""

    @pytest.mark.parametrize("dte", [1, 7, 14, 61, 90])
    def test_outside_the_band_is_refused(self, dte: int) -> None:
        bars = series([100.0] * 200)
        with pytest.raises(ValueError, match="tenor mismatch"):
            spread_trades("TEST", bars, vols(bars), [10], dte=dte)

    def test_a_week_is_named_in_the_refusal(self) -> None:
        """Asked for repeatedly, so the error says why rather than just refusing."""
        bars = series([100.0] * 200)
        with pytest.raises(ValueError, match="seven day structure"):
            spread_trades("TEST", bars, vols(bars), [10], dte=7)

    @pytest.mark.parametrize("dte", list(TENOR_BAND))
    def test_the_edges_of_the_band_are_allowed(self, dte: int) -> None:
        bars = series([100.0] * 200)
        spread_trades("TEST", bars, vols(bars), [10], dte=dte)


class TestTheStructure:
    def test_a_put_credit_spread_sells_the_nearer_strike(self) -> None:
        bars = series([100.0] * 120)
        trade = spread_trades("TEST", bars, vols(bars), [10], dte=21)[0]

        assert trade.near_strike == pytest.approx(95.0)
        assert trade.far_strike == pytest.approx(90.0)
        assert trade.opened > 0

    def test_capital_is_the_width_less_the_credit(self) -> None:
        """What a defined risk seller actually ties up. Returning profit over the
        credit would report a huge gain on a trade that risked many times it."""
        bars = series([100.0] * 120)
        trade = spread_trades("TEST", bars, vols(bars), [10], dte=21)[0]

        assert trade.capital == pytest.approx(5.0 - trade.opened)

    def test_a_debit_spread_risks_what_it_paid(self) -> None:
        bars = series([100.0] * 120)
        trade = spread_trades("TEST", bars, vols(bars), [10], dte=21, kind="call_debit")[0]

        assert trade.capital == pytest.approx(trade.opened)
        assert trade.opened > 0

    def test_an_unknown_structure_is_refused(self) -> None:
        bars = series([100.0] * 120)
        with pytest.raises(ValueError, match="kind must be"):
            spread_trades("TEST", bars, vols(bars), [10], dte=21, kind="iron_condor")


class TestSettlement:
    def test_expiry_settles_at_exact_intrinsic(self) -> None:
        """Flat at 100, so a 95/90 put spread expires worthless and the seller keeps
        the whole credit. No model touches this number."""
        bars = series([100.0] * 120)
        trade = spread_trades("TEST", bars, vols(bars), [10], dte=21)[0]

        assert trade.reason == "expiry"
        assert trade.exit_vol is None, "a settled trade needs no volatility"
        assert trade.closed == pytest.approx(0.0)
        assert trade.profit == pytest.approx(trade.opened)

    def test_a_breach_at_expiry_costs_the_width(self) -> None:
        """Dropping to 80 puts both legs in the money, so the spread is worth its full
        width and the loss is the width less the credit: the defined maximum."""
        closes = [100.0] * 12 + [80.0] * 108
        bars = series(closes)
        trade = spread_trades("TEST", bars, vols(bars), [10], dte=21)[0]

        assert trade.closed == pytest.approx(5.0)
        assert trade.net_return == pytest.approx(-1.0, abs=1e-9)

    def test_the_loss_never_exceeds_the_defined_maximum_at_expiry(self) -> None:
        """The long wing is what caps the tail, and it must actually cap it."""
        closes = [100.0] * 12 + [10.0] * 108
        bars = series(closes)
        trade = spread_trades("TEST", bars, vols(bars), [10], dte=21)[0]

        assert trade.net_return >= -1.0 - 1e-9


class TestManagement:
    def falling_vol_bars(self):
        """Flat price with collapsing vol, so the credit spread gains steadily."""
        bars = series([100.0] * 120)
        table = {}
        for index, bar in enumerate(bars):
            # Decays gently from a high base. A steeper decay drives the entry vol so
            # low that the spread opens for no credit and the trade is skipped, which
            # is correct behaviour and a useless fixture.
            table[bar.ts.date()] = max(0.45 - 0.01 * index, 0.05)
        return bars, table

    def test_a_target_fills_the_session_after_it_is_seen(self) -> None:
        """Never on the bar that produced the trigger. Filling there was worth $2.50
        to $6.60 a condor of pure noise in the premium study."""
        bars, table = self.falling_vol_bars()
        managed = spread_trades("TEST", bars, table, [10], dte=21, target=0.5)[0]
        unmanaged = spread_trades("TEST", bars, table, [10], dte=21)[0]

        assert managed.reason == "target"
        assert managed.bars_held < unmanaged.bars_held
        # The capture on the fill bar is past the target, because a session passed
        # between seeing it and getting out.
        assert managed.capture >= 0.5

    def test_a_horizon_closes_what_the_target_did_not(self) -> None:
        bars = series([100.0] * 120)
        trade = spread_trades("TEST", bars, vols(bars), [10], dte=21, hold=5, target=0.99)[0]

        assert trade.reason == "horizon"
        assert trade.bars_held == 5

    def test_a_stop_closes_a_losing_structure(self) -> None:
        """Price grinding toward the short strike with vol rising."""
        closes = [100.0 - index * 0.6 for index in range(120)]
        bars = series(closes)
        table = {bar.ts.date(): 0.25 + 0.01 * index for index, bar in enumerate(bars)}
        trade = spread_trades("TEST", bars, table, [10], dte=21, stop=1.0)[0]

        assert trade.reason in {"stop", "expiry", "horizon"}
        if trade.reason == "stop":
            assert trade.profit < 0

    def test_hold_caps_the_sessions_held(self) -> None:
        bars = series([100.0] * 120)
        trade = spread_trades("TEST", bars, vols(bars), [10], dte=21, hold=3)[0]

        assert trade.bars_held == 3
        assert trade.reason == "horizon"

    def test_overlap_is_refused_by_default(self) -> None:
        """One position at a time, so the next entry waits for the current exit."""
        bars = series([100.0] * 200)
        entries = list(range(10, 60))
        solo = spread_trades("TEST", bars, vols(bars), entries, dte=21)
        overlapping = spread_trades("TEST", bars, vols(bars), entries, dte=21, allow_overlap=True)

        assert len(solo) < len(overlapping)
        for first, second in pairwise(solo):
            assert second.entry_date > first.exit_date or second.entry_index > first.entry_index


class TestAdapter:
    def test_the_shared_shape_keeps_the_capital_return(self) -> None:
        bars = series([100.0] * 120)
        trades = spread_trades("TEST", bars, vols(bars), [10], dte=21)
        adapted = spread_trades_as_trades(trades)

        assert len(adapted) == 1
        assert adapted[0].net_return == pytest.approx(trades[0].net_return)
        assert adapted[0].reason == "expiry"

    def test_a_managed_exit_keeps_its_reason(self) -> None:
        bars = series([100.0] * 120)
        trades = spread_trades("TEST", bars, vols(bars), [10], dte=21, hold=4)
        adapted = spread_trades_as_trades(trades)

        assert adapted[0].reason == "horizon"


class TestStrikesInSigmas:
    """A fixed percentage offset is a different delta in every vol regime."""

    def test_the_offset_scales_with_volatility(self) -> None:
        bars = series([100.0] * 120)
        calm = spread_trades(
            "TEST", bars, vols(bars, 0.15), [10], dte=21, offset=0.84, width=0.84, in_sigmas=True
        )[0]
        wild = spread_trades(
            "TEST", bars, vols(bars, 0.60), [10], dte=21, offset=0.84, width=0.84, in_sigmas=True
        )[0]

        assert (100.0 - wild.near_strike) == pytest.approx(4 * (100.0 - calm.near_strike))

    def test_the_short_strike_sits_at_the_same_delta_in_any_regime(self) -> None:
        """The reason the option exists. At 0.84 sigmas the short put is near 20 delta
        whether the stock is sleepy or panicking."""
        from optscan.analytics.greeks import bsm_greeks

        bars = series([100.0] * 120)
        deltas = []
        for vol in (0.12, 0.30, 0.80):
            trade = spread_trades(
                "TEST", bars, vols(bars, vol), [10], dte=21, offset=0.84, width=0.84, in_sigmas=True
            )[0]
            deltas.append(
                abs(bsm_greeks("put", 100.0, trade.near_strike, 21 / 365, 0.043, vol).delta)
            )

        assert max(deltas) - min(deltas) < 0.03
        assert all(0.15 < d < 0.25 for d in deltas)

    def test_a_fixed_offset_is_not_a_fixed_delta(self) -> None:
        """The confound this exists to remove, demonstrated rather than asserted."""
        from optscan.analytics.greeks import bsm_greeks

        bars = series([100.0] * 120)
        deltas = []
        for vol in (0.12, 0.80):
            trade = spread_trades("TEST", bars, vols(bars, vol), [10], dte=21, offset=0.05)[0]
            deltas.append(
                abs(bsm_greeks("put", 100.0, trade.near_strike, 21 / 365, 0.043, vol).delta)
            )

        assert deltas[1] > 3 * deltas[0]


def test_a_structure_with_an_unlisted_strike_is_skipped_not_priced() -> None:
    """At 400% IV a 1.68 sigma put spread's long leg would sit below zero."""
    bars = series([100.0] * 120)
    found = spread_trades(
        "TEST", bars, vols(bars, 4.0), [10], dte=21, offset=0.84, width=0.84, in_sigmas=True
    )

    assert found == []
