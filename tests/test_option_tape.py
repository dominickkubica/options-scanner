"""The option tape: the batched fetch, the cache, and the resumable sync.

The fetch is driven through httpx.MockTransport like the other Alpaca tests. The sync
runs against a fake provider, because what matters there is the bookkeeping: which
sessions are asked for, which are skipped, and that a failure is retried rather than
remembered as done.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from optscan.config import Settings
from optscan.jobs.option_tape import (
    following_sessions,
    strike_ladder,
    sync_tape,
    trading_sessions,
)
from optscan.models import PriceBar
from optscan.providers import ProviderError
from optscan.storage.option_tape import (
    has_session,
    read_session,
    sessions_held,
    tape_root,
    write_session,
)
from tests.test_provider_alpaca import build

SESSION = date(2025, 4, 7)
OPEN = datetime(2025, 4, 7, 13, 30, tzinfo=UTC)
CLOSE = datetime(2025, 4, 7, 20, 0, tzinfo=UTC)


def minute(ts: datetime, price: float) -> PriceBar:
    return PriceBar(
        symbol="QQQ",
        ts=ts,
        open=price,
        high=price + 0.5,
        low=price - 0.5,
        close=price,
        volume=1000,
        fetched_at=ts,
        source="fake",
    )


def tape_row(ts: datetime, close: float, expiry: date = SESSION, right: str = "P") -> dict:
    return {
        "expiry": expiry,
        "right": right,
        "strike": 418.0,
        "ts": ts,
        "close": close,
        "vwap": close,
        "volume": 5,
        "trades": 2,
    }


class TestFetch:
    def test_the_tape_is_batched_and_every_page_kept(self) -> None:
        asked: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            symbols = request.url.params["symbols"].split(",")
            asked.append(request.url.params["symbols"])
            assert request.url.params["timeframe"] == "1Min"
            bars = {
                symbol: [{"t": "2025-04-07T14:00:00Z", "c": 1.25, "vw": 1.24, "v": 7, "n": 3}]
                for symbol in symbols
            }
            return httpx.Response(200, json={"bars": bars, "next_page_token": None})

        contracts = ["QQQ250407P00418000", "QQQ250407C00418000", "QQQ250407P00417000"]
        tape = build(handler).get_option_tape(contracts, OPEN, CLOSE, batch=2)

        assert len(asked) == 2, "three contracts in batches of two is two requests"
        assert set(tape) == set(contracts)
        row = tape["QQQ250407C00418000"][0]
        assert (row["expiry"], row["right"], row["strike"]) == (SESSION, "C", 418.0)
        assert (row["close"], row["vwap"], row["volume"], row["trades"]) == (1.25, 1.24, 7, 3)

    def test_a_row_without_a_price_is_dropped_rather_than_zeroed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            rows = [
                {"t": "2025-04-07T14:00:00Z", "c": None},
                {"t": "2025-04-07T14:01:00Z", "c": 0.9},
            ]
            return httpx.Response(200, json={"bars": {"QQQ250407P00418000": rows}})

        tape = build(handler).get_option_tape(["QQQ250407P00418000"], OPEN, CLOSE)
        assert [row["close"] for row in tape["QQQ250407P00418000"]] == [0.9]

    def test_the_ladder_asks_for_a_put_and_a_call_at_every_strike_and_expiry(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.extend(request.url.params["symbols"].split(","))
            return httpx.Response(200, json={"bars": {}})

        asked, tape = build(handler).get_option_ladder_tape(
            "qqq", [SESSION, date(2025, 4, 8)], [417.0, 418.0], OPEN, CLOSE
        )
        assert asked == 8 and len(set(seen)) == 8
        assert "QQQ250408C00417000" in seen
        assert tape == {}

    def test_session_minutes_are_raw_prices(self) -> None:
        """Adjusted 2024 prices sit dollars under what traded, off every strike."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.params["adjustment"] == "raw"
            row = {"t": "2025-04-07T13:30:00Z", "o": 418, "h": 419, "l": 417, "c": 418.5, "v": 9}
            return httpx.Response(200, json={"bars": {"QQQ": [row]}})

        bars = build(handler).get_session_minutes("QQQ", OPEN, CLOSE)
        assert [bar.close for bar in bars] == [418.5]


class TestStorage:
    def test_a_session_round_trips(self, tmp_path) -> None:
        minutes = [minute(OPEN + timedelta(minutes=i), 418 + i / 10) for i in range(3)]
        tape = {
            "QQQ250407P00418000": [tape_row(OPEN, 1.2), tape_row(OPEN + timedelta(minutes=1), 1.1)]
        }
        write_session(tmp_path, "QQQ", SESSION, minutes, tape, manifest={"source": "fake"})

        stored = read_session(tmp_path, "QQQ", SESSION)
        assert stored is not None
        assert len(stored.minutes) == 3
        assert list(stored.options["close"]) == [1.2, 1.1]
        assert stored.manifest["contracts_with_trades"] == 1
        assert sessions_held(tmp_path, "QQQ") == [SESSION]

    def test_a_session_is_held_only_once_its_manifest_exists(self, tmp_path) -> None:
        assert not has_session(tmp_path, "QQQ", SESSION)
        assert read_session(tmp_path, "QQQ", SESSION) is None

    def test_no_trades_is_stored_as_an_answer(self, tmp_path) -> None:
        write_session(tmp_path, "QQQ", SESSION, [minute(OPEN, 418)], {}, manifest={})
        stored = read_session(tmp_path, "QQQ", SESSION)
        assert stored is not None and stored.options.empty

    def test_no_underlying_is_refused(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="no underlying minutes"):
            write_session(tmp_path, "QQQ", SESSION, [], {}, manifest={})

    def test_a_path_is_not_a_ticker(self, tmp_path) -> None:
        with pytest.raises(ValueError):
            write_session(tmp_path, "../../pwn", SESSION, [minute(OPEN, 418)], {}, manifest={})
        assert sessions_held(tmp_path, "../../pwn") == []


class TestCalendar:
    def test_the_ladder_reaches_past_the_range_in_whole_dollars(self) -> None:
        ladder = strike_ladder(100.4, 101.6, width=0.03)
        assert ladder[0] == 97.0 and ladder[-1] == 105.0
        assert all(strike.is_integer() for strike in ladder)

    def test_a_nonsense_range_is_refused(self) -> None:
        with pytest.raises(ValueError):
            strike_ladder(0.0, 10.0)

    def test_the_next_session_skips_a_weekend_and_a_holiday(self) -> None:
        # 2025-04-17 was a Thursday; the 18th was Good Friday.
        assert following_sessions(date(2025, 4, 17), 1) == [date(2025, 4, 21)]
        assert trading_sessions(date(2025, 4, 17), date(2025, 4, 21)) == [
            date(2025, 4, 17),
            date(2025, 4, 21),
        ]


class FakeTapeProvider:
    name = "fake"

    def __init__(self, fail_on: set[date] | None = None) -> None:
        self.fail_on = fail_on or set()
        self.minute_calls: list[date] = []

    def get_session_minutes(self, symbol, start, end):
        day = start.date()
        self.minute_calls.append(day)
        if day in self.fail_on:
            raise ProviderError("the fake was told to fail")
        return [minute(start + timedelta(minutes=i), 418 + i / 100) for i in range(5)]

    def get_option_ladder_tape(self, underlying, expiries, strikes, start, end):
        contract = f"{underlying}{expiries[0]:%y%m%d}P00418000"
        return len(expiries) * len(strikes) * 2, {contract: [tape_row(start, 1.0, expiries[0])]}


class TestSync:
    def test_it_fetches_skips_what_it_holds_and_retries_what_failed(
        self, tmp_settings: Settings
    ) -> None:
        start, end = date(2025, 4, 7), date(2025, 4, 9)
        broken = FakeTapeProvider(fail_on={date(2025, 4, 8)})
        first = sync_tape(tmp_settings, ["QQQ"], start=start, end=end, provider=broken)

        assert (first.stored, first.held, len(first.failed)) == (2, 0, 1)
        root = tape_root(tmp_settings.data_path)
        assert not has_session(root, "QQQ", date(2025, 4, 8)), "a failure is not remembered as done"

        mended = FakeTapeProvider()
        second = sync_tape(tmp_settings, ["QQQ"], start=start, end=end, provider=mended)
        assert (second.stored, second.held, len(second.failed)) == (1, 2, 0)
        assert mended.minute_calls == [date(2025, 4, 8)]
        assert sessions_held(root, "QQQ") == [date(2025, 4, 7), date(2025, 4, 8), date(2025, 4, 9)]

    def test_a_provider_without_option_history_is_refused(self, tmp_settings: Settings) -> None:
        class Plain:
            name = "plain"

        with pytest.raises(ProviderError, match="needs alpaca"):
            sync_tape(tmp_settings, ["QQQ"], provider=Plain())  # type: ignore[arg-type]

    def test_nothing_before_the_vendors_history_is_asked_for(self, tmp_settings: Settings) -> None:
        fake = FakeTapeProvider()
        sync_tape(
            tmp_settings, ["QQQ"], start=date(2023, 1, 1), end=date(2024, 2, 2), provider=fake
        )
        assert min(fake.minute_calls) == date(2024, 2, 1)

    def test_a_rate_limit_is_waited_out_rather_than_failing_the_session(
        self, tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """At the vendor's full rate one session in forty came back 429 and had to wait
        for a second run. A refusal is now waited out and the session asked again."""
        from optscan.jobs import option_tape
        from optscan.providers.errors import RateLimited

        waits: list[float] = []
        monkeypatch.setattr(option_tape, "sleep", waits.append)

        class Throttled(FakeTapeProvider):
            def __init__(self) -> None:
                super().__init__()
                self.refusals = 1

            def get_session_minutes(self, symbol, start, end):
                if self.refusals:
                    self.refusals -= 1
                    raise RateLimited("slow down")
                return super().get_session_minutes(symbol, start, end)

        report = sync_tape(tmp_settings, ["QQQ"], start=SESSION, end=SESSION, provider=Throttled())
        assert (report.stored, len(report.failed)) == (1, 0)
        assert len(waits) == 1
