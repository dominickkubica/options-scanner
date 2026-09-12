"""The backtest engine, tested for the ways a backtest lies rather than for arithmetic.

The arithmetic tests are here and they are the easy half. The half that matters is the
set of properties that stop this module flattering a strategy: entries fill on the next
bar, ties go to the stop, unfinished trades are dropped, overlapping rows are not
counted as independent, and above all **a rule with no skill must not beat the null in a
market that rose**, which is the single check that separates a backtest from a sales
pitch.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pytest

from optscan.analytics.backtest import (
    DEFAULT_BLOCK_BARS,
    MIN_BLOCKS_FOR_A_CLAIM,
    TENOR_BAND,
    Direction,
    ExitReason,
    MissingImpliedVol,
    Trade,
    block_bootstrap_interval,
    blocks_to_detect,
    effective_sample,
    equity_curve,
    every_bar,
    measure_edge,
    null_distribution,
    require_implied_vol,
    short_option_trades,
    simulate,
    summarize,
    sweep,
    yearly,
)
from optscan.models import PriceBar

FIXTURE = Path(__file__).parent / "fixtures" / "spy_daily_bars.json"
BASE = datetime(2020, 1, 2, tzinfo=UTC)


def bar(
    close: float,
    *,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    day: int = 0,
) -> PriceBar:
    open_ = close if open_ is None else open_
    return PriceBar(
        symbol="TEST",
        ts=BASE + timedelta(days=day),
        open=open_,
        high=max(close, open_) if high is None else high,
        low=min(close, open_) if low is None else low,
        close=close,
        volume=1_000_000,
        fetched_at=BASE,
        source="test",
    )


def flat(count: int, price: float = 100.0) -> list[PriceBar]:
    return [bar(price, day=index) for index in range(count)]


@pytest.fixture(scope="session")
def spy_bars() -> list[PriceBar]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [
        PriceBar(
            symbol=payload["symbol"],
            ts=datetime.fromisoformat(row["ts"]),
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            volume=row["volume"],
            fetched_at=BASE,
            source="fixture",
        )
        for row in payload["bars"]
    ]


# --------------------------------------------------------------------------------
# No lookahead
# --------------------------------------------------------------------------------


def test_entry_fills_on_the_next_bar_open_not_the_signal_close() -> None:
    """A rule reading today's close cannot be filled at today's close.

    The bar after the signal opens at 200 while the signal bar closed at 100. A backtest
    that filled at the close would book the whole gap as profit on every trade.
    """
    bars = [bar(100.0, day=0), bar(200.0, open_=200.0, day=1), bar(210.0, day=2)]
    trades = simulate("TEST", bars, [0], horizon=1, cost=0.0)
    assert len(trades) == 1
    assert trades[0].entry_price == 200.0
    assert trades[0].entry_date == bars[1].ts.date()


def test_a_signal_on_the_last_bar_is_not_tradable() -> None:
    """There is no next open to fill against, so it is dropped rather than filled today."""
    bars = flat(10)
    assert simulate("TEST", bars, [len(bars) - 1], horizon=1) == []


# --------------------------------------------------------------------------------
# Exits
# --------------------------------------------------------------------------------


def test_a_bar_that_hits_both_target_and_stop_is_recorded_as_the_stop() -> None:
    """Intrabar order is unknowable, and assuming the good one is a free lunch."""
    bars = [
        bar(100.0, day=0),
        bar(100.0, open_=100.0, high=120.0, low=80.0, day=1),
        bar(100.0, day=2),
    ]
    trades = simulate("TEST", bars, [0], horizon=1, target=0.10, stop=0.10, cost=0.0)
    assert trades[0].reason is ExitReason.STOP
    assert trades[0].net_return == pytest.approx(-0.10)


def test_a_target_is_taken_at_the_target_not_the_extreme() -> None:
    bars = [
        bar(100.0, day=0),
        bar(100.0, open_=100.0, high=140.0, low=99.0, day=1),
        bar(100.0, day=2),
    ]
    trades = simulate("TEST", bars, [0], horizon=1, target=0.10, cost=0.0)
    assert trades[0].reason is ExitReason.TARGET
    assert trades[0].net_return == pytest.approx(0.10)


def test_a_trade_that_has_not_finished_is_dropped_not_marked_to_market() -> None:
    """Otherwise the newest weeks fill with trades cut short at whatever was happening,
    which is exactly the period a reader looks at hardest."""
    bars = flat(10)
    assert simulate("TEST", bars, [7], horizon=21) == []


def test_a_short_trade_profits_when_price_falls() -> None:
    bars = [bar(100.0, day=0), bar(100.0, open_=100.0, day=1), bar(90.0, day=2)]
    trades = simulate("TEST", bars, [0], direction=Direction.SHORT, horizon=1, cost=0.0)
    assert trades[0].net_return == pytest.approx(0.10)


def test_costs_are_charged_once_per_round_trip() -> None:
    bars = [bar(100.0, day=0), bar(100.0, open_=100.0, day=1), bar(100.0, day=2)]
    trades = simulate("TEST", bars, [0], horizon=1, cost=0.002)
    assert trades[0].net_return == pytest.approx(-0.002)


def test_overlapping_entries_are_skipped_by_default() -> None:
    """A portfolio cannot hold the same symbol five times over."""
    bars = flat(60)
    trades = simulate("TEST", bars, list(range(50)), horizon=10)
    starts = [t.entry_index for t in trades]
    assert all(b - a >= 10 for a, b in pairwise(starts))


# --------------------------------------------------------------------------------
# Independence
# --------------------------------------------------------------------------------


def test_effective_sample_counts_blocks_not_rows() -> None:
    """Twenty trades inside one block are one episode wearing twenty rows."""
    trades = [
        Trade(
            symbol="SPY",
            entry_date=date(2024, 1, 2),
            exit_date=date(2024, 2, 2),
            entry_price=100.0,
            exit_price=101.0,
            direction=Direction.LONG,
            reason=ExitReason.HORIZON,
            entry_index=index,
            bars_held=21,
            net_return=0.01,
        )
        for index in range(20)
    ]
    assert len(trades) == 20
    assert effective_sample(trades, DEFAULT_BLOCK_BARS) == 1


def dated(symbol: str, day: date, *, index: int = 3, value: float = 0.01) -> Trade:
    return Trade(
        symbol=symbol,
        entry_date=day,
        exit_date=day + timedelta(days=30),
        entry_price=100.0,
        exit_price=101.0,
        direction=Direction.LONG,
        reason=ExitReason.HORIZON,
        entry_index=index,
        bars_held=21,
        net_return=value,
    )


def test_effective_sample_pools_symbols_inside_one_block() -> None:
    """Ninety-eight tech names oversold in the same week are one observation.

    The per-symbol key looks more careful and is the trap: with overlapping entries
    suppressed, consecutive trades on a symbol are already a horizon apart, so keying on
    (symbol, block) returns the trade count and measures nothing.
    """
    same_week = [dated(symbol, date(2024, 1, 2)) for symbol in ("SPY", "QQQ", "IWM", "DIA")]
    assert effective_sample(same_week) == 1

    # Different months are separate episodes whatever the symbol.
    spread = [dated("SPY", date(2024, 1, 2)), dated("QQQ", date(2024, 6, 3))]
    assert effective_sample(spread) == 2


def test_blocks_follow_the_calendar_not_the_bar_index() -> None:
    """The same day is the same episode for every symbol, however long each has traded.

    Bar 2000 of SPY and bar 12 of a stock that listed last month can be the same session,
    and bar 500 of each is years apart. Keying blocks on the index pooled the second pair
    and split the first, and 46 of the 294 stored symbols listed after 2016.
    """
    same_day = [
        dated("SPY", date(2025, 3, 3), index=2000),
        dated("ARM", date(2025, 3, 3), index=12),
    ]
    assert effective_sample(same_day) == 1

    same_index = [
        dated("SPY", date(2018, 9, 4), index=500),
        dated("ARM", date(2025, 9, 2), index=500),
    ]
    assert effective_sample(same_index) == 2


def test_the_interval_is_as_wide_as_the_episodes_not_the_rows() -> None:
    """Two hundred trades in ten weeks are ten observations, and the interval says so.

    Resampling rows would give a clustered sample the confidence of two hundred
    independent draws. The same returns spread one per week are genuinely independent,
    so their interval must come out much narrower.
    """
    rng = __import__("random").Random(3)
    week_values = [rng.gauss(0.0, 0.02) for _ in range(10)]
    start = date(2020, 1, 6)

    clustered = [
        dated(f"S{n}", start + timedelta(weeks=5 * week), value=value)
        for week, value in enumerate(week_values)
        for n in range(20)
    ]
    spread = [
        dated("S", start + timedelta(weeks=5 * i), value=week_values[i % 10]) for i in range(200)
    ]

    wide = block_bootstrap_interval(clustered)
    narrow = block_bootstrap_interval(spread)
    assert wide is not None and narrow is not None
    assert (wide[1] - wide[0]) > 3 * (narrow[1] - narrow[0])


def test_an_interval_needs_at_least_two_blocks() -> None:
    assert block_bootstrap_interval([dated("SPY", date(2024, 1, 2))]) is None
    stats = summarize([dated("SPY", date(2024, 1, 2))])
    assert stats is not None and stats.ci_low is None


def test_blocks_to_detect_scales_with_the_inverse_square_of_the_edge() -> None:
    """Half the edge takes four times the episodes. That is the whole sample size story."""
    base = blocks_to_detect(0.01, 0.01, 100)
    assert base == 619  # 100 * (1.645 + 0.842) ** 2, rounded up
    half = blocks_to_detect(0.005, 0.01, 100)
    assert half is not None and 4 * base - 4 <= half <= 4 * base + 4
    assert blocks_to_detect(0.0, 0.01, 100) is None
    assert blocks_to_detect(-0.01, 0.01, 100) is None


def test_stats_refuse_to_claim_below_the_block_floor() -> None:
    bars = flat(200)
    trades = simulate("TEST", bars, list(range(0, 100, 30)), horizon=21)
    stats = summarize(trades)
    assert stats is not None
    assert stats.effective_sample < MIN_BLOCKS_FOR_A_CLAIM
    assert stats.enough_to_claim is False


# --------------------------------------------------------------------------------
# The null. This is the file's reason for existing.
# --------------------------------------------------------------------------------


def signal_indices(trades: list[Trade]) -> list[int]:
    """The signal bars that actually produced trades.

    `entry_index` is the fill, one bar after the signal, and the forward return table is
    indexed by the signal. Off by one here would shift the entire null by a day.
    """
    return [t.entry_index - 1 for t in trades]


def test_a_rule_with_no_skill_does_not_beat_the_null_on_real_bars(
    spy_bars: list[PriceBar],
) -> None:
    """A calendar rule makes money on eleven hundred rising SPY sessions, and must
    still come out unremarkable against a null that contains the same drift.

    Entering every twenty-first bar carries no market information at all: it is a rule
    about the calendar. If this test ever fails, the null has stopped carrying the
    market's return and every long strategy in the tool is about to look brilliant.
    """
    calendar = list(range(60, len(spy_bars), 21))
    trades = simulate("SPY", spy_bars, calendar, horizon=21, cost=0.0)
    stats = summarize(trades)
    assert stats is not None
    # It really does make money. That is the point: the raw number is not evidence.
    assert stats.mean_return > 0

    nulls = null_distribution(
        {"SPY": spy_bars},
        {"SPY": signal_indices(trades)},
        horizon=21,
        cost=0.0,
        draws=200,
    )
    edge = measure_edge(stats.mean_return, nulls)
    assert edge is not None
    assert edge.p_value > 0.05, (
        f"a rule that enters on a fixed calendar scored p={edge.p_value:.3f}, which "
        "means the null is not carrying the drift"
    )


def test_the_null_is_centred_near_what_holding_actually_returned(
    spy_bars: list[PriceBar],
) -> None:
    """A sanity check on the null itself: random 21 bar holds of a rising index should
    average a small positive number, not zero."""
    scattered = list(range(60, len(spy_bars) - 40, 25))
    nulls = null_distribution(
        {"SPY": spy_bars}, {"SPY": scattered}, horizon=21, cost=0.0, draws=200
    )
    assert nulls
    average = sum(nulls) / len(nulls)
    assert 0.0 < average < 0.10


def test_a_strategy_that_cheats_does_beat_the_null(spy_bars: list[PriceBar]) -> None:
    """The other direction: if the null cannot be beaten by a rule with genuine
    foresight, the test above is passing for the wrong reason.

    This rule looks at the future, which is exactly why it must score well. It is the
    positive control, not a strategy.
    """
    horizon = 21
    cheating = [
        index
        for index in range(len(spy_bars) - horizon - 2)
        if spy_bars[index + horizon + 1].close > spy_bars[index + 1].open * 1.02
    ]
    trades = simulate("SPY", spy_bars, cheating, horizon=horizon, cost=0.0)
    stats = summarize(trades)
    assert stats is not None

    nulls = null_distribution(
        {"SPY": spy_bars},
        {"SPY": signal_indices(trades)},
        horizon=horizon,
        cost=0.0,
        draws=200,
    )
    edge = measure_edge(stats.mean_return, nulls)
    assert edge is not None
    assert edge.p_value < 0.01
    assert edge.edge > 0


def staggered_universe(seed: int = 7) -> dict[str, list[PriceBar]]:
    """Twelve symbols on one market factor, listed at three different dates.

    The market has volatility regimes, because the null only matters when entries
    cluster in time and *which* time they cluster in changes the outcome. A third of the
    symbols trade from the start, a third from session 500, a third from session 1000,
    which is the shape of the real universe: 46 of 294 listed after 2016.
    """
    import math
    import random

    rng = random.Random(seed)
    sessions = 1500
    market = [rng.gauss(0.0004, 0.03 if (t // 150) % 3 == 0 else 0.008) for t in range(sessions)]
    universe: dict[str, list[PriceBar]] = {}
    for n, start in enumerate([0] * 4 + [500] * 4 + [1000] * 4):
        price = 100.0
        bars = []
        for t in range(start, sessions):
            price *= math.exp(market[t] + rng.gauss(0.0, 0.006))
            bars.append(
                PriceBar(
                    symbol=f"S{n}",
                    ts=BASE + timedelta(days=t),
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=1_000_000,
                    fetched_at=BASE,
                    source="test",
                )
            )
        universe[f"S{n}"] = bars
    return universe


def test_the_null_is_calibrated_on_a_universe_that_listed_at_different_times() -> None:
    """A placebo check on the whole null: rules with no skill should look like no skill.

    Each placebo picks a handful of random days and enters every symbol listed on each,
    which is how real rules fire: in market-wide clusters. None of them can know anything,
    so their p-values should be uniform, and about one in ten should land in the outer
    five percent at either end. A null that is too narrow puts far more there.

    The old null shifted bar indices, wrapping each symbol at its own length, so symbols
    that listed at different times drifted into different years and the co-movement the
    shift exists to preserve was scattered. Measured on this universe it put 21 of 120
    placebos in the tails where about 12 belong; the calendar shift puts 13. This is its
    regression test, and the bound sits between the two.
    """
    import random

    universe = staggered_universe()
    starts = {symbol: 1500 - len(bars) for symbol, bars in universe.items()}
    trials = 120
    tails = 0

    for trial in range(trials):
        pick = random.Random(1000 + trial)
        events = pick.sample(range(0, 1480), 8)
        fired: dict[str, list[int]] = {}
        trades: list[Trade] = []
        for symbol, bars in universe.items():
            local = sorted(t - starts[symbol] for t in events if t >= starts[symbol])
            found = simulate(symbol, bars, local, horizon=5, cost=0.0)
            if found:
                trades.extend(found)
                fired[symbol] = signal_indices(found)
        stats = summarize(trades, interval=False)
        assert stats is not None
        nulls = null_distribution(universe, fired, horizon=5, cost=0.0, draws=200)
        edge = measure_edge(stats.mean_return, nulls)
        assert edge is not None
        if edge.p_value <= 0.05 or edge.p_value >= 0.95:
            tails += 1

    # Twelve expected. Twenty is about three standard deviations of a binomial above it.
    assert tails <= 20, f"{tails} of {trials} placebos landed in the tails; expected ~12"


def test_the_p_value_is_never_exactly_zero() -> None:
    """No finite resample is entitled to claim it."""
    edge = measure_edge(99.0, [0.0] * 100)
    assert edge is not None
    assert edge.p_value > 0
    assert edge.p_value == pytest.approx(1 / 101)


def test_the_null_is_reproducible() -> None:
    """A p-value that moves on every refresh is not a number anybody can act on."""
    bars = flat(400, 100.0)
    rising = [bar(100.0 + index * 0.1, day=index) for index in range(400)]
    picks = list(range(60, 300, 20))
    first = null_distribution({"A": rising}, {"A": picks}, horizon=5, draws=50)
    second = null_distribution({"A": rising}, {"A": picks}, horizon=5, draws=50)
    assert first == second
    assert bars  # the flat series is unused here, kept for the contrast


# --------------------------------------------------------------------------------
# Reporting over time
# --------------------------------------------------------------------------------


def test_yearly_splits_by_exit_year(spy_bars: list[PriceBar]) -> None:
    trades = simulate("SPY", spy_bars, every_bar(spy_bars), horizon=21)
    rows = yearly(trades)
    assert len(rows) > 1
    assert [row.year for row in rows] == sorted(row.year for row in rows)
    assert sum(row.trades for row in rows) == len(trades)


def test_equity_curve_is_a_monthly_portfolio_not_a_chain_of_trades() -> None:
    """Concurrent trades must not compound as though they were consecutive.

    Three trades on three symbols in the same month are one month of an equal weighted
    portfolio, not three sequential bets each staking the whole account. Multiplying them
    is how a real run produced a yearly total of +2,737,313 percent.
    """

    def one(symbol: str, value: float, month: int) -> Trade:
        return Trade(
            symbol=symbol,
            entry_date=date(2024, month, 1),
            exit_date=date(2024, month, 20),
            entry_price=100.0,
            exit_price=100.0,
            direction=Direction.LONG,
            reason=ExitReason.HORIZON,
            entry_index=0,
            bars_held=1,
            net_return=value,
        )

    concurrent = [one("A", 0.10, 3), one("B", 0.10, 3), one("C", 0.10, 3)]
    curve = equity_curve(concurrent)
    assert len(curve) == 1
    # The month returned 10 percent, not 33.1 percent.
    assert curve[0][1] == pytest.approx(1.10)

    sequential = [one("A", 0.10, 3), one("A", 0.10, 4), one("A", 0.10, 5)]
    assert equity_curve(sequential)[-1][1] == pytest.approx(1.1**3)


def test_equity_curve_compounds_in_exit_order() -> None:
    bars = [bar(100.0, day=index) for index in range(10)]
    trades = [
        Trade(
            symbol="A",
            entry_date=date(2024, 1, 1),
            exit_date=date(2024, 3, 1),
            entry_price=100.0,
            exit_price=110.0,
            direction=Direction.LONG,
            reason=ExitReason.HORIZON,
            entry_index=0,
            bars_held=1,
            net_return=0.10,
        ),
        Trade(
            symbol="A",
            entry_date=date(2024, 1, 1),
            exit_date=date(2024, 2, 1),
            entry_price=100.0,
            exit_price=90.0,
            direction=Direction.LONG,
            reason=ExitReason.HORIZON,
            entry_index=0,
            bars_held=1,
            net_return=-0.10,
        ),
    ]
    curve = equity_curve(trades)
    assert [when for when, _ in curve] == ["2024-02", "2024-03"]
    assert curve[-1][1] == pytest.approx(0.9 * 1.1)
    assert bars


# --------------------------------------------------------------------------------
# Options
# --------------------------------------------------------------------------------


def rising(count: int, start: float = 100.0, step: float = 0.05) -> list[PriceBar]:
    return [bar(start + step * index, day=index) for index in range(count)]


def test_an_option_backtest_refuses_a_tenor_the_vol_cannot_price() -> None:
    """A 30 day implied vol does not price a 7 day option. This project already made
    that mistake once in the IV rank."""
    bars = rising(120)
    implied = {b.ts.date(): 0.20 for b in bars}
    with pytest.raises(ValueError, match="tenor mismatch"):
        short_option_trades("TEST", bars, implied, [0], dte=7)
    with pytest.raises(ValueError, match="tenor mismatch"):
        short_option_trades("TEST", bars, implied, [0], dte=200)
    for dte in TENOR_BAND:
        short_option_trades("TEST", bars, implied, [0], dte=dte)


def test_realized_vol_is_refused_as_a_substitute_for_implied() -> None:
    """The substitution removes the variance risk premium, which is the edge being
    measured. Refusing is the whole point of the guard."""
    with pytest.raises(MissingImpliedVol, match="variance risk premium"):
        require_implied_vol("NVDA", available=0, needed=500)
    require_implied_vol("QQQ", available=500, needed=500)


def test_a_put_that_expires_worthless_keeps_the_whole_credit() -> None:
    bars = rising(120, start=100.0, step=0.10)  # drifts up, so a 5% OTM put is safe
    implied = {b.ts.date(): 0.20 for b in bars}
    trades = short_option_trades("TEST", bars, implied, [0], dte=30, slippage=0.0)
    assert len(trades) == 1
    trade = trades[0]
    assert trade.spot_at_expiry > trade.strike
    assert trade.profit == pytest.approx(trade.credit)
    assert trade.net_return == pytest.approx(trade.credit / trade.strike)


def test_a_put_that_finishes_in_the_money_loses_the_intrinsic() -> None:
    bars = [bar(100.0 - index * 0.5, day=index) for index in range(120)]
    implied = {b.ts.date(): 0.30 for b in bars}
    trades = short_option_trades("TEST", bars, implied, [0], dte=30, slippage=0.0)
    trade = trades[0]
    assert trade.spot_at_expiry < trade.strike
    expected = trade.credit - (trade.strike - trade.spot_at_expiry)
    assert trade.profit == pytest.approx(expected)
    assert trade.profit < 0


def test_the_return_is_measured_against_collateral_not_the_credit() -> None:
    """Profit over credit reports a 40 percent gain on a trade that tied up twenty
    times that in cash."""
    bars = rising(120, step=0.10)
    implied = {b.ts.date(): 0.20 for b in bars}
    trade = short_option_trades("TEST", bars, implied, [0], dte=30, slippage=0.0)[0]
    assert trade.net_return == pytest.approx(trade.profit / trade.strike)
    assert trade.net_return < 0.10


def test_slippage_reduces_the_credit_and_therefore_the_profit() -> None:
    bars = rising(120, step=0.10)
    implied = {b.ts.date(): 0.20 for b in bars}
    clean = short_option_trades("TEST", bars, implied, [0], dte=30, slippage=0.0)[0]
    haircut = short_option_trades("TEST", bars, implied, [0], dte=30, slippage=0.10)[0]
    assert haircut.credit == pytest.approx(clean.credit * 0.9)
    assert haircut.profit < clean.profit


def test_an_entry_with_no_stored_implied_vol_is_skipped_silently() -> None:
    """Skipped rather than priced off the nearest available day: a vol from three weeks
    ago is a different quote, and carrying it forward is the staleness trap again."""
    bars = rising(120)
    implied = {}
    assert short_option_trades("TEST", bars, implied, [0], dte=30) == []


def test_a_higher_implied_vol_pays_a_larger_credit() -> None:
    bars = rising(120, step=0.10)
    cheap = short_option_trades(
        "TEST", bars, {b.ts.date(): 0.15 for b in bars}, [0], dte=30, slippage=0.0
    )[0]
    rich = short_option_trades(
        "TEST", bars, {b.ts.date(): 0.45 for b in bars}, [0], dte=30, slippage=0.0
    )[0]
    assert rich.credit > cheap.credit


# --------------------------------------------------------------------------------
# Sweeps
# --------------------------------------------------------------------------------


def test_a_sweep_over_noise_says_it_found_noise() -> None:
    """Twenty cells of nothing still have a best cell, and it still looks good."""
    import random as stdlib_random

    rng = stdlib_random.Random(7)
    cells = []
    for index in range(20):
        trades = [
            Trade(
                symbol="A",
                # Five weeks apart, so each trade is its own calendar block. Blocks
                # follow the date, not the index.
                entry_date=date(2018, 1, 1) + timedelta(weeks=5 * step),
                exit_date=date(2018, 2, 1) + timedelta(weeks=5 * step),
                entry_price=100.0,
                exit_price=100.0,
                direction=Direction.LONG,
                reason=ExitReason.HORIZON,
                entry_index=step * DEFAULT_BLOCK_BARS,
                bars_held=21,
                net_return=rng.gauss(0.0, 0.05),
            )
            for step in range(30)
        ]
        cells.append((f"cell{index}", {"n": index}, trades))

    nulls = [rng.gauss(0.0, 0.05 / 30**0.5) for _ in range(2000)]
    result = sweep(cells, nulls)
    assert result.best is not None
    assert result.expected_best_under_null is not None
    # The winner of twenty noise cells is well above zero, which is exactly the trap.
    assert result.best.mean_return > 0
    assert "noise" in result.note or "smaller than the raw number" in result.note


def test_a_sweep_without_enough_null_draws_says_the_winner_is_uninterpretable() -> None:
    cells = [("only", {}, [])]
    result = sweep(cells, [])
    assert result.best is None
    assert "No cell produced a trade" in result.note
