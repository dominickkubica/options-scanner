"""Plain-English journal findings, tested for the rules that keep them honest.

A comparison needs enough trades on both sides, a small sample says so in the sentence,
and an average is only called an edge when its interval excludes zero.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from optscan.analytics.calibration import MIN_CLUSTERS_FOR_A_CLAIM, Interval
from optscan.analytics.insights import (
    MIN_GROUP,
    _best_worst,
    _curve,
    _money,
    _performance,
    _sizing,
    _unsure,
    insights,
)
from optscan.analytics.journal import Breakdown, DayPoint, Estimate, build_report
from optscan.analytics.ledger import build_trades, journal_entries
from optscan.analytics.positions import Sizing, StopReport, build_book, build_positions


def group(key: str, trades: int, mean: float, clusters: int | None = None) -> Breakdown:
    return Breakdown(
        key=key,
        trades=trades,
        clusters=trades if clusters is None else clusters,
        win_rate=None,
        total_profit=mean * trades,
        mean_profit=mean,
    )


def sizing(win: float, win_n: int, loss: float, loss_n: int) -> Sizing:
    return Sizing(
        points=[],
        basis="dollars",
        median_risk=67.80,
        after_win_risk=win,
        after_win_n=win_n,
        after_loss_risk=loss,
        after_loss_n=loss_n,
        starting_balance=None,
        net_deposits=913.0,
    )


def test_a_comparison_needs_enough_trades_on_both_sides() -> None:
    """Below MIN_GROUP, 'your best strategy' is one good trade."""
    assert (
        _best_worst([group("condor", 30, 5.0), group("strangle", MIN_GROUP - 1, -40.0)], "x")
        is None
    )
    sentence = _best_worst([group("condor", 30, 5.0), group("spread", MIN_GROUP, -4.0)], "strategy")
    assert sentence is not None
    assert "condor has done best" in sentence and "spread worst" in sentence


def test_a_small_comparison_says_so_in_the_sentence() -> None:
    small = _best_worst([group("a", 6, 5.0), group("b", 6, -5.0)], "entry time")
    large = _best_worst([group("a", 30, 5.0), group("b", 30, -5.0)], "entry time")
    assert small is not None and "too few to be sure" in small
    assert large is not None and "too few" not in large


def test_the_sizing_finding_reads_the_way_a_person_would_say_it() -> None:
    [median, creep] = _sizing(SimpleNamespace(sizing=sizing(77.29, 26, 108.41, 11)))
    assert median == "Your median trade risks $68: 1R, what your stop puts at risk."
    assert creep == (
        "After a losing day your trades risk 40% more than after a winning day "
        "($108 vs $77), though 11 trades is too few to be sure."
    )


def test_a_size_that_does_not_move_is_not_dressed_up_as_a_finding() -> None:
    lines = _sizing(SimpleNamespace(sizing=sizing(100.0, 30, 104.0, 30)))
    assert any("barely changes" in line for line in lines)
    thin = _sizing(SimpleNamespace(sizing=sizing(77.0, 26, 108.0, MIN_GROUP - 1)))
    assert len(thin) == 1, "a comparison against too few trades is not made"


def test_an_edge_is_only_called_one_when_its_range_excludes_zero() -> None:
    def report(low: float, high: float) -> SimpleNamespace:
        return SimpleNamespace(
            expectancy=Estimate(value=3.50, low=low, high=high, observations=44, clusters=44),
            win_rate=Interval(value=0.68, low=0.53, high=0.80, observations=44, clusters=44),
            avg_win=23.33,
            avg_loss=-39.01,
        )

    book = SimpleNamespace(
        stops=StopReport(
            planned_multiple=2.0,
            stopped=11,
            mean_multiple=1.37,
            worst_multiple=1.97,
            beyond_stop=0,
            planned_exits=0,
            mean_slippage=None,
        ),
        r_expectancy=None,
    )
    unproven = _performance(report(-10.52, 17.52), book)
    proven = _performance(report(0.49, 17.88), book)
    assert "isn't a proven edge yet" in unproven[0]
    assert "sits above zero" in proven[0]
    # 39.01 / (23.33 + 39.01) is about 63 percent to break even, against a 68 win rate.
    assert "you need about 63% to break even: a thin margin" in unproven[1]
    assert "11 trades closed at a loss, averaging 1.37x the credit; none went past" in unproven[2]


def test_a_rounded_zero_carries_no_sign() -> None:
    """-0.4 printed as -$0, which reads as a loss on a range's lower end."""
    assert _money(-0.4) == "$0"
    assert _money(-0.6) == "-$1"
    assert _money(3.5, 2) == "+$3.50"


def test_the_caveat_counts_trades_the_sentence_already_shows() -> None:
    """Decided on clusters, worded in trades: 'over 11 ... 10 trades' reads as a typo."""
    assert _unsure(10, 11) == ", though 11 trades is too few to be sure"
    assert _unsure(MIN_CLUSTERS_FOR_A_CLAIM, 25) == ""


def test_positive_r_with_an_unproven_dollar_average_is_explained_as_size() -> None:
    report = SimpleNamespace(
        expectancy=Estimate(value=3.50, low=-10.52, high=17.52, observations=44, clusters=44),
        win_rate=None,
        avg_win=None,
        avg_loss=None,
    )
    stops = StopReport(
        planned_multiple=2.0,
        stopped=0,
        mean_multiple=None,
        worst_multiple=None,
        beyond_stop=0,
        planned_exits=0,
        mean_slippage=None,
    )
    r = Estimate(value=0.17, low=0.01, high=0.32, observations=44, clusters=44)

    def trade(risk: float, r_multiple: float) -> SimpleNamespace:
        return SimpleNamespace(risk_unit=risk, r_multiple=r_multiple)

    typical = [trade(50.0, 0.4)] * 6
    big = [trade(150.0, -0.3)] * 6
    book = SimpleNamespace(stops=stops, r_expectancy=r, positions=typical + big)
    lines = _performance(report, book)
    assert "(range +0.01R to +0.32R, above zero)" in lines[1]
    assert lines[2].endswith(
        "The difference is size: trades above your median risk averaged -0.30R, the rest +0.40R."
    )

    # Big trades that did no worse: the gap is only stated as how dollars weigh trades.
    book.positions = typical + [trade(150.0, 0.5)] * 6
    assert "dollars let the biggest ones dominate" in _performance(report, book)[2]
    # Too few above the median to compare: same, rather than a claim from three trades.
    book.positions = typical + big[:3]
    assert "dollars let the biggest ones dominate" in _performance(report, book)[2]


def test_a_worst_day_bigger_than_the_whole_total_is_named() -> None:
    worst = DayPoint(day=date(2026, 8, 24), profit=-241.14, trades=1, clusters=1, cumulative=0.0)
    best = DayPoint(day=date(2026, 9, 3), profit=121.89, trades=3, clusters=3, cumulative=0.0)
    lines = _curve(
        SimpleNamespace(worst_day=worst, best_day=best, total_profit=153.86, max_drawdown=241.14)
    )
    assert lines[0] == "Your worst day, Aug 24 (-$241), lost more than your whole total of +$154."
    assert lines[1] == "Your deepest drawdown ($241) was that single day."
    assert lines[2] == "Your best day was Sep 03 (+$122)."


def test_every_panel_gets_a_list_from_a_real_statement() -> None:
    import csv
    import io

    from optscan.imports.robinhood import parse_rows
    from tests.test_journal_positions import STATEMENT

    txns = parse_rows(list(csv.DictReader(io.StringIO(STATEMENT))))
    positions = build_positions(build_trades(txns))
    report = build_report(journal_entries(build_trades(txns)))
    book = build_book(positions, positions)
    found = insights(report, book)
    assert set(found) == {
        "performance",
        "curve",
        "trades",
        "breakdowns",
        "time",
        "regime",
        "sizing",
    }
    assert all(isinstance(items, list) for items in found.values())
    # Four positions: no group is big enough to compare, so no comparison is invented.
    assert not any("has done best" in line for line in found["breakdowns"])
