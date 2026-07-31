"""Tradier adapter tests.

Offline, like every other test here. httpx.MockTransport stands in for the network, so
these exercise the real client, the real headers, and the real URL building without a
socket. The fixtures under tests/fixtures/tradier are built from documentation rather
than captured, which its README says plainly: these prove the adapter parses the
documented shapes, not that Tradier sends them.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from optscan.models import Right
from optscan.providers.errors import (
    AuthenticationError,
    MalformedResponse,
    NoDataAvailable,
    ProviderUnavailable,
    RateLimited,
    SymbolNotFound,
)
from optscan.providers.ratelimit import RateLimiter
from optscan.providers.tradier import TradierProvider

FIXTURES = Path(__file__).parent / "fixtures" / "tradier"

FROZEN_NOW = datetime(2026, 7, 31, 15, 45, tzinfo=UTC)


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def build(
    handler,
    *,
    realtime: bool = False,
    per_minute: int = 60,
) -> TradierProvider:
    """A provider wired to a mock transport and a limiter that never really sleeps."""
    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://sandbox.tradier.com",
        headers={"Authorization": "Bearer test-token", "Accept": "application/json"},
    )
    provider = TradierProvider(
        "test-token",
        base_url="https://sandbox.tradier.com",
        realtime=realtime,
        client=client,
        limiter=RateLimiter(per_minute, clock=lambda: 0.0, sleep=lambda _: None),
    )
    provider.now = lambda: FROZEN_NOW  # type: ignore[method-assign]
    return provider


def responder(payload: dict, status: int = 200, headers: dict | None = None):
    """A handler that answers everything with one payload, recording the requests."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=payload, headers=headers or {})

    handler.seen = seen  # type: ignore[attr-defined]
    return handler


# --------------------------------------------------------------------------------
# Quotes
# --------------------------------------------------------------------------------


def test_single_symbol_quote_comes_back_as_an_object() -> None:
    """Tradier's schema is a oneOf. One symbol is an object, not a one item array."""
    handler = responder(load("quote_single"))
    quote = build(handler).get_quote("spy")

    assert quote.symbol == "SPY"
    assert quote.last == pytest.approx(638.42)
    assert quote.bid == pytest.approx(638.4)
    assert quote.ask == pytest.approx(638.44)
    assert quote.bid_size == 12
    assert quote.previous_close == pytest.approx(640.2)
    assert quote.source == "tradier"
    assert quote.fetched_at == FROZEN_NOW
    # mid, not last, because both sides are quoted.
    assert quote.price == pytest.approx((638.4 + 638.44) / 2)


def test_multi_symbol_quote_comes_back_as_an_array() -> None:
    """The array form must not be iterated as a dict, which would walk its keys."""
    quote = build(responder(load("quote_multi"))).get_quote("SPY")
    assert quote.symbol == "SPY"
    assert quote.last == pytest.approx(638.42)


def test_unknown_symbol_raises_rather_than_returning_someone_elses_quote() -> None:
    with pytest.raises(SymbolNotFound):
        build(responder(load("quote_unmatched"))).get_quote("NOSUCHTICKER")


def test_quote_request_carries_the_bearer_token_and_the_json_accept_header() -> None:
    handler = responder(load("quote_single"))
    build(handler).get_quote("SPY")

    request = handler.seen[0]  # type: ignore[attr-defined]
    assert request.url.path == "/v1/markets/quotes"
    assert request.url.params["symbols"] == "SPY"
    assert request.headers["Authorization"] == "Bearer test-token"
    assert request.headers["Accept"] == "application/json"


# --------------------------------------------------------------------------------
# Expirations
# --------------------------------------------------------------------------------


def test_expirations_are_sorted_ascending() -> None:
    """The fixture lists them out of order on purpose. The interface promises sorted."""
    expirations = build(responder(load("expirations"))).get_expirations("SPY")
    assert expirations == [
        date(2026, 8, 7),
        date(2026, 8, 21),
        date(2026, 9, 18),
        date(2026, 12, 18),
    ]


def test_a_symbol_with_no_listed_options_returns_an_empty_list_not_an_error() -> None:
    """A null expirations block is a fact about the symbol, and the base class says so."""
    assert build(responder({"expirations": None})).get_expirations("BRK.A") == []


def test_an_unparseable_expiry_is_a_schema_change_and_raises() -> None:
    with pytest.raises(MalformedResponse):
        build(responder({"expirations": {"date": ["not-a-date"]}})).get_expirations("SPY")


# --------------------------------------------------------------------------------
# Chains
# --------------------------------------------------------------------------------


def test_chain_parses_and_drops_only_the_rows_it_cannot_normalize() -> None:
    """Five rows in the fixture, three parseable. One bad row must not lose the rest."""
    chain = build(responder(load("chain"))).get_chain("SPY", date(2026, 8, 21))

    assert len(chain.contracts) == 3
    assert chain.symbol == "SPY"
    assert chain.expiry == date(2026, 8, 21)
    assert chain.source == "tradier"
    # Tradier's chain payload names the underlying but never prices it, and inventing
    # a spot here is exactly the silent substitution the interface forbids.
    assert chain.underlying_price is None


def test_a_zero_bid_survives_as_zero_and_does_not_become_unknown() -> None:
    """None means unknown, 0.0 means the vendor said zero. The wing strike says zero."""
    chain = build(responder(load("chain"))).get_chain("SPY", date(2026, 8, 21))
    wing = chain.get(780.0, Right.CALL)

    assert wing is not None
    assert wing.bid == 0.0
    assert wing.ask == pytest.approx(0.05)
    assert wing.has_two_sided_market is False
    assert wing.mid is None


def test_a_zero_trade_date_is_no_trade_rather_than_1970() -> None:
    """Otherwise every staleness check passes on a contract that has never traded."""
    chain = build(responder(load("chain"))).get_chain("SPY", date(2026, 8, 21))
    wing = chain.get(780.0, Right.CALL)

    assert wing is not None
    assert wing.last_trade_at is None
    assert wing.quote_age_seconds(FROZEN_NOW) is None


def test_millisecond_epochs_are_detected_rather_than_read_as_seconds() -> None:
    """1785513600000 ms is 2026. The same number read as seconds would be the year 58565."""
    chain = build(responder(load("chain"))).get_chain("SPY", date(2026, 8, 21))
    call = chain.get(630.0, Right.CALL)

    assert call is not None
    assert call.last_trade_at is not None
    assert call.last_trade_at.year == 2026


def test_vendor_iv_is_kept_for_comparison_and_a_null_greeks_block_is_tolerated() -> None:
    chain = build(responder(load("chain"))).get_chain("SPY", date(2026, 8, 21))

    call = chain.get(630.0, Right.CALL)
    assert call is not None
    assert call.vendor_iv == pytest.approx(0.1391)

    wing = chain.get(780.0, Right.CALL)
    assert wing is not None
    assert wing.vendor_iv is None


def test_chain_request_asks_for_greeks_and_names_the_expiration() -> None:
    handler = responder(load("chain"))
    build(handler).get_chain("SPY", date(2026, 8, 21))

    request = handler.seen[0]  # type: ignore[attr-defined]
    assert request.url.path == "/v1/markets/options/chains"
    assert request.url.params["symbol"] == "SPY"
    assert request.url.params["expiration"] == "2026-08-21"
    assert request.url.params["greeks"] == "true"


def test_a_missing_chain_is_no_data_available_not_a_failure() -> None:
    with pytest.raises(NoDataAvailable):
        build(responder({"options": None})).get_chain("SPY", date(2026, 8, 21))


def test_a_chain_where_every_row_is_unparseable_raises_rather_than_returning_empty() -> None:
    """One bad row is a quirk. Every row bad is a schema change, and must be loud."""
    payload = {"options": {"option": [{"description": "no strike, no type"}]}}
    with pytest.raises(MalformedResponse):
        build(responder(payload)).get_chain("SPY", date(2026, 8, 21))


# --------------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------------


def test_history_drops_an_incoherent_bar_without_losing_the_series() -> None:
    """The fixture's last bar closes at 999.99 inside a 638-639 range. One bad session
    from a vendor must not take out the whole price chart."""
    bars = build(responder(load("history"))).get_history("SPY", 5)

    assert len(bars) == 3
    assert [bar.ts.date() for bar in bars] == [
        date(2026, 7, 28),
        date(2026, 7, 29),
        date(2026, 7, 30),
    ]
    assert bars[-1].close == pytest.approx(638.42)
    assert all(bar.source == "tradier" for bar in bars)


def test_history_request_spans_the_requested_window_of_calendar_days() -> None:
    handler = responder(load("history"))
    build(handler).get_history("SPY", 30)

    request = handler.seen[0]  # type: ignore[attr-defined]
    assert request.url.path == "/v1/markets/history"
    assert request.url.params["interval"] == "daily"
    assert request.url.params["end"] == "2026-07-31"
    assert request.url.params["start"] == "2026-07-01"


def test_empty_history_is_no_data_available() -> None:
    with pytest.raises(NoDataAvailable):
        build(responder({"history": None})).get_history("SPY", 5)


# --------------------------------------------------------------------------------
# Failure mapping
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_credentials_are_not_retryable(status: int) -> None:
    """403 is included because a token with no market data entitlement fails that way."""
    with pytest.raises(AuthenticationError) as caught:
        build(responder({}, status=status)).get_quote("SPY")
    assert caught.value.retryable is False


def test_a_429_is_retryable_and_carries_the_window_expiry_as_a_wait() -> None:
    expiry_ms = (FROZEN_NOW.timestamp() + 30) * 1000
    handler = responder({}, status=429, headers={"X-Ratelimit-Expiry": str(int(expiry_ms))})

    with pytest.raises(RateLimited) as caught:
        build(handler).get_quote("SPY")

    assert caught.value.retryable is True
    assert caught.value.retry_after_seconds == pytest.approx(30.0)


def test_a_rate_limit_window_that_has_already_rolled_gives_no_wait() -> None:
    """A negative remaining is not a wait of zero seconds dressed up. It is no answer,
    and the caller's own backoff is the honest fallback."""
    expiry_ms = (FROZEN_NOW.timestamp() - 30) * 1000
    handler = responder({}, status=429, headers={"X-Ratelimit-Expiry": str(int(expiry_ms))})

    with pytest.raises(RateLimited) as caught:
        build(handler).get_quote("SPY")

    assert caught.value.retry_after_seconds is None


def test_a_server_error_is_retryable() -> None:
    with pytest.raises(ProviderUnavailable) as caught:
        build(responder({}, status=503)).get_quote("SPY")
    assert caught.value.retryable is True


def test_a_timeout_becomes_provider_unavailable_rather_than_an_httpx_exception() -> None:
    """Rule two of the interface: never let a vendor exception escape the adapter."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("too slow", request=request)

    with pytest.raises(ProviderUnavailable):
        build(handler).get_quote("SPY")


def test_non_json_is_malformed_rather_than_a_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    with pytest.raises(MalformedResponse):
        build(handler).get_quote("SPY")


# --------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------


def test_a_blank_token_is_refused_at_construction() -> None:
    """Better here than as a 401 on the first fetch at 15:45 with nobody watching."""
    with pytest.raises(AuthenticationError):
        TradierProvider("   ", base_url="https://sandbox.tradier.com")


def test_realtime_is_an_instance_fact_not_a_class_fact() -> None:
    """Sandbox and production share an adapter and differ on the one thing that matters."""
    assert build(responder({}), realtime=False).realtime is False
    assert build(responder({}), realtime=True).realtime is True


def test_the_vendor_counter_clamps_the_local_budget_down() -> None:
    """Our bucket cannot see requests another process made against the same token."""
    handler = responder(load("quote_single"), headers={"X-Ratelimit-Available": "3"})
    limiter = RateLimiter(60, clock=lambda: 0.0, sleep=lambda _: None)
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://x.test")
    provider = TradierProvider("t", base_url="https://x.test", client=client, limiter=limiter)

    assert limiter.available == pytest.approx(60.0)
    provider.get_quote("SPY")
    assert limiter.available == pytest.approx(3.0)
