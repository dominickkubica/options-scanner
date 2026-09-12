"""The strategy search, tested for the properties that make its answer worth reading.

Two of these matter more than the rest. **No training trade may settle inside the
holdout**, or the split is not a split. And **ranking must be by edge over each
candidate's own null, not by raw return**, because raw return ranks the holding period:
on the first real run every one of the top twelve candidates was a 63 day hold, since
three months of market drift beats any rule's contribution whatever the rule was.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from optscan.analytics.backtest import Direction
from optscan.config import Settings
from optscan.jobs.backtest import Strategy
from optscan.jobs.search import (
    MIN_CANDIDATES_FOR_CORRECTION,
    _entries_in,
    boundary_date,
    expected_best_z,
    run_search,
    split_index,
)
from optscan.models.vendor import VendorDailyBar
from optscan.storage import db
from optscan.storage.vendor import import_daily_bars

SOURCE = "test"
LAST = date(2026, 9, 4)


def series(
    symbol: str, *, count: int = 900, start: float = 100.0, step: float = 0.02
) -> list[VendorDailyBar]:
    bars = []
    for index in range(count):
        close = start + step * index
        # A little shape so the indicators are not all flat and rules actually fire.
        close += 3.0 * ((index % 37) - 18) / 18.0
        bars.append(
            VendorDailyBar(
                source=SOURCE,
                symbol=symbol,
                session_date=LAST - timedelta(days=count - 1 - index),
                open=close,
                high=close + 1.0,
                low=close - 1.0,
                close=close,
                volume=1_000_000 + (index % 11) * 200_000,
            )
        )
    return bars


@pytest.fixture
def seeded(tmp_settings: Settings) -> Settings:
    with db.session(tmp_settings.sqlite_path) as conn:
        for name, start in (("AAA", 100.0), ("BBB", 50.0), ("CCC", 220.0)):
            import_daily_bars(conn, series(name, start=start))
    return tmp_settings


SMALL_GRID = {"rsi_below": [{"threshold": 30}], "squeeze": [{}]}


# --------------------------------------------------------------------------------
# The split
# --------------------------------------------------------------------------------


def test_no_training_trade_settles_inside_the_holdout() -> None:
    """The margin that keeps the split a split.

    Without the horizon margin the last month of training reads the first month of the
    holdout, which is the quiet way a held-out period stops being held out.
    """
    entries = list(range(100))
    kept = _entries_in([], entries, low=0, high=50, horizon=10)
    assert kept
    assert max(kept) + 10 + 1 < 50


def test_holdout_entries_start_at_the_boundary() -> None:
    entries = list(range(100))
    kept = _entries_in([], entries, low=50, high=100, horizon=5)
    assert min(kept) >= 50


def test_the_boundary_is_one_calendar_date_for_every_symbol(seeded: Settings) -> None:
    """A per-symbol percentile would put one symbol's holdout in 2019 and another's in
    2024, so the held-out set would span different regimes for different names."""
    from optscan.jobs.backtest import load_series

    loaded, _ = load_series(seeded, ["AAA", "BBB", "CCC"])
    split = boundary_date(loaded, 0.5)
    assert split is not None
    for bars in loaded.values():
        index = split_index(bars, split)
        assert bars[index].ts.date() >= split
        if index > 0:
            assert bars[index - 1].ts.date() < split


def test_split_index_puts_everything_in_training_when_the_boundary_is_past_the_end(
    seeded: Settings,
) -> None:
    """A symbol that stopped trading before the split contributes only training data.

    Returning len(bars) rather than raising is what makes that a quiet, correct outcome:
    the symbol has no holdout, and the search simply has less to test it on.
    """
    from optscan.jobs.backtest import load_series

    loaded, _ = load_series(seeded, ["AAA"])
    bars = loaded["AAA"]
    assert split_index(bars, date(2099, 1, 1)) == len(bars)
    assert split_index(bars, date(1990, 1, 1)) == 0


# --------------------------------------------------------------------------------
# The multiplicity correction
# --------------------------------------------------------------------------------


def test_the_best_of_n_bar_grows_with_the_search(seeded: Settings) -> None:
    """More candidates means a higher bar, which is the whole correction."""
    small = expected_best_z(20)
    large = expected_best_z(500)
    assert small is not None and large is not None
    assert large > small
    assert small == pytest.approx(2.45, abs=0.05)
    assert large == pytest.approx(3.53, abs=0.05)


def test_a_single_candidate_needs_no_correction() -> None:
    assert expected_best_z(MIN_CANDIDATES_FOR_CORRECTION - 1) is None


# --------------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------------


def test_candidates_are_ranked_by_edge_not_by_raw_return(seeded: Settings) -> None:
    """The bug this replaced: raw return ranks the horizon, not the rule.

    With several horizons in the grid, a raw ranking puts every long horizon above every
    short one regardless of the rule, because three months of drift dwarfs anything a
    rule contributes. Ranking on the z against each candidate's own null removes that.
    """
    result = run_search(
        seeded,
        Strategy(symbols=["AAA", "BBB", "CCC"]),
        grid=SMALL_GRID,
        horizons=(5, 63),
        directions=(Direction.LONG,),
        finalists=1,
        draws=40,
        search_draws=40,
        min_blocks=1,
    )
    assert result.candidates
    scored = [c for c in result.candidates if c.stats]
    assert scored

    # The ordering follows z, and z is not simply the horizon.
    zs = [c.z_score for c in scored]
    assert zs == sorted(zs, reverse=True)
    horizons = [c.horizon for c in scored]
    assert set(horizons) == {5, 63}, "both horizons should be present to compare"
    assert horizons != sorted(horizons, reverse=True), (
        "the ranking still puts every long horizon first, so it is ranking the horizon"
    )


def test_a_candidate_carries_its_own_null(seeded: Settings) -> None:
    result = run_search(
        seeded,
        Strategy(symbols=["AAA", "BBB"]),
        grid=SMALL_GRID,
        horizons=(21,),
        directions=(Direction.LONG,),
        finalists=1,
        draws=40,
        search_draws=40,
        min_blocks=1,
    )
    for candidate in result.candidates:
        if candidate.stats:
            assert candidate.edge == pytest.approx(candidate.mean_return - candidate.null_mean)


# --------------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------------


def test_a_search_reports_what_it_tried_and_holds_out_a_period(
    seeded: Settings,
) -> None:
    result = run_search(
        seeded,
        Strategy(symbols=["AAA", "BBB", "CCC"]),
        grid=SMALL_GRID,
        horizons=(5, 21),
        directions=(Direction.LONG, Direction.SHORT),
        finalists=2,
        draws=40,
        search_draws=40,
        min_blocks=1,
    )
    assert result.tried == 2 * 2 * 2
    assert result.train_end is not None
    assert len(result.finalists) <= 2
    assert any("combinations were tried" in note for note in result.notes)
    assert any("prior selection is invisible" in note for note in result.notes)


def test_the_report_warns_that_finalists_are_themselves_multiple(
    seeded: Settings,
) -> None:
    result = run_search(
        seeded,
        Strategy(symbols=["AAA", "BBB", "CCC"]),
        grid=SMALL_GRID,
        horizons=(5, 21),
        directions=(Direction.LONG,),
        finalists=3,
        draws=40,
        search_draws=40,
        min_blocks=1,
    )
    if len(result.finalists) > 1:
        assert any("one of that many" in note for note in result.notes)


def test_a_search_with_no_usable_candidate_says_so(seeded: Settings) -> None:
    """A floor nothing clears is a finding about the data, not an error."""
    result = run_search(
        seeded,
        Strategy(symbols=["AAA"]),
        grid={"rsi_below": [{"threshold": 2}]},
        horizons=(5,),
        directions=(Direction.LONG,),
        draws=20,
        search_draws=20,
        min_blocks=10_000,
    )
    assert result.finalists == []
    assert any("nothing worth carrying" in note for note in result.notes)


# --------------------------------------------------------------------------------
# Nothing about the holdout reaches the ranking
# --------------------------------------------------------------------------------


def test_costs_are_measured_on_one_side_of_the_boundary_at_a_time(
    seeded: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ranking used to be charged spreads estimated over the whole history.

    That let the holdout's liquidity into the training ranking. Every cost estimate must
    now read bars from one side of the boundary only: training bars for the ranking,
    test bars for the out-of-sample evaluation.
    """
    import optscan.jobs.search as search_module

    seen: list[tuple[date, date]] = []
    original = search_module.symbol_costs

    def spy(template, series):
        days = [bar.ts.date() for bars in series.values() for bar in bars]
        seen.append((min(days), max(days)))
        return original(template, series)

    monkeypatch.setattr(search_module, "symbol_costs", spy)
    result = run_search(
        seeded,
        Strategy(symbols=["AAA", "BBB", "CCC"], cost_model="estimated"),
        grid=SMALL_GRID,
        horizons=(5,),
        directions=(Direction.LONG,),
        finalists=1,
        draws=20,
        search_draws=20,
        min_blocks=1,
        record=False,
    )
    assert result.train_end is not None and seen
    for first, last in seen:
        assert last < result.train_end or first >= result.train_end, (
            f"a cost estimate read {first} to {last}, across the boundary {result.train_end}"
        )


# --------------------------------------------------------------------------------
# The ledger
# --------------------------------------------------------------------------------


def _small(settings: Settings, symbols: list[str], **kwargs) -> object:
    return run_search(
        settings,
        Strategy(symbols=symbols),
        grid=SMALL_GRID,
        horizons=(5,),
        directions=(Direction.LONG,),
        finalists=2,
        draws=20,
        search_draws=20,
        min_blocks=1,
        **kwargs,
    )


def test_a_second_look_at_the_same_holdout_raises_the_bar(seeded: Settings) -> None:
    """A holdout is held out once. The second search on it is told about the first."""
    first = _small(seeded, ["AAA", "BBB"])
    assert first.finalists
    assert first.prior_uses == 0

    second = _small(seeded, ["AAA", "BBB"])
    assert second.prior_uses == 1
    assert second.prior_finalists == len(first.finalists)
    assert second.threshold < first.threshold
    assert any("evaluated 1 time(s) before" in note for note in second.notes)


def test_overlap_is_by_data_not_by_label(seeded: Settings) -> None:
    """A search sharing even one symbol with an earlier one shares its holdout count;
    a search on entirely different symbols does not."""
    _small(seeded, ["AAA"])
    disjoint = _small(seeded, ["BBB"])
    assert disjoint.prior_uses == 0
    overlapping = _small(seeded, ["AAA", "CCC"])
    assert overlapping.prior_uses == 1


def test_an_unrecorded_search_leaves_no_trace(seeded: Settings) -> None:
    _small(seeded, ["AAA"], record=False)
    assert _small(seeded, ["AAA"]).prior_uses == 0


# --------------------------------------------------------------------------------
# Walk-forward
# --------------------------------------------------------------------------------


def test_walk_forward_tests_each_window_once_in_order(seeded: Settings) -> None:
    """Every fold trains strictly before its window and the windows tile the future."""
    from optscan.jobs.search import run_walk_forward

    result = run_walk_forward(
        seeded,
        Strategy(symbols=["AAA", "BBB", "CCC"]),
        grid=SMALL_GRID,
        horizons=(5,),
        directions=(Direction.LONG,),
        folds=3,
        first_train_share=0.4,
        draws=20,
        search_draws=20,
        min_blocks=1,
    )
    assert len(result.folds) == 3
    for current, following in zip(result.folds, result.folds[1:], strict=False):
        assert current.test_end == following.train_end
        assert current.train_end < current.test_end
    assert result.folds[-1].test_end is None
    assert any("no fold's choice saw it" in note for note in result.notes)

    payload = result.as_dict()
    assert len(payload["folds"]) == 3

    # Each fold with a winner is a recorded look at its window.
    looked = sum(1 for fold in result.folds if fold.winner is not None)
    again = _small(seeded, ["AAA", "BBB", "CCC"])
    assert again.prior_uses >= min(1, looked)


def test_walk_forward_refuses_a_single_fold(seeded: Settings) -> None:
    from optscan.jobs.search import run_walk_forward

    with pytest.raises(ValueError, match="at least two folds"):
        run_walk_forward(seeded, Strategy(symbols=["AAA"]), folds=1)


def test_a_search_round_trips_to_plain_data(seeded: Settings) -> None:
    result = run_search(
        seeded,
        Strategy(symbols=["AAA", "BBB"]),
        grid=SMALL_GRID,
        horizons=(21,),
        directions=(Direction.LONG,),
        finalists=1,
        draws=40,
        search_draws=40,
        min_blocks=1,
    )
    payload = result.as_dict()
    assert payload["tried"] == len(payload["candidates"])
    assert "train_end" in payload
    for row in payload["candidates"]:
        assert {"label", "edge", "z_score", "blocks"} <= set(row)
