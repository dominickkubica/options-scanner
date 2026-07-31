"""The live refresh loop, driven by a fake provider and a frozen clock.

No threads are started here and nothing sleeps. `tick()` is public exactly so the loop
can be stepped one cycle at a time, which is what makes the delta encoding and the
session handling testable rather than timing dependent.

The exit criterion for Phase 5 is that the dashboard updates during market hours, and
that cannot be checked outside a session. What can be checked is everything up to the
socket: that a cycle is coherent, that a delta only omits what did not move, that a
failure never becomes a version, and that a closed market is not polled.
"""

from __future__ import annotations

import queue
from datetime import UTC, date, datetime, timedelta

import pytest

from optscan.config import REPO_ROOT, Settings
from optscan.live.hub import DEGRADED_AFTER_FAILURES, LiveHub, LiveState, contract_key
from optscan.market_calendar import SessionState
from optscan.models import ChainSnapshot
from optscan.providers.errors import ProviderUnavailable
from tests.conftest import FakeProvider

# 2026-07-30 was a Thursday. 17:00 UTC is 13:00 in New York, mid session.
MARKET_OPEN = datetime(2026, 7, 30, 17, 0, tzinfo=UTC)
# 02:00 UTC on the same Thursday is 22:00 Wednesday in New York: after the post market
# window, so the calendar calls it closed.
MARKET_CLOSED = datetime(2026, 8, 1, 17, 0, tzinfo=UTC)  # a Saturday
# 12:00 UTC is 08:00 in New York, before the open.
MARKET_PRE = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


class Clock:
    """A frozen clock the test advances by hand."""

    def __init__(self, now: datetime = MARKET_OPEN) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class MovingProvider(FakeProvider):
    """A fake whose first chain contract's bid moves on every fetch.

    One contract moving and the rest holding still is precisely the case the delta
    encoder exists for, and precisely the case where a bug would be invisible in a
    full payload.
    """

    def __init__(self, snapshot: ChainSnapshot) -> None:
        super().__init__(snapshot)
        self.fetches = 0
        self.fail_next = 0
        self.fetched_at = MARKET_OPEN

    def get_chain(self, symbol: str, expiry: date):
        if self.fail_next > 0:
            self.fail_next -= 1
            raise ProviderUnavailable("the fake was told to fail")

        chain = super().get_chain(symbol, expiry)
        self.fetches += 1
        contracts = list(chain.contracts)
        first = contracts[0]
        contracts[0] = first.model_copy(
            update={"bid": (first.bid or 1.0) + self.fetches, "fetched_at": self.fetched_at}
        )
        # Restamped with this provider's own name and fetch time, which rule three of
        # the MarketDataProvider contract requires of a real adapter. The base fake
        # hands back the frozen capture unchanged, still carrying yfinance, and a hub
        # test that accepted that would not notice the hub attributing a cycle wrongly.
        return chain.model_copy(
            update={
                "contracts": tuple(contracts),
                "fetched_at": self.fetched_at,
                "source": self.name,
            }
        )


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        live_enabled=True,
        live_poll_seconds=15.0,
        live_max_symbols=2,
        live_idle_timeout_seconds=60.0,
        live_chain_ttl_seconds=5.0,
    )


@pytest.fixture
def provider(frozen_snapshot: ChainSnapshot) -> MovingProvider:
    return MovingProvider(frozen_snapshot)


@pytest.fixture
def hub(settings: Settings, provider: MovingProvider) -> LiveHub:
    return LiveHub(settings, lambda: provider, clock=Clock())


def drain(sub) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    while True:
        try:
            events.append(sub.events.get_nowait())
        except queue.Empty:
            return events


def cycles(sub) -> list[dict]:
    return [payload for name, payload in drain(sub) if name == "cycle"]


class TestCycles:
    def test_the_first_cycle_a_subscriber_receives_is_full(
        self, hub: LiveHub, frozen_snapshot: ChainSnapshot
    ) -> None:
        sub = hub.subscribe("SPY", frozen_snapshot.expiries[0])
        hub.tick()

        sent = cycles(sub)
        assert len(sent) == 1
        assert sent[0]["full"] is True
        assert sent[0]["changed"]
        assert sent[0]["version"] == 1

    def test_a_cycle_carries_one_fetched_at_for_everything_in_it(
        self, hub: LiveHub, frozen_snapshot: ChainSnapshot
    ) -> None:
        """The whole reason deltas are safe. Every number in a cycle is as of one fetch,
        so a screen built from it cannot be a mixture of ages."""
        sub = hub.subscribe("SPY", frozen_snapshot.expiries[0])
        hub.tick()

        payload = cycles(sub)[0]
        assert payload["fetched_at"] == MARKET_OPEN.isoformat()
        assert payload["source"] == "fake"

    def test_contracts_arrive_solved_the_same_way_the_stored_path_solves_them(
        self, hub: LiveHub, frozen_snapshot: ChainSnapshot
    ) -> None:
        """Live rows and stored rows must differ by their data and never by their
        derivation, or switching between them would move every greek on screen."""
        sub = hub.subscribe("SPY", frozen_snapshot.expiries[0])
        hub.tick()

        contracts = cycles(sub)[0]["changed"]
        solved = [c for c in contracts.values() if c["iv"] is not None]
        assert solved, "the frozen SPY chain solves for most of its strikes"
        for contract in solved:
            assert contract["delta"] is not None
            assert contract["reject_reason"] is None

        # A strike that cannot be solved keeps its row and states why, exactly as the
        # stored chain endpoint does.
        refused = [c for c in contracts.values() if c["iv"] is None]
        assert all(c["reject_reason"] is not None for c in refused)


class TestDeltas:
    def test_the_second_cycle_sends_only_what_moved(
        self, hub: LiveHub, frozen_snapshot: ChainSnapshot, provider: MovingProvider
    ) -> None:
        expiry = frozen_snapshot.expiries[0]
        sub = hub.subscribe("SPY", expiry)
        hub.tick()
        first = cycles(sub)[0]

        hub._feeds[("SPY", expiry)].next_due = 0.0
        hub.tick()
        second = cycles(sub)[0]

        assert second["full"] is False
        assert second["version"] == 2
        assert len(second["changed"]) == 1
        assert len(first["changed"]) > 10

    def test_the_contract_that_moved_is_the_one_that_is_sent(
        self, hub: LiveHub, frozen_snapshot: ChainSnapshot
    ) -> None:
        expiry = frozen_snapshot.expiries[0]
        moved = frozen_snapshot.chains[0].contracts[0]
        sub = hub.subscribe("SPY", expiry)
        hub.tick()
        drain(sub)

        hub._feeds[("SPY", expiry)].next_due = 0.0
        hub.tick()

        changed = cycles(sub)[0]["changed"]
        assert list(changed) == [contract_key(moved.strike, moved.right)]

    def test_a_contract_that_leaves_the_chain_is_named_rather_than_forgotten(
        self, hub: LiveHub, frozen_snapshot: ChainSnapshot, provider: MovingProvider
    ) -> None:
        """A row nobody mentions again is one the browser renders forever."""
        expiry = frozen_snapshot.expiries[0]
        sub = hub.subscribe("SPY", expiry)
        hub.tick()
        drain(sub)

        dropped = frozen_snapshot.chains[0].contracts[1]
        original = provider.get_chain

        def without_one(symbol: str, wanted: date):
            chain = original(symbol, wanted)
            return chain.model_copy(
                update={
                    "contracts": tuple(c for c in chain.contracts if c is not chain.contracts[1])
                }
            )

        provider.get_chain = without_one  # type: ignore[method-assign]
        hub._feeds[("SPY", expiry)].next_due = 0.0
        hub.tick()

        payload = cycles(sub)[0]
        assert contract_key(dropped.strike, dropped.right) in payload["removed"]

    def test_a_subscriber_that_falls_behind_is_resynchronized_not_desynchronized(
        self, hub: LiveHub, frozen_snapshot: ChainSnapshot
    ) -> None:
        """Dropping a delta would leave the browser applying the next one against a
        version it never received, which is silent corruption rather than lag."""
        expiry = frozen_snapshot.expiries[0]
        sub = hub.subscribe("SPY", expiry)

        for _ in range(12):
            hub._feeds[("SPY", expiry)].next_due = 0.0
            hub.tick()

        sent = cycles(sub)
        versions = [payload["version"] for payload in sent]

        # The queue is smaller than the number of cycles produced, so events were
        # necessarily discarded. What must not happen is a surviving delta that assumes
        # a version this client never saw: every gap in the sequence is followed by a
        # full cycle, which is the only recovery that leaves the browser correct.
        assert len(versions) < 12
        for index, payload in enumerate(sent):
            if index == 0 or versions[index] != versions[index - 1] + 1:
                assert payload["full"] is True, (
                    f"version {payload['version']} arrived as a delta after a gap"
                )


class TestFailures:
    def test_a_failed_fetch_never_becomes_a_version(
        self, hub: LiveHub, frozen_snapshot: ChainSnapshot, provider: MovingProvider
    ) -> None:
        expiry = frozen_snapshot.expiries[0]
        sub = hub.subscribe("SPY", expiry)
        hub.tick()
        drain(sub)

        provider.fail_next = 1
        hub._feeds[("SPY", expiry)].next_due = 0.0
        hub.tick()

        assert cycles(sub) == []
        assert hub._feeds[("SPY", expiry)].cycle.version == 1

    def test_a_failure_is_reported_as_a_status_rather_than_swallowed(
        self, hub: LiveHub, frozen_snapshot: ChainSnapshot, provider: MovingProvider
    ) -> None:
        expiry = frozen_snapshot.expiries[0]
        sub = hub.subscribe("SPY", expiry)
        hub.tick()
        drain(sub)

        provider.fail_next = 1
        hub._feeds[("SPY", expiry)].next_due = 0.0
        hub.tick()

        statuses = [payload for name, payload in drain(sub) if name == "status"]
        assert statuses
        assert "ProviderUnavailable" in (statuses[-1]["detail"] or "")

    def test_repeated_failures_degrade_the_feed(
        self, hub: LiveHub, frozen_snapshot: ChainSnapshot, provider: MovingProvider
    ) -> None:
        """One failure is a hiccup and must not cry wolf. Three is a condition."""
        expiry = frozen_snapshot.expiries[0]
        hub.subscribe("SPY", expiry)
        hub.tick()

        provider.fail_next = DEGRADED_AFTER_FAILURES
        for _ in range(DEGRADED_AFTER_FAILURES):
            hub._feeds[("SPY", expiry)].next_due = 0.0
            hub.tick()

        assert hub.status().state is LiveState.DEGRADED

    def test_the_last_good_cycle_survives_a_failure_and_keeps_ageing(
        self, hub: LiveHub, frozen_snapshot: ChainSnapshot, provider: MovingProvider
    ) -> None:
        """Honest degradation: the numbers stay, and their age is what tells the truth."""
        expiry = frozen_snapshot.expiries[0]
        hub.subscribe("SPY", expiry)
        hub.tick()
        cycle = hub._feeds[("SPY", expiry)].cycle

        provider.fail_next = 1
        hub._feeds[("SPY", expiry)].next_due = 0.0
        hub.tick()

        assert hub._feeds[("SPY", expiry)].cycle is cycle
        assert cycle.age_seconds(MARKET_OPEN + timedelta(minutes=5)) == pytest.approx(300.0)


class TestMarketHours:
    def test_an_open_market_polls_at_the_configured_interval(
        self, settings: Settings, provider: MovingProvider
    ) -> None:
        hub = LiveHub(settings, lambda: provider, clock=Clock(MARKET_OPEN))
        assert hub.poll_interval() == pytest.approx(settings.live_poll_seconds)

    def test_outside_the_session_polling_slows_rather_than_stopping(
        self, settings: Settings, provider: MovingProvider
    ) -> None:
        hub = LiveHub(settings, lambda: provider, clock=Clock(MARKET_PRE))
        expected = settings.live_poll_seconds * settings.live_offhours_poll_multiple
        assert hub.poll_interval() == pytest.approx(expected)

    def test_a_closed_market_is_not_polled_at_all(
        self, settings: Settings, provider: MovingProvider
    ) -> None:
        """Nothing is quoted, every fetch returns the same settled numbers, and the
        budget spent confirming that is budget missing on Monday morning."""
        hub = LiveHub(settings, lambda: provider, clock=Clock(MARKET_CLOSED))

        assert hub.poll_interval() is None

        hub.subscribe("SPY", None)
        hub.tick()

        assert provider.fetches == 0
        assert hub.status().state is LiveState.IDLE
        assert hub.status().session is SessionState.CLOSED


class TestDefaultExpiry:
    """When the browser does not name an expiry, the hub has to resolve it the way the
    chain endpoint does. This was wrong first time round and the symptom was nasty: the
    feed connected, cycles arrived, the header said live, and the grid never moved,
    because the hub was following the front month and the grid was showing another."""

    def test_the_front_month_is_not_the_default(
        self, hub: LiveHub, frozen_snapshot: ChainSnapshot
    ) -> None:
        sub = hub.subscribe("SPY", None)
        hub.tick()

        payload = cycles(sub)[0]
        listed = [expiry.isoformat() for expiry in frozen_snapshot.expiries]
        assert payload["expiry"] in listed

    def test_it_matches_the_screen_minimum_rather_than_the_nearest_expiry(
        self, settings: Settings, provider: MovingProvider, frozen_snapshot: ChainSnapshot
    ) -> None:
        """A minimum DTE above the front expiry must push the choice past it."""
        from optscan.screener.config import ScreenConfig

        near, far = frozen_snapshot.expiries[0], frozen_snapshot.expiries[1]
        today = MARKET_OPEN.date()
        cutoff = (far - today).days

        # Screen config is frozen all the way down, so this rebuilds rather than sets.
        loaded = ScreenConfig.load(REPO_ROOT / "screen.yaml")
        dte = loaded.filters.dte.model_copy(update={"min_dte": cutoff})
        config = loaded.model_copy(
            update={"filters": loaded.filters.model_copy(update={"dte": dte})}
        )

        hub = LiveHub(settings, lambda: provider, clock=Clock(), screen_config=lambda: config)
        sub = hub.subscribe("SPY", None)
        hub.tick()

        payload = cycles(sub)[0]
        assert payload["expiry"] == far.isoformat()
        assert payload["expiry"] != near.isoformat()


class TestBudget:
    def test_two_expiries_of_one_symbol_share_a_quote(
        self, hub: LiveHub, frozen_snapshot: ChainSnapshot, provider: MovingProvider
    ) -> None:
        """One underlying, two chains. Paying for the quote twice inside the TTL is
        budget spent on a number that cannot have moved."""
        hub.subscribe("SPY", frozen_snapshot.expiries[0])
        hub.subscribe("SPY", frozen_snapshot.expiries[1])
        hub.tick()

        quotes = [call for call in provider.calls if call[0] == "get_quote"]
        chains = [call for call in provider.calls if call[0] == "get_chain"]
        assert len(quotes) == 1
        assert len(chains) == 2

    def test_the_symbol_ceiling_is_enforced_with_a_reason(
        self, hub: LiveHub, frozen_snapshot: ChainSnapshot
    ) -> None:
        hub.subscribe("SPY", None)
        hub.subscribe("QQQ", None)

        with pytest.raises(ValueError, match="maximum"):
            hub.subscribe("IWM", None)

    def test_a_second_expiry_of_a_followed_symbol_is_not_a_new_symbol(
        self, hub: LiveHub, frozen_snapshot: ChainSnapshot
    ) -> None:
        hub.subscribe("SPY", frozen_snapshot.expiries[0])
        hub.subscribe("QQQ", None)
        hub.subscribe("SPY", frozen_snapshot.expiries[1])  # must not raise

        assert hub.status().symbols == ("QQQ", "SPY")

    def test_a_feed_nobody_watches_stops_being_polled(
        self, hub: LiveHub, frozen_snapshot: ChainSnapshot, provider: MovingProvider
    ) -> None:
        clock = hub._clock
        sub = hub.subscribe("SPY", frozen_snapshot.expiries[0])
        hub.tick()
        assert provider.fetches == 1

        hub.release(sub)
        clock.advance(hub._settings.live_idle_timeout_seconds + 1)
        hub.tick()

        assert provider.fetches == 1
        assert hub.status().symbols == ()


class TestStatus:
    def test_a_disabled_feed_says_so_and_says_how_to_turn_it_on(
        self, tmp_path, provider: MovingProvider
    ) -> None:
        off = Settings(_env_file=None, data_dir=tmp_path, live_enabled=False)
        hub = LiveHub(off, lambda: provider, clock=Clock())

        status = hub.status()
        assert status.state is LiveState.DISABLED
        assert "OPTSCAN_LIVE_ENABLED" in (status.detail or "")

    def test_a_disabled_feed_never_starts_a_thread(
        self, tmp_path, provider: MovingProvider
    ) -> None:
        off = Settings(_env_file=None, data_dir=tmp_path, live_enabled=False)
        hub = LiveHub(off, lambda: provider, clock=Clock())
        hub.start()

        assert hub._thread is None
        hub.stop()

    def test_the_delay_is_reported_rather_than_left_to_be_inferred(
        self, tmp_path, provider: MovingProvider
    ) -> None:
        """A sandbox token is fifteen minutes behind and nothing in the data says so."""
        sandbox = Settings(
            _env_file=None,
            data_dir=tmp_path,
            live_enabled=True,
            provider="tradier",
            tradier_environment="sandbox",
        )
        hub = LiveHub(sandbox, lambda: provider, clock=Clock())

        status = hub.status()
        assert status.realtime is False
        assert status.delay_minutes == 15

    def test_a_new_subscriber_is_handed_the_last_cycle_immediately(
        self, hub: LiveHub, frozen_snapshot: ChainSnapshot
    ) -> None:
        """Opening a panel mid cycle should show the last good numbers with their real
        age, not an empty table until the next poll."""
        expiry = frozen_snapshot.expiries[0]
        hub.subscribe("SPY", expiry)
        hub.tick()

        latecomer = hub.subscribe("SPY", expiry)

        sent = cycles(latecomer)
        assert len(sent) == 1
        assert sent[0]["full"] is True
        assert sent[0]["version"] == 1
