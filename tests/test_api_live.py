"""The live endpoints, exercised through the real app without a network.

The provider is injected and overridden, the same way every other API test keeps the
suite offline.

The stream itself is driven directly rather than through TestClient, and that is a
limitation of the client and not a shortcut. Starlette's TestClient buffers a whole
response into a BytesIO and only returns once the ASGI app has sent its final body
message. An SSE stream never sends one: when there is nothing to say it emits a
keepalive and waits. So a TestClient request against this endpoint does not read
slowly, it never returns at all. The generator is therefore exercised on its own, which
is where the framing, the disconnect handling, and the subscription release live
anyway. What that leaves unproven is the ASGI plumbing between the generator and a
socket, and the browser check covers it.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import aclosing
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from optscan.api.app import create_app
from optscan.api.deps import clear_caches, live_hub, provider_factory_dep
from optscan.api.routers.live import _events
from optscan.config import Settings
from optscan.models import ChainSnapshot
from tests.conftest import FakeProvider

MARKET_OPEN = datetime(2026, 7, 30, 17, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean() -> None:
    clear_caches()
    yield
    clear_caches()


def make_client(settings: Settings, provider: FakeProvider) -> TestClient:
    app = create_app(settings)
    app.dependency_overrides[provider_factory_dep] = lambda: lambda: provider
    hub = live_hub(settings)
    hub._provider_factory = lambda: provider
    hub._clock = lambda: MARKET_OPEN
    return TestClient(app)


@pytest.fixture
def live_settings(tmp_path) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path, live_enabled=True)


@pytest.fixture
def off_settings(tmp_path) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path, live_enabled=False)


class StubRequest:
    """The one thing the stream generator asks of a Request: has the client gone.

    Counting the checks is what lets a test end the stream deterministically at a
    chosen point instead of waiting for a real disconnect.
    """

    def __init__(self, disconnect_after: int = 1_000) -> None:
        self.checks = 0
        self.disconnect_after = disconnect_after

    async def is_disconnected(self) -> bool:
        self.checks += 1
        return self.checks > self.disconnect_after


def run_stream(hub, subscription, request: StubRequest, limit: int = 20) -> list[str]:
    """Drive the SSE generator to completion or to `limit` frames, whichever is first."""

    async def go() -> list[str]:
        frames: list[str] = []
        async with aclosing(_events(request, hub, subscription)) as stream:
            async for frame in stream:
                frames.append(frame)
                if len(frames) >= limit:
                    break
        return frames

    return asyncio.run(go())


def parse(frames: list[str]) -> list[tuple[str, dict]]:
    """SSE text back into (event, payload) pairs, ignoring keepalive comments."""
    events: list[tuple[str, dict]] = []
    for frame in frames:
        lines = frame.strip().split("\n")
        if not lines or lines[0].startswith(":"):
            continue
        name = lines[0].removeprefix("event: ")
        payload = lines[1].removeprefix("data: ")
        events.append((name, json.loads(payload)))
    return events


class TestStatusEndpoint:
    def test_a_disabled_feed_still_answers(
        self, off_settings: Settings, fake_provider: FakeProvider
    ) -> None:
        """ "Off" is the fact the UI most needs, because it is what makes the rest of
        the screen readable as stored rather than live."""
        client = make_client(off_settings, fake_provider)
        body = client.get("/api/live/status").json()

        assert body["state"] == "disabled"
        assert "OPTSCAN_LIVE_ENABLED" in body["detail"]

    def test_the_session_is_reported_alongside_the_feed_state(
        self, live_settings: Settings, fake_provider: FakeProvider
    ) -> None:
        client = make_client(live_settings, fake_provider)
        body = client.get("/api/live/status").json()

        assert body["session"] == "open"
        assert body["realtime"] is False


class TestHealth:
    def test_health_reports_the_delay_rather_than_only_a_false(
        self, tmp_path, fake_provider: FakeProvider
    ) -> None:
        """realtime false says the data is not live. It does not say how late it is,
        and fifteen minutes versus unknown are different things to a user.

        Alpaca's free indicative feed is the case: documented as fifteen minutes
        behind, so the number is publishable rather than inferred."""
        delayed = Settings(
            _env_file=None,
            data_dir=tmp_path,
            provider="alpaca",
            alpaca_key_id="k",
            alpaca_secret_key="s",
        )
        client = make_client(delayed, fake_provider)
        body = client.get("/api/health").json()

        assert body["realtime"] is False
        assert body["delay_minutes"] == 15

    def test_an_undocumented_delay_is_null_and_not_zero(
        self, live_settings: Settings, fake_provider: FakeProvider
    ) -> None:
        """yfinance is delayed but does not publish by how much. Null means unknown."""
        client = make_client(live_settings, fake_provider)
        body = client.get("/api/health").json()

        assert body["realtime"] is False
        assert body["delay_minutes"] is None


class TestStream:
    def test_a_disabled_feed_refuses_the_stream_instead_of_opening_a_silent_one(
        self, off_settings: Settings, fake_provider: FakeProvider
    ) -> None:
        """A stream that connects and never speaks looks exactly like a market that
        has stopped moving, which is the one thing this must never look like."""
        client = make_client(off_settings, fake_provider)
        response = client.get("/api/live/stream", params={"symbol": "SPY"})

        assert response.status_code == 409
        assert "OPTSCAN_LIVE_ENABLED" in response.json()["detail"]

    def test_the_stream_announces_itself_and_says_what_the_feed_is_doing(
        self, live_settings: Settings, fake_provider: FakeProvider
    ) -> None:
        """A browser must learn the feed's condition before any data arrives, because
        "connected but nothing is moving" and "the market is closed" look identical
        until one of them is stated."""
        make_client(live_settings, fake_provider)
        hub = live_hub(live_settings)
        subscription = hub.subscribe("SPY", None)

        events = parse(run_stream(hub, subscription, StubRequest(disconnect_after=2)))

        names = [name for name, _ in events]
        assert names[0] == "hello"
        assert "status" in names

    def test_a_cycle_reaches_the_wire_with_its_provenance(
        self, live_settings: Settings, fake_provider: FakeProvider, frozen_snapshot: ChainSnapshot
    ) -> None:
        make_client(live_settings, fake_provider)
        hub = live_hub(live_settings)
        subscription = hub.subscribe("SPY", frozen_snapshot.expiries[0])
        hub.tick()

        events = parse(run_stream(hub, subscription, StubRequest(disconnect_after=4)))

        cycles = [payload for name, payload in events if name == "cycle"]
        assert cycles, f"no cycle in {[name for name, _ in events]}"

        cycle = cycles[0]
        assert cycle["full"] is True
        assert cycle["symbol"] == "SPY"
        assert cycle["fetched_at"]
        assert cycle["source"]
        assert cycle["changed"]

    def test_every_frame_is_terminated_so_nothing_is_read_as_the_next_one(
        self, live_settings: Settings, fake_provider: FakeProvider
    ) -> None:
        make_client(live_settings, fake_provider)
        hub = live_hub(live_settings)
        subscription = hub.subscribe("SPY", None)

        frames = run_stream(hub, subscription, StubRequest(disconnect_after=2))

        assert frames
        for frame in frames:
            assert frame.endswith("\n\n")

    def test_the_symbol_ceiling_is_a_refusal_with_a_reason(
        self, tmp_path, fake_provider: FakeProvider
    ) -> None:
        settings = Settings(
            _env_file=None, data_dir=tmp_path, live_enabled=True, live_max_symbols=1
        )
        client = make_client(settings, fake_provider)
        live_hub(settings).subscribe("QQQ", None)

        response = client.get("/api/live/stream", params={"symbol": "SPY"})

        assert response.status_code == 429
        assert "OPTSCAN_LIVE_MAX_SYMBOLS" in response.json()["detail"]

    def test_a_disconnected_client_releases_its_subscription(
        self, live_settings: Settings, fake_provider: FakeProvider
    ) -> None:
        """Otherwise every page navigation leaves a symbol being polled forever, and
        the request budget drains into tabs nobody has open."""
        make_client(live_settings, fake_provider)
        hub = live_hub(live_settings)
        subscription = hub.subscribe("SPY", None)

        assert sum(len(feed.subscribers) for feed in hub._feeds.values()) == 1

        run_stream(hub, subscription, StubRequest(disconnect_after=1))

        assert sum(len(feed.subscribers) for feed in hub._feeds.values()) == 0

    def test_a_client_that_stops_reading_still_releases_its_subscription(
        self, live_settings: Settings, fake_provider: FakeProvider
    ) -> None:
        """The break-out-of-the-loop case rather than the disconnect case. Both have to
        reach the release, which is why it is in a finally and not after the loop."""
        make_client(live_settings, fake_provider)
        hub = live_hub(live_settings)
        subscription = hub.subscribe("SPY", None)

        run_stream(hub, subscription, StubRequest(), limit=1)

        assert sum(len(feed.subscribers) for feed in hub._feeds.values()) == 0

    def test_a_frame_never_contains_a_raw_newline_in_its_data(
        self, live_settings: Settings, fake_provider: FakeProvider
    ) -> None:
        """A newline inside a data field is read as a field separator, which is how an
        SSE stream starts silently truncating events."""
        from optscan.api.routers.live import _frame

        frame = _frame("cycle", {"detail": "line one\nline two", "n": 1})
        body = frame.split("data: ", 1)[1]

        assert body.count("\n") == 2  # the two that terminate the frame
        assert "line one\\nline two" in body
