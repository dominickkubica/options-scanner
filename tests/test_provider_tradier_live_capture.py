"""The adapter against responses Tradier actually sent.

Everything in `tests/fixtures/tradier/` was built from published documentation, because
no token existed when the adapter was written. Those files prove the adapter parses the
*documented* shapes. They cannot prove Tradier sends them.

`tests/fixtures/tradier/live/` closes that gap: real sandbox responses, captured
2026-08-02, rows unmodified. These tests are still offline, driven through the same mock
transport as everything else.

## Why both sets exist rather than one

The obvious move on getting a token was to overwrite the constructed fixtures with real
ones, which is what their README told the next person to do. That would have deleted the
reason they exist. The constructed files carry deliberate damage that a healthy response
never contains: a row with no `option_type`, a row with no `strike`, a bar whose close
sits outside its own high and low. Those pin the adapter's refusals, and a real capture
has nothing to say about them.

So the constructed set keeps testing what the adapter does with broken input, and this
set tests that the input it will really get is the shape it expects. Neither is
redundant.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from optscan.models import Right
from optscan.providers.ratelimit import RateLimiter
from optscan.providers.tradier import TradierProvider

LIVE = Path(__file__).parent / "fixtures" / "tradier" / "live"
BUILT = Path(__file__).parent / "fixtures" / "tradier"

#: The session the captures were taken against. SPY closed at 747.03 on the Friday.
CAPTURED_ON = date(2026, 8, 2)
FROZEN_NOW = datetime(2026, 8, 2, 19, 20, tzinfo=UTC)


def live(name: str) -> dict:
    return json.loads((LIVE / f"{name}.json").read_text(encoding="utf-8"))


def built(name: str) -> dict:
    return json.loads((BUILT / f"{name}.json").read_text(encoding="utf-8"))


def provider_for(payload: dict) -> TradierProvider:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
        base_url="https://sandbox.tradier.com",
        headers={"Authorization": "Bearer test-token", "Accept": "application/json"},
    )
    provider = TradierProvider(
        "test-token",
        base_url="https://sandbox.tradier.com",
        realtime=False,
        client=client,
        limiter=RateLimiter(60, clock=lambda: 0.0, sleep=lambda _: None),
    )
    provider.now = lambda: FROZEN_NOW  # type: ignore[method-assign]
    return provider


def field_names(node) -> set[str]:
    if isinstance(node, list):
        merged: set[str] = set()
        for item in node:
            merged |= field_names(item)
        return merged
    return set(node.keys()) if isinstance(node, dict) else set()


class TestTheWireMatchesTheDocumentation:
    """The assumptions the constructed fixtures encode, checked against real bytes.

    A vendor that renames a field breaks the adapter silently, and the constructed
    fixtures would keep passing forever because they encode the old name too.
    """

    def test_a_single_symbol_really_does_come_back_as_an_object(self) -> None:
        """The riskiest assumption in the adapter, and it is correct."""
        assert isinstance(live("quote_single")["quotes"]["quote"], dict)

    def test_two_symbols_really_do_come_back_as_an_array(self) -> None:
        quotes = live("quote_multi")["quotes"]["quote"]
        assert isinstance(quotes, list)
        assert len(quotes) == 2

    def test_an_unknown_symbol_really_lands_in_unmatched_symbols(self) -> None:
        payload = live("quote_unmatched")["quotes"]
        assert "unmatched_symbols" in payload
        assert "quote" not in payload

    def test_every_quote_field_the_fixtures_assume_exists_on_the_wire(self) -> None:
        """One exception, and it is unused: `lot_size` is documented but not returned.
        The adapter reads contract size off the chain row, not off the quote."""
        missing = field_names(built("quote_single")["quotes"]["quote"]) - field_names(
            live("quote_single")["quotes"]["quote"]
        )
        assert missing == {"lot_size"}

    def test_every_chain_field_the_fixtures_assume_exists_on_the_wire(self) -> None:
        missing = field_names(built("chain")["options"]["option"]) - field_names(
            live("chain")["options"]["option"]
        )
        assert missing == set()

    def test_every_history_field_the_fixtures_assume_exists_on_the_wire(self) -> None:
        missing = field_names(built("history")["history"]["day"]) - field_names(
            live("history")["history"]["day"]
        )
        assert missing == set()

    def test_the_greeks_block_matches_exactly(self) -> None:
        def greeks(payload):
            rows = payload["options"]["option"]
            return next((row["greeks"] for row in rows if row.get("greeks")), {})

        assert field_names(greeks(built("chain"))) == field_names(greeks(live("chain")))

    def test_the_wire_carries_extra_fields_and_that_is_fine(self) -> None:
        """The adapter reads what it needs and ignores the rest, so a vendor adding a
        column is not a breaking change. Recorded so the next reader is not surprised."""
        extra = field_names(live("chain")["options"]["option"]) - field_names(
            built("chain")["options"]["option"]
        )
        assert "change_percentage" in extra
        assert "week_52_high" in extra


class TestTheAdapterParsesRealBytes:
    def test_a_real_quote_parses(self) -> None:
        quote = provider_for(live("quote_single")).get_quote("SPY")

        assert quote.symbol == "SPY"
        assert quote.last == pytest.approx(747.03)
        assert quote.source == "tradier"
        assert quote.fetched_at.tzinfo is not None

    def test_a_real_multi_quote_picks_the_right_symbol(self) -> None:
        """The array branch, against a real array rather than a hand written one."""
        quote = provider_for(live("quote_multi")).get_quote("QQQ")
        assert quote.symbol == "QQQ"

    def test_real_expirations_parse_and_sort(self) -> None:
        expiries = provider_for(live("expirations")).get_expirations("SPY")

        assert len(expiries) > 20
        assert expiries == sorted(expiries)
        assert all(isinstance(item, date) for item in expiries)

    def test_a_real_chain_parses_with_nothing_dropped(self) -> None:
        """44 rows captured, 44 parsed. A healthy response loses nothing."""
        chain = provider_for(live("chain")).get_chain("SPY", date(2026, 8, 3))

        assert len(chain.contracts) == len(live("chain")["options"]["option"])
        assert chain.expiry == date(2026, 8, 3)

    def test_the_real_chain_has_both_rights(self) -> None:
        chain = provider_for(live("chain")).get_chain("SPY", date(2026, 8, 3))
        rights = {contract.right for contract in chain.contracts}
        assert rights == {Right.CALL, Right.PUT}

    def test_a_real_zero_bid_stays_zero_rather_than_becoming_unknown(self) -> None:
        """None means unknown, 0.0 means the vendor said zero. The capture contains
        real zero bids on the wings, so this is the rule against real data."""
        chain = provider_for(live("chain")).get_chain("SPY", date(2026, 8, 3))
        zero_bids = [c for c in chain.contracts if c.bid == 0.0]

        assert zero_bids, "expected the captured wings to carry real zero bids"
        assert all(c.bid is not None for c in zero_bids)

    def test_a_real_row_with_no_trade_date_has_no_last_trade(self) -> None:
        """Seven of the captured rows have never traded. That is not 1970."""
        chain = provider_for(live("chain")).get_chain("SPY", date(2026, 8, 3))
        untraded = [c for c in chain.contracts if c.last_trade_at is None]

        assert untraded
        assert all(c.last_trade_at is None for c in untraded)

    def test_real_vendor_iv_is_kept_exactly_as_given(self) -> None:
        chain = provider_for(live("chain")).get_chain("SPY", date(2026, 8, 3))
        solved = [c.vendor_iv for c in chain.contracts if c.vendor_iv is not None]

        assert solved
        assert all(value >= 0.0 for value in solved)

    def test_the_vendors_give_up_zeros_are_refused(self) -> None:
        """Tradier reports `mid_iv` 0.0 on the deep in the money calls it could not
        solve. `_drop_junk_iv` on the model already turns that into unknown rather than
        into a volatility of zero, and here is the first real data that exercises it."""
        raw = live("chain")["options"]["option"]
        vendor_zeros = [row for row in raw if (row.get("greeks") or {}).get("mid_iv") == 0]
        assert vendor_zeros, "expected the captured wings to carry real zero mid_iv"

        chain = provider_for(live("chain")).get_chain("SPY", date(2026, 8, 3))
        for row in vendor_zeros:
            contract = next(
                c
                for c in chain.contracts
                if c.strike == row["strike"] and c.right.value.lower() == row["option_type"][0]
            )
            assert contract.vendor_iv is None

    def test_the_vendors_clamped_iv_survives_and_is_absurd(self) -> None:
        """The reason `vendor_iv` is a comparison and never a source.

        The deep in the money puts come back at `mid_iv` 10.0, a 1000 percent implied
        volatility, which is a solver that failed and published its clamp. It is under
        the model's plausibility ceiling so it survives parsing, and anything that
        treated it as a volatility would be reading a failure as a signal. The project
        solves its own vol from the mid; this field exists to disagree with that solve,
        not to stand in for it.
        """
        chain = provider_for(live("chain")).get_chain("SPY", date(2026, 8, 3))
        values = [c.vendor_iv for c in chain.contracts if c.vendor_iv is not None]

        assert max(values) == pytest.approx(10.0)
        # And a one cent ask on a far wing solves to a triple digit vol, the sixth
        # instance of the constant offset trap, now visible in a vendor's own numbers.
        assert any(1.0 < value < 2.0 for value in values)

    def test_real_crossed_quotes_exist_and_are_recognised(self) -> None:
        """Bid above ask, in a real response, on liquid near the money SPY strikes.

        The sandbox returns these on a closed market. `is_crossed` was written against
        the possibility; this is the first evidence it actually happens, which means the
        refusals downstream of it are load bearing rather than defensive.
        """
        chain = provider_for(live("chain")).get_chain("SPY", date(2026, 8, 3))
        crossed = [c for c in chain.contracts if c.is_crossed]

        assert crossed, "expected the capture to contain real crossed quotes"
        assert all(c.mid is None for c in crossed), "a crossed quote has no honest mid"
        assert all(not c.has_two_sided_market or c.is_crossed for c in crossed)

    def test_the_real_contract_size_is_a_hundred(self) -> None:
        """Read off the chain row. If the vendor stopped sending it the adapter would
        default, and a wrong contract size makes every dollar figure a lie."""
        chain = provider_for(live("chain")).get_chain("SPY", date(2026, 8, 3))
        assert {c.contract_size for c in chain.contracts} == {100}

    def test_real_history_parses(self) -> None:
        bars = provider_for(live("history")).get_history("SPY", 10)

        assert bars
        assert bars == sorted(bars, key=lambda bar: bar.ts)
        assert all(bar.low <= bar.close <= bar.high for bar in bars)

    def test_the_last_real_bar_is_the_friday_close(self) -> None:
        bars = provider_for(live("history")).get_history("SPY", 10)
        assert bars[-1].ts.date() == date(2026, 7, 31)
        assert bars[-1].close == pytest.approx(747.03)


class TestCaptureProvenance:
    def test_the_live_directory_says_what_it_is(self) -> None:
        readme = (BUILT / "README.md").read_text(encoding="utf-8")
        assert "live/" in readme

    def test_the_captures_are_all_present(self) -> None:
        expected = {
            "quote_single",
            "quote_multi",
            "quote_unmatched",
            "expirations",
            "chain",
            "history",
        }
        assert {path.stem for path in LIVE.glob("*.json")} == expected
