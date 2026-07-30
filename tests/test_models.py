"""Models.

These carry the conventions the rest of the tool depends on, so the tests are mostly
about what the models refuse to accept.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from optscan.models import ChainSnapshot, OptionChain, OptionContract, PriceBar, Quote, Right

NOW = datetime(2026, 7, 30, 18, 0, tzinfo=UTC)


def contract(**overrides) -> OptionContract:
    defaults = dict(
        symbol="SPY",
        expiry=date(2026, 8, 21),
        strike=740.0,
        right=Right.PUT,
        bid=4.0,
        ask=4.2,
        fetched_at=NOW,
        source="test",
    )
    return OptionContract(**{**defaults, **overrides})


class TestRight:
    @pytest.mark.parametrize("raw", ["C", "c", "call", "CALL", "Calls", Right.CALL])
    def test_call_spellings(self, raw) -> None:
        assert Right.parse(raw) is Right.CALL

    @pytest.mark.parametrize("raw", ["P", "put", "PUTS", Right.PUT])
    def test_put_spellings(self, raw) -> None:
        assert Right.parse(raw) is Right.PUT

    def test_nonsense_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="cannot interpret"):
            Right.parse("straddle")


class TestRecordProvenance:
    def test_naive_fetched_at_is_rejected(self) -> None:
        """Guessing a timezone here would silently shift the whole IV history."""
        with pytest.raises(ValidationError, match="timezone aware"):
            contract(fetched_at=datetime(2026, 7, 30, 18, 0))

    def test_fetched_at_is_converted_to_utc(self) -> None:
        from zoneinfo import ZoneInfo

        eastern = datetime(2026, 7, 30, 14, 0, tzinfo=ZoneInfo("America/New_York"))
        assert contract(fetched_at=eastern).fetched_at == NOW

    def test_age_seconds(self) -> None:
        record = contract()
        assert record.age_seconds(NOW) == 0.0
        assert record.age_seconds(datetime(2026, 7, 30, 18, 1, tzinfo=UTC)) == 60.0

    def test_records_are_frozen(self) -> None:
        with pytest.raises(ValidationError):
            contract().bid = 99.0


class TestQuote:
    def test_symbol_is_normalized(self) -> None:
        quote = Quote(symbol="  spy ", last=740.5, fetched_at=NOW, source="test")
        assert quote.symbol == "SPY"

    def test_blank_symbol_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Quote(symbol="   ", fetched_at=NOW, source="test")

    def test_price_prefers_mid_over_last(self) -> None:
        quote = Quote(symbol="SPY", last=740.5, bid=740.0, ask=741.0, fetched_at=NOW, source="test")
        assert quote.mid == 740.5
        assert quote.price == 740.5

    def test_price_falls_back_to_last_without_a_two_sided_market(self) -> None:
        quote = Quote(symbol="SPY", last=740.5, bid=0.0, ask=0.0, fetched_at=NOW, source="test")
        assert quote.mid is None
        assert quote.price == 740.5

    def test_negative_price_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Quote(symbol="SPY", last=-1.0, fetched_at=NOW, source="test")


class TestOptionContract:
    def test_mid_and_spread(self) -> None:
        c = contract(bid=4.0, ask=4.2)
        assert c.mid == pytest.approx(4.1)
        assert c.spread == pytest.approx(0.2)
        # 0.2 / 4.1 = 0.048780..., about 4.9 percent of mid
        assert c.spread_pct_of_mid == pytest.approx(0.0487804878, rel=1e-6)

    def test_no_bid_is_not_the_same_as_no_quote(self) -> None:
        """0.0 means the vendor said zero. None means it said nothing."""
        zero_bid = contract(bid=0.0, ask=0.05)
        assert zero_bid.bid == 0.0
        assert zero_bid.has_two_sided_market is False
        assert zero_bid.mid is None

        missing = contract(bid=None, ask=None)
        assert missing.has_two_sided_market is False
        assert missing.mid is None

    def test_crossed_market_has_no_mid(self) -> None:
        crossed = contract(bid=4.5, ask=4.0)
        assert crossed.is_crossed is True
        assert crossed.mid is None
        assert crossed.spread_pct_of_mid is None

    def test_junk_vendor_iv_becomes_none(self) -> None:
        """Yahoo reports 1e-05 as a null and occasionally a five digit vol."""
        assert contract(vendor_iv=1e-5).vendor_iv is None
        assert contract(vendor_iv=0.0).vendor_iv is None
        assert contract(vendor_iv=50.0).vendor_iv is None
        assert contract(vendor_iv=0.185).vendor_iv == pytest.approx(0.185)

    def test_zero_strike_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            contract(strike=0.0)

    def test_unknown_field_is_rejected(self) -> None:
        """A vendor adding a column must be a decision, not a silent passthrough."""
        with pytest.raises(ValidationError):
            contract(theta=-0.05)

    def test_dte(self) -> None:
        c = contract(expiry=date(2026, 8, 21))
        assert c.dte(date(2026, 7, 30)) == 22
        assert c.dte(date(2026, 8, 21)) == 0
        assert c.dte(date(2026, 8, 22)) == -1

    def test_quote_age_needs_a_last_trade(self) -> None:
        assert contract(last_trade_at=None).quote_age_seconds(NOW) is None
        traded = contract(last_trade_at=datetime(2026, 7, 30, 17, 0, tzinfo=UTC))
        assert traded.quote_age_seconds(NOW) == 3600.0


class TestPriceBar:
    def test_incoherent_bar_is_rejected(self) -> None:
        base = dict(symbol="SPY", ts=NOW, fetched_at=NOW, source="test")
        with pytest.raises(ValidationError, match="below low"):
            PriceBar(**base, open=10, high=9, low=11, close=10)
        with pytest.raises(ValidationError, match="outside the low/high"):
            PriceBar(**base, open=99, high=11, low=9, close=10)

    def test_valid_bar(self) -> None:
        bar = PriceBar(
            symbol="SPY",
            ts=NOW,
            open=740,
            high=742,
            low=739,
            close=741,
            volume=1_000,
            fetched_at=NOW,
            source="test",
        )
        assert bar.close == 741


class TestOptionChain:
    def test_contracts_must_match_the_chain(self) -> None:
        with pytest.raises(ValidationError, match="expires"):
            OptionChain(
                symbol="SPY",
                expiry=date(2026, 8, 21),
                contracts=(contract(expiry=date(2026, 9, 18)),),
                fetched_at=NOW,
                source="test",
            )
        with pytest.raises(ValidationError, match="has symbol"):
            OptionChain(
                symbol="SPY",
                expiry=date(2026, 8, 21),
                contracts=(contract(symbol="QQQ"),),
                fetched_at=NOW,
                source="test",
            )

    def test_splits_and_lookups(self) -> None:
        chain = OptionChain(
            symbol="SPY",
            expiry=date(2026, 8, 21),
            underlying_price=740.5,
            contracts=(
                contract(strike=735.0, right=Right.PUT),
                contract(strike=740.0, right=Right.PUT),
                contract(strike=740.0, right=Right.CALL),
            ),
            fetched_at=NOW,
            source="test",
        )
        assert len(chain.puts) == 2
        assert len(chain.calls) == 1
        assert chain.strikes == (735.0, 740.0)
        assert chain.get(740.0, "C").right is Right.CALL
        assert chain.get(999.0, "C") is None


class TestChainSnapshot:
    def _snapshot(self, **overrides) -> ChainSnapshot:
        defaults = dict(
            symbol="SPY",
            session_date=date(2026, 7, 30),
            quote=Quote(symbol="SPY", last=740.5, fetched_at=NOW, source="test"),
            chains=(
                OptionChain(
                    symbol="SPY",
                    expiry=date(2026, 8, 21),
                    contracts=(contract(),),
                    fetched_at=NOW,
                    source="test",
                ),
            ),
            fetched_at=NOW,
            source="test",
        )
        return ChainSnapshot(**{**defaults, **overrides})

    def test_counts(self) -> None:
        snapshot = self._snapshot()
        assert snapshot.contract_count == 1
        assert snapshot.expiries == (date(2026, 8, 21),)
        assert len(list(snapshot.contracts())) == 1

    def test_quote_symbol_must_match(self) -> None:
        with pytest.raises(ValidationError, match="quote is for"):
            self._snapshot(quote=Quote(symbol="QQQ", last=1.0, fetched_at=NOW, source="test"))

    def test_duplicate_expiry_is_rejected(self) -> None:
        chain = OptionChain(
            symbol="SPY", expiry=date(2026, 8, 21), contracts=(), fetched_at=NOW, source="test"
        )
        with pytest.raises(ValidationError, match="same expiry twice"):
            self._snapshot(chains=(chain, chain))
