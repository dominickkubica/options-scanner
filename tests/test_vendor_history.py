"""The Market Chameleon import, and the vol history it unblocks.

The fixture is a real trimmed export: the 40 most recent QQQ sessions plus the three
the vendor published with no IV30 at all. Real rather than synthetic on purpose, and
for the usual reason in this project: synthetic data agrees with whatever the parser
happens to do, and the three blank sessions in particular are the kind of thing nobody
invents when writing a fixture by hand.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from optscan.analytics.ivrank import Confidence, iv_rank_from_series
from optscan.imports.marketchameleon import (
    SOURCE,
    MarketChameleonParseError,
    parse_file,
    symbol_from_filename,
)
from optscan.screener.context import DEFAULT_IV_HISTORY_DTE, atm_iv_near_dte
from optscan.screener.history import IvHistory, choose_iv_history, vendor_iv_history
from optscan.storage import db
from optscan.storage.vendor import (
    daily_bars,
    import_daily_bars,
    iv30_series,
)

FIXTURE = Path(__file__).parent / "fixtures" / "marketchameleon_qqq_sample.csv"


@pytest.fixture
def bars():
    return parse_file(FIXTURE, "QQQ")


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()


class TestParsing:
    def test_vol_points_become_decimals(self, bars) -> None:
        """The single most dangerous conversion in this importer.

        The file's last row reads IV30 = 17.19, meaning 17.19 vol points. Everything
        in this project stores vol as a decimal and only the UI multiplies by 100. A
        hundredfold error here is obvious in a payoff diagram and completely invisible
        in a rank, which is the only place this number is used.
        """
        latest = bars[-1]
        assert latest.session_date == date(2026, 9, 4)
        assert latest.iv30 == pytest.approx(0.1719)

    def test_dates_are_month_first(self, bars) -> None:
        """9/4/2026 is September 4th, not April 9th.

        Forced by the real file rather than assumed: across the full export the first
        field is never above 12 and the second is above 12 on 1,926 rows. A day-first
        reading would keep the ambiguous rows and drop the rest, which is a subset
        that correlates with the day of the month.
        """
        assert bars[-1].session_date == date(2026, 9, 4)
        assert all(bar.session_date.weekday() < 5 for bar in bars)

    def test_rows_come_back_oldest_first(self, bars) -> None:
        """The file is newest first and every consumer assumes the opposite.

        Realized volatility over a reversed series returns a number rather than an
        error, which is the worst possible failure mode.
        """
        dates = [bar.session_date for bar in bars]
        assert dates == sorted(dates)

    def test_a_blank_vol_is_none_and_never_zero(self, bars) -> None:
        """Three real sessions carry no IV30. Zero would be a published number.

        A zero would drag any mean computed over the series and would sit at the
        bottom of any range a rank is measured against.
        """
        blank = {bar.session_date for bar in bars if bar.iv30 is None}
        assert blank == {date(2014, 7, 2), date(2014, 7, 8), date(2015, 6, 23)}
        assert not any(bar.iv30 == 0.0 for bar in bars)

    def test_open_interest_is_absent_before_2018(self, bars) -> None:
        """Coverage varies inside one file, so absent must not read as zero."""
        early = next(bar for bar in bars if bar.session_date == date(2014, 7, 2))
        assert early.call_open_interest is None
        assert early.put_open_interest is None
        assert bars[-1].call_open_interest == 5260903

    def test_a_changed_header_is_refused(self, tmp_path) -> None:
        """A vendor adding a column beside another is how a value gets read from the
        wrong field. Refusing beats a best effort read."""
        path = tmp_path / "HistoricalPrices_QQQ.csv"
        path.write_text("Date,Open,Close\n9/4/2026,1,2\n", encoding="utf-8")
        with pytest.raises(MarketChameleonParseError, match="verified Market Chameleon layout"):
            parse_file(path, "QQQ")

    def test_a_duplicated_session_is_refused(self, tmp_path) -> None:
        """One day twice would double that day's weight in every rank over it."""
        text = FIXTURE.read_text(encoding="utf-8-sig").splitlines()
        path = tmp_path / "HistoricalPrices_QQQ.csv"
        path.write_text("\n".join([*text, text[1]]), encoding="utf-8")
        with pytest.raises(MarketChameleonParseError, match="appears twice"):
            parse_file(path, "QQQ")

    def test_the_symbol_comes_only_from_the_filename(self) -> None:
        """There is no symbol column anywhere in the format. This is the whole risk."""
        assert symbol_from_filename(Path("HistoricalPrices_QQQ.csv")) == "QQQ"
        assert symbol_from_filename(Path("HistoricalPrices_BRK.B.csv")) == "BRK.B"
        assert symbol_from_filename(Path("some_other_download.csv")) is None


class TestStorage:
    def test_a_reimport_changes_nothing(self, conn, bars) -> None:
        """Exports overlap by design: tomorrow's covers today's plus a day."""
        first = import_daily_bars(conn, bars, file_name="a.csv")
        assert first.rows_inserted == len(bars)
        assert first.rows_duplicate == 0

        second = import_daily_bars(conn, bars, file_name="a.csv")
        assert second.rows_inserted == 0
        assert second.rows_duplicate == len(bars)
        assert second.already_known
        assert second.warning() is None
        assert len(daily_bars(conn, "QQQ", source=SOURCE)) == len(bars)

    def test_a_mislabelled_file_is_caught_and_nothing_is_overwritten(self, conn, bars) -> None:
        """The failure this format invites: no symbol column, so a renamed download
        files one instrument's twelve years under another's name.

        Distinguished from a vendor revision by shape. A revision touches a handful of
        sessions; a wrong ticker disagrees on essentially all of them.
        """
        import_daily_bars(conn, bars, file_name="qqq.csv")

        # Same sessions, different instrument's prices.
        wrong = [bar.model_copy(update={"close": bar.close / 4.0}) for bar in bars]
        report = import_daily_bars(conn, wrong, file_name="not_really_qqq.csv")

        assert report.rows_inserted == 0
        assert report.rows_conflicting == len(bars)
        assert report.looks_like_wrong_ticker
        assert "no symbol column" in report.warning()
        assert report.conflict_examples

        stored = {bar.session_date: bar.close for bar in daily_bars(conn, "QQQ", source=SOURCE)}
        assert stored[date(2026, 9, 4)] == pytest.approx(718.96), "the original survived"

    def test_mixing_symbols_in_one_import_is_a_programming_error(self, conn, bars) -> None:
        mixed = [bars[0], bars[1].model_copy(update={"symbol": "SPY"})]
        with pytest.raises(ValueError, match="one source and one symbol"):
            import_daily_bars(conn, mixed)

    def test_the_series_skips_sessions_with_no_vol(self, conn, bars) -> None:
        """Never forward filled: an invented observation looks exactly like a real one."""
        import_daily_bars(conn, bars)
        series = iv30_series(conn, "QQQ", source=SOURCE)
        assert len(series) == len(bars) - 3
        assert date(2014, 7, 2) not in {when for when, _ in series}
        assert series == sorted(series)

    def test_the_series_is_bounded_at_the_session_being_scored(self, conn, bars) -> None:
        """A rank must never see observations from after the capture it describes."""
        import_daily_bars(conn, bars)
        series = iv30_series(conn, "QQQ", source=SOURCE, until=date(2026, 8, 1))
        assert series
        assert max(when for when, _ in series) <= date(2026, 8, 1)


class TestChoosingAHistory:
    """Two vendors' series are never merged. See choose_iv_history for why."""

    def test_the_longer_series_wins_and_the_other_is_discarded(self) -> None:
        own = IvHistory(points=[(date(2026, 8, d), 0.2) for d in range(1, 9)], source="yfinance")
        vendor = IvHistory(points=[(date(2026, 8, d), 0.3) for d in range(1, 21)], source=SOURCE)
        chosen = choose_iv_history(own, vendor)
        assert chosen is vendor
        assert len(chosen.points) == 20, "no points from the loser leaked in"

    def test_an_empty_vendor_series_leaves_the_local_one_alone(self) -> None:
        own = IvHistory(points=[(date(2026, 8, 1), 0.2)], source="yfinance")
        assert choose_iv_history(own, IvHistory(source=SOURCE)) is own

    def test_the_vendor_series_holds_its_own_latest_reading_out_of_its_range(self) -> None:
        """Two rules at once, and both are load bearing.

        The current value must come from the same vendor as the range, because a
        locally solved vol and a downloaded one disagree by about 3 percent in
        different directions per symbol. And today must not be inside the range it is
        ranked against, or the rank is biased toward the middle.
        """
        points = [(date(2026, 8, 1), 0.10), (date(2026, 8, 2), 0.20), (date(2026, 8, 3), 0.30)]
        history = vendor_iv_history(points, SOURCE)

        assert history.current == pytest.approx(0.30)
        assert history.points == points[:-1]
        assert (date(2026, 8, 3), 0.30) not in history.points

    def test_an_empty_vendor_series_has_no_current(self) -> None:
        assert vendor_iv_history([], SOURCE).current is None


class TestTenorMatching:
    """The tenth instance of this project's recurring bug, caught before it fired.

    Ranking the front expiry against a thirty day history compares two quantities that
    differ structurally, not two levels. It never fired only because no history was
    ever long enough to produce a rank.
    """

    class _Expiry:
        def __init__(self, dte: int, atm_iv: float | None) -> None:
            self.dte = dte
            self.atm_iv = atm_iv

    def test_the_nearest_expiry_to_the_target_wins(self) -> None:
        expiries = [self._Expiry(2, 0.83), self._Expiry(28, 0.25), self._Expiry(60, 0.24)]
        assert atm_iv_near_dte(expiries, 30) == pytest.approx(0.25)

    def test_a_zero_day_expiry_is_never_used_for_a_thirty_day_rank(self) -> None:
        """Measured on a real AAPL capture: the front expiry solved to 83 vol points
        while the thirty day point was 25.5. Against a range whose one year high is
        near 31, the front reading ranks full on the calmest day of the year."""
        assert atm_iv_near_dte([self._Expiry(0, 0.83)], 30) is None

    def test_nothing_inside_the_band_returns_none_rather_than_the_nearest(self) -> None:
        """A rank from a mismatched tenor is worse than no rank, because nothing
        downstream can tell it was mismatched."""
        assert atm_iv_near_dte([self._Expiry(4, 0.4), self._Expiry(8, 0.35)], 30) is None
        assert atm_iv_near_dte([self._Expiry(200, 0.2)], 30) is None

    def test_the_band_edges_are_inclusive(self) -> None:
        assert atm_iv_near_dte([self._Expiry(15, 0.3)], 30) == pytest.approx(0.3)
        assert atm_iv_near_dte([self._Expiry(60, 0.2)], 30) == pytest.approx(0.2)

    def test_an_expiry_with_no_solved_vol_is_skipped(self) -> None:
        expiries = [self._Expiry(30, None), self._Expiry(35, 0.22)]
        assert atm_iv_near_dte(expiries, 30) == pytest.approx(0.22)


class TestTheRankItUnblocks:
    def test_a_real_year_of_vendor_history_supports_a_high_confidence_rank(
        self, conn, bars
    ) -> None:
        """The point of the whole exercise.

        Before this, every recorded candidate carried a null IV rank, because a rank
        needs 20 observations and the project had 8 after weeks of capture. One
        downloaded file is years of them.
        """
        import_daily_bars(conn, bars)
        series = iv30_series(conn, "QQQ", source=SOURCE)
        history = vendor_iv_history(series, SOURCE)

        rank = iv_rank_from_series(
            history.current,
            history.points,
            asof=date(2026, 9, 4),
            lookback_days=365,
        )
        assert rank.confidence is not Confidence.INSUFFICIENT
        assert rank.rank is not None
        assert 0.0 <= rank.rank <= 1.0

    def test_the_rank_matches_a_hand_computed_position_in_the_range(self) -> None:
        """Hand verified, with exactly the 20 observations a rank is allowed to use.

        The range runs 0.10 to 0.50, so 0.20 sits (0.20 - 0.10) / (0.50 - 0.10) = 0.25
        of the way up it. Five of the twenty prior observations are strictly below
        0.20, so the percentile is 5 / 20 = 0.25 as well. The two agreeing here is a
        coincidence of the values chosen, not a property: rank measures position in
        the range and percentile measures share of observations below.
        """
        values = [
            0.10,
            0.12,
            0.14,
            0.16,
            0.18,  # the five strictly below 0.20
            0.20,
            0.21,
            0.23,
            0.25,
            0.26,
            0.28,
            0.30,
            0.31,
            0.33,
            0.35,
            0.36,
            0.38,
            0.40,
            0.45,
            0.50,
        ]
        points = [
            (date(2026, 1, 5) + timedelta(days=index), value) for index, value in enumerate(values)
        ]

        rank = iv_rank_from_series(0.20, points, asof=date(2026, 2, 1), lookback_days=365)

        assert rank.observations == 20
        assert rank.confidence is not Confidence.INSUFFICIENT
        assert rank.rank == pytest.approx(0.25)
        assert rank.percentile == pytest.approx(0.25)
        assert rank.low == pytest.approx(0.10)
        assert rank.high == pytest.approx(0.50)

    def test_one_observation_short_of_the_minimum_refuses_to_rank(self) -> None:
        """The refusal is the feature. Nineteen observations produce an IVRank object
        that says insufficient rather than a number nobody can qualify."""
        points = [
            (date(2026, 1, 5) + timedelta(days=index), 0.20 + index / 100) for index in range(19)
        ]
        rank = iv_rank_from_series(0.25, points, asof=date(2026, 2, 1))
        assert rank.confidence is Confidence.INSUFFICIENT
        assert rank.rank is None
        assert "need" in (rank.caveat() or "")

    def test_the_history_tenor_default_matches_what_the_vendor_publishes(self) -> None:
        """Market Chameleon's column is literally IV30 and this project's own
        reconstruction targets 30 days. If those ever diverge, the rank silently
        compares two tenors."""
        from optscan.screener.history import TARGET_DTE

        assert DEFAULT_IV_HISTORY_DTE == TARGET_DTE == 30


class TestHarmonizedRanking:
    """The eleventh instance of the recurring bug, caught before it shipped.

    Importing a vol history for some tickers and not others made the ones *without*
    data score higher, because `composite` renormalizes a missing component into the
    average of the candidate's other components.
    """

    @staticmethod
    def _opportunity(symbol: str, iv_rank: float | None):
        from datetime import UTC, datetime

        from optscan.models import Leg, Opportunity, ScoreComponents, Strategy
        from optscan.models.opportunity import Action
        from optscan.screener.config import ScreenConfig
        from optscan.screener.scoring import composite

        now = datetime.now(UTC)
        components = ScoreComponents(
            premium=1.0,
            iv_rank=iv_rank,
            liquidity=0.90,
            probability=0.84,
            event_risk=1.0,
            fetched_at=now,
            source="test",
        )
        return Opportunity(
            symbol=symbol,
            strategy=Strategy.CASH_SECURED_PUT,
            expiry=date(2026, 10, 16),
            dte=39,
            legs=(
                Leg(
                    action=Action.SELL,
                    right="P",
                    strike=100.0,
                    expiry=date(2026, 10, 16),
                    quantity=1,
                    mid=1.0,
                    fetched_at=now,
                    source="test",
                ),
            ),
            underlying_price=110.0,
            credit=1.0,
            max_profit=100.0,
            max_loss=9900.0,
            capital=10000.0,
            probability_of_profit=0.84,
            liquidity_score=0.90,
            score=composite(components, ScreenConfig()),
            components=components,
            fetched_at=now,
            source="test",
        )

    def test_having_a_low_iv_rank_beat_having_no_data_at_all(self) -> None:
        """The bug itself, pinned so it cannot come back.

        Two otherwise identical candidates. One has a real, honest, low IV rank. The
        other has no history at all. Unharmonized, the one with no data wins.
        """
        from optscan.screener.config import ScreenConfig
        from optscan.screener.scoring import composite

        config = ScreenConfig()
        with_data = self._opportunity("QQQ", 0.18)
        without = self._opportunity("IWM", None)

        assert without.score > with_data.score
        assert without.score - with_data.score == pytest.approx(0.152, abs=0.005)

        # And the break even: only an IV rank above this makes having data pay.
        assert composite(
            with_data.components.model_copy(update={"iv_rank": 0.9387}), config
        ) == pytest.approx(without.score, abs=0.001)

    def test_harmonizing_drops_the_component_nobody_shares(self) -> None:
        from optscan.screener.config import ScreenConfig
        from optscan.screener.scoring import harmonize_scores

        pair = [self._opportunity("QQQ", 0.18), self._opportunity("IWM", None)]
        ranked = harmonize_scores(pair, ScreenConfig())

        assert ranked[0].score == pytest.approx(ranked[1].score), (
            "identical candidates must tie once the uncomparable component is gone"
        )
        assert all("Ranked without iv_rank" in " ".join(item.warnings) for item in ranked)

    def test_the_components_are_kept_even_though_the_score_ignores_them(self) -> None:
        """The dropped value still explains the candidate; it just cannot rank it."""
        from optscan.screener.config import ScreenConfig
        from optscan.screener.scoring import harmonize_scores

        pair = [self._opportunity("QQQ", 0.18), self._opportunity("IWM", None)]
        ranked = harmonize_scores(pair, ScreenConfig())
        qqq = next(item for item in ranked if item.symbol == "QQQ")
        assert qqq.components.iv_rank == pytest.approx(0.18)

    def test_a_set_that_all_share_everything_is_left_alone(self) -> None:
        from optscan.screener.config import ScreenConfig
        from optscan.screener.scoring import harmonize_scores

        pair = [self._opportunity("QQQ", 0.18), self._opportunity("AAPL", 0.53)]
        ranked = harmonize_scores(pair, ScreenConfig())
        assert [item.score for item in ranked] == [item.score for item in pair]
        assert not any("Ranked without" in " ".join(item.warnings) for item in ranked)

    def test_the_shared_set_is_an_intersection_not_a_union(self) -> None:
        from optscan.screener.scoring import comparable_components

        trio = [
            self._opportunity("QQQ", 0.18),
            self._opportunity("AAPL", 0.53),
            self._opportunity("IWM", None),
        ]
        assert "iv_rank" not in comparable_components(trio)
        assert comparable_components(trio[:2]) == {
            "premium",
            "iv_rank",
            "liquidity",
            "probability",
            "event_risk",
        }


class TestRangeReconciliation:
    """Real exports contain rows whose open sits a hair outside their own low.

    Two twelve year files, 6,382 sessions, one such row in each:

        AMZN 2019-03-18   open 85.6195 against a low of 85.6315   0.014%
        SPY  2018-03-29   open 259.83  against a low of 259.8389  0.003%

    Before this, one of those rows failed validation and took the entire twelve year
    import down with it. The whole file being rejected over 0.012 of a dollar is the
    wrong trade, and so is trusting the row blindly.
    """

    HEADER = (
        "Date,Open,High,Low,Close,Adj Close,Change,Pct Change,Volume,Day VWAP,IV30,"
        "IV30 Change,IV30 Pct Change,Call Option Volume,Put Option Volume,"
        "Call Open Interest,Put Open Interest"
    )

    def write(self, tmp_path, open_, high, low, close):
        path = tmp_path / "HistoricalPrices_TEST.csv"
        path.write_text(
            f"{self.HEADER}\n"
            f"3/18/2019,{open_},{high},{low},{close},{close},0,0,1000,,25.0,0,0,1,1,1,1\n",
            encoding="utf-8-sig",
        )
        return path

    def test_a_rounding_sized_breach_widens_the_range(self, tmp_path) -> None:
        """An opening print is a trade, so the low was at most the open."""
        path = self.write(tmp_path, 85.6195, 87.5, 85.6315, 86.0)
        bar = parse_file(path, "TEST")[0]

        assert bar.open == pytest.approx(85.6195)
        assert bar.low == pytest.approx(85.6195), "low should widen to contain the open"
        assert bar.high == pytest.approx(87.5), "high is untouched when it is not breached"

    def test_the_close_is_contained_too(self, tmp_path) -> None:
        path = self.write(tmp_path, 86.0, 87.5, 85.0, 87.505)
        bar = parse_file(path, "TEST")[0]
        assert bar.high == pytest.approx(87.505)

    def test_a_split_sized_breach_is_still_refused(self, tmp_path) -> None:
        """The failure this must not wave through: an unadjusted open beside an
        adjusted range is off by the split ratio, not by a rounding step."""
        path = self.write(tmp_path, 1712.39, 87.5, 85.6315, 86.0)
        with pytest.raises(MarketChameleonParseError, match="rounding tolerance"):
            parse_file(path, "TEST")

    def test_a_clean_row_is_left_exactly_alone(self, tmp_path) -> None:
        path = self.write(tmp_path, 86.0, 87.5, 85.0, 86.5)
        bar = parse_file(path, "TEST")[0]
        assert (bar.open, bar.high, bar.low, bar.close) == (86.0, 87.5, 85.0, 86.5)
