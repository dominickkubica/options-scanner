"""Bulk price history, and the curated universes it runs over."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from optscan.config import Settings
from optscan.jobs.prices import SyncReport, batched, sync_daily_history
from optscan.models.vendor import VendorDailyBar
from optscan.storage import db
from optscan.storage.vendor import daily_bars
from optscan.universe import STALE_AFTER_DAYS, Universe, UniverseError, load_universe

BAR = {
    "t": "2026-09-04T04:00:00Z",
    "o": 1.0,
    "h": 2.0,
    "l": 0.5,
    "c": 1.5,
    "v": 1000,
    "n": 25,
    "vw": 1.4,
}


class FakeBulkProvider:
    """Stands in for Alpaca. Returns rows only for symbols it knows, and says nothing
    at all about the ones it does not, which is the real behaviour."""

    name = "fake"

    def __init__(self, known: dict[str, list[dict]]) -> None:
        self.known = known
        self.calls: list[list[str]] = []

    def get_daily_bars_bulk(self, symbols, start, end, **_):
        self.calls.append(list(symbols))
        return {s: self.known[s] for s in symbols if s in self.known}


class NoBulkProvider:
    name = "nobulk"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path)


class TestUniverse:
    def test_the_shipped_file_loads_and_every_group_has_members(self) -> None:
        universe = load_universe()
        assert universe.groups
        assert all(members for members in universe.groups.values())
        assert universe.date_checked is not None, "an undated curation cannot be judged"

    def test_symbols_are_deduplicated_across_groups(self) -> None:
        """A symbol in two groups must be fetched once. Twice would double its weight
        in anything counted over the result."""
        universe = Universe(groups={"a": ("NVDA", "AAPL"), "b": ("NVDA", "MSFT")})
        assert universe.symbols("a", "b") == ["AAPL", "MSFT", "NVDA"]

    def test_no_group_named_means_all_of_them(self) -> None:
        universe = Universe(groups={"a": ("NVDA",), "b": ("MSFT",)})
        assert universe.symbols() == ["MSFT", "NVDA"]
        assert universe.symbols("all") == ["MSFT", "NVDA"]

    def test_an_unknown_group_is_refused_and_names_the_real_ones(self) -> None:
        universe = Universe(groups={"tech": ("NVDA",)})
        with pytest.raises(UniverseError, match="tech"):
            universe.symbols("nonsense")

    def test_a_missing_file_raises_rather_than_defaulting_to_empty(self, tmp_path) -> None:
        """An empty universe would make a sync succeed having done nothing."""
        with pytest.raises(UniverseError, match="no universe file"):
            load_universe(tmp_path / "absent.yaml")

    def test_a_file_with_no_groups_is_refused(self, tmp_path) -> None:
        path = tmp_path / "universe.yaml"
        path.write_text("meta:\n  date_checked: 2026-01-01\n", encoding="utf-8")
        with pytest.raises(UniverseError, match="no groups"):
            load_universe(path)

    def test_stale_curation_says_so(self) -> None:
        """These lists do not notice index changes, so age is the only warning there is."""
        checked = date(2026, 1, 1)
        universe = Universe(groups={"a": ("NVDA",)}, date_checked=checked)
        assert universe.staleness_note(checked + timedelta(days=STALE_AFTER_DAYS - 1)) is None
        stale = universe.staleness_note(checked + timedelta(days=STALE_AFTER_DAYS + 1))
        assert "curated lists" in stale

    def test_an_undated_universe_is_always_flagged(self) -> None:
        universe = Universe(groups={"a": ("NVDA",)}, date_checked=None)
        assert "no date_checked" in universe.staleness_note(date(2026, 1, 1))


class TestBatching:
    def test_symbols_are_split_into_batches(self) -> None:
        assert list(batched(list("abcdef"), 2)) == [["a", "b"], ["c", "d"], ["e", "f"]]

    def test_a_partial_final_batch_is_kept(self) -> None:
        assert list(batched(list("abc"), 2)) == [["a", "b"], ["c"]]

    def test_one_request_covers_a_whole_batch(self, settings) -> None:
        """The point of the feature: fifty symbols is one call, not fifty."""
        provider = FakeBulkProvider({s: [BAR] for s in ("A", "B", "C")})
        sync_daily_history(settings, ["A", "B", "C"], provider=provider)
        assert len(provider.calls) == 1
        assert provider.calls[0] == ["A", "B", "C"]


class TestSync:
    def test_bars_are_stored_with_all_three_volume_fields(self, settings) -> None:
        provider = FakeBulkProvider({"NVDA": [BAR]})
        report = sync_daily_history(settings, ["NVDA"], provider=provider)
        assert report.inserted == 1

        with db.session(settings.sqlite_path) as conn:
            stored = daily_bars(conn, "NVDA", source="fake")
        assert len(stored) == 1
        assert stored[0].volume == 1000
        assert stored[0].trade_count == 25
        assert stored[0].vwap == pytest.approx(1.4)

    def test_average_trade_size_needs_the_count(self) -> None:
        """Volume alone cannot tell a block day from an ordinary one."""
        bar = VendorDailyBar(
            source="fake",
            symbol="NVDA",
            session_date=date(2026, 9, 4),
            open=1.0,
            high=2.0,
            low=0.5,
            close=1.5,
            volume=1000,
            trade_count=25,
        )
        assert bar.average_trade_size == pytest.approx(40.0)
        assert bar.model_copy(update={"trade_count": None}).average_trade_size is None

    def test_a_symbol_the_vendor_omits_is_reported_not_dropped(self, settings) -> None:
        """The measured behaviour: a 58 symbol request came back with 57 and no error.
        A hole in a price history is invisible in every chart drawn over it."""
        provider = FakeBulkProvider({"NVDA": [BAR]})
        report = sync_daily_history(settings, ["NVDA", "NOTREAL"], provider=provider)
        assert report.missing == ["NOTREAL"]
        assert report.resolved == 1
        assert "NOTREAL" in report.warning()

    def test_a_resync_stores_nothing_new(self, settings) -> None:
        provider = FakeBulkProvider({"NVDA": [BAR]})
        first = sync_daily_history(settings, ["NVDA"], provider=provider)
        second = sync_daily_history(settings, ["NVDA"], provider=provider)
        assert first.inserted == 1
        assert second.inserted == 0
        assert second.duplicate == 1

    def test_an_incoherent_bar_is_dropped_rather_than_repaired(self, settings) -> None:
        """The model refuses a close outside its own high and low. A repaired bar would
        be a number nobody could trace back to anything."""
        provider = FakeBulkProvider({"NVDA": [{**BAR, "c": 99.0}, BAR]})
        report = sync_daily_history(settings, ["NVDA"], provider=provider)
        assert report.inserted == 1, "the good bar survived, the broken one did not"

    def test_a_provider_that_cannot_batch_says_so(self, settings) -> None:
        report = sync_daily_history(settings, ["NVDA"], provider=NoBulkProvider())
        assert report.failed
        assert "several symbols at once" in next(iter(report.failed.values()))

    def test_nothing_requested_is_not_an_error(self, settings) -> None:
        assert sync_daily_history(settings, [], provider=NoBulkProvider()).requested == []


class TestReport:
    def test_everything_resolved_needs_no_warning(self) -> None:
        assert SyncReport(requested=["A"], stored={"A": 10}).warning() is None

    def test_many_missing_symbols_are_summarised_not_listed_in_full(self) -> None:
        missing = [f"SYM{i}" for i in range(30)]
        warning = SyncReport(requested=missing, missing=missing).warning()
        assert "30 symbols returned no bars" in warning
        assert "and 18 more" in warning
