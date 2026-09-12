"""Journal aggregation: the arithmetic a report surface is read for.

The failure mode this guards against is not a crash. It is a plausible looking number.
A drawdown that reports zero because it only measured from the first peak, an
expectancy interval computed across rows instead of clusters, a profit factor rendered
as infinity when nothing lost - each of those draws a confident chart over a wrong
figure, and none of them raise.

The cluster arithmetic gets the most attention here because it is the whole basis for
the report refusing to make a claim. If `expectancy` divided by the row count the
interval would be roughly twelve times too narrow on the current sample, zero would
fall outside it, and the view would present a screen with three settlement dates as a
proven edge.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date

import pytest

from optscan.analytics.journal import (
    SCORE_BANDS,
    DayPoint,
    build_report,
    cluster_totals,
    daily_series,
    expectancy,
    group_by,
    group_by_band,
    max_drawdown,
)


@dataclass(frozen=True)
class FakeOutcome:
    """The subset of LoggedOpportunity the journal actually reads."""

    symbol: str
    expiry: date
    strategy: str = "iron_condor"
    dte: int = 25
    score: float = 0.8
    profit: float | None = 0.0


def make(symbol: str, expiry: str, profit: float, **kwargs) -> FakeOutcome:
    return FakeOutcome(symbol=symbol, expiry=date.fromisoformat(expiry), profit=profit, **kwargs)


class TestClustering:
    def test_rows_on_one_chain_are_one_observation(self) -> None:
        """The premise the whole report rests on."""
        rows = [make("SPY", "2026-01-16", 10.0) for _ in range(50)]
        assert len(cluster_totals(rows)) == 1

    def test_cluster_totals_sum_rather_than_average(self) -> None:
        """Several candidates on one chain are several positions into one settlement."""
        rows = [make("SPY", "2026-01-16", 10.0), make("SPY", "2026-01-16", 30.0)]
        assert cluster_totals(rows) == [40.0]

    def test_symbol_and_expiry_both_separate_clusters(self) -> None:
        rows = [
            make("SPY", "2026-01-16", 1.0),
            make("QQQ", "2026-01-16", 1.0),
            make("SPY", "2026-02-20", 1.0),
        ]
        assert len(cluster_totals(rows)) == 3


class TestExpectancy:
    def test_point_estimate_is_per_row(self) -> None:
        """What a person sizes in, even though the interval is not."""
        rows = [make("SPY", "2026-01-16", 100.0), make("QQQ", "2026-02-20", 200.0)]
        assert expectancy(rows).value == 150.0

    def test_interval_widens_to_the_cluster_count(self) -> None:
        """The point of the whole module.

        Same rows, same mean, but the second set is one chain repeated. Fewer
        independent observations must not produce a tighter interval.
        """
        spread = [make("SPY", "2026-01-16", 100.0), make("QQQ", "2026-02-20", -50.0)] * 10
        clumped = [make("SPY", "2026-01-16", 100.0), make("SPY", "2026-01-16", -50.0)] * 10

        wide = expectancy(spread)
        narrow = expectancy(clumped)
        assert wide.clusters == 2
        assert narrow.clusters == 1
        # One cluster has no spread to measure, so it gets no interval at all rather
        # than a zero width one that would read as certainty.
        assert narrow.low is None and narrow.high is None

    def test_single_cluster_is_flagged_rather_than_claimed(self) -> None:
        estimate = expectancy([make("SPY", "2026-01-16", 100.0)])
        assert estimate.clusters == 1
        assert estimate.indistinguishable_from_zero is True

    def test_zero_inside_the_interval_is_reported_as_such(self) -> None:
        """A mean that could be zero must never be presented as an edge."""
        rows = [
            make("SPY", "2026-01-16", 100.0),
            make("QQQ", "2026-02-20", -98.0),
            make("IWM", "2026-03-20", 60.0),
            make("AAPL", "2026-04-17", -55.0),
        ]
        assert expectancy(rows).indistinguishable_from_zero is True

    def test_no_rows_is_none_not_zero(self) -> None:
        assert expectancy([]) is None


class TestDailySeries:
    def test_keyed_on_expiry_and_cumulative_accumulates(self) -> None:
        rows = [
            make("SPY", "2026-01-16", 100.0),
            make("QQQ", "2026-01-16", 50.0),
            make("SPY", "2026-02-20", -30.0),
        ]
        points = daily_series(rows)
        assert [point.day.isoformat() for point in points] == ["2026-01-16", "2026-02-20"]
        assert [point.profit for point in points] == [150.0, -30.0]
        assert [point.cumulative for point in points] == [150.0, 120.0]
        assert points[0].trades == 2
        assert points[0].clusters == 2


class TestDrawdown:
    def test_measures_peak_to_trough(self) -> None:
        points = [
            DayPoint(date(2026, 1, 1), 100.0, 1, 1, 100.0),
            DayPoint(date(2026, 1, 2), -40.0, 1, 1, 60.0),
            DayPoint(date(2026, 1, 3), 10.0, 1, 1, 70.0),
        ]
        assert max_drawdown(points) == 40.0

    def test_a_curve_that_only_falls_reports_its_whole_fall(self) -> None:
        """Measured from zero, not from the first peak.

        Seeding the peak at the first point instead of at zero would report 30 here
        and call the first 50 of the loss free.
        """
        points = [
            DayPoint(date(2026, 1, 1), -50.0, 1, 1, -50.0),
            DayPoint(date(2026, 1, 2), -30.0, 1, 1, -80.0),
        ]
        assert max_drawdown(points) == 80.0

    def test_a_curve_that_only_rises_has_no_drawdown(self) -> None:
        points = [
            DayPoint(date(2026, 1, 1), 10.0, 1, 1, 10.0),
            DayPoint(date(2026, 1, 2), 20.0, 1, 1, 30.0),
        ]
        assert max_drawdown(points) == 0.0


class TestBreakdowns:
    def test_group_by_orders_by_total_profit(self) -> None:
        rows = [
            make("SPY", "2026-01-16", 10.0, strategy="a"),
            make("QQQ", "2026-02-20", 90.0, strategy="b"),
        ]
        assert [row.key for row in group_by(rows, "strategy")] == ["b", "a"]

    def test_bands_keep_band_order_not_profit_order(self) -> None:
        """A gradient re-sorted by profit stops showing whether it is monotonic."""
        rows = [
            make("SPY", "2026-01-16", 500.0, score=0.30),
            make("QQQ", "2026-02-20", 10.0, score=0.90),
        ]
        bands = group_by_band(rows, "score", SCORE_BANDS)
        assert [row.key for row in bands] == ["0.00-0.60", "0.85-1.00"]

    def test_a_group_carries_its_own_cluster_count(self) -> None:
        """A filter can gut the sample without changing anything else on screen."""
        rows = [make("SPY", "2026-01-16", 1.0, strategy="a") for _ in range(20)]
        [group] = group_by(rows, "strategy")
        assert group.trades == 20
        assert group.clusters == 1
        assert group.reportable is False


class TestReport:
    def test_unresolved_candidates_are_dropped_not_counted_flat(self) -> None:
        """An open position is not a flat one."""
        rows = [make("SPY", "2026-01-16", 100.0), make("QQQ", "2026-02-20", None)]
        report = build_report(rows)
        assert report.trades == 1
        assert report.total_profit == 100.0

    def test_profit_factor_is_undefined_rather_than_infinite(self) -> None:
        """Rendering an empty denominator as a huge number reads as a great result."""
        report = build_report([make("SPY", "2026-01-16", 100.0)])
        assert report.profit_factor is None

    def test_profit_factor_is_gross_win_over_gross_loss(self) -> None:
        rows = [make("SPY", "2026-01-16", 100.0), make("QQQ", "2026-02-20", -25.0)]
        assert build_report(rows).profit_factor == 4.0

    def test_small_samples_are_not_reportable_and_say_why(self) -> None:
        report = build_report([make("SPY", "2026-01-16", 100.0)])
        assert report.reportable is False
        assert any("cluster" in note for note in report.notes)

    def test_settlement_date_count_is_reported_separately_from_clusters(self) -> None:
        """Six symbols on one Friday are six clusters but one market move."""
        rows = [
            make(symbol, "2026-01-16", 10.0)
            for symbol in ("SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA")
        ]
        report = build_report(rows)
        assert report.clusters == 6
        assert report.settlement_dates == 1
        assert any("settlement date" in note for note in report.notes)

    def test_empty_input_is_an_empty_report_not_a_crash(self) -> None:
        report = build_report([])
        assert report.trades == 0
        assert report.total_profit == 0.0
        assert report.expectancy is None
        assert report.days == []
        assert report.notes


# --------------------------------------------------------------------------------
# Uploading a statement from the browser
# --------------------------------------------------------------------------------

STATEMENT = (
    '"Activity Date","Process Date","Settle Date","Instrument","Description",'
    '"Trans Code","Quantity","Price","Amount"\n'
    '"9/10/2026","9/10/2026","9/11/2026","AAPL","AAPL 9/18/2026 Put $300.00",'
    '"STO","1","$1.50","$150.00"\n'
)


def _client(settings):
    from fastapi.testclient import TestClient

    from optscan.api.app import create_app
    from optscan.api.deps import clear_caches

    clear_caches()
    return TestClient(create_app(settings))


def test_a_statement_uploaded_twice_inserts_once(tmp_settings) -> None:
    """The reason this endpoint is safe to press repeatedly.

    Brokers export date ranges, not deltas, so every download after the first overlaps
    the last. Rows are keyed by a digest of their own contents, so the second upload
    must add nothing rather than doubling a position.
    """
    client = _client(tmp_settings)

    first = client.post(
        "/api/journal/import", content=STATEMENT, headers={"content-type": "text/csv"}
    ).json()
    second = client.post(
        "/api/journal/import", content=STATEMENT, headers={"content-type": "text/csv"}
    ).json()

    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert second["duplicate"] == 1
    # "0 new" is a correct outcome that looks like a failure without the count beside it.
    assert "already held" in second["detail"]


def test_a_file_that_is_not_a_statement_is_refused_with_a_reason(tmp_settings) -> None:
    client = _client(tmp_settings)
    response = client.post(
        "/api/journal/import", content="a,b,c\n1,2,3\n", headers={"content-type": "text/csv"}
    )
    assert response.status_code == 422
    assert "export" in response.json()["detail"].lower()


def _loaded(settings):
    """A client with the positions test statement already imported."""
    from tests.test_journal_positions import STATEMENT as BOOK

    client = _client(settings)
    client.post("/api/journal/import", content=BOOK, headers={"content-type": "text/csv"})
    return client


CONDOR = "QQQ|2026-09-10|2026-09-10"


def test_the_journal_carries_one_row_per_position(tmp_settings) -> None:
    book = _loaded(tmp_settings).get("/api/journal").json()["book"]
    keys = {p["key"] for p in book["positions"]}
    assert CONDOR in keys
    condor = next(p for p in book["positions"] if p["key"] == CONDOR)
    assert condor["strategy"] == "iron condor"
    assert len(condor["legs"]) == 4
    assert book["stop_multiple"] == 2.0
    assert "held past stop" in book["mistake_tags"]


def test_a_tag_and_a_strategy_override_are_saved_and_filterable(tmp_settings) -> None:
    client = _loaded(tmp_settings)
    saved = client.put(
        "/api/journal/annotation",
        json={
            "key": CONDOR,
            "strategy": "0DTE condor",
            "tags": ["Chased Entry", "chased entry"],
            "notes": "  faded the open  ",
            "entry_time": "9:45",
        },
    )
    assert saved.status_code == 200
    assert saved.json()["tags"] == ["chased entry"]
    assert saved.json()["entry_time"] == "09:45"

    data = client.get("/api/journal", params={"tag": "chased entry"}).json()
    assert [p["key"] for p in data["book"]["positions"]] == [CONDOR]
    assert data["book"]["positions"][0]["strategy"] == "0DTE condor"
    assert data["book"]["positions"][0]["entry_time"] == "09:45"
    # The headline is rebuilt from the filtered positions, so the two agree.
    assert data["total_profit"] == data["book"]["positions"][0]["realized"]


def test_an_annotation_is_validated(tmp_settings) -> None:
    client = _loaded(tmp_settings)
    unknown = client.put("/api/journal/annotation", json={"key": "NOPE|-|2026-01-01"})
    assert unknown.status_code == 404
    bad = client.put("/api/journal/annotation", json={"key": CONDOR, "entry_time": "25:00"})
    assert bad.status_code == 422


def test_a_screenshot_round_trips_and_only_images_are_taken(tmp_settings) -> None:
    client = _loaded(tmp_settings)
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    stored = client.post(
        "/api/journal/screenshot",
        params={"key": CONDOR},
        content=png,
        headers={"content-type": "image/png"},
    )
    assert stored.status_code == 200
    shot = stored.json()["id"]
    assert client.get(f"/api/journal/screenshot/{shot}").content == png
    position = next(
        p for p in client.get("/api/journal").json()["book"]["positions"] if p["key"] == CONDOR
    )
    assert position["screenshots"] == [shot]

    refused = client.post(
        "/api/journal/screenshot",
        params={"key": CONDOR},
        content=b"MZ",
        headers={"content-type": "application/octet-stream"},
    )
    assert refused.status_code == 415
    assert client.delete(f"/api/journal/screenshot/{shot}").status_code == 200
    assert client.get(f"/api/journal/screenshot/{shot}").status_code == 404


def test_the_starting_balance_feeds_sizing(tmp_settings) -> None:
    client = _loaded(tmp_settings)
    client.put("/api/journal/balance", json={"starting_balance": 4000})
    sizing = client.get("/api/journal").json()["book"]["sizing"]
    assert sizing["starting_balance"] == 4000
    assert sizing["net_deposits"] == 1000


def test_vix_is_fetched_through_a_seam_and_tagged_per_day(tmp_settings, monkeypatch) -> None:
    import optscan.api.routers.journal as journal_router

    monkeypatch.setattr(
        journal_router,
        "_vix_history",
        lambda days: [(date(2026, 9, 10), 16.5), (date(2026, 9, 11), 22.0)],
    )
    client = _loaded(tmp_settings)
    assert client.post("/api/journal/regime").json()["stored"] == 2
    positions = client.get("/api/journal").json()["book"]["positions"]
    condor = next(p for p in positions if p["key"] == CONDOR)
    assert condor["vix"] == 16.5


def test_an_excluded_day_leaves_the_view_with_all_its_positions(tmp_settings) -> None:
    """The what-if view: the day's positions go, the headline follows, the real total
    stays on the banner, and nothing is stored."""
    client = _loaded(tmp_settings)
    real = client.get("/api/journal").json()
    view = client.get("/api/journal", params={"exclude": "2026-09-11"}).json()

    assert all(p["closed_at"] != "2026-09-11" for p in view["book"]["positions"])
    assert view["view"]["excluded"] == ["2026-09-11"]
    # The 9/11 condor lost 95.40; leaving it out lifts the total by that much.
    assert view["view"]["excluded_profit"] == pytest.approx(-95.40)
    assert view["total_profit"] == pytest.approx(real["total_profit"] + 95.40)
    assert view["view"]["actual_total"] == pytest.approx(real["total_profit"])
    # Nothing was saved: without the parameter the day is back.
    assert client.get("/api/journal").json()["total_profit"] == real["total_profit"]


def test_a_normalized_view_scales_every_trade_to_the_median_risk(tmp_settings) -> None:
    client = _loaded(tmp_settings)
    data = client.get("/api/journal", params={"normalize": "true"}).json()
    view = data["view"]
    assert view["normalized"] is True
    assert view["normalized_risk"] > 0
    # Each counted trade is its R times the median 1R, so the total is their sum.
    positions = [p for p in data["book"]["positions"] if not p["is_open"]]
    expected = sum(round(p["r_multiple"] * view["normalized_risk"], 2) for p in positions)
    assert data["total_profit"] == pytest.approx(expected, abs=0.05)
    # The table still reports what really happened.
    assert all(p["realized"] is not None for p in positions)


def test_both_exports_are_csv_files(tmp_settings) -> None:
    client = _loaded(tmp_settings)
    positions = client.get("/api/journal/export.csv", params={"kind": "positions"})
    assert positions.headers["content-type"].startswith("text/csv")
    assert "attachment" in positions.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(positions.text)))
    assert any(r["key"] == CONDOR for r in rows)
    fills = client.get("/api/journal/export.csv", params={"kind": "fills"}).text
    assert "trans_code" in fills.splitlines()[0]
    assert CONDOR in fills


def test_an_oversized_upload_is_refused(tmp_settings) -> None:
    """A mistaken upload must not be able to occupy the process."""
    from optscan.api.routers.journal import MAX_STATEMENT_BYTES

    client = _client(tmp_settings)
    response = client.post(
        "/api/journal/import",
        content="x" * (MAX_STATEMENT_BYTES + 1),
        headers={"content-type": "text/csv"},
    )
    assert response.status_code == 413
