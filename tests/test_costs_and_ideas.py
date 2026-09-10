"""Trading costs, and the trade ideas that depend on them.

The cost estimator matters because it decides whether a finding survives: the strongest
result this project has is worth roughly seventy basis points a trade, and the flat ten
basis points every backtest assumed turned out to be a median of forty-seven.

The ideas registry matters for a different reason. It is the one surface that tells
somebody what to do, so the property worth testing hardest is that **nothing reaches it
without evidence attached**, and that a strategy which failed its holdout stays visible
as retired rather than quietly disappearing.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from optscan.analytics.costs import (
    MIN_BARS,
    blended_cost,
    corwin_schultz_daily,
    estimate_spread,
)
from optscan.config import Settings
from optscan.jobs.backtest import Strategy, symbol_costs
from optscan.jobs.ideas import REGISTRY, Status, as_report, find_ideas, supported
from optscan.models import PriceBar
from optscan.models.vendor import VendorDailyBar
from optscan.storage import db
from optscan.storage.vendor import import_daily_bars

BASE = datetime(2024, 1, 2, tzinfo=UTC)
LAST = date(2026, 9, 4)


def bar(close: float, *, spread: float = 0.0, day: int = 0, wobble: float = 1.0):
    """A bar whose high and low straddle the close by `wobble`, plus half a spread."""
    half = close * spread / 2
    return PriceBar(
        symbol="TEST",
        ts=BASE + timedelta(days=day),
        open=close,
        high=close + wobble + half,
        low=close - wobble - half,
        close=close,
        volume=1_000_000,
        fetched_at=BASE,
        source="test",
    )


# --------------------------------------------------------------------------------
# The estimator
# --------------------------------------------------------------------------------


def test_a_wider_quoted_spread_produces_a_larger_estimate() -> None:
    """The property the whole module rests on: it has to be monotone in the spread."""
    tight = [bar(100.0, spread=0.001, day=i) for i in range(200)]
    wide = [bar(100.0, spread=0.05, day=i) for i in range(200)]
    assert estimate_spread("TIGHT", tight).spread < estimate_spread("WIDE", wide).spread


def test_the_estimator_needs_a_minimum_history() -> None:
    assert estimate_spread("SHORT", [bar(100.0, day=i) for i in range(MIN_BARS - 5)]) is None


def test_a_degenerate_bar_is_skipped_rather_than_crashing() -> None:
    """A zero or negative low is bad data, not a free trade."""
    broken = PriceBar(
        symbol="X",
        ts=BASE,
        open=1.0,
        high=1.0,
        low=0.0,
        close=1.0,
        volume=1,
        fetched_at=BASE,
        source="test",
    )
    assert corwin_schultz_daily(broken, bar(100.0)) is None


def walk(spread: float, *, days: int = 400, vol: float = 0.015, seed: int = 3):
    """A random walk with a real overnight move and a quoted spread inside each bar.

    A constant-close fixture will not do. The estimator separates spread from volatility
    by comparing a one day range against a two day one, so with no overnight movement the
    two are equal and it correctly attributes the *whole* intraday range to spread. That
    is right, and it means a fixture without volatility cannot exercise the separation at
    all: an early version of this test asserted a 2% series had no spread.
    """
    import random as stdlib_random

    rng = stdlib_random.Random(seed)
    bars = []
    price = 100.0
    for index in range(days):
        price *= 1 + rng.gauss(0.0, vol)
        # Intraday travel, then the spread on top of it.
        reach = abs(rng.gauss(0.0, vol)) * price
        half = price * spread / 2
        bars.append(
            PriceBar(
                symbol="W",
                ts=BASE + timedelta(days=index),
                open=price,
                high=price + reach + half,
                low=price - reach - half,
                close=price,
                volume=1_000_000,
                fetched_at=BASE,
                source="test",
            )
        )
    return bars


def test_negative_daily_estimates_are_floored_not_dropped() -> None:
    """Dropping them keeps only the upward noise and biases the average high.

    A series whose bars carry no spread produces negative estimates constantly, and the
    right answer is a number near zero rather than one built from whichever days happened
    to come out positive.
    """
    estimate = estimate_spread("NONE", walk(0.0))
    assert estimate is not None
    assert estimate.spread < 0.01
    assert estimate.negative_share > 0.3


def test_a_high_negative_share_does_not_reject_the_estimate() -> None:
    """An earlier version rejected anything above 35% negative and threw away all 188
    real symbols, because that share is normal for this estimator rather than a fault."""
    estimate = estimate_spread("NONE", walk(0.0))
    assert estimate.negative_share > 0.3
    assert estimate.trustworthy


def test_the_estimate_tracks_a_known_spread_through_real_volatility() -> None:
    """The separation the estimator exists to perform: two series with identical
    volatility and different spreads must come apart."""
    quiet = estimate_spread("A", walk(0.0))
    wide = estimate_spread("B", walk(0.04))
    assert wide.spread > quiet.spread + 0.01


def test_blended_cost_never_returns_zero() -> None:
    """No round trip is free, even on the most liquid instrument there is."""
    assert blended_cost([]) > 0
    flat = [bar(100.0, spread=0.0, day=i) for i in range(200)]
    assert blended_cost([estimate_spread("FLAT", flat)]) > 0


# --------------------------------------------------------------------------------
# Wiring the cost into the backtester
# --------------------------------------------------------------------------------


def series(symbol: str, *, count: int = 400, wobble: float = 1.0):
    return [
        VendorDailyBar(
            source="test",
            symbol=symbol,
            session_date=LAST - timedelta(days=count - 1 - index),
            open=100.0 + index * 0.02,
            high=100.0 + index * 0.02 + wobble,
            low=100.0 + index * 0.02 - wobble,
            close=100.0 + index * 0.02,
            volume=1_000_000,
        )
        for index in range(count)
    ]


@pytest.fixture
def seeded(tmp_settings: Settings) -> Settings:
    with db.session(tmp_settings.sqlite_path) as conn:
        import_daily_bars(conn, series("TIGHT", wobble=0.2))
        import_daily_bars(conn, series("WIDE", wobble=6.0))
    return tmp_settings


def test_the_flat_model_charges_every_symbol_the_same(seeded: Settings) -> None:
    from optscan.jobs.backtest import load_series

    loaded, _ = load_series(seeded, ["TIGHT", "WIDE"])
    costs = symbol_costs(Strategy(cost_model="flat", cost=0.001), loaded)
    assert set(costs.values()) == {0.001}


def test_the_estimated_model_charges_a_wide_symbol_more(seeded: Settings) -> None:
    from optscan.jobs.backtest import load_series

    loaded, _ = load_series(seeded, ["TIGHT", "WIDE"])
    costs = symbol_costs(Strategy(cost_model="estimated"), loaded)
    assert costs["WIDE"] > costs["TIGHT"]


def test_an_unknown_cost_model_is_refused() -> None:
    with pytest.raises(ValueError, match="cost_model must be one of"):
        Strategy(cost_model="vibes").validate()


def test_costs_do_not_change_the_edge_only_the_take_home(seeded: Settings) -> None:
    """The null pays what the strategy pays, so cost cancels out of a timing comparison.

    This is the distinction that matters when reading a result: raising the cost lowers
    what you would have kept without touching whether the rule's timing did anything.
    """
    from optscan.jobs.backtest import run

    cheap = run(seeded, Strategy(entry="every_bar", symbols=["TIGHT", "WIDE"], cost=0.0, draws=60))
    dear = run(seeded, Strategy(entry="every_bar", symbols=["TIGHT", "WIDE"], cost=0.02, draws=60))
    assert cheap.stats.mean_return > dear.stats.mean_return
    assert cheap.edge.edge == pytest.approx(dear.edge.edge, abs=1e-9)


# --------------------------------------------------------------------------------
# Trade ideas
# --------------------------------------------------------------------------------


def test_every_registered_strategy_carries_evidence() -> None:
    """The rule the module exists to enforce. A ticker with no numbers beside it is the
    artefact this whole surface is meant to avoid producing."""
    assert REGISTRY
    for item in REGISTRY:
        assert item.evidence.tested_on
        assert item.evidence.cost_basis
        assert item.evidence.blocks > 0
        assert item.rationale


def test_only_out_of_sample_survivors_are_marked_supported() -> None:
    for item in supported():
        assert item.status is Status.SUPPORTED
        assert item.evidence.p_value <= 0.05
        assert item.evidence.caveats, "a supported strategy still needs its caveats"


def test_a_failed_strategy_is_retired_with_its_reason_not_deleted() -> None:
    """The record of what was believed and why is what makes the next search less
    credulous than the last."""
    retired = [item for item in REGISTRY if item.status is Status.RETIRED]
    assert retired
    for item in retired:
        assert item.evidence.caveats
        assert any("out of sample" in caveat for caveat in item.evidence.caveats)


def test_retired_strategies_never_produce_ideas(seeded: Settings) -> None:
    ideas, _ = find_ideas(seeded, ["TIGHT", "WIDE"])
    retired = {item.key for item in REGISTRY if item.status is Status.RETIRED}
    assert not [idea for idea in ideas if idea.strategy_key in retired]


def test_a_stale_symbol_never_becomes_an_idea(tmp_settings: Settings) -> None:
    """A delisted ticker's last session triggers forever and would sit at the top of a
    list of things to trade today. The backtester keeps those symbols; this must not.

    The stale symbol here stopped only sixty days ago, deliberately. A ticker dead for
    two years falls outside the screen's own lookback and is dropped when the bars are
    read, which excludes it for the wrong reason and would let this pass with the
    staleness guard removed. Sixty days is inside the read and well past MAX_STALE_DAYS,
    so the guard is what has to catch it.
    """
    with db.session(tmp_settings.sqlite_path) as conn:
        import_daily_bars(conn, series("FRESH"))
        recently_dead = [
            VendorDailyBar(
                **{**row.model_dump(), "session_date": row.session_date - timedelta(days=60)}
            )
            for row in series("DEAD")
        ]
        import_daily_bars(conn, recently_dead)

    ideas, notes = find_ideas(tmp_settings, ["FRESH", "DEAD"])
    assert not [idea for idea in ideas if idea.symbol == "DEAD"]
    assert any("stale" in note for note in notes)


def test_an_empty_list_explains_itself(seeded: Settings) -> None:
    """Nothing triggering is the normal state, and an empty table with no sentence reads
    as a broken feature."""
    ideas, notes = find_ideas(seeded, ["TIGHT"], freshness=0)
    if not ideas:
        assert any("normal state" in note for note in notes)


def test_the_report_carries_the_evidence_alongside_the_ideas(seeded: Settings) -> None:
    ideas, notes = find_ideas(seeded, ["TIGHT", "WIDE"])
    report = as_report(ideas, notes)
    assert "strategies" in report
    assert len(report["strategies"]) == len(REGISTRY)
    for row in report["strategies"]:
        assert row["evidence"]["p_value"] is not None
        assert row["evidence"]["tested_on"]


def test_ideas_are_ordered_cheapest_to_trade_first(seeded: Settings) -> None:
    """The edge is a fixed number and the cost is not, so the same signal is worth
    materially less on a thin name."""
    ideas, _ = find_ideas(seeded, ["TIGHT", "WIDE"], freshness=400)
    same_age = [i for i in ideas if i.age == ideas[0].age] if ideas else []
    costs = [i.cost for i in same_age]
    assert costs == sorted(costs)


# --------------------------------------------------------------------------------
# The ideas endpoint
# --------------------------------------------------------------------------------


@pytest.fixture
def client(seeded: Settings):
    from fastapi.testclient import TestClient

    from optscan.api.app import create_app
    from optscan.api.deps import clear_caches

    clear_caches()
    return TestClient(create_app(seeded))


def test_the_endpoint_returns_evidence_in_the_same_payload_as_the_symbols(client) -> None:
    """A separate call for the evidence is the design that ends up showing tickers with
    no numbers on them whenever the second request is slow or never wired up."""
    payload = client.get("/api/ideas").json()
    assert payload["strategies"]
    for row in payload["strategies"]:
        assert row["evidence"]["tested_on"]
        assert row["evidence"]["blocks"] > 0


def test_the_endpoint_includes_retired_strategies(client) -> None:
    """A panel showing only survivors looks like a list of things that work."""
    payload = client.get("/api/ideas").json()
    assert any(row["status"] == "retired" for row in payload["strategies"])


def test_an_unreasonable_freshness_window_is_refused(client) -> None:
    from optscan.api.routers.ideas import MAX_FRESHNESS_DAYS

    assert client.get("/api/ideas", params={"freshness": -1}).status_code == 422
    assert client.get("/api/ideas", params={"freshness": MAX_FRESHNESS_DAYS + 1}).status_code == 422
    assert client.get("/api/ideas", params={"freshness": 0}).status_code == 200


# --------------------------------------------------------------------------------
# The net edge gate
# --------------------------------------------------------------------------------
#
# The bug this section exists to prevent: until 2026-09-10 every triggering symbol was
# listed regardless of its own cost. The oversold rule is worth +71bp gross, and TROX
# costs 151bp to round trip, so the list was asserting a positive expectation that the
# strategy's own measured evidence denies. The cost was computed, printed in its own
# column, and then not used to decide anything.


def test_a_symbol_whose_spread_eats_the_edge_is_not_listed(seeded: Settings) -> None:
    from optscan.jobs.ideas import MIN_NET_EDGE

    ideas, _notes = find_ideas(seeded, ["TIGHT", "WIDE"], freshness=400)

    for idea in ideas:
        assert idea.net_edge >= MIN_NET_EDGE, f"{idea.symbol} listed at {idea.net_edge}"


def falling(symbol: str, *, wobble: float, count: int = 400):
    """A series that ends in a sustained decline, so RSI drops under 30 on the last bar.

    The `seeded` fixture rises steadily and never triggers the only supported rule, so a
    gate test built on it would pass while asserting nothing.
    """
    bars = []
    for index in range(count):
        # Flat for most of the history, then a clean slide into the last thirty sessions.
        close = 100.0 if index < count - 30 else 100.0 - (index - (count - 31)) * 1.2
        bars.append(
            VendorDailyBar(
                source="test",
                symbol=symbol,
                session_date=LAST - timedelta(days=count - 1 - index),
                open=close,
                high=close + wobble,
                low=close - wobble,
                close=close,
                volume=1_000_000,
            )
        )
    return bars


def seed_falling(settings: Settings, symbol: str, wobble: float) -> None:
    with db.session(settings.sqlite_path) as conn:
        import_daily_bars(conn, falling(symbol, wobble=wobble))
        conn.commit()


def test_net_edge_is_gross_minus_this_symbols_own_cost(tmp_settings: Settings) -> None:
    """The only number on the row that answers "is this worth taking *here*"."""
    seed_falling(tmp_settings, "CHEAP", wobble=0.15)

    ideas, _ = find_ideas(tmp_settings, ["CHEAP"], freshness=400)

    assert ideas, "a falling series should trigger the oversold rule"
    for idea in ideas:
        assert idea.net_edge == pytest.approx(idea.gross_edge - idea.cost)
        assert idea.gross_edge > 0


def test_a_priced_out_symbol_is_named_rather_than_vanishing(tmp_settings: Settings) -> None:
    """A symbol disappearing with no reason is how somebody stops trusting the list."""
    # Same trigger, but a range so wide the round trip swallows any plausible edge.
    seed_falling(tmp_settings, "THIN", wobble=8.0)

    ideas, notes = find_ideas(tmp_settings, ["THIN"], freshness=400)

    assert not any(idea.symbol == "THIN" for idea in ideas)
    priced_out = [n for n in notes if "round trip cost" in n]
    assert priced_out, f"expected an explanation, got {notes}"
    assert "THIN" in priced_out[0]


def test_the_report_carries_the_net_edge(seeded: Settings) -> None:
    """The UI ranks on this, so it has to cross the wire."""
    ideas, notes = find_ideas(seeded, ["TIGHT", "WIDE"], freshness=400)
    report = as_report(ideas, notes)
    for row in report["ideas"]:
        assert "net_edge_bp" in row
        assert row["net_edge_bp"] == pytest.approx(row["gross_edge_bp"] - row["cost_bp"])
